"""汇总计算测试。"""

from __future__ import annotations

import pytest

from garmaple.stats import human_duration, human_pace, summarize, zone_summary


def row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "date": "2026-10-01",
        "sport": "running",
        "distance_m": 10_000.0,
        "duration_s": 3600.0,
        "moving_s": 3500.0,
        "elevation_gain_m": 100.0,
        "avg_hr": 150,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, None), (0, None), (45, "45s"), (90, "1m30s"), (3600, "1h00m"), (5430, "1h30m")],
)
def test_human_duration(seconds, expected) -> None:
    assert human_duration(seconds) == expected


@pytest.mark.parametrize(
    "pace,expected",
    [(None, None), (0, None), (360, "6'00\"/km"), (334.4, "5'34\"/km")],
)
def test_human_pace(pace, expected) -> None:
    assert human_pace(pace) == expected


def test_empty_input() -> None:
    assert summarize([]) == {"count": 0}


def test_single_activity() -> None:
    out = summarize([row()])
    assert out["count"] == 1
    assert out["total_distance_km"] == pytest.approx(10.0)
    assert out["total_duration_s"] == 3600
    assert out["avg_pace_s_per_km"] == pytest.approx(360.0)
    assert out["sports"] == {"running": 1}


def test_pace_is_weighted_not_arithmetic_mean() -> None:
    """一次 1km 的快跑不应把 20km 的慢跑拉快。"""
    rows = [
        row(distance_m=20_000.0, duration_s=8000.0),  # 400 s/km
        row(distance_m=1_000.0, duration_s=200.0),    # 200 s/km
    ]
    out = summarize(rows)
    # 加权：(8000+200) / ((20000+1000)/1000) = 390.5
    assert out["avg_pace_s_per_km"] == pytest.approx(390.5, abs=0.1)
    arithmetic_mean = (400 + 200) / 2
    assert out["avg_pace_s_per_km"] != pytest.approx(arithmetic_mean)


def test_totals_accumulate() -> None:
    out = summarize([row(), row(distance_m=5000.0, duration_s=1800.0)])
    assert out["count"] == 2
    assert out["total_distance_km"] == pytest.approx(15.0)
    assert out["total_duration_s"] == 5400
    assert out["total_elevation_m"] == 200


def test_longest_picked() -> None:
    out = summarize([row(), row(date="2026-10-02", distance_m=21_097.5)])
    assert out["longest"]["date"] == "2026-10-02"
    assert out["longest"]["distance_km"] == pytest.approx(21.1, abs=0.05)


def test_fastest_only_counts_runs_over_3km() -> None:
    rows = [
        row(distance_m=500.0, duration_s=100.0),   # 200 s/km，太短，忽略
        row(distance_m=5000.0, duration_s=1500.0), # 300 s/km
        row(distance_m=10_000.0, duration_s=2700.0),  # 270 s/km，最快
    ]
    out = summarize(rows)
    assert out["fastest"]["pace_s_per_km"] == pytest.approx(270.0)


def test_fastest_ignores_non_running() -> None:
    rows = [row(sport="cycling", distance_m=40_000.0, duration_s=4000.0)]
    out = summarize(rows)
    assert "fastest" not in out


def test_sports_sorted_by_count() -> None:
    rows = [row() for _ in range(3)] + [row(sport="cycling") for _ in range(1)]
    out = summarize(rows)
    assert list(out["sports"].items()) == [("running", 3), ("cycling", 1)]


def test_heart_rate_averaged_over_records_with_value() -> None:
    rows = [row(avg_hr=140), row(avg_hr=160), row(avg_hr=None)]
    out = summarize(rows)
    assert out["avg_hr"] == 150
    assert out["max_hr"] == 160


def test_no_hr_no_field() -> None:
    out = summarize([row(avg_hr=None)])
    assert "avg_hr" not in out
    assert "max_hr" not in out


def test_zero_distance_does_not_divide_by_zero() -> None:
    out = summarize([row(distance_m=0, duration_s=600)])
    assert "avg_pace_s_per_km" not in out
    assert out["count"] == 1


def test_missing_keys_are_tolerated() -> None:
    out = summarize([{"sport": "running"}])
    assert out["count"] == 1
    assert out["total_distance_km"] == 0.0


def test_none_values_are_not_emitted() -> None:
    out = summarize([row(moving_s=None, elevation_gain_m=None)])
    # 缺失字段直接不输出，避免返回值被 null 撑大
    assert "total_moving" not in out
    assert out["total_elevation_m"] == 0


# ---------- 心率区间 ----------


def zone_row(zones: list[int], **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {f"hr_zone_{i}": v for i, v in enumerate(zones)}
    base.update(overrides)
    return base


def test_zone_summary_counts_time_and_percent() -> None:
    out = zone_summary(
        [
            zone_row([0, 0, 600, 1800, 600, 0, 0]),
            zone_row([0, 0, 0, 1200, 300, 0, 0]),
        ]
    )
    assert out["activities"] == 2
    assert out["with_zone_data"] == 2
    assert out["total_s"] == 4500
    assert [z["seconds"] for z in out["zones"]] == [0, 0, 600, 3000, 900, 0, 0]
    assert out["zones"][3]["pct"] == 66.7
    assert out["dominant_zone"] == 3


def test_zone_summary_skips_activities_without_data() -> None:
    """没有区间数据的活动不能被当成「各档都是 0」。

    当成 0 会把每一档的占比一起稀释，等于说「他那几次没在任何区间待过」。
    """
    out = zone_summary([zone_row([0, 0, 0, 1000, 0, 0, 0]), zone_row([0] * 7)])
    assert out["with_zone_data"] == 1
    assert out["without_zone_data"] == 1
    assert out["total_s"] == 1000
    assert out["zones"][3]["pct"] == 100.0


def test_zone_summary_without_any_data_has_no_zones() -> None:
    """一条区间数据都没有时，不给 zones 数组——全 0 的分布会被读成「没有强度」。"""
    out = zone_summary([zone_row([0] * 7)])
    assert out["total_s"] == 0
    assert "zones" not in out
    assert "dominant_zone" not in out


def test_zone_summary_splits_by_sport() -> None:
    out = zone_summary(
        [
            zone_row([0, 0, 0, 1000, 0, 0, 0], sport="running"),
            zone_row([0, 100, 0, 0, 0, 0, 0], sport="cycling"),
        ]
    )
    assert set(out["by_sport"]) == {"running", "cycling"}
    assert out["by_sport"]["running"]["pct"][3] == 100.0
    assert out["by_sport"]["cycling"]["activities"] == 1


def test_zone_summary_single_sport_has_no_breakdown() -> None:
    """只有一种运动时再给一份同样的分布只是重复。"""
    out = zone_summary([zone_row([0, 0, 0, 1000, 0, 0, 0], sport="running")])
    assert "by_sport" not in out


def test_zone_summary_tolerates_missing_columns() -> None:
    out = zone_summary([{"sport": "running"}])
    assert out["activities"] == 1
    assert out["with_zone_data"] == 0
    assert out["total_s"] == 0
