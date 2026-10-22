"""个人最佳与装备的解析测试。"""

from __future__ import annotations

from garmaple.parser.records import (
    COUNT,
    DAYS,
    GROUP_CYCLE,
    GROUP_GENERAL,
    GROUP_RUN,
    GROUP_SWIM,
    METERS,
    SECONDS,
    dedupe_gears,
    dedupe_records,
    kind_for,
    parse_gears,
    parse_record,
    parse_records,
)


def make_raw(
    *,
    recordId: int = 900001,
    recordType: str = "Best 5km Run",
    value: float = 1108.3,
    activityId: int = 545198159,
    created: str = "2025-12-04",
    current: bool = True,
) -> dict:
    """一条纪录的原始形态。ID 用假值，不从真实导出包里抄。"""
    return {
        "personalRecordId": recordId,
        "personalRecordType": recordType,
        "value": value,
        "activityId": activityId,
        "createdDate": created,
        "current": current,
        "confirmed": True,
        "prStartTimeGMT": "Thu Dec 04 06:05:36 GMT 2025",
        "userProfilePK": 999,
    }


def wrap(*records: dict) -> list[dict]:
    return [{"personalRecords": list(records)}]


# ---------- 单位：这是最容易出错的一处 ----------


def test_time_record_unit_is_seconds() -> None:
    rec = parse_record(make_raw(recordType="Best 5km Run", value=1108.3))
    assert rec is not None
    assert rec.unit == SECONDS
    assert rec.value == 1108.3


def test_distance_record_unit_is_meters() -> None:
    """同样是「跑步最好成绩」，Farthest 是米而不是秒。"""
    rec = parse_record(make_raw(recordType="Farthest Run", value=21116.36))
    assert rec is not None
    assert rec.unit == METERS


def test_steps_record_unit_is_count() -> None:
    rec = parse_record(make_raw(recordType="Most Steps in a Day", value=36434.0))
    assert rec is not None
    assert rec.unit == COUNT


def test_streak_record_unit_is_days() -> None:
    """`Longest` 前缀不统一：Longest Goal Streak 是天，Longest Pool Swim 是米。"""
    streak = parse_record(make_raw(recordType="Longest Goal Streak", value=9))
    swim = parse_record(make_raw(recordType="Longest Pool Swim", value=5100))
    assert streak is not None and streak.unit == DAYS
    assert swim is not None and swim.unit == METERS


def test_every_known_type_has_a_label_and_group() -> None:
    for name in (
        "Best 5km Run",
        "Best Half Marathon",
        "Farthest Run",
        "Best 100m Pool Swim",
        "Longest Pool Swim",
        "Best 40km Cycle",
        "Farthest Cycle",
        "Max Elevation Gain",
        "Most Steps in a Month",
        "Current Goal Streak",
    ):
        kind = kind_for(name)
        assert kind.label, name
        assert kind.unit, name


def test_unknown_type_does_not_guess_unit_but_keeps_name() -> None:
    """表里没有的类型不编单位，也不编译名——原样给类型名。"""
    rec = parse_record(make_raw(recordType="Best 20km Trail Run", value=5000))
    assert rec is not None
    assert rec.unit == SECONDS  # 前缀可推
    assert rec.label == "Best 20km Trail Run"  # 但不编中文

    odd = parse_record(make_raw(recordType="Mystery Badge", value=3))
    assert odd is not None
    assert odd.unit is None
    assert odd.label == "Mystery Badge"
    assert odd.group == GROUP_GENERAL


def test_group_inferred_from_name() -> None:
    for name, group in (
        ("Best 10km Run", GROUP_RUN),
        ("Best Half Marathon", GROUP_RUN),
        ("Longest Pool Swim", GROUP_SWIM),
        ("Farthest Cycle", GROUP_CYCLE),
        ("Max Elevation Gain", GROUP_GENERAL),
    ):
        assert kind_for(name).group == group, name


# ---------- 日期口径 ----------


def test_record_with_activity_gets_date() -> None:
    rec = parse_record(make_raw(created="2025-12-04", activityId=545198159))
    assert rec is not None
    assert rec.date == "2025-12-04"


def test_summary_record_has_no_date() -> None:
    """步数 / 连续达标类没有对应活动，createdDate 只是建档日，不能当达成日。"""
    steps = parse_record(make_raw(recordType="Most Steps in a Day", activityId=0))
    streak = parse_record(make_raw(recordType="Longest Goal Streak", activityId=0))
    assert steps is not None and steps.date is None
    assert streak is not None and streak.date is None


def test_invalid_created_date_becomes_none() -> None:
    rec = parse_record(make_raw(created="not-a-date"))
    assert rec is not None
    assert rec.date is None


# ---------- 无效输入 ----------


def test_zero_value_is_dropped() -> None:
    """0 秒的 5K 不是纪录，是缺失；混进来会变成「5K 用了 0 秒」。"""
    assert parse_record(make_raw(value=0)) is None
    assert parse_record(make_raw(value=-3)) is None


def test_missing_id_or_type_is_dropped() -> None:
    assert parse_record(make_raw(recordId=0)) is None
    assert parse_record(make_raw(recordType="   ")) is None
    assert parse_record("not a dict") is None


