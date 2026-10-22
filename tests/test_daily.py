"""每日汇总解析测试。

样本字段取自真实 UDSFile 的形态（键名、嵌套、单位都照抄），
数值是手写的小数字——单测不依赖任何个人数据。
"""

from __future__ import annotations

from datetime import datetime

from garmaple.parser.daily import (
    dedupe,
    parse_daily,
    parse_days,
    utc_offset_seconds,
)

# 真实记录里 GMT 起点是本地零点减 8 小时
GMT_START = "2026-07-23T16:00:00.0"
LOCAL_START = "2026-07-24T00:00:00.0"


def make_block(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "userProfilePK": 999,
        "calendarDate": "2026-07-24",
        "wellnessStartTimeGmt": GMT_START,
        "wellnessEndTimeGmt": "2026-07-24T16:00:00.0",
        "wellnessStartTimeLocal": LOCAL_START,
        "wellnessEndTimeLocal": "2026-07-25T00:00:00.0",
        "totalSteps": 9523,
        "dailyStepGoal": 2620,
        "totalDistanceMeters": 18617,
        "moderateIntensityMinutes": 30,
        "vigorousIntensityMinutes": 18,
        "userIntensityMinutesGoal": 150,
        "restingHeartRate": 48,
        "minHeartRate": 45,
        "maxHeartRate": 155,
        "totalKilocalories": 2748.0,
        "activeKilocalories": 916.0,
        "bmrKilocalories": 1832.0,
        "allDayStress": {
            "calendarDate": "2026-07-24",
            "aggregatorList": [
                {
                    "type": "AWAKE",
                    "averageStressLevel": 55,
                    "maxStressLevel": 99,
                    "stressOffWristCount": 202,
                },
                {
                    "type": "TOTAL",
                    "averageStressLevel": 33,
                    "maxStressLevel": 99,
                    "stressOffWristCount": 203,
                    "restDuration": 34440,
                    "lowDuration": 12180,
                    "mediumDuration": 6900,
                    "highDuration": 8460,
                    # count 类字段是分钟，不是秒
                    "totalStressCount": 1433,
                    "totalDuration": 85980,
                },
            ],
        },
        "bodyBattery": {
            "chargedValue": 70,
            "drainedValue": 65,
            "bodyBatteryStatList": [
                {"bodyBatteryStatType": "HIGHEST", "statsValue": 75, "statTimestamp": "2026-07-24T02:57:00.0"},
                {"bodyBatteryStatType": "LOWEST", "statsValue": 5, "statTimestamp": "2026-07-23T16:01:00.0"},
                {"bodyBatteryStatType": "STARTOFDAY", "statsValue": 5, "statTimestamp": "2026-07-23T16:01:00.0"},
                {"bodyBatteryStatType": "ENDOFDAY", "statsValue": 10, "statTimestamp": "2026-07-24T16:00:00.0"},
                # 这一项日汇总用不到，确认不会被混进高低点
                {"bodyBatteryStatType": "DURINGSLEEP", "statsValue": 70, "statTimestamp": "2026-07-24T03:03:00.0"},
            ],
        },
    }
    block.update(overrides)
    return block


def test_parses_core_metrics() -> None:
    d = parse_daily(make_block())
    assert d is not None
    assert d.date == "2026-07-24"
    assert d.steps == 9523
    assert d.step_goal == 2620
    assert d.distance_m == 18617
    assert d.distance_km == 18.617
    assert d.resting_hr == 48
    assert d.min_hr == 45
    assert d.max_hr == 155
    assert d.calories_total == 2748.0
    assert d.calories_active == 916.0
    assert d.calories_bmr == 1832.0


def test_stress_uses_total_entry_not_awake() -> None:
    d = parse_daily(make_block())
    assert d is not None
    assert d.stress_avg == 33  # TOTAL 口径，不是 AWAKE 的 55
    assert d.stress_max == 99
    assert d.stress_off_wrist_min == 203


