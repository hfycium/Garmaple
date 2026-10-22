"""索引层测试：幂等性、筛选、日期区间。"""

from __future__ import annotations

from pathlib import Path

import pytest

from garmaple.parser.activities import parse_activity
from garmaple.parser.daily import parse_daily
from garmaple.store import Store, row_to_daily

from .test_activities import make_raw
from .test_daily import make_block


def act(activity_id: int, **overrides: object):
    a = parse_activity(make_raw(activityId=activity_id, **overrides))
    assert a is not None
    return a


def day(calendar_date: str, **overrides: object):
    d = parse_daily(make_block(calendarDate=calendar_date, **overrides))
    assert d is not None
    return d


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "cache" / "test.sqlite3")
    s.upsert_activities([act(1), act(2, activityType="cycling")], source="pkg.zip")
    yield s
    s.close()


def test_db_file_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "x.sqlite3"
    s = Store(path)
    s.close()
    assert path.exists()


def test_count_after_insert(store: Store) -> None:
    assert store.count_activities() == 2


def test_upsert_is_idempotent(store: Store) -> None:
    store.upsert_activities([act(1), act(2, activityType="cycling")], source="pkg.zip")
    assert store.count_activities() == 2


def test_upsert_updates_existing_row(store: Store) -> None:
    store.upsert_activities([act(1, name="改过的名字")], source="pkg.zip")
    rows = store.query_activities()
    names = {r["activity_id"]: r["name"] for r in rows}
    assert names[1] == "改过的名字"
    assert store.count_activities() == 2


def test_filter_by_sport(store: Store) -> None:
    rows = store.query_activities(sport="cycling")
    assert len(rows) == 1
    assert rows[0]["sport"] == "cycling"


def test_sport_filter_is_case_insensitive(store: Store) -> None:
    assert len(store.query_activities(sport="CYCLING")) == 1


def test_filter_by_date_range(store: Store) -> None:
    # make_raw 的时间固定为 2025-02-19 前后（UTC），本地时间 +8h
    all_rows = store.query_activities()
    day = all_rows[0]["date"]
    assert len(store.query_activities(start=day, end=day)) == 2
    assert store.query_activities(start="2099-01-01") == []


def test_order_asc_and_desc(store: Store) -> None:
    desc = [r["activity_id"] for r in store.query_activities(order="desc")]
    asc = [r["activity_id"] for r in store.query_activities(order="asc")]
    assert desc == list(reversed(asc))


def test_limit(store: Store) -> None:
    assert len(store.query_activities(limit=1)) == 1


def test_distinct_sports(store: Store) -> None:
    assert set(store.distinct_sports()) == {"running", "cycling"}


def test_date_range(store: Store) -> None:
    lo, hi = store.date_range()
    assert lo is not None and hi is not None
    assert lo <= hi


def test_meta_roundtrip(store: Store) -> None:
    store.set_meta("indexed_at", "2026-10-22")
    assert store.get_meta("indexed_at") == "2026-10-22"
    store.set_meta("indexed_at", "2026-10-23")
    assert store.get_meta("indexed_at") == "2026-10-23"
    assert store.get_meta("nope") is None


def test_sensitive_fields_are_stored_locally(store: Store) -> None:
    """坐标入本地库是允许的（cache/ 已 gitignore），外露与否由 privacy 决定。"""
    row = store.query_activities()[0]
    assert row["start_lat"] == pytest.approx(30.0)
    assert row["location_name"] == "某市"


def test_empty_insert_is_noop(tmp_path: Path) -> None:
    s = Store(tmp_path / "e.sqlite3")
    assert s.upsert_activities([], source="x") == 0
    assert s.count_activities() == 0
    s.close()


def test_hr_zones_persisted(store: Store) -> None:
    row = store.query_activities()[0]
    assert row["hr_zone_3"] == 1800
    assert row["hr_zone_4"] == 900
    assert row["hr_zone_0"] == 0


