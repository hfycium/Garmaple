"""训练状态解析测试。

字段名与取值照抄真实 TrainingHistory 的形态。三个样本刻意复刻了真实数据里
最容易写错的地方：同一天多条快照、周负荷为 0、隧道边界缺失。
"""

from __future__ import annotations

from datetime import datetime

from garmaple.parser.training import (
    STATUS_LABELS,
    TREND_LABELS,
    dedupe,
    parse_training,
    parse_trainings,
)


def make_block(**overrides: object) -> dict[str, object]:
    """一条普通快照：状态有成效，周负荷 453，落在隧道 281~640 内。"""
    block: dict[str, object] = {
        "userProfilePK": 999,
        "calendarDate": "2025-04-23",
        "deviceId": 9000,
        "timestamp": "2025-04-22T16:01:57.0",
        "sport": "RUNNING",
        "subSport": "GENERIC",
        "weeklyTrainingLoadSum": 453,
        "loadTunnelMin": 281,
        "loadTunnelMax": 640,
        "trainingStatus": "PRODUCTIVE",
        "fitnessLevelTrend": "NO_CHANGE",
        "loadLevelTrend": "INCREASING",
    }
    block.update(overrides)
    return block


def parse(block: dict[str, object]):
    record = parse_training(block)
    assert record is not None
    return record


# ---------------- 基础 ----------------

def test_parses_all_fields() -> None:
    r = parse(make_block())
    assert r.date == "2025-04-23"
    assert r.status == "PRODUCTIVE"
    assert r.weekly_load == 453
    assert r.tunnel_min == 281
    assert r.tunnel_max == 640
    assert r.fitness_trend == "NO_CHANGE"
    assert r.load_trend == "INCREASING"
    assert r.sport == "RUNNING"
    assert r.updated_at == datetime(2025, 4, 22, 16, 1, 57)
    assert r.user_profile_id == 999


def test_zero_load_is_a_value_not_a_gap() -> None:
    """周负荷 0 是「这段时间没有负荷」，不是「没测到」。

    写成 `value or None` 就会把它吞成缺失，真实数据里有 108 天是 0，
    那 108 天会整列消失。
    """
    r = parse(make_block(weeklyTrainingLoadSum=0))
    assert r.weekly_load == 0
    assert r.load_position == "below"


def test_missing_load_stays_none() -> None:
    r = parse(make_block(weeklyTrainingLoadSum=None))
    assert r.weekly_load is None
    assert r.load_position is None


def test_negative_load_treated_as_missing() -> None:
    """负数不可能是负荷，按缺失处理，避免被算进平均值。"""
    r = parse(make_block(weeklyTrainingLoadSum=-1))
    assert r.weekly_load is None


# ---------------- 负荷隧道位置 ----------------

def test_position_in_tunnel_boundaries_are_inclusive() -> None:
    """正好压在边界上算「在隧道内」，不算超出。"""
    assert parse(make_block(weeklyTrainingLoadSum=281)).load_position == "in"
    assert parse(make_block(weeklyTrainingLoadSum=640)).load_position == "in"


def test_position_below_and_above() -> None:
    assert parse(make_block(weeklyTrainingLoadSum=200)).load_position == "below"
    assert parse(make_block(weeklyTrainingLoadSum=700)).load_position == "above"


def test_position_requires_both_bounds() -> None:
    """隧道边界缺失就不给位置——没有区间谈不上在不在里面。"""
    assert parse(make_block(loadTunnelMin=None)).load_position is None
    assert parse(make_block(loadTunnelMax=None)).load_position is None


# ---------------- 标签 ----------------

def test_status_labels_cover_every_real_value() -> None:
    """真实归档出现过 8 种状态，每种都要有中文标签。

    少一个就会出现 status_label 为空，模型只能拿英文枚举自己猜。
    """
    real = {
        "MAINTAINING", "PRODUCTIVE", "NO_STATUS", "DETRAINING",
        "RECOVERY", "UNPRODUCTIVE", "PEAKING", "OVERREACHING",
    }
    assert real == set(STATUS_LABELS)


def test_productive_and_unproductive_are_distinguishable() -> None:
    """这两个词只差一个前缀，标签必须明确区分，否则容易被读反。"""
    assert STATUS_LABELS["PRODUCTIVE"] != STATUS_LABELS["UNPRODUCTIVE"]


