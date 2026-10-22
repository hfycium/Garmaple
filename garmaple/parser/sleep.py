"""解析 `DI-Connect-Wellness/*sleepData.json` 中的睡眠记录。

文件按季度分片，每片是一个数组，一个元素对应一晚。字段很少，全貌是：

    sleepStartTimestampGMT / sleepEndTimestampGMT   入睡与醒来的 GMT 钟面
    calendarDate                                     归属日（本地）
    sleepWindowConfirmationType                      睡眠窗的可信度
    deep/light/awake/unmeasurable/remSleepSeconds    各分期秒数
    average/lowest/highestRespiration                睡眠呼吸率
    retro                                            是否事后补录

两个实测结论决定了这个模块的写法：

1. **`calendarDate` 是本地「醒来日」**，不是入睡日。608 条里 561 条的
   GMT 醒来日与它同日，用当日时区偏移换算成本地后是 608/608 全部同日。
   因此「昨晚睡得怎么样」要去查**今天**的记录——这个语义写进工具说明，
   否则 AI 会查错一天。

2. **记录里只有 GMT，没有本地时间字段**。要给出钟面时刻只能借 UDS
   当天的偏移（`DailyStat.utc_offset_hours`）。拿不到偏移时不猜、不给
   时刻，只给不依赖时区的时长与分期——时长是差值，不受时区影响。

第三件最容易写错的事：**无有效分期的记录不能报「睡了 0 分钟」**。
608 条里有 104 条是这样：

    70 条 OFF_WRIST                摘表，分期字段存在但全 0
    16 条 AUTO_CONFIRMED_FINAL     确认了睡眠窗，但分期全 0
    18 条 UNCONFIRMED              连分期字段都没有，窗口是固定占位值

它们各自有 8~13 小时的「睡眠窗」，按秒数相加却得到 0。这里用 `status`
区分「没戴表」「设备没记录分期」「睡眠窗未确认」三种情况，时长一律留空。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

SEC_PER_MIN = 60.0

# 分期字段。REM 单独拎出来是因为最早的一批记录根本没有这个键，
# 但实测「有 REM 的 504 条」五项加总严格等于睡眠跨度，说明 awake 与
# unmeasurable 都包含在窗口内，asleep 只能是 深 + 浅 + REM。
_STAGE_KEYS = (
    ("deep_min", "deepSleepSeconds"),
    ("light_min", "lightSleepSeconds"),
    ("rem_min", "remSleepSeconds"),
    ("awake_min", "awakeSleepSeconds"),
    ("unmeasured_min", "unmeasurableSeconds"),
)

# 状态：ok 之外的一律没有时长
STATUS_OK = "ok"
STATUS_OFF_WRIST = "off_wrist"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_NO_STAGES = "no_stages"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(round(n)) if n is not None else None


def _parse_ts(value: Any) -> datetime | None:
    """解析 GMT 钟面字符串；佳明写成 `2025-12-25T15:20:00.0`。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _to_local(ts: datetime | None, offset_hours: float | None) -> datetime | None:
    """GMT → 本地钟面。偏移未知时返回 None，不用 0 冒充。"""
    if ts is None or offset_hours is None:
        return None
    return ts + timedelta(hours=offset_hours)


@dataclass
class SleepRecord:
    """一晚睡眠。缺失字段一律为 None，不用 0 冒充。"""

    date: str
    # --- 时间（本地钟面，仅在拿到时区偏移时才有值）---
    start_at: datetime | None = None
    end_at: datetime | None = None
    # --- 时长（不依赖时区）---
    in_bed_min: int | None = None
    deep_min: int | None = None
    light_min: int | None = None
    rem_min: int | None = None
    awake_min: int | None = None
    unmeasured_min: int | None = None
    # --- 呼吸率 ---
    respiration_avg: float | None = None
    respiration_low: float | None = None
    respiration_high: float | None = None
    # --- 可信度 ---
    status: str = STATUS_OK
    retro: bool = False
    # 秒级原值：逐字段取整到分钟再相加会累积误差（实测 11 条对不上），
    # 所以睡着时长先按秒算，最后一次取整
    _asleep_seconds: int | None = field(default=None, repr=False, compare=False)

    @property
    def asleep_min(self) -> int | None:
        """真正睡着的时间 = 睡眠窗 − 清醒 − 测不到。

        不直接用「深 + 浅 + REM」，是因为有 16 晚设备没有记 REM 字段
        （`AUTO_CONFIRMED_FINAL`），四项加总仍严格等于睡眠窗。那种情况下
        「深 + 浅 + REM」会少算一整段，而「窗口减去清醒」两种情形都成立：
        有 REM 时它与「深 + 浅 + REM」等价（实测 504 条五项加总 == 跨度）。
        """
        if self._asleep_seconds is None:
            return None
        return int(round(self._asleep_seconds / SEC_PER_MIN))

    @property
    def has_stages(self) -> bool:
        return self.status == STATUS_OK