def test_pseudo_id_is_persisted_and_stable(store: Store) -> None:
    from garmaple.privacy import pseudo_activity_id

    row = store.query_activities(order="asc")[0]
    assert row["pseudo_id"] == pseudo_activity_id(row["activity_id"])
    assert row["pseudo_id"].startswith("act_")


def test_find_activity_by_pseudo_id(store: Store) -> None:
    row = store.query_activities(order="asc")[0]
    found = store.find_activity(row["pseudo_id"])
    assert found is not None
    assert found["activity_id"] == row["activity_id"]


def test_find_activity_by_raw_id(store: Store) -> None:
    found = store.find_activity("1")
    assert found is not None
    assert found["activity_id"] == 1


def test_find_activity_unknown_returns_none(store: Store) -> None:
    assert store.find_activity("act_00000000") is None
    assert store.find_activity("not-an-id") is None


def test_schema_version_recorded(store: Store) -> None:
    from garmaple.store import SCHEMA_VERSION

    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_reopening_old_schema_rebuilds(tmp_path: Path) -> None:
    """结构完整时，仅有版本号不一致不应丢弃已索引的数据。"""
    from garmaple.store import SCHEMA_VERSION

    path = tmp_path / "old.sqlite3"
    s = Store(path)
    s.upsert_activities([act(1)], source="x")
    s.set_meta("schema_version", "1")
    s.close()

    s2 = Store(path)  # 触发版本检查
    assert s2.count_activities() == 1  # 结构没问题，数据保留
    assert s2.get_meta("schema_version") == str(SCHEMA_VERSION)  # 版本号被推进
    s2.close()


def test_broken_table_self_heals_even_if_version_looks_new(tmp_path: Path) -> None:
    """回归：版本号写着 2 但表缺 pseudo_id 时，必须能自愈。

    真实踩过的坑——迁移只查 meta 版本号，元数据被提前写入后表永远修不好，
    启动服务时报 `no such column: pseudo_id`。
    """
    import sqlite3

    path = tmp_path / "broken.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE activities (activity_id INTEGER PRIMARY KEY, sport TEXT, date TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '2');
        """
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(activities)")}
        assert "pseudo_id" in cols
        assert s.count_activities() == 0
        assert s.upsert_activities([act(7)], source="x") == 1
        assert s.count_activities() == 1


# ---------------- days 表 ----------------

def test_days_upsert_and_count(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        assert s.upsert_days([day("2026-07-24"), day("2026-07-25")], source="x") == 2
        assert s.count_days() == 2


def test_days_upsert_is_idempotent(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days([day("2026-07-24")], source="x")
        s.upsert_days([day("2026-07-24", totalSteps=1)], source="x")
        assert s.count_days() == 1
        row = s.query_days()[0]
        assert row["steps"] == 1


def test_days_query_is_ascending_by_default(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days([day("2026-07-25"), day("2026-07-23"), day("2026-07-24")], source="x")
        assert [r["date"] for r in s.query_days()] == [
            "2026-07-23", "2026-07-24", "2026-07-25",
        ]


def test_days_date_range_is_inclusive(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days([day("2026-07-23"), day("2026-07-24"), day("2026-07-25")], source="x")
        rows = s.query_days(start="2026-07-24", end="2026-07-25")
        assert [r["date"] for r in rows] == ["2026-07-24", "2026-07-25"]
        assert s.query_days(start="2099-01-01") == []


def test_days_limit_desc_returns_most_recent_in_order(tmp_path: Path) -> None:
    """「最近 2 天」应是最新那两天，且仍按时间正序返回。"""
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days(
            [day("2026-07-22"), day("2026-07-23"), day("2026-07-24"), day("2026-07-25")],
            source="x",
        )
        rows = s.query_days(limit=2, order="desc")
        assert [r["date"] for r in rows] == ["2026-07-24", "2026-07-25"]


def test_days_limit_asc(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days([day("2026-07-23"), day("2026-07-24")], source="x")
        assert [r["date"] for r in s.query_days(limit=1)] == ["2026-07-23"]


def test_day_range(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days([day("2026-07-23"), day("2026-07-25")], source="x")
        assert s.day_range() == ("2026-07-23", "2026-07-25")


def test_day_range_on_empty_table(tmp_path: Path) -> None:
    with Store(tmp_path / "d.sqlite3") as s:
        assert s.day_range() == (None, None)


def test_days_roundtrip_keeps_values_and_local_times(tmp_path: Path) -> None:
    from datetime import datetime

    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_days([day("2026-07-24")], source="x")
        restored = row_to_daily(s.query_days()[0])
    assert restored.steps == 9523
    assert restored.stress_avg == 33
    assert restored.intensity_minutes == 30 + 2 * 18
    assert restored.bb_high_at == datetime(2026, 7, 24, 10, 57)
    assert restored.bb_low_at == datetime(2026, 7, 24, 0, 1)


def test_days_and_activities_coexist(tmp_path: Path) -> None:
    """两张表互不影响：写日汇总不该动活动索引。"""
    with Store(tmp_path / "d.sqlite3") as s:
        s.upsert_activities([act(1)], source="x")
        s.upsert_days([day("2026-07-24")], source="x")
        assert s.count_activities() == 1
        assert s.count_days() == 1


def test_broken_days_table_self_heals(tmp_path: Path) -> None:
    """旧库里的 days 表若缺列（例如只见过早期骨架），打开时应重建。"""
    import sqlite3

    path = tmp_path / "old-days.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE days (date TEXT PRIMARY KEY, steps INTEGER);
        CREATE TABLE activities (activity_id INTEGER PRIMARY KEY, pseudo_id TEXT,
                                 sport TEXT, date TEXT, distance_m REAL);
        INSERT INTO days VALUES ('2026-07-24', 1);
        """
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(days)")}
        assert "stress_avg" in cols
        assert s.count_days() == 0  # 旧表被丢弃，等下次 index 重灌
        assert s.upsert_days([day("2026-07-24")], source="x") == 1


