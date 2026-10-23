"""汇总计算测试。"""

from __future__ import annotations

import pytest

from garmaple.stats import (
    pace_hr_drift,
    human_duration,
    human_pace,
    pace_from_speed,
    pearson_r,
    recovery_correlations,
    spearman_rho,
    summarize,
    zone_summary,
)


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


def test_pace_from_speed_returns_none_when_standstill():
    """静止点不给配速。

    0.01 m/s 照 1000/speed 算会得到 100000"/km，画出来会把整条配速曲线压平。
    """
    assert pace_from_speed(0.0) is None
    assert pace_from_speed(0.29) is None
    assert pace_from_speed(None) is None
    assert pace_from_speed(3.0) == 333  # 5'33"/km


def test_pace_from_speed_keeps_walking_speed():
    assert pace_from_speed(1.4) == 714  # 11'54"/km，快走，仍是有效配速


# ---------------- 配速心率漂移 ----------------


class _Sample:
    """只为算解耦造的最小采样：只需要偏移、心率、速度。"""

    __slots__ = ("offset_s", "hr", "speed_mps")

    def __init__(self, offset_s: int, hr: int | None, speed_mps: float | None) -> None:
        self.offset_s = offset_s
        self.hr = hr
        self.speed_mps = speed_mps


def _series(n: int, *, hr_of, speed_of, step: int = 2) -> list[_Sample]:
    return [_Sample(i * step, hr_of(i * step), speed_of(i * step)) for i in range(n)]


