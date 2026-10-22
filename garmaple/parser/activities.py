"""解析 `*_summarizedActivities.json` 中的活动摘要。

佳明这份 JSON 的单位不是 SI，需要换算。实测交叉验证（282 条真实活动回归，
`(distance/100)/(duration/1000)` 与 `avgSpeed` 的比值中位数恰为 10.000）::

    distance        厘米    -> 米      / 100
    avgSpeed        0.1 米/秒 -> 米/秒  * 10   <- 注意是「乘」，不是常见的除 100
    elevationGain   厘米    -> 米      / 100
    duration        毫秒    -> 秒      / 1000
    beginTimestamp  毫秒 epoch（UTC）
    startTimeLocal  毫秒 epoch，已加过时区偏移，取其钟面时间即为当地时间

`avgSpeed` 这个系数是踩过的坑：若按 distance/duration 的直觉去除以 100，
得到的配速会荒谬到几十万秒每公里。改写前务必先看 tests/ 里的单位回归用例。

坐标与地点名称会被完整保留在 `Activity` 中，由上层 privacy 模块决定是否外露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

CM_PER_M = 100.0
MS_PER_S = 1000.0
# avgSpeed 存储值 = 真实 m/s / 10（实测比值中位数 10.000，n=282）
SPEED_TO_MPS = 10.0


def _num(value: Any) -> float | None:
    """佳明有时把数值写成字符串或留空，统一成 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def _ms_to_dt(value: Any) -> datetime | None:
    n = _num(value)
    if n is None or n <= 0:
        return None
    return datetime.fromtimestamp(n / MS_PER_S, tz=timezone.utc)


def _local_dt(value: Any) -> datetime | None:
    """startTimeLocal 是「本地钟面时间 + 时区偏移」的 epoch，用 UTC 解释即得钟面时间。"""
    n = _num(value)
    if n is None or n <= 0:
        return None
    return datetime.fromtimestamp(n / MS_PER_S, tz=timezone.utc)


@dataclass
class Activity:
    activity_id: int
    sport: str
    name: str | None
    utc_start: datetime | None
    local_start: datetime | None
    distance_m: float | None
    duration_s: float | None
    moving_s: float | None
    elapsed_s: float | None
    avg_hr: int | None
    max_hr: int | None
    calories: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    aerobic_te: float | None
    anaerobic_te: float | None
    vo2max: float | None
    avg_speed_mps: float | None
    stride_length_cm: float | None
    cadence_spm: float | None
    lap_count: int | None
    hr_zone_seconds: list[int] = field(default_factory=list)
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    location_name: str | None = None
    start_lat: float | None = None
    start_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    device_id: int | None = None
    user_profile_id: int | None = None

    @property
    def pace_s_per_km(self) -> float | None:
        if not self.distance_m or not self.duration_s or self.distance_m < 1:
            return None
        return self.duration_s / (self.distance_m / 1000.0)


def _sport(raw: Any) -> str:
    """sportType 常缺失，回退到 activityType。"""
    for key in (raw.get("sportType"), raw.get("activityType")):
        if isinstance(key, str) and key.strip():
            return key.strip().lower()
    return "unknown"


def parse_activity(raw: dict[str, Any]) -> Activity | None:
    aid = _opt_int(raw.get("activityId"))
    if aid is None:
        return None

    zones: list[int] = []
    for i in range(7):
        v = _num(raw.get(f"hrTimeInZone_{i}"))
        zones.append(int(v) if v is not None else 0)

    speed = _num(raw.get("avgSpeed"))

    return Activity(
        activity_id=aid,
        sport=_sport(raw),
        name=(raw.get("name") or None),
        utc_start=_ms_to_dt(raw.get("beginTimestamp")),
        local_start=_local_dt(raw.get("startTimeLocal")),
        distance_m=_scale(raw.get("distance"), CM_PER_M),
        duration_s=_scale(raw.get("duration"), MS_PER_S),
        moving_s=_scale(raw.get("movingDuration"), MS_PER_S),
        elapsed_s=_scale(raw.get("elapsedDuration"), MS_PER_S),
        avg_hr=_opt_int(raw.get("avgHr")),
        max_hr=_opt_int(raw.get("maxHr")),
        calories=_num(raw.get("calories")),
        elevation_gain_m=_scale(raw.get("elevationGain"), CM_PER_M),
        elevation_loss_m=_scale(raw.get("elevationLoss"), CM_PER_M),
        aerobic_te=_num(raw.get("aerobicTrainingEffect")),
        anaerobic_te=_num(raw.get("anaerobicTrainingEffect")),
        vo2max=_num(raw.get("vO2MaxValue")),
        avg_speed_mps=speed * SPEED_TO_MPS if speed is not None else None,
        stride_length_cm=_num(raw.get("avgStrideLength")),
        cadence_spm=_num(raw.get("avgRunCadence")),
        lap_count=_opt_int(raw.get("lapCount")),
        hr_zone_seconds=zones,
        location_name=(raw.get("locationName") or None),
        start_lat=_num(raw.get("startLatitude")),
        start_lon=_num(raw.get("startLongitude")),
        end_lat=_num(raw.get("endLatitude")),
        end_lon=_num(raw.get("endLongitude")),
        device_id=_opt_int(raw.get("deviceId")),
        user_profile_id=_opt_int(raw.get("userProfileId")),
    )


def _scale(value: Any, divisor: float) -> float | None:
    n = _num(value)
    return round(n / divisor, 3) if n is not None else None


def iter_payload(raw_export: Any) -> Iterable[dict[str, Any]]:
    """从一个分片文件的顶层结构里取出活动列表。

    形如 ``[{"summarizedActivitiesExport": [...]}]``，但不同固件版本可能略有差异，
    因此对常见的两层结构都做兼容。
    """
    if isinstance(raw_export, list):
        for block in raw_export:
            if isinstance(block, dict):
                items = block.get("summarizedActivitiesExport")
                if isinstance(items, list):
                    yield from (i for i in items if isinstance(i, dict))
    elif isinstance(raw_export, dict):
        items = raw_export.get("summarizedActivitiesExport")
        if isinstance(items, list):
            yield from (i for i in items if isinstance(i, dict))


def parse_activities(raw_export: Any) -> list[Activity]:
    out: list[Activity] = []
    for raw in iter_payload(raw_export):
        a = parse_activity(raw)
        if a is not None:
            out.append(a)
    return out
