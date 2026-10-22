"""索引层测试：幂等性、筛选、日期区间。"""

from __future__ import annotations

from pathlib import Path

import pytest

from garmaple.parser.activities import parse_activity
from garmaple.store import Store

from .test_activities import make_raw


def act(activity_id: int, **overrides: object):
    a = parse_activity(make_raw(activityId=activity_id, **overrides))
    assert a is not None
    return a


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