def test_drift_is_positive_when_pace_holds_but_hr_climbs():
    """配速不变、心率上升 = 后半程效率下降，drift 为正。"""
    samples = _series(650, hr_of=lambda t: 140 + t // 60, speed_of=lambda t: 3.0)
    out = pace_hr_drift(samples)
    assert out["available"] is True
    assert out["drift_pct"] > 5
    assert out["reliable"] is True


def test_drift_is_negative_when_hr_holds_but_pace_improves():
    """心率不变、越跑越快 = 效率提升，drift 为负。"""
    samples = _series(600, hr_of=lambda t: 150, speed_of=lambda t: 2.5 + t / 2000)
    out = pace_hr_drift(samples)
    assert out["available"] is True
    assert out["drift_pct"] < -5


def test_drift_ignores_standstill_samples():
    """静止点不参与平均。

    否则前半程混进几个等红灯的 0 速度点，平均速度被拉低，会凭空造出一个
    「后半程效率提升」的结论。
    """
    moving = _series(600, hr_of=lambda t: 150, speed_of=lambda t: 3.0)
    with_stops = [
        *[s for s in moving[: 300]],
        *[_Sample(600 + i * 2, 150, 0.0) for i in range(50)],  # 中途停了一会儿
        *[s for s in moving[300:]],
    ]
    clean = pace_hr_drift(moving)
    dirty = pace_hr_drift(with_stops)
    assert clean["available"] and dirty["available"]
    assert dirty["drift_pct"] == pytest.approx(clean["drift_pct"], abs=1.0)


def test_drift_needs_enough_points():
    samples = _series(20, hr_of=lambda t: 150, speed_of=lambda t: 3.0)
    out = pace_hr_drift(samples)
    assert out["available"] is False
    assert out["reason"] == "too_few_points"
    assert out["needed"] == 60


def test_drift_unavailable_without_heart_rate():
    samples = _series(600, hr_of=lambda t: None, speed_of=lambda t: 3.0)
    out = pace_hr_drift(samples)
    assert out["available"] is False
    assert out["reason"] == "no_hr_or_speed"


def test_drift_flags_short_activities_as_unreliable():
    """点数够但时长不足 20 分钟：能算，但标 unreliable。"""
    samples = _series(300, hr_of=lambda t: 150, speed_of=lambda t: 3.0, step=2)  # 598s
    out = pace_hr_drift(samples)
    assert out["available"] is True
    assert out["reliable"] is False


# ---------------- CTL / ATL / TSB ----------------


def test_training_load_series_empty():
    from garmaple.stats import training_load_series

    assert training_load_series([]) == []


def test_training_load_series_fills_date_gaps():
    """日期缺口补 0（休息日），不是丢掉那些天。"""
    from garmaple.stats import training_load_series

    out = training_load_series([("2026-01-01", 100.0), ("2026-01-03", 50.0)])
    assert len(out) == 3
    assert out[0]["date"] == "2026-01-01"
    assert out[1]["date"] == "2026-01-02"
    assert out[1]["tss"] == 0.0
    assert out[2]["date"] == "2026-01-03"


def test_training_load_series_ctl_ema():
    """首日 TSS=42 → CTL = 42/42 = 1.0，ATL = 42/7 = 6.0。"""
    from garmaple.stats import training_load_series

    out = training_load_series([("2026-01-01", 42.0)])
    assert out[0]["ctl"] == pytest.approx(1.0)
    assert out[0]["atl"] == pytest.approx(6.0)
    assert out[0]["tsb"] == pytest.approx(-5.0)


def test_training_load_series_tsb_is_ctl_minus_atl():
    from garmaple.stats import training_load_series

    out = training_load_series([("2026-01-01", 50.0), ("2026-01-02", 0.0)])
    for day in out:
        # ctl/atl 各自 round 到 1 位小数，与 tsb（先减再 round）可能差 ±0.1
        assert day["tsb"] == pytest.approx(day["ctl"] - day["atl"], abs=0.15)


def test_training_load_series_rest_day_decays():
    """休息日 TSS=0，CTL/ATL 逐步衰减。"""
    from garmaple.stats import training_load_series

    out = training_load_series([("2026-01-01", 100.0), ("2026-01-02", 0.0)])
    assert out[1]["ctl"] < out[0]["ctl"]
    assert out[1]["atl"] < out[0]["atl"]


def test_training_load_series_over_42_days_converges():
    """连续恒定 TSS 后 CTL 逐步逼近 TSS，但 42 天只走到 1−e⁻¹ ≈ 63%。

    EMA 收敛公式：ctl(n) = TSS × (1 − (1−1/42)ⁿ)。
    42 天后 (41/42)^42 ≈ e⁻¹ ≈ 0.368，CTL ≈ TSS × 0.632。
    约 3 个时间常数（126 天）才进 5% 以内。
    """
    from garmaple.stats import training_load_series

    data = [(f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d-31:02d}", 50.0) for d in range(1, 43)]
    out = training_load_series(data)
    assert len(out) == 42
    assert out[-1]["ctl"] == pytest.approx(31.6, abs=1.0)


# ---------------- 恢复与表现的相关性 ----------------


def test_pearson_perfect_lines():
    assert pearson_r([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson_r([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_known_value():
    """手算锚点：xs=[1..5], ys=[2,4,5,4,5] → cov=6, √(vx·vy)=√60，r≈0.7746。"""
    r = pearson_r([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    assert r == pytest.approx(6 / 60**0.5)


def test_pearson_zero_variance_returns_none():
    """x 恒定时不给 0——「没法算」与「无关」是两件事。"""
    assert pearson_r([5, 5, 5, 5], [1, 2, 3, 4]) is None
    assert pearson_r([1, 2, 3, 4], [7, 7, 7, 7]) is None


def test_spearman_captures_monotonic_nonlinear():
    """y = x² 单调但非线性：Pearson 不到 1，Spearman 应为 1。"""
    xs = [float(i) for i in range(1, 11)]
    ys = [x * x for x in xs]
    assert pearson_r(xs, ys) < 0.99
    assert spearman_rho(xs, ys) == pytest.approx(1.0)


def test_spearman_handles_ties():
    """并列取平均秩：ys=[1,2,2,4] 的秩是 [1, 2.5, 2.5, 4]，ρ≈0.949。"""
    rho = spearman_rho([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 2.0, 4.0])
    assert rho == pytest.approx(0.9487, abs=0.001)


def test_recovery_correlations_too_few_pairs():
    """5 个样本不够任何一项：不给系数，只给计数与所需样本量。"""
    samples = [
        {
            "pace_s_per_km": 360.0 - i,
            "sleep_min": 420 + i,
            "prev_stress_avg": 30 + i,
        }
        for i in range(5)
    ]
    out = recovery_correlations(samples)
    assert out["available"] is False
    assert out["samples"] == 5
    by_key = {c["key"]: c for c in out["correlations"]}
    entry = by_key["sleep_min"]
    assert entry["available"] is False
    assert entry["reason"] == "too_few_pairs"
    assert entry["n"] == 5
    assert entry["needed"] == 10
    assert "pearson_r" not in entry


def test_recovery_correlations_counts_missing_pairs():
    """各 x 的 n 不同：x 缺记录的对不参与，n 是「同时有记录」的对数。"""
    samples = [
        {
            "pace_s_per_km": 360.0,
            "sleep_min": 420,
            "prev_bb_end": None if i % 2 else 50,
        }
        for i in range(12)
    ]
    out = recovery_correlations(samples)
    by_key = {c["key"]: c for c in out["correlations"]}
    assert by_key["sleep_min"]["n"] == 12
    assert by_key["prev_bb_end"]["n"] == 6
    assert by_key["prev_stress_avg"]["n"] == 0
    assert by_key["prev_stress_avg"]["reason"] == "too_few_pairs"


def test_recovery_correlations_strong_negative_is_significant():
    """睡得越多配速越快的理想数据：r=-1，过 α=0.05 的显著性门槛。"""
    samples = [
        {
            "pace_s_per_km": 360.0 - i,
            "sleep_min": 420 + i,
            "deep_min": 90,  # 恒定：应判 no_variance 而不是 r=0
        }
        for i in range(12)
    ]
    out = recovery_correlations(samples)
    assert out["available"] is True
    by_key = {c["key"]: c for c in out["correlations"]}

    entry = by_key["sleep_min"]
    assert entry["available"] is True
    assert entry["n"] == 12
    assert entry["pearson_r"] == pytest.approx(-1.0)
    assert entry["spearman_rho"] == pytest.approx(-1.0)
    assert entry["significant"] is True
    assert 0 < entry["crit_r_05"] < 1

    # 恒定值不给系数：no_variance，不会冒出「深睡与配速无关」的假结论
    deep = by_key["deep_min"]
    assert deep["available"] is False
    assert deep["reason"] == "no_variance"


def test_recovery_correlations_notes_always_present():
    """统计口径的提醒必须随结果走，不能靠调用方记得。"""
    out = recovery_correlations([])
    assert out["samples"] == 0
    assert out["available"] is False
    assert any("因果" in n for n in out["notes"])
    assert any("显著" in n for n in out["notes"])
    assert any("越小越快" in v for v in out["y"].values())


def test_recovery_correlations_significance_threshold_grows_with_n():
    """n 越小门槛越高：8 对时 |r| 要过 0.632，n=40 时只需 0.304。"""
    from garmaple.stats import _r_critical

    assert _r_critical(10) == pytest.approx(0.632)
    assert _r_critical(12) == pytest.approx(0.576)
    assert _r_critical(42) == pytest.approx(0.304)
    # n 不足时连门槛都不给——根本谈不上显著
    assert _r_critical(9) is None