# ---------------- sleeps 表 ----------------

def sleep(calendar_date: str, **overrides: object):
    from garmaple.parser.sleep import parse_sleep

    from .test_sleep import make_sleep

    rec = parse_sleep(make_sleep(calendarDate=calendar_date, **overrides), offset_hours=8.0)
    assert rec is not None
    return rec


def test_sleeps_upsert_and_count(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        assert s.upsert_sleeps([sleep("2026-07-24"), sleep("2026-07-25")], source="x") == 2
        assert s.count_sleeps() == 2


def test_sleeps_upsert_is_idempotent(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_sleeps([sleep("2026-07-24")], source="x")
        s.upsert_sleeps([sleep("2026-07-24", deepSleepSeconds=1800)], source="x")
        assert s.count_sleeps() == 1
        assert s.query_sleeps()[0]["deep_min"] == 30


def test_sleeps_date_range_is_inclusive(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_sleeps(
            [sleep("2026-07-23"), sleep("2026-07-24"), sleep("2026-07-25")], source="x"
        )
        rows = s.query_sleeps(start="2026-07-24", end="2026-07-25")
        assert [r["date"] for r in rows] == ["2026-07-24", "2026-07-25"]
        assert s.query_sleeps(start="2099-01-01") == []


def test_sleeps_limit_desc_returns_most_recent_in_order(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_sleeps(
            [sleep("2026-07-22"), sleep("2026-07-23"),
             sleep("2026-07-24"), sleep("2026-07-25")], source="x"
        )
        assert [r["date"] for r in s.query_sleeps(limit=2, order="desc")] == [
            "2026-07-24", "2026-07-25",
        ]


def test_sleeps_plain_desc_stays_descending(tmp_path: Path) -> None:
    """不带 limit 的 desc 仍返回降序——翻回正序只在「取最近 N 天」时才成立。"""
    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_sleeps([sleep("2026-07-23"), sleep("2026-07-25")], source="x")
        assert [r["date"] for r in s.query_sleeps(order="desc")] == [
            "2026-07-25", "2026-07-23",
        ]


def test_sleep_range(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        assert s.sleep_range() == (None, None)
        s.upsert_sleeps([sleep("2026-07-23"), sleep("2026-07-25")], source="x")
        assert s.sleep_range() == ("2026-07-23", "2026-07-25")


def test_sleep_roundtrip_keeps_stages_and_times(tmp_path: Path) -> None:
    from datetime import datetime

    from garmaple.store import row_to_sleep

    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_sleeps([sleep("2026-07-24")], source="x")
        restored = row_to_sleep(s.query_sleeps()[0])
    assert restored.in_bed_min == 661
    assert restored.deep_min == 90
    assert restored.start_at == datetime(2026, 7, 23, 23, 20)
    assert restored.end_at == datetime(2026, 7, 24, 10, 21)
    # asleep_min 不在表里，读回来要能重算出来
    assert restored.asleep_min == 661 - 52


def test_sleep_roundtrip_keeps_status_without_duration(tmp_path: Path) -> None:
    """摘表那晚读回来仍必须是「无时长」，不能在往返路上变成 0。"""
    from garmaple.store import row_to_sleep

    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_sleeps(
            [sleep("2026-07-24", sleepWindowConfirmationType="OFF_WRIST",
                   deepSleepSeconds=0, lightSleepSeconds=0, remSleepSeconds=0,
                   awakeSleepSeconds=0, unmeasurableSeconds=0)],
            source="x",
        )
        restored = row_to_sleep(s.query_sleeps()[0])
    assert restored.status == "off_wrist"
    assert restored.asleep_min is None
    assert restored.in_bed_min is None


def test_three_tables_coexist(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_activities([act(1)], source="x")
        s.upsert_days([day("2026-07-24")], source="x")
        s.upsert_sleeps([sleep("2026-07-24")], source="x")
        assert (s.count_activities(), s.count_days(), s.count_sleeps()) == (1, 1, 1)


def test_broken_sleeps_table_self_heals(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old-sleeps.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sleeps (date TEXT PRIMARY KEY, minutes INTEGER);
        CREATE TABLE days (date TEXT PRIMARY KEY, steps INTEGER, resting_hr INTEGER,
                           stress_avg INTEGER, utc_offset_hours REAL);
        CREATE TABLE activities (activity_id INTEGER PRIMARY KEY, pseudo_id TEXT,
                                 sport TEXT, date TEXT, distance_m REAL);
        INSERT INTO sleeps VALUES ('2026-07-24', 480);
        """
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(sleeps)")}
        assert "status" in cols
        assert s.count_sleeps() == 0  # 旧表被丢弃，等下次 index 重灌
        assert s.upsert_sleeps([sleep("2026-07-24")], source="x") == 1


def test_old_days_table_without_offset_self_heals(tmp_path: Path) -> None:
    """days 表新增了 utc_offset_hours：旧库缺这列时必须重建，否则读取会 KeyError。"""
    import sqlite3

    path = tmp_path / "old-days2.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE days (date TEXT PRIMARY KEY, steps INTEGER, resting_hr INTEGER,
                           stress_avg INTEGER);
        INSERT INTO days VALUES ('2026-07-24', 100, 50, 30);
        """
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(days)")}
        assert "utc_offset_hours" in cols
        assert s.count_days() == 0


# ---------------- training 表 ----------------

def train(calendar_date: str, **overrides: object):
    from garmaple.parser.training import parse_training

    from .test_training import make_block

    rec = parse_training(make_block(calendarDate=calendar_date, **overrides))
    assert rec is not None
    return rec


def test_training_upsert_and_count(tmp_path: Path) -> None:
    with Store(tmp_path / "t.sqlite3") as s:
        assert s.upsert_training(
            [train("2025-04-23"), train("2025-04-24")], source="x"
        ) == 2
        assert s.count_training() == 2


def test_training_upsert_is_idempotent(tmp_path: Path) -> None:
    with Store(tmp_path / "t.sqlite3") as s:
        s.upsert_training([train("2025-04-23")], source="x")
        s.upsert_training([train("2025-04-23", trainingStatus="RECOVERY")], source="x")
        assert s.count_training() == 1
        assert s.query_training()[0]["status"] == "RECOVERY"


def test_training_date_range_is_inclusive(tmp_path: Path) -> None:
    with Store(tmp_path / "t.sqlite3") as s:
        s.upsert_training(
            [train("2025-04-23"), train("2025-04-24"), train("2025-04-25")], source="x"
        )
        rows = s.query_training(start="2025-04-24", end="2025-04-25")
        assert [r["date"] for r in rows] == ["2025-04-24", "2025-04-25"]
        assert s.query_training(start="2099-01-01") == []


def test_training_limit_desc_returns_most_recent_in_order(tmp_path: Path) -> None:
    with Store(tmp_path / "t.sqlite3") as s:
        s.upsert_training(
            [train("2025-04-22"), train("2025-04-23"),
             train("2025-04-24"), train("2025-04-25")], source="x"
        )
        assert [r["date"] for r in s.query_training(limit=2, order="desc")] == [
            "2025-04-24", "2025-04-25",
        ]


def test_training_range(tmp_path: Path) -> None:
    with Store(tmp_path / "t.sqlite3") as s:
        assert s.training_range() == (None, None)
        s.upsert_training([train("2025-04-23"), train("2025-04-25")], source="x")
        assert s.training_range() == ("2025-04-23", "2025-04-25")


def test_training_roundtrip_keeps_zero_load(tmp_path: Path) -> None:
    """周负荷 0 往返一趟仍是 0，不能变成 NULL。"""
    from garmaple.store import row_to_training

    with Store(tmp_path / "t.sqlite3") as s:
        s.upsert_training([train("2025-04-23", weeklyTrainingLoadSum=0)], source="x")
        restored = row_to_training(s.query_training()[0])
    assert restored.weekly_load == 0
    assert restored.load_position == "below"


def test_training_roundtrip_keeps_tunnel_and_trends(tmp_path: Path) -> None:
    from datetime import datetime

    from garmaple.store import row_to_training

    with Store(tmp_path / "t.sqlite3") as s:
        s.upsert_training([train("2025-04-23")], source="x")
        restored = row_to_training(s.query_training()[0])
    assert restored.tunnel_min == 281
    assert restored.tunnel_max == 640
    assert restored.load_position == "in"
    assert restored.fitness_trend == "NO_CHANGE"
    assert restored.load_trend == "INCREASING"
    assert restored.updated_at == datetime(2025, 4, 22, 16, 1, 57)


def test_four_tables_coexist(tmp_path: Path) -> None:
    with Store(tmp_path / "s.sqlite3") as s:
        s.upsert_activities([act(1)], source="x")
        s.upsert_days([day("2025-04-23")], source="x")
        s.upsert_sleeps([sleep("2025-04-23")], source="x")
        s.upsert_training([train("2025-04-23")], source="x")
        assert (
            s.count_activities(), s.count_days(),
            s.count_sleeps(), s.count_training(),
        ) == (1, 1, 1, 1)


def test_broken_training_table_self_heals(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old-training.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE training (date TEXT PRIMARY KEY, status TEXT);
        INSERT INTO training VALUES ('2025-04-23', 'PRODUCTIVE');
        """
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(training)")}
        assert "weekly_load" in cols
        assert s.count_training() == 0  # 旧表被丢弃，等下次 index 重灌
        assert s.upsert_training([train("2025-04-23")], source="x") == 1