def test_nan_value_is_dropped() -> None:
    assert parse_record(make_raw(value=float("nan"))) is None
    assert parse_record(make_raw(value=float("inf"))) is None


# ---------- 整包 ----------


def test_parse_reads_nested_block() -> None:
    payload = wrap(
        make_raw(recordId=900001, recordType="Best 5km Run"),
        make_raw(recordId=900002, recordType="Farthest Run", value=21116.4),
    )
    records = parse_records(payload)
    assert len(records) == 2
    assert {r.type for r in records} == {"Best 5km Run", "Farthest Run"}


def test_parse_accepts_bare_dict() -> None:
    """顶层被包成单块 dict 时也要能读。"""
    records = parse_records({"personalRecords": [make_raw()]})
    assert len(records) == 1


def test_parse_ignores_entries_without_records_key() -> None:
    assert parse_records([{"somethingElse": []}]) == []
    assert parse_records({"personalRecords": "oops"}) == []


def test_current_flag_is_preserved() -> None:
    records = parse_records(
        wrap(
            make_raw(recordId=1, current=True, value=1108.3),
            make_raw(recordId=2, current=False, value=1194.2),
        )
    )
    assert {r.record_id: r.current for r in records} == {1: True, 2: False}


def test_dedupe_keeps_one_per_record_id() -> None:
    records = parse_records(wrap(make_raw(recordId=7), make_raw(recordId=7)))
    assert len(dedupe_records(records)) == 1


def test_dedupe_sorts_by_group_then_type() -> None:
    records = parse_records(
        wrap(
            make_raw(recordId=1, recordType="Most Steps in a Day", value=36434),
            make_raw(recordId=2, recordType="Farthest Cycle", value=61134),
            make_raw(recordId=3, recordType="Best 5km Run", value=1108),
        )
    )
    ordered = [r.group for r in dedupe_records(records)]
    assert ordered == [GROUP_RUN, GROUP_CYCLE, GROUP_GENERAL]


# ---------- 装备 ----------


def make_gear(
    gearPk: int = 700001,
    name: str = "测试跑鞋",
    maxMeters: float = 636000.0,
    status: str = "active",
) -> dict:
    return {
        "gearPk": gearPk,
        "uuid": "0123456789abcdef0123456789abcdef",
        "userProfilePk": 999,
        "gearTypeName": "Shoes",
        "gearStatusName": status,
        "customMakeModel": name,
        "dateBegin": "2025-04-18",
        "maximumMeters": maxMeters,
    }


def make_gear_payload(gears: list[dict], links: dict | None = None) -> list[dict]:
    return [{"gearDTOS": gears, "gearActivityDTOs": links or {}}]


def test_parse_gear_basic_fields() -> None:
    gears = parse_gears(make_gear_payload([make_gear()]))
    assert len(gears) == 1
    gear = gears[0]
    assert gear.gear_id == 700001
    assert gear.name == "测试跑鞋"
    assert gear.gear_type == "Shoes"
    assert gear.max_meters == 636000.0
    assert gear.started_on == "2025-04-18"
    assert gear.user_profile_id == 999


def test_gear_links_are_read_per_gear() -> None:
    links = {"700001": [{"gearPk": 700001, "activityId": 11},
                        {"gearPk": 700001, "activityId": 12}]}
    gears = parse_gears(make_gear_payload([make_gear()], links))
    assert gears[0].activity_ids == [11, 12]


def test_gear_without_link_key_has_no_activities() -> None:
    """实测有一双鞋整个关联键不存在——不能因此编出「0 公里」。"""
    gears = parse_gears(make_gear_payload([make_gear(gearPk=700002)], {}))
    assert gears[0].activity_ids == []


def test_gear_links_drop_invalid_activity_ids() -> None:
    links = {"700001": [{"activityId": 0}, {"activityId": -5}, {"activityId": 12}, {}]}
    gears = parse_gears(make_gear_payload([make_gear()], links))
    assert gears[0].activity_ids == [12]


def test_gear_without_id_is_dropped() -> None:
    bad = make_gear()
    bad.pop("gearPk")
    assert parse_gears(make_gear_payload([bad])) == []


def test_gear_without_model_name_keeps_none() -> None:
    raw = make_gear()
    raw["customMakeModel"] = ""
    gear = parse_gears(make_gear_payload([raw]))[0]
    assert gear.name is None


def test_dedupe_gears_merges_activity_ids() -> None:
    """同一双鞋出现在两个分片里，关联要取并集，不能只留一份。"""
    first = parse_gears(make_gear_payload([make_gear()], {"700001": [{"activityId": 1}]}))
    second = parse_gears(make_gear_payload([make_gear()], {"700001": [{"activityId": 2}]}))
    merged = dedupe_gears(first + second)
    assert len(merged) == 1
    assert sorted(merged[0].activity_ids) == [1, 2]


def test_gear_payload_ignores_missing_blocks() -> None:
    assert parse_gears([]) == []
    assert parse_gears({"gearDTOS": "oops"}) == []
