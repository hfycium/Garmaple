"""活动解析的单元测试。

全部使用合成数据，不依赖任何真实导出包，因此可以在 CI 与贡献者机器上直接跑。
单位相关的用例是回归保护：avgSpeed 的换算系数曾被写错成「除以 100」，
导致配速荒谬，这里用真实数据的比例关系固定住正确行为。
"""

from __future__ import annotations

import pytest

from garmaple.parser.activities import (
    CM_PER_M,
    SPEED_TO_MPS,
    iter_payload,
    parse_activities,
    parse_activity,
)


def make_raw(**overrides: object) -> dict[str, object]:
    """一条最小可用的活动记录，单位与佳明导出保持一致。"""
    raw: dict[str, object] = {
        "activityId": 123456789,
        "activityType": "running",
        "name": "测试跑步",
        "beginTimestamp": 1_740_000_000_000,  # ms, UTC
        "startTimeLocal": 1_740_028_800_000,  # +8h
        "distance": 1_000_000.0,  # cm  -> 10000 m
        "duration": 3_600_000.0,  # ms  -> 3600 s
        "movingDuration": 3_500_000.0,
        "elapsedDuration": 3_700_000.0,
        "avgSpeed": 0.2777,  # 需 *10 -> 2.777 m/s
        "avgHr": 150,
        "maxHr": 172,
        "calories": 700.0,
        "elevationGain": 12_000.0,  # cm -> 120 m
        "elevationLoss": 11_000.0,
        "aerobicTrainingEffect": 3.2,
        "anaerobicTrainingEffect": 0.4,
        "vO2MaxValue": 52.0,
        "avgStrideLength": 105.5,
        "avgRunCadence": 178.0,
        "lapCount": 10,
        "hrTimeInZone_3": 1800,
        "hrTimeInZone_4": 900,
        "locationName": "某市",
        "startLatitude": 30.0,
        "startLongitude": 120.0,
        "endLatitude": 30.1,
        "endLongitude": 120.1,
        "deviceId": 999,
        "userProfileId": 111,
    }
    raw.update(overrides)
    return raw


def test_distance_and_duration_units() -> None:
    act = parse_activity(make_raw())
    assert act is not None
    assert act.distance_m == pytest.approx(10_000.0)
    assert act.duration_s == pytest.approx(3_600.0)
    assert act.elevation_gain_m == pytest.approx(120.0)


def test_speed_uses_multiplier_not_divisor() -> None:
    """回归用例：avgSpeed 必须 *10 才是 m/s。"""
    raw = make_raw()
    act = parse_activity(raw)
    assert act is not None
    expected = float(raw["avgSpeed"]) * SPEED_TO_MPS  # type: ignore[arg-type]
    assert act.avg_speed_mps == pytest.approx(expected)
    assert act.avg_speed_mps == pytest.approx(2.777, abs=1e-3)


def test_speed_times_duration_equals_distance() -> None:
    """真实数据里这三者应当自洽，这是单位没写错的判据。"""
    act = parse_activity(make_raw())
    assert act is not None
    assert act.avg_speed_mps is not None and act.duration_s is not None
    assert act.avg_speed_mps * act.duration_s == pytest.approx(act.distance_m, rel=0.01)


def test_pace_derived_from_distance_and_duration() -> None:
    act = parse_activity(make_raw())
    assert act is not None
    # 3600 s / 10 km = 360 s/km = 6'00"
    assert act.pace_s_per_km == pytest.approx(360.0)


def test_pace_is_none_for_zero_distance() -> None:
    act = parse_activity(make_raw(distance=0))
    assert act is not None
    assert act.pace_s_per_km is None


def test_local_start_is_wall_clock_time() -> None:
    act = parse_activity(make_raw())
    assert act is not None and act.utc_start and act.local_start
    # 本地时间比 UTC 晚 8 小时，钟面时间应体现这一点
    assert (act.local_start - act.utc_start).total_seconds() == pytest.approx(8 * 3600)


def test_missing_fields_do_not_crash() -> None:
    act = parse_activity({"activityId": 1})
    assert act is not None
    assert act.sport == "unknown"
    assert act.distance_m is None
    assert act.avg_hr is None
    assert act.hr_zone_seconds == [0] * 7


def test_sport_falls_back_to_activity_type() -> None:
    act = parse_activity(make_raw(sportType=None, activityType="Cycling"))
    assert act is not None
    assert act.sport == "cycling"


def test_record_without_activity_id_is_skipped() -> None:
    assert parse_activity({"distance": 100}) is None


def test_sensitive_fields_are_preserved_for_privacy_layer() -> None:
    """坐标与地点必须原样保留，由 privacy 层决定是否输出。"""
    act = parse_activity(make_raw())
    assert act is not None
    assert act.start_lat == pytest.approx(30.0)
    assert act.location_name == "某市"
    assert act.device_id == 999
    assert act.user_profile_id == 111


def test_iter_payload_handles_list_and_dict_shapes() -> None:
    item = {"activityId": 1}
    assert list(iter_payload([{"summarizedActivitiesExport": [item]}])) == [item]
    assert list(iter_payload({"summarizedActivitiesExport": [item]})) == [item]
    assert list(iter_payload([])) == []
    assert list(iter_payload({"unexpected": 1})) == []


def test_parse_activities_returns_all_valid_records() -> None:
    payload = [
        {
            "summarizedActivitiesExport": [
                make_raw(activityId=1),
                make_raw(activityId=2),
                {"no_id": True},  # 应被跳过
            ]
        }
    ]
    acts = parse_activities(payload)
    assert [a.activity_id for a in acts] == [1, 2]


def test_constant_documented_consistency() -> None:
    assert CM_PER_M == 100.0
    assert SPEED_TO_MPS == 10.0
