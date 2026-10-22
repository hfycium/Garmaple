"""逐秒记录（FIT 文件）解析。

为什么需要单独的模块：活动摘要 JSON 里只有整段汇总值（平均心率、总爬升），
没有「第 12 分钟心率多少」。逐秒数据只存在于导出包的 `UploadedFiles_*.zip`
里那批 `.fit` 文件中，必须自己解。

三条实测结论决定了这里的写法（真实导出包核对过）：

1. **`fitdecode` 默认已经应用了 FIT 的 scale / offset。** 返回的 `distance`
   就是米（末条与 `session.total_distance` 比值 1.0000），`enhanced_speed`
   就是 m/s，`enhanced_altitude` 就是米，`cadence` 就是步/分，`heart_rate`
   就是 bpm。不要再自己 ÷100 或 ÷1000，否则会整体缩小一千倍。
2. **只有 `enhanced_*` 系列字段。** 归档里没有旧的 `speed` / `altitude`
   字段（304 个活动文件里一个都没有），因此只读 enhanced 版本。
3. **不是每个 `.fit` 都是活动。** 6487 个文件里只有 304 个含逐秒记录，
   其余是每日监测、睡眠阶段等。判据是「有 session 且有 record」，不能按
   文件名猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterable

import fitdecode


@dataclass
class FitSample:
    """一个采样点。字段缺失时保持 None，不要补 0——补 0 会拉低平均值。

    `power_w` 是 FIT `record` 的标准字段 `power`（瓦特）。本机设备没功率计，
    304 个活动一次都没写过，但格式里是有的——留着是为了让有功率计的导出
    也能用，不要因为「我这份数据取不到」就把它删掉。
    """

    offset_s: int
    hr: int | None = None
    speed_mps: float | None = None
    altitude_m: float | None = None
    cadence: int | None = None
    distance_m: float | None = None
    power_w: int | None = None


@dataclass
class FitStream:
    """一次活动的逐秒数据。"""

    utc_start: datetime
    sport: str | None = None
    sub_sport: str | None = None
    samples: list[FitSample] = field(default_factory=list)
    # 主观评价，来自 FIT session 的 workout_rpe / workout_feel。
    # 本机 304 个活动里有 9 个写了这两个字段，是少见的「设备写了但没人读」的情况。
    # rpe 已换算成 Borg CR10（0–10）；feel 是 0–100 分，低=差、高=好。
    workout_rpe: float | None = None
    workout_feel: int | None = None

    def __len__(self) -> int:
        return len(self.samples)


@dataclass
class FitHeader:
    """文件头信息，用来给归档里的 `.fit` 建轻量索引。

    `entry` 是这个文件在归档内的定位（该归档内嵌 zip 里第几个 `.fit`），
    不含文件名——文件名形如 `<设备序列号>_<内部编号>.fit`，序列号不能入库。
    """

    entry: int
    time_created: datetime | None
    kind: str | None

    @property
    def is_activity(self) -> bool:
        """`file_id.type` 为 activity 时才是活动记录。

        实测 6487 个 `.fit` 里正好 304 个是 activity，与「含逐秒记录」的文件数
        完全一致；其余是 monitoring_b（每日监测）、49、44、41 等类型。
        """
        return self.kind == "activity"


def _aware(dt: datetime | None) -> datetime | None:
    """FIT 里的时间都是 UTC，但 fitdecode 返回的是 naive datetime。"""
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 瞬时速度的物理上限。跑步的世界纪录约 10.4 m/s，骑行下坡可以到 20 m/s 以上；
# 再高就不是人在动，而是 GPS 漂移。实测一个 6'51"/km 的慢跑里出现了
# 24.05 m/s（86 km/h）的单点，换算成配速是 42 秒/公里。
MAX_SPEED_MPS = 20.0


def _speed_mps(value: Any) -> float | None:
    """`enhanced_speed` 已是 m/s。超出物理上限的值按「测坏了」处理。

    不清洗的话，一个毛刺点会同时污染序列（画出 42 秒/公里的配速）和汇总
    （「最高速度 86 km/h」）。这类点只能算缺失，不能算极值。
    """
    v = _as_float(value)
    if v is None or v < 0 or v > MAX_SPEED_MPS:
        return None
    return v


def read_header(stream: BinaryIO, entry: int = 0) -> FitHeader:
    """只读到 `file_id` 帧就停，用于给全部 `.fit` 快速建索引。

    全量解析 6487 个文件要 5 分钟，而活动只占其中 304 个。先读文件头拿到
    `type` 与 `time_created`，就能把候选缩到一两个，再对候选做完整解析——
    实测读完全部文件头只要 2 秒。
    """
    kind: str | None = None
    time_created: datetime | None = None
    try:
        for frame in fitdecode.FitReader(stream):
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "file_id":
                fields = {f.name: f.value for f in frame.fields}
                raw_kind = fields.get("type")
                kind = str(raw_kind) if raw_kind is not None else None
                time_created = _aware(fields.get("time_created"))
                break
    except (fitdecode.FitError, OSError, ValueError):
        # 截断或损坏的文件不该让整个索引失败
        return FitHeader(entry=entry, time_created=None, kind=None)
    return FitHeader(entry=entry, time_created=time_created, kind=kind)


def parse_fit(stream: BinaryIO, entry: int = 0) -> FitStream | None:
    """解析一次活动的逐秒记录。不是活动文件（无 session 或无 record）时返回 None。"""
    session: dict[str, Any] = {}
    raw_records: list[dict[str, Any]] = []

    try:
        for frame in fitdecode.FitReader(stream):
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "session" and not session:
                session = {f.name: f.value for f in frame.fields}
            elif frame.name == "record":
                raw_records.append({f.name: f.value for f in frame.fields})
    except (fitdecode.FitError, OSError, ValueError):
        return None

    start = _aware(session.get("start_time"))
    if start is None or not raw_records:
        return None

    samples: list[FitSample] = []
    for rec in raw_records:
        ts = _aware(rec.get("timestamp"))
        if ts is None:
            continue
        offset = int(round((ts - start).total_seconds()))
        if offset < 0:
            # 开始前的记录（设备在按下开始前已在采样）不计入，否则时间轴会倒着走
            continue
        samples.append(
            FitSample(
                offset_s=offset,
                hr=_as_int(rec.get("heart_rate")),
                speed_mps=_speed_mps(rec.get("enhanced_speed")),
                altitude_m=_as_float(rec.get("enhanced_altitude")),
                cadence=_as_int(rec.get("cadence")),
                distance_m=_as_float(rec.get("distance")),
                power_w=_as_int(rec.get("power")),
            )
        )

    if not samples:
        return None

    sport = session.get("sport")
    sub_sport = session.get("sub_sport")
    rpe = _as_int(session.get("workout_rpe"))
    feel = _as_int(session.get("workout_feel"))
    return FitStream(
        utc_start=start,
        sport=str(sport) if sport is not None else None,
        sub_sport=str(sub_sport) if sub_sport is not None else None,
        samples=samples,
        # workout_rpe 在 FIT 里是 Borg CR10（0–10）量表**乘以 10** 存的，
        # 所以 80 表示 Borg 8.0 而不是「RPE 80」。这里除回来，避免下游误读。
        workout_rpe=round(rpe / 10.0, 1) if rpe is not None else None,
        # workout_feel 是 0–100 分，低=感觉差、高=感觉好（FIT Profile 原文如此，
        # 没有给离散档位，所以不映射成「好/差」这类标签）。
        workout_feel=feel,
    )


def downsample(samples: list[FitSample], max_points: int) -> list[FitSample]:
    """等间隔抽稀到 max_points 个点，首尾必然保留。

    抽稀只用于看趋势。**峰值不能从抽稀结果里读**——最大心率、最快配速可能
    正好落在被跳过的点上，需要精确值时算全量（见 `sample_summary`）。
    """
    if max_points <= 0 or len(samples) <= max_points:
        return list(samples)
    step = len(samples) / max_points
    picked = [samples[min(int(i * step), len(samples) - 1)] for i in range(max_points)]
    if picked[-1].offset_s != samples[-1].offset_s:
        picked[-1] = samples[-1]
    return picked


def sample_summary(samples: Iterable[FitSample]) -> dict[str, Any]:
    """全量统计，用于在抽稀后的返回里给准确极值。

    速度只给平均值与 95 分位，**不给最大值**：即便清洗掉超过 20 m/s 的毛刺，
    剩下接近上限的单点仍然不能代表「最快配速」，反而会被当成能力指标引用。
    """
    items = list(samples)
    hrs = [s.hr for s in items if s.hr is not None]
    speeds = [s.speed_mps for s in items if s.speed_mps is not None]
    alts = [s.altitude_m for s in items if s.altitude_m is not None]
    cads = [s.cadence for s in items if s.cadence is not None]
    powers = [s.power_w for s in items if s.power_w is not None]
    out: dict[str, Any] = {
        "points": len(items),
        "hr_min": min(hrs) if hrs else None,
        "hr_max": max(hrs) if hrs else None,
        "hr_avg": round(sum(hrs) / len(hrs), 1) if hrs else None,
        "speed_avg_mps": round(sum(speeds) / len(speeds), 2) if speeds else None,
        "speed_p95_mps": round(percentile(speeds, 0.95), 2) if speeds else None,
        "altitude_min_m": round(min(alts), 1) if alts else None,
        "altitude_max_m": round(max(alts), 1) if alts else None,
        "cadence_max": max(cads) if cads else None,
    }
    if powers:
        # 功率的峰值是有效指标（冲刺能力），不像速度会因 GPS 毛刺失真，所以给最大值。
        out["power_avg_w"] = round(sum(powers) / len(powers), 1)
        out["power_max_w"] = max(powers)
    return out


def percentile(values: list[float], fraction: float) -> float:
    """线性插值分位数，避免为了一个数引入 numpy。"""
    if not values:
        raise ValueError("空序列没有分位数")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = fraction * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