def _status_of(raw: dict[str, Any], stages_sum: int) -> str:
    kind = str(raw.get("sleepWindowConfirmationType") or "").upper()
    if kind == "OFF_WRIST":
        return STATUS_OFF_WRIST
    if "deepSleepSeconds" not in raw:
        # 连字段都没有：睡眠窗只是占位（实测窗口固定 8 或 9 小时整）
        return STATUS_UNCONFIRMED
    if stages_sum <= 0:
        # 字段在但全 0：设备确认了窗口却没记分期
        return STATUS_NO_STAGES
    return STATUS_OK


def parse_sleep(raw: Any, offset_hours: float | None = None) -> SleepRecord | None:
    """解析单晚记录；`calendarDate` 缺失或非法时返回 None。

    `offset_hours` 是**当日**的本地时区偏移，由调用方从 UDS 日汇总里取。
    拿不到就传 None：时刻留空，时长照给。
    """
    if not isinstance(raw, dict):
        return None

    day = raw.get("calendarDate")
    try:
        date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None

    stages: dict[str, int | None] = {}
    stages_sum = 0
    for field_name, key in _STAGE_KEYS:
        seconds = _num(raw.get(key))
        stages[field_name] = int(round(seconds / SEC_PER_MIN)) if seconds is not None else None
        if seconds is not None:
            stages_sum += seconds

    status = _status_of(raw, stages_sum)

    start_gmt = _parse_ts(raw.get("sleepStartTimestampGMT"))
    end_gmt = _parse_ts(raw.get("sleepEndTimestampGMT"))
    span_s: float | None = None
    if start_gmt and end_gmt:
        delta = (end_gmt - start_gmt).total_seconds()
        if delta > 0:
            span_s = delta

    record = SleepRecord(
        date=str(day),
        start_at=_to_local(start_gmt, offset_hours),
        end_at=_to_local(end_gmt, offset_hours),
        in_bed_min=None,
        respiration_avg=_num(raw.get("averageRespiration")),
        respiration_low=_num(raw.get("lowestRespiration")),
        respiration_high=_num(raw.get("highestRespiration")),
        status=status,
        retro=bool(raw.get("retro")),
    )

    if status == STATUS_OK:
        record.in_bed_min = int(round(span_s / SEC_PER_MIN)) if span_s else None
        record.deep_min = stages["deep_min"]
        record.light_min = stages["light_min"]
        record.rem_min = stages["rem_min"]
        record.awake_min = stages["awake_min"]
        record.unmeasured_min = stages["unmeasured_min"]
        if span_s is not None:
            # 清醒与「测不到」不算睡着，从窗口里扣掉；两者缺失按 0 处理
            awake_s = _num(raw.get("awakeSleepSeconds")) or 0.0
            unmeasured_s = _num(raw.get("unmeasurableSeconds")) or 0.0
            asleep_s = span_s - awake_s - unmeasured_s
            record._asleep_seconds = int(round(asleep_s)) if asleep_s >= 0 else None
    # 无有效分期时只保留时刻与状态：OFF_WRIST 的窗口是「摘下到戴上」，
    # UNCONFIRMED 的是固定占位值，当成睡眠时长会被误读成睡了十几小时。

    return record


def _first_list(payload: dict[str, Any]) -> list[Any]:
    """顶层被包一层时，取第一个元素是对象的数组（沿袭 daily.py 的做法）。"""
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(i, dict) for i in value):
            return value
    return []


def parse_sleeps(
    payload: Any,
    offsets: Mapping[str, float] | None = None,
) -> list[SleepRecord]:
    """解析一个分片。`offsets` 是「本地日期 → 时区小时数」映射。"""
    if isinstance(payload, dict):
        payload = _first_list(payload)
    if not isinstance(payload, list):
        return []

    lookups = offsets or {}
    out: list[SleepRecord] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        record = parse_sleep(raw, offset_hours=lookups.get(str(raw.get("calendarDate"))))
        if record is not None:
            out.append(record)
    return out


def dedupe(records: Iterable[SleepRecord]) -> list[SleepRecord]:
    """多次导出可能带同一晚，按日期去重后排序（后写入的覆盖先写入的）。"""
    unique = {r.date: r for r in records}
    return [unique[k] for k in sorted(unique)]
