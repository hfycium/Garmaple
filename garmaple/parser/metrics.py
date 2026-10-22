"""解析 `DI-Connect-Metrics/` 下的比赛成绩预测与最大摄氧量。

这两份文件由设备自己算好写进去，本模块只负责还原，不做任何推算。

**比赛预测**（`RunRacePredictions_*.json`）给出 5K / 10K / 半马 / 全马四个
距离的预计完赛时间，单位是秒。实测 937 条落在 604 天，其中 178 天有多条：
设备每次同步都会重算一次，153 天里的数值当天确实会变（例如 1092 → 1086）。
所以和训练状态一样，必须按 `timestamp` 取当日最后一条。
`timestamp` 同样是 UTC 钟面（`calendarDate: 2025-12-21` 对应 `16:03:52`），
只用于排序，不展示。

**最大摄氧量**（`MetricsMaxMetData_*.json`）实测 93 条落在 88 天，
日期很稀疏——只有设备在户外跑出有效数据时才写一条。同日多条已核对
**数值不冲突**（88 天零冲突），按日期收敛即可。
`sport` 字段 90 条为 `RUNNING`、3 条没有，同日不会出现两个项目。

`fitnessAge` 一律不解析：93 条里它**恒为字符串 "20"**，是设备未更新的
占位值。把它放进输出会让模型读出「体能年龄 20 岁」这个并不存在的结论。

另一处来源 `userBioMetrics.json` 的 `vo2MaxRunning` 与这份文件**逐日完全一致**
（94 条重叠、零差异），因此只取一处，不做跨文件合并——合并只会在两边
某天开始不同步时制造两份互相矛盾的真相。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Protocol, TypeVar

# 四个预测距离。半马与全马用标准距离，不是 21km / 42km 的近似值——
# 预测时间本身来自设备，配速必须按真实距离还原，否则 42km 会把配速算快 0.5%。
RACE_DISTANCES_KM: dict[str, float] = {
    "5k": 5.0,
    "10k": 10.0,
    "half": 21.0975,
    "marathon": 42.195,
}


class _Dated(Protocol):
    """两种记录共有的字段，供同日去重使用。"""

    date: str
    updated_at: datetime | None


_T = TypeVar("_T", bound=_Dated)


def _seconds(value: Any) -> int | None:
    """预测成绩是秒。0 与负数都不可能是有效成绩，按缺失处理。"""
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _positive_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


@dataclass
class RacePrediction:
    """某一天设备给出的四个距离预计完赛时间。缺失距离为 None，不用 0 冒充。"""

    date: str
    time_5k_s: int | None = None
    time_10k_s: int | None = None
    time_half_s: int | None = None
    time_marathon_s: int | None = None
    # 当日最后一次更新的 UTC 时刻，仅用于同日去重排序
    updated_at: datetime | None = None
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    user_profile_id: int | None = None

    @property
    def times(self) -> dict[str, int | None]:
        return {
            "5k": self.time_5k_s,
            "10k": self.time_10k_s,
            "half": self.time_half_s,
            "marathon": self.time_marathon_s,
        }

    def pace_s_per_km(self, key: str) -> float | None:
        """预测配速（秒/公里）。由时间与标准距离推出，不单独存。"""
        seconds = self.times.get(key)
        km = RACE_DISTANCES_KM.get(key)
        if not seconds or not km:
            return None
        return seconds / km


@dataclass
class Vo2maxSample:
    """一次最大摄氧量测量。`vo2max` 必填，缺失样本不产生记录。"""

    date: str
    vo2max: float
    # 实测 90 条 RUNNING、3 条为空；同日不会并存两个项目
    sport: str | None = None
    updated_at: datetime | None = None
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    user_profile_id: int | None = None


def parse_race_prediction(raw: Any) -> RacePrediction | None:
    """解析单条预测；日期非法或四个距离全缺时返回 None。"""
    if not isinstance(raw, dict):
        return None

    day = raw.get("calendarDate")
    try:
        date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None

    times = {
        "5k": _seconds(raw.get("raceTime5K")),
        "10k": _seconds(raw.get("raceTime10K")),
        "half": _seconds(raw.get("raceTimeHalf")),
        "marathon": _seconds(raw.get("raceTimeMarathon")),
    }
    if all(v is None for v in times.values()):
        return None

    return RacePrediction(
        date=str(day),
        time_5k_s=times["5k"],
        time_10k_s=times["10k"],
        time_half_s=times["half"],
        time_marathon_s=times["marathon"],
        updated_at=_parse_ts(raw.get("timestamp")),
        user_profile_id=_int(raw.get("userProfilePK")),
    )


def parse_vo2max(raw: Any) -> Vo2maxSample | None:
    """解析单条最大摄氧量；日期非法或数值缺失时返回 None。"""
    if not isinstance(raw, dict):
        return None

    day = raw.get("calendarDate")
    try:
        date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None

    value = _positive_float(raw.get("vo2MaxValue"))
    if value is None:
        return None

    return Vo2maxSample(
        date=str(day),
        vo2max=value,
        sport=_text(raw.get("sport")),
        updated_at=_parse_ts(raw.get("updateTimestamp")),
        user_profile_id=_int(raw.get("userProfilePK")),
    )


def _first_list(payload: dict[str, Any]) -> list[Any]:
    """顶层被包一层时，取第一个元素是对象的数组。

    与其余解析器同一策略：不枚举候选键名，按结构找，免得漏掉固件差异。
    """
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(i, dict) for i in value):
            return value
    return []


def _unwrap(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        payload = _first_list(payload)
    return payload if isinstance(payload, list) else []


def parse_race_predictions(payload: Any) -> list[RacePrediction]:
    """解析一个分片。实测顶层是数组；若被包了一层，退回按结构查找。"""
    out: list[RacePrediction] = []
    for raw in _unwrap(payload):
        rec = parse_race_prediction(raw)
        if rec is not None:
            out.append(rec)
    return out


def parse_vo2max_samples(payload: Any) -> list[Vo2maxSample]:
    """解析一个分片。"""
    out: list[Vo2maxSample] = []
    for raw in _unwrap(payload):
        rec = parse_vo2max(raw)
        if rec is not None:
            out.append(rec)
    return out


def _is_newer(candidate: _T, current: _T) -> bool:
    if candidate.updated_at is None:
        return False
    if current.updated_at is None:
        return True
    return candidate.updated_at > current.updated_at


def _latest_per_day(records: Iterable[_T]) -> list[_T]:
    """同一天只留 `updated_at` 最晚的一条，再按日期排序。

    时间戳缺失的记录不会盖掉有时戳的那条——它的新鲜度无从判断。
    """
    best: dict[str, _T] = {}
    for rec in records:
        current = best.get(rec.date)
        if current is None or _is_newer(rec, current):
            best[rec.date] = rec
    return [best[k] for k in sorted(best)]


def dedupe_predictions(records: Iterable[RacePrediction]) -> list[RacePrediction]:
    """比赛预测的同日收敛。"""
    return _latest_per_day(records)


def dedupe_vo2max(records: Iterable[Vo2maxSample]) -> list[Vo2maxSample]:
    """最大摄氧量的同日收敛。同日数值实测不冲突，取最后一条只是取定稿。"""
    return _latest_per_day(records)


__all__ = [
    "RACE_DISTANCES_KM",
    "RacePrediction",
    "Vo2maxSample",
    "dedupe_predictions",
    "dedupe_vo2max",
    "parse_race_prediction",
    "parse_race_predictions",
    "parse_vo2max",
    "parse_vo2max_samples",
]
