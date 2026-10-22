"""比赛预测与最大摄氧量解析测试。

样本值照抄真实 RunRacePredictions / MetricsMaxMetData 的形态。刻意复刻了
真实数据里最容易写错的两处：同一天多条预测且数值不同、`fitnessAge` 恒为 "20"。
"""

from __future__ import annotations

from datetime import datetime

from garmaple.parser.metrics import (
    RACE_DISTANCES_KM,
    RacePrediction,
    dedupe_predictions,
    dedupe_vo2max,
    parse_race_prediction,
    parse_race_predictions,
    parse_vo2max,
    parse_vo2max_samples,
)


def make_prediction(**overrides: object) -> dict[str, object]:
    """一条普通预测：5K 17m45s、10K 37m13s、半马 1h49m、全马 3h54m。"""
    block: dict[str, object] = {
        "userProfilePK": 999,
        "calendarDate": "2025-12-04",
        "deviceId": 9000,
        "timestamp": "2025-12-03T16:01:24.0",
        "raceTime5K": 1065,
        "raceTime10K": 2233,
        "raceTimeHalf": 6540,
        "raceTimeMarathon": 14040,
    }
    block.update(overrides)
    return block


def make_vo2max(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "userProfilePK": 999,
        "calendarDate": "2025-11-25",
        "deviceId": 9000,
        "updateTimestamp": "2025-11-25T07:23:37.0",
        "sport": "RUNNING",
        "subSport": "GENERIC",
        "vo2MaxValue": 62.0,
        # 真实数据里恒为字符串 "20"，是占位值
        "fitnessAge": "20",
        "maxMet": 16.7,
        "maxMetCategory": "GENERIC",
        "calibratedData": 1,
    }
    block.update(overrides)
    return block


def parse(block: dict[str, object]) -> RacePrediction:
    rec = parse_race_prediction(block)
    assert rec is not None
    return rec


# ---------------- 基础 ----------------

def test_parses_all_fields() -> None:
    r = parse(make_prediction())
    assert r.date == "2025-12-04"
    assert r.time_5k_s == 1065
    assert r.time_10k_s == 2233
    assert r.time_half_s == 6540
    assert r.time_marathon_s == 14040
    assert r.updated_at == datetime(2025, 12, 3, 16, 1, 24)
    assert r.user_profile_id == 999


def test_times_map_covers_all_four_distances() -> None:
    r = parse(make_prediction())
    assert r.times == {"5k": 1065, "10k": 2233, "half": 6540, "marathon": 14040}


def test_pace_uses_standard_marathon_distance() -> None:
    """全马按 42.195km 算，不能按 42km——否则配速虚快 0.46%。"""
    r = parse(make_prediction(raceTimeMarathon=14040))
    pace = r.pace_s_per_km("marathon")
    assert pace is not None
    assert abs(pace - 14040 / 42.195) < 1e-9
    assert RACE_DISTANCES_KM["marathon"] == 42.195


def test_pace_missing_distance_is_none() -> None:
    r = parse(make_prediction(raceTimeHalf=None))
    assert r.pace_s_per_km("half") is None


def test_unknown_distance_has_no_pace() -> None:
    r = parse(make_prediction())
    assert r.pace_s_per_km("100k") is None


# ---------------- 缺失与非法值 ----------------

def test_partial_prediction_is_kept() -> None:
    """个别距离缺失不影响其余三个——真实数据里也会有设备只算部分距离的情况。"""
    r = parse(make_prediction(raceTimeMarathon=None))
    assert r.time_marathon_s is None
    assert r.time_5k_s == 1065


def test_all_distances_missing_gives_nothing() -> None:
    assert parse_race_prediction(
        make_prediction(
            raceTime5K=None, raceTime10K=None,
            raceTimeHalf=None, raceTimeMarathon=None,
        )
    ) is None


def test_zero_and_negative_times_are_missing() -> None:
    """0 秒不可能是完赛成绩，与「没预测」是同一件事。"""
    r = parse(make_prediction(raceTime5K=0, raceTime10K=-5))
    assert r.time_5k_s is None
    assert r.time_10k_s is None
    assert r.time_half_s == 6540