def test_stress_durations_are_seconds_but_counts_are_minutes() -> None:
    """回归：同一个结构里 restDuration 是秒，stressOffWristCount 却是分钟。"""
    d = parse_daily(make_block())
    assert d is not None
    assert d.stress_rest_s == 34440
    assert d.stress_low_s == 12180
    assert d.stress_medium_s == 6900
    assert d.stress_high_s == 8460
    assert d.stress_off_wrist_min == 203  # 不再除以 60


def test_body_battery_values_and_types_are_separated() -> None:
    d = parse_daily(make_block())
    assert d is not None
    assert (d.bb_charged, d.bb_drained) == (70, 65)
    assert d.bb_high == 75
    assert d.bb_low == 5  # LOWEST，不是 DURINGSLEEP 的 70
    assert d.bb_start == 5
    assert d.bb_end == 10


def test_body_battery_timestamps_converted_from_gmt_to_local() -> None:
    """statTimestamp 是 UTC 钟面；当日偏移 +8h 后才对得上本地作息。"""
    d = parse_daily(make_block())
    assert d is not None
    assert d.bb_high_at == datetime(2026, 7, 24, 10, 57)
    # 16:01 UTC 是本地 00:01，仍属于 24 号这一天
    assert d.bb_low_at == datetime(2026, 7, 24, 0, 1)


def test_zero_offset_day_keeps_timestamps_as_is() -> None:
    """早期记录里 GMT 与本地起点相同（偏移 0），此时不应再硬加 8 小时。"""
    block = make_block(
        wellnessStartTimeGmt=LOCAL_START,
        wellnessEndTimeGmt="2026-07-25T00:00:00.0",
    )
    d = parse_daily(block)
    assert d is not None
    assert d.bb_high_at == datetime(2026, 7, 24, 2, 57)


def test_intensity_minutes_counts_vigorous_double() -> None:
    d = parse_daily(make_block())
    assert d is not None
    assert d.intensity_minutes == 30 + 2 * 18
    assert d.intensity_goal_min == 150


def test_negative_sentinel_becomes_none() -> None:
    """-1 是「未测量」：实测 9 天 averageStressLevel == -1，整天摘表。"""
    block = make_block()
    block["allDayStress"] = {
        "aggregatorList": [
            {"type": "TOTAL", "averageStressLevel": -1, "maxStressLevel": -1,
             "restDuration": -1, "stressOffWristCount": 1438}
        ]
    }
    d = parse_daily(block)
    assert d is not None
    assert d.stress_avg is None
    assert d.stress_max is None
    assert d.stress_rest_s is None
    assert d.stress_off_wrist_min == 1438  # 摘表时长本身有效


def test_resting_hr_falls_back_to_current_day_value() -> None:
    d = parse_daily(make_block(restingHeartRate=None, currentDayRestingHeartRate=46))
    assert d is not None
    assert d.resting_hr == 46


def test_missing_body_battery_leaves_fields_none() -> None:
    block = make_block()
    del block["bodyBattery"]
    d = parse_daily(block)
    assert d is not None
    assert d.bb_high is None and d.bb_low is None and d.bb_charged is None
    assert d.bb_high_at is None


def test_missing_all_day_stress_leaves_fields_none() -> None:
    block = make_block()
    del block["allDayStress"]
    d = parse_daily(block)
    assert d is not None
    assert d.stress_avg is None and d.stress_high_s is None


def test_block_without_total_entry_is_tolerated() -> None:
    block = make_block()
    block["allDayStress"] = {"aggregatorList": [{"type": "AWAKE", "averageStressLevel": 55}]}
    d = parse_daily(block)
    assert d is not None
    assert d.stress_avg is None


def test_numeric_strings_are_coerced() -> None:
    d = parse_daily(make_block(totalSteps="1234", restingHeartRate="50"))
    assert d is not None
    assert d.steps == 1234
    assert d.resting_hr == 50


