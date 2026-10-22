"""睡眠解析测试。

字段名与嵌套照抄真实 sleepData 的形态，数值是手写的小数字。
几个样本刻意复刻了真实数据的三种「没有分期」情形——那是这个模块最容易写错的地方。
"""

from __future__ import annotations

from datetime import datetime

from garmaple.parser.sleep import (
    STATUS_NO_STAGES,
    STATUS_OFF_WRIST,
    STATUS_OK,
    STATUS_UNCONFIRMED,
    dedupe,
    parse_sleep,
    parse_sleeps,
)


def make_sleep(**overrides: object) -> dict[str, object]:
    """一晚正常睡眠：23:20 入睡，07:21 醒来（GMT 15:20 → 02:21，本地 +8）。

    各分期加总 661 分钟 == 睡眠窗（与真实数据一致）。
    """
    block: dict[str, object] = {
        "sleepStartTimestampGMT": "2026-07-23T15:20:00.0",
        "sleepEndTimestampGMT": "2026-07-24T02:21:00.0",
        "calendarDate": "2026-07-24",
        "sleepWindowConfirmationType": "ENHANCED_CONFIRMED",
        "deepSleepSeconds": 5400,      # 90
        "lightSleepSeconds": 21540,    # 359
        "remSleepSeconds": 9600,       # 160
        "awakeSleepSeconds": 3120,     # 52
        "unmeasurableSeconds": 0,
        "averageRespiration": 14.2,
        "lowestRespiration": 11.5,
        "highestRespiration": 17.8,
        "retro": False,
    }
    block.update(overrides)
    return block


def parse(block: dict[str, object], offset: float | None = 8.0):
    record = parse_sleep(block, offset_hours=offset)
    assert record is not None
    return record


# ---------------- 基础 ----------------

def test_parses_duration_and_stages() -> None:
    r = parse(make_sleep())
    assert r.date == "2026-07-24"
    assert r.in_bed_min == 661
    assert r.deep_min == 90
    assert r.light_min == 359
    assert r.rem_min == 160
    assert r.awake_min == 52
    assert r.status == STATUS_OK


def test_asleep_excludes_awake() -> None:
    """睡着的是窗口减去清醒，不是把整晚都算成睡眠。"""
    r = parse(make_sleep())
    assert r.asleep_min == 661 - 52


def test_asleep_equals_deep_light_rem_within_a_minute() -> None:
    """两种口径在秒级等价，分钟级可能差 1（各字段独立取整）。"""
    r = parse(make_sleep())
    assert abs(r.asleep_min - (r.deep_min + r.light_min + r.rem_min)) <= 1


def test_missing_date_is_rejected() -> None:
    assert parse_sleep({"calendarDate": None}, 8.0) is None
    assert parse_sleep({"calendarDate": "2026-13-40"}, 8.0) is None
    assert parse_sleep("not a dict", 8.0) is None


# ---------------- 时区 ----------------

def test_times_are_converted_to_local() -> None:
    r = parse(make_sleep())
    assert r.start_at == datetime(2026, 7, 23, 23, 20)
    assert r.end_at == datetime(2026, 7, 24, 10, 21)


def test_without_offset_times_are_withheld_but_duration_kept() -> None:
    """拿不到时区就別猜：时刻留空，但时长是差值，与偏移无关。"""
    r = parse(make_sleep(), offset=None)
    assert r.start_at is None
    assert r.end_at is None
    assert r.in_bed_min == 661
    assert r.asleep_min is not None


def test_offset_is_not_hardcoded() -> None:
    """换个时区，时刻跟着变，日期归属仍以 calendarDate 为准。"""
    r = parse(make_sleep(), offset=0.0)
    assert r.start_at == datetime(2026, 7, 23, 15, 20)
    assert r.end_at == datetime(2026, 7, 24, 2, 21)


# ---------------- 无有效分期的三种情形 ----------------

def test_off_wrist_has_no_duration() -> None:
    """摘表：分期字段在但全 0，窗口是「摘下到戴上」，不能当睡眠时长。"""
    r = parse(
        make_sleep(
            sleepWindowConfirmationType="OFF_WRIST",
            deepSleepSeconds=0,
            lightSleepSeconds=0,
            remSleepSeconds=0,
            awakeSleepSeconds=0,
            unmeasurableSeconds=0,
        )
    )
    assert r.status == STATUS_OFF_WRIST
    assert r.asleep_min is None
    assert r.in_bed_min is None
    assert r.has_stages is False