def test_skips_block_with_bad_date() -> None:
    assert parse_race_prediction(make_prediction(calendarDate="2025-13-40")) is None
    assert parse_race_prediction(make_prediction(calendarDate=None)) is None


def test_skips_non_dict() -> None:
    assert parse_race_prediction("nope") is None  # type: ignore[arg-type]


# ---------------- 同日去重 ----------------

def test_dedupe_keeps_last_snapshot_of_the_day() -> None:
    """实测同一天会出现 1092 → 1086 这种变化，最后一条才是定稿。"""
    rows = [
        make_prediction(timestamp="2025-12-20T16:03:52.0", raceTime5K=1092),
        make_prediction(timestamp="2025-12-21T04:46:13.0", raceTime5K=1086),
    ]
    recs = dedupe_predictions(parse_race_predictions(rows))
    assert len(recs) == 1
    assert recs[0].time_5k_s == 1086


def test_dedupe_without_timestamp_does_not_override() -> None:
    rows = [
        make_prediction(timestamp="2025-12-21T04:46:13.0", raceTime5K=1086),
        make_prediction(timestamp=None, raceTime5K=1092),
    ]
    recs = dedupe_predictions(parse_race_predictions(rows))
    assert [r.time_5k_s for r in recs] == [1086]


def test_dedupe_sorts_by_date() -> None:
    rows = [
        make_prediction(calendarDate="2025-12-05"),
        make_prediction(calendarDate="2025-12-03"),
        make_prediction(calendarDate="2025-12-04"),
    ]
    recs = dedupe_predictions(parse_race_predictions(rows))
    assert [r.date for r in recs] == ["2025-12-03", "2025-12-04", "2025-12-05"]


# ---------------- 分片包装 ----------------

def test_parse_on_plain_list() -> None:
    assert len(parse_race_predictions([make_prediction()])) == 1


def test_parse_unwraps_dict() -> None:
    assert len(parse_race_predictions({"runRacePredictions": [make_prediction()]})) == 1


def test_parse_on_garbage() -> None:
    assert parse_race_predictions(None) == []
    assert parse_race_predictions("x") == []
    assert parse_race_predictions([1, 2, 3]) == []


# ---------------- 最大摄氧量 ----------------

def test_vo2max_parses_value_and_sport() -> None:
    rec = parse_vo2max(make_vo2max())
    assert rec is not None
    assert rec.date == "2025-11-25"
    assert rec.vo2max == 62.0
    assert rec.sport == "RUNNING"
    assert rec.updated_at == datetime(2025, 11, 25, 7, 23, 37)


def test_vo2max_has_no_fitness_age_field() -> None:
    """fitnessAge 恒为 "20"，是占位值。带上它模型会读出「体能年龄 20 岁」。"""
    rec = parse_vo2max(make_vo2max())
    assert rec is not None
    assert not hasattr(rec, "fitness_age")


def test_vo2max_without_sport_is_kept() -> None:
    """实测 3 条没有 sport，仍是一条有效测量。"""
    rec = parse_vo2max(make_vo2max(sport=None))
    assert rec is not None
    assert rec.sport is None
    assert rec.vo2max == 62.0


def test_vo2max_missing_or_zero_value_is_dropped() -> None:
    assert parse_vo2max(make_vo2max(vo2MaxValue=None)) is None
    assert parse_vo2max(make_vo2max(vo2MaxValue=0)) is None
    assert parse_vo2max(make_vo2max(vo2MaxValue=-1)) is None


def test_vo2max_bad_date_is_dropped() -> None:
    assert parse_vo2max(make_vo2max(calendarDate="not-a-date")) is None


def test_vo2max_dedupe_keeps_latest_update() -> None:
    rows = [
        make_vo2max(updateTimestamp="2025-11-25T07:00:00.0", vo2MaxValue=60.0),
        make_vo2max(updateTimestamp="2025-11-25T09:00:00.0", vo2MaxValue=62.0),
    ]
    recs = dedupe_vo2max(parse_vo2max_samples(rows))
    assert [r.vo2max for r in recs] == [62.0]


def test_vo2max_parse_on_garbage() -> None:
    assert parse_vo2max_samples(None) == []
    assert parse_vo2max_samples([{"calendarDate": "bad"}]) == []