def test_invalid_or_missing_date_returns_none() -> None:
    assert parse_daily(make_block(calendarDate="")) is None
    assert parse_daily(make_block(calendarDate="2026/07/24")) is None
    block = make_block()
    del block["calendarDate"]
    assert parse_daily(block) is None


def test_non_dict_is_rejected() -> None:
    assert parse_daily(None) is None
    assert parse_daily([1, 2]) is None
    assert parse_daily("2026-07-24") is None


def test_user_profile_kept_for_privacy_layer() -> None:
    d = parse_daily(make_block())
    assert d is not None
    assert d.user_profile_id == 999


def test_utc_offset_from_day_boundaries() -> None:
    assert utc_offset_seconds(make_block()) == 8 * 3600
    assert utc_offset_seconds({}) == 0
    assert utc_offset_seconds({"wellnessStartTimeGmt": "x", "wellnessStartTimeLocal": "y"}) == 0
    # 超出 ±14 小时视为异常，按 0 处理
    bad = {"wellnessStartTimeGmt": "2026-07-24T00:00:00.0",
           "wellnessStartTimeLocal": "2026-07-26T00:00:00.0"}
    assert utc_offset_seconds(bad) == 0


def test_parse_days_handles_list() -> None:
    days = parse_days([make_block(), make_block(calendarDate="2026-07-25")])
    assert [d.date for d in days] == ["2026-07-24", "2026-07-25"]


def test_parse_days_handles_wrapped_payload() -> None:
    days = parse_days({"someListName": [make_block()]})
    assert len(days) == 1


def test_parse_days_skips_garbage() -> None:
    days = parse_days([make_block(), None, "x", {"calendarDate": "bad"}])
    assert len(days) == 1
    assert parse_days(None) == []
    assert parse_days("not json") == []


def test_dedupe_keeps_last_and_sorts() -> None:
    days = parse_days([
        make_block(calendarDate="2026-07-25", totalSteps=1),
        make_block(calendarDate="2026-07-24", totalSteps=2),
        make_block(calendarDate="2026-07-25", totalSteps=3),
    ])
    out = dedupe(days)
    assert [d.date for d in out] == ["2026-07-24", "2026-07-25"]
    assert out[1].steps == 3  # 后写入的覆盖先写入的


# ---------- 时区元数据异常的天 ----------


def test_offset_reliability() -> None:
    from garmaple.parser.daily import offset_is_reliable

    assert offset_is_reliable(make_block())
    # 恰好 0 是可信的（用户就在 UTC）
    assert offset_is_reliable(make_block(wellnessStartTimeGmt=LOCAL_START))
    # 非零但只有 -1 分钟：设备没写入时区
    assert not offset_is_reliable(
        make_block(
            wellnessStartTimeGmt="2026-07-24T00:01:00.0",
            wellnessStartTimeLocal="2026-07-24T00:00:00.0",
        )
    )
    assert not offset_is_reliable({})


def test_days_borrow_shard_offset_when_metadata_broken() -> None:
    """回归：最早 4 天的偏移是 -1 分钟，若按它自己算会整体差一个时区。"""
    broken = make_block(
        calendarDate="2026-07-23",
        wellnessStartTimeGmt="2026-07-23T00:01:00.0",
        wellnessStartTimeLocal="2026-07-23T00:00:00.0",
    )
    days = parse_days([make_block(), broken])
    by_date = {d.date: d for d in days}
    # 坏掉的那天借用同分片的 +8，而不是按 0 输出
    assert by_date["2026-07-23"].bb_high_at == datetime(2026, 7, 24, 10, 57)
    assert by_date["2026-07-24"].bb_high_at == datetime(2026, 7, 24, 10, 57)


def test_parse_daily_without_fallback_uses_local_metadata() -> None:
    broken = make_block(
        wellnessStartTimeGmt="2026-07-24T00:01:00.0",
        wellnessStartTimeLocal="2026-07-24T00:00:00.0",
    )
    d = parse_daily(broken)
    assert d is not None
    assert d.bb_high_at == datetime(2026, 7, 24, 2, 57)  # 偏移按 0 处理