def test_unconfirmed_has_no_stage_fields_at_all() -> None:
    """未确认：连分期字段都没有，窗口是 8/9 小时的固定占位值。"""
    block = make_sleep(sleepWindowConfirmationType="UNCONFIRMED")
    for key in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds",
                "awakeSleepSeconds", "unmeasurableSeconds"):
        block.pop(key)
    r = parse(block)
    assert r.status == STATUS_UNCONFIRMED
    assert r.asleep_min is None
    assert r.in_bed_min is None


def test_confirmed_window_with_all_zero_stages_is_not_ok() -> None:
    """确认了睡眠窗但分期全 0（真实数据里的 AUTO_CONFIRMED_FINAL）。"""
    r = parse(
        make_sleep(
            sleepWindowConfirmationType="AUTO_CONFIRMED_FINAL",
            deepSleepSeconds=0,
            lightSleepSeconds=0,
            remSleepSeconds=0,
            awakeSleepSeconds=0,
            unmeasurableSeconds=0,
        )
    )
    assert r.status == STATUS_NO_STAGES
    assert r.asleep_min is None


def test_missing_stages_are_never_reported_as_zero() -> None:
    """这是本模块最关键的约束：没数据 ≠ 睡了 0 分钟。"""
    for kind in ("OFF_WRIST", "UNCONFIRMED", "AUTO_CONFIRMED_FINAL"):
        block = make_sleep(sleepWindowConfirmationType=kind, deepSleepSeconds=0,
                           lightSleepSeconds=0, remSleepSeconds=0,
                           awakeSleepSeconds=0, unmeasurableSeconds=0)
        if kind == "UNCONFIRMED":
            for key in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds",
                        "awakeSleepSeconds", "unmeasurableSeconds"):
                block.pop(key)
        r = parse(block)
        assert r.asleep_min is None, f"{kind} 被报成了 {r.asleep_min} 分钟"


# ---------------- REM 缺失 ----------------

def test_asleep_does_not_under_count_when_rem_is_absent() -> None:
    """16 晚设备没记 REM（四项加总仍等于窗口），不能少算一整段。"""
    block = make_sleep()
    block.pop("remSleepSeconds")
    block["lightSleepSeconds"] = 9600 + 21540  # REM 那段时间并入浅睡
    r = parse(block)
    assert r.status == STATUS_OK
    assert r.rem_min is None          # 如实标记缺失
    assert r.asleep_min == 661 - 52   # 但睡着时长不受影响


# ---------------- 分片与去重 ----------------

def test_parse_sleeps_injects_offset_per_date() -> None:
    a = make_sleep()
    b = make_sleep(calendarDate="2026-07-25")
    records = parse_sleeps([a, b], offsets={"2026-07-24": 8.0, "2026-07-25": 9.0})
    assert len(records) == 2
    assert records[0].start_at == datetime(2026, 7, 23, 23, 20)
    assert records[1].start_at == datetime(2026, 7, 24, 0, 20)


def test_parse_sleeps_without_offsets_keeps_durations() -> None:
    records = parse_sleeps([make_sleep()])
    assert records[0].start_at is None
    assert records[0].asleep_min == 661 - 52


def test_parse_sleeps_accepts_wrapped_payload() -> None:
    """顶层被包一层时按结构找数组，不猜键名。"""
    assert len(parse_sleeps({"sleepList": [make_sleep()]}, {"2026-07-24": 8.0})) == 1
    assert parse_sleeps({"nope": []}) == []


def test_dedupe_keeps_latest_and_sorts() -> None:
    older = parse(make_sleep(deepSleepSeconds=0, lightSleepSeconds=0,
                             remSleepSeconds=0, awakeSleepSeconds=0,
                             unmeasurableSeconds=0,
                             sleepWindowConfirmationType="OFF_WRIST"))
    newer = parse(make_sleep())
    # 多次导出时后写入的覆盖先写入的，与 daily.dedupe 同一约定
    out = dedupe([older, newer])
    assert [r.date for r in out] == ["2026-07-24"]
    assert out[0].status == STATUS_OK


def test_respiration_is_parsed() -> None:
    r = parse(make_sleep())
    assert (r.respiration_avg, r.respiration_low, r.respiration_high) == (14.2, 11.5, 17.8)


def test_negative_seconds_are_treated_as_missing() -> None:
    """与 daily 一致：负数是未测量哨兵。"""
    r = parse(make_sleep(deepSleepSeconds=-1))
    assert r.deep_min is None
