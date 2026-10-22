"""解析 `DI-Connect-Aggregator/UDSFile_*.json` 中的每日汇总。

UDS 是佳明的 User Daily Summary：按日聚合的 wellness 数据，文件按日期区间分片，
每片是一个数组，一个元素对应一天。这里关注四类信息：

- 活动量：步数、距离、强度分钟
- 心率：静息、全天最低 / 最高
- 压力：`allDayStress.aggregatorList` 中 `type == "TOTAL"` 的那条
- 身体电量：充放电总量、当日高低点及其时刻

单位（实测）：时长类字段是**秒**，而 `stressOffWristCount` / `totalStressCount`
这类 count 字段实际是**分钟**——两者混在同一结构里，很容易看错。
校验方式：`totalStressCount == totalDuration / 60`。

时区（实测）：`bodyBatteryStatList[].statTimestamp` 是 UTC 钟面，而 `calendarDate`
是本地日历日。604 天的 `wellnessStartTimeLocal - wellnessStartTimeGmt` 恒为 +8:00，
另有 4 天是 -1 分钟（设备还没写入时区），这些天按分片内最常见的偏移兜底。
因此这里按当日偏移换算成本地钟面时间，而不是写死东八区——换时区或
个别天的元数据坏掉都不会算错。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

SEC_PER_MIN = 60.0
# 时区偏移按 15 分钟取整（现实中的时区都是 15 分钟的整数倍）；
# 超出 ±14 小时视为数据异常，按 0 处理
_OFFSET_STEP_S = 900.0
_MAX_OFFSET_S = 14 * 3600.0
# 非零但不足 5 分钟的偏移不可能是真实时区：实测最早 4 天是 -1 分钟，
# 属设备尚未写入时区的记录，这些天需要借分片内其它天的偏移兜底
_UNRELIABLE_OFFSET_S = 300.0


def _num(value: Any) -> float | None:
    """佳明有时把数值写成字符串或留空，统一成 float。

    负数是「未测量」的哨兵（实测 9 天的 `averageStressLevel == -1`，对应整天摘表），
    任何日汇总指标都不会真的是负数，因此一律当作缺失，避免被算进平均值。
    """
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


def _raw_offset_seconds(block: dict[str, Any]) -> float | None:
    """本地起点减 GMT 起点，未取整、未做合理性判断。"""
    gmt = block.get("wellnessStartTimeGmt")
    local = block.get("wellnessStartTimeLocal")
    try:
        g = datetime.fromisoformat(str(gmt))
        l = datetime.fromisoformat(str(local))
    except (TypeError, ValueError):
        return None
    return (l - g).total_seconds()


def utc_offset_seconds(block: dict[str, Any]) -> float:
    """从当天的 GMT / 本地起止时间反推时区偏移（秒，取整到 15 分钟）。"""
    raw = _raw_offset_seconds(block)
    if raw is None or abs(raw) > _MAX_OFFSET_S:
        return 0.0
    return round(raw / _OFFSET_STEP_S) * _OFFSET_STEP_S


def offset_is_reliable(block: dict[str, Any]) -> bool:
    """判断当天的时区偏移是否可信。

    - 恰好为 0：可信（用户就在 UTC，或设备按 UTC 记录）
    - 非零但不足 5 分钟：不可信（实测最早 4 天是 -1 分钟）
    - 超过 ±14 小时：不可信
    """
    raw = _raw_offset_seconds(block)
    if raw is None or abs(raw) > _MAX_OFFSET_S:
        return False
    return not (0 < abs(raw) < _UNRELIABLE_OFFSET_S)


def _to_local(value: Any, offset_s: float) -> datetime | None:
    """把 UTC 钟面字符串换算成本地钟面时间（naive，仅用于展示）。"""
    if not value:
        return None
    try:
        utc = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return utc + timedelta(seconds=offset_s)


@dataclass
class DailyStat:
    """一天的 wellness 汇总。缺失字段一律为 None，不用 0 冒充。"""

    date: str
    # --- 活动量 ---
    steps: int | None = None
    step_goal: int | None = None
    distance_m: float | None = None
    moderate_min: int | None = None
    vigorous_min: int | None = None
    intensity_goal_min: int | None = None
    # --- 心率 ---
    resting_hr: int | None = None
    min_hr: int | None = None
    max_hr: int | None = None
    # --- 热量 ---
    calories_total: float | None = None
    calories_active: float | None = None
    calories_bmr: float | None = None
    # --- 压力（TOTAL 口径）---
    stress_avg: int | None = None
    stress_max: int | None = None
    stress_off_wrist_min: int | None = None
    stress_rest_s: int | None = None
    stress_low_s: int | None = None
    stress_medium_s: int | None = None
    stress_high_s: int | None = None
    # --- 身体电量 ---
    bb_charged: int | None = None
    bb_drained: int | None = None
    bb_high: int | None = None
    bb_low: int | None = None
    bb_start: int | None = None
    bb_end: int | None = None
    bb_high_at: datetime | None = None
    bb_low_at: datetime | None = None
    # --- 当日时区偏移 ---
    # 睡眠数据里只有 GMT 时间戳、没有本地字段，要靠这里借偏移才能换算钟面时刻
    utc_offset_hours: float | None = None
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    user_profile_id: int | None = None

    @property
    def intensity_minutes(self) -> int | None:
        """佳明的强度分钟口径：中等算 1 分钟，高强度算 2 分钟。"""
        if self.moderate_min is None and self.vigorous_min is None:
            return None
        return (self.moderate_min or 0) + 2 * (self.vigorous_min or 0)

    @property
    def distance_km(self) -> float | None:
        return round(self.distance_m / 1000.0, 3) if self.distance_m is not None else None


def _stress_total(block: dict[str, Any]) -> dict[str, Any] | None:
    """取出 `type == "TOTAL"` 的压力聚合。

    AWAKE / ASLEEP 两条是睡眠视角的拆分，日汇总用不上，先丢掉。
    """
    stress = block.get("allDayStress")
    if not isinstance(stress, dict):
        return None
    for item in stress.get("aggregatorList") or []:
        if isinstance(item, dict) and item.get("type") == "TOTAL":
            return item
    return None


def _battery_stats(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 bodyBatteryStatList 按类型索引，便于按 HIGHEST / LOWEST 取值。"""
    battery = block.get("bodyBattery")
    if not isinstance(battery, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in battery.get("bodyBatteryStatList") or []:
        if isinstance(item, dict):
            kind = item.get("bodyBatteryStatType")
            if isinstance(kind, str):
                out[kind] = item
    return out


def parse_daily(raw: Any, fallback_offset: float = 0.0) -> DailyStat | None:
    """解析单日记录；`calendarDate` 缺失或非法时返回 None。

    `fallback_offset` 用于当天时区偏移不可信的情况（见 `offset_is_reliable`），
    由 `parse_days` 按分片内最常见的偏移传入。
    """
    if not isinstance(raw, dict):
        return None

    day = raw.get("calendarDate")
    try:
        date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None

    offset = utc_offset_seconds(raw)
    if not offset_is_reliable(raw):
        offset = fallback_offset
    stress = _stress_total(raw) or {}
    battery = _battery_stats(raw)

    def bb_value(kind: str) -> int | None:
        return _int((battery.get(kind) or {}).get("statsValue"))

    def bb_time(kind: str) -> datetime | None:
        return _to_local((battery.get(kind) or {}).get("statTimestamp"), offset)

    body = raw.get("bodyBattery")
    body = body if isinstance(body, dict) else {}

    def seconds(value: Any) -> int | None:
        n = _num(value)
        return int(round(n)) if n is not None else None

    return DailyStat(
        date=str(day),
        steps=_int(raw.get("totalSteps")),
        step_goal=_int(raw.get("dailyStepGoal")),
        distance_m=_num(raw.get("totalDistanceMeters")),
        moderate_min=_int(raw.get("moderateIntensityMinutes")),
        vigorous_min=_int(raw.get("vigorousIntensityMinutes")),
        intensity_goal_min=_int(raw.get("userIntensityMinutesGoal")),
        # currentDayRestingHeartRate 是当天实时值，历史日回退到它以免整列缺失
        resting_hr=_int(raw.get("restingHeartRate"))
        or _int(raw.get("currentDayRestingHeartRate")),
        min_hr=_int(raw.get("minHeartRate")),
        max_hr=_int(raw.get("maxHeartRate")),
        calories_total=_num(raw.get("totalKilocalories")),
        calories_active=_num(raw.get("activeKilocalories")),
        calories_bmr=_num(raw.get("bmrKilocalories")),
        stress_avg=_int(stress.get("averageStressLevel")),
        stress_max=_int(stress.get("maxStressLevel")),
        stress_off_wrist_min=_int(stress.get("stressOffWristCount")),
        stress_rest_s=seconds(stress.get("restDuration")),
        stress_low_s=seconds(stress.get("lowDuration")),
        stress_medium_s=seconds(stress.get("mediumDuration")),
        stress_high_s=seconds(stress.get("highDuration")),
        bb_charged=_int(body.get("chargedValue")),
        bb_drained=_int(body.get("drainedValue")),
        bb_high=bb_value("HIGHEST"),
        bb_low=bb_value("LOWEST"),
        bb_start=bb_value("STARTOFDAY"),
        bb_end=bb_value("ENDOFDAY"),
        bb_high_at=bb_time("HIGHEST"),
        bb_low_at=bb_time("LOWEST"),
        utc_offset_hours=round(offset / 3600.0, 4),
        user_profile_id=_int(raw.get("userProfilePK")),
    )


def _first_list(payload: dict[str, Any]) -> list[Any]:
    """顶层被包一层时，取第一个元素是对象的数组。

    不枚举候选键名——猜键名比按结构找更容易漏掉固件差异。
    """
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(i, dict) for i in value):
            return value
    return []


def parse_days(payload: Any) -> list[DailyStat]:
    """解析一个分片。实测顶层是数组；若被包了一层，退回按结构查找。

    分片内先取「可信偏移」的众数作为兜底，再逐日解析：
    个别天（实测最早 4 天）的时区元数据是坏的，若按它自己算，
    身体电量峰谷时刻会整体差一个时区。
    """
    if isinstance(payload, dict):
        payload = _first_list(payload)
    if not isinstance(payload, list):
        return []

    blocks = [b for b in payload if isinstance(b, dict)]
    trusted = [
        utc_offset_seconds(b) for b in blocks if offset_is_reliable(b)
    ]
    fallback = Counter(trusted).most_common(1)[0][0] if trusted else 0.0

    out: list[DailyStat] = []
    for raw in blocks:
        stat = parse_daily(raw, fallback_offset=fallback)
        if stat is not None:
            out.append(stat)
    return out


def dedupe(days: Iterable[DailyStat]) -> list[DailyStat]:
    """多次导出可能带同一天，按日期去重后排序（后写入的覆盖先写入的）。"""
    unique = {d.date: d for d in days}
    return [unique[k] for k in sorted(unique)]