def test_trend_labels() -> None:
    assert set(TREND_LABELS) == {"INCREASING", "DECREASING", "NO_CHANGE", "NO_RESULT"}
    r = parse(make_block(fitnessLevelTrend="DECREASING", loadLevelTrend="INCREASING"))
    assert r.fitness_trend_label == "下降"
    assert r.load_trend_label == "上升"


def test_unknown_status_has_no_label() -> None:
    """遇到没见过的枚举时不编造标签，让原始值自己说话。"""
    r = parse(make_block(trainingStatus="SOMETHING_NEW"))
    assert r.status == "SOMETHING_NEW"
    assert r.status_label is None


# ---------------- 脏数据 ----------------

def test_skips_block_without_status() -> None:
    assert parse_training(make_block(trainingStatus=None)) is None
    assert parse_training(make_block(trainingStatus="")) is None


def test_skips_block_with_bad_date() -> None:
    assert parse_training(make_block(calendarDate="2025-13-01")) is None
    assert parse_training(make_block(calendarDate=None)) is None
    assert parse_training("not a dict") is None


def test_ignores_unknown_fields() -> None:
    """固件多写字段不该让解析失败。"""
    r = parse(make_block(someFutureField={"nested": True}))
    assert r.date == "2025-04-23"


# ---------------- 分片包装 ----------------

def test_parse_trainings_on_plain_list() -> None:
    out = parse_trainings([make_block(), make_block(calendarDate="2025-04-24")])
    assert len(out) == 2


def test_parse_trainings_unwraps_dict() -> None:
    payload = {"trainingHistoryList": [make_block()], "totalCount": 1}
    assert len(parse_trainings(payload)) == 1


def test_parse_trainings_on_garbage() -> None:
    assert parse_trainings(None) == []
    assert parse_trainings("text") == []
    assert parse_trainings([]) == []


# ---------------- 同日去重 ----------------

def test_dedupe_keeps_last_snapshot_of_the_day() -> None:
    """同一天多条是设备多次同步，数值随时间往前走，只留最后一条。"""
    early = parse(make_block(
        timestamp="2025-04-18T10:47:50.0", weeklyTrainingLoadSum=341,
        trainingStatus="MAINTAINING",
    ))
    late = parse(make_block(
        timestamp="2025-04-18T12:24:07.0", weeklyTrainingLoadSum=564,
        trainingStatus="PRODUCTIVE",
    ))
    out = dedupe([late, early])
    assert len(out) == 1
    assert out[0].weekly_load == 564
    assert out[0].status == "PRODUCTIVE"


def test_dedupe_without_timestamp_does_not_override() -> None:
    """没有时间戳的记录新鲜度不知道，不该盖掉有时间戳的那条。"""
    timed = parse(make_block(timestamp="2025-04-18T12:24:07.0", weeklyTrainingLoadSum=564))
    untimed = parse(make_block(timestamp=None, weeklyTrainingLoadSum=999))
    out = dedupe([timed, untimed])
    assert out[0].weekly_load == 564


def test_dedupe_without_any_timestamp_keeps_first() -> None:
    a = parse(make_block(timestamp=None, weeklyTrainingLoadSum=100))
    b = parse(make_block(timestamp=None, weeklyTrainingLoadSum=200))
    assert dedupe([a, b])[0].weekly_load == 100


def test_dedupe_sorts_by_date() -> None:
    out = dedupe([
        parse(make_block(calendarDate="2025-04-25")),
        parse(make_block(calendarDate="2025-04-23")),
        parse(make_block(calendarDate="2025-04-24")),
    ])
    assert [r.date for r in out] == ["2025-04-23", "2025-04-24", "2025-04-25"]


def test_dedupe_across_shards() -> None:
    """分片区间端点重叠（实测相邻分片共用一天），跨分片也要去重。"""
    shard_a = parse_trainings([make_block(timestamp="2025-06-04T10:00:00.0", weeklyTrainingLoadSum=300)])
    shard_b = parse_trainings([make_block(timestamp="2025-06-04T16:00:00.0", weeklyTrainingLoadSum=420)])
    out = dedupe(shard_a + shard_b)
    assert len(out) == 1
    assert out[0].weekly_load == 420
