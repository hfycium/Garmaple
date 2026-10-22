"""脱敏层测试。

重点保护三件事：
1. strict 下绝不出现坐标、地点名、原始 ID
2. 伪 ID 稳定且不可反查
3. raw 档位才恢复完整字段
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from garmaple.parser.activities import parse_activity
from garmaple.privacy import Privacy

from .test_activities import make_raw


def make_activity(**overrides: object):
    act = parse_activity(make_raw(**overrides))
    assert act is not None
    return act


def test_invalid_level_rejected() -> None:
    with pytest.raises(ValueError):
        Privacy(level="whatever")


@pytest.mark.parametrize("forbidden", ["start", "end", "location_name", "device_id", "user_profile_id"])
def test_strict_drops_location_and_identity(forbidden: str) -> None:
    out = Privacy("strict").activity(make_activity())
    assert forbidden not in out


def test_strict_coarsens_time_to_date() -> None:
    out = Privacy("strict").activity(make_activity())
    assert isinstance(out["when"], str)
    assert len(out["when"]) == 10  # YYYY-MM-DD
    assert ":" not in out["when"]


def test_strict_hides_raw_activity_id() -> None:
    out = Privacy("strict").activity(make_activity())
    assert out["id"] != "123456789"
    assert str(out["id"]).startswith("act_")
    assert "123456789" not in str(out["id"])


def test_pseudo_id_is_stable_across_calls() -> None:
    p = Privacy("strict")
    assert p.public_id(42) == p.public_id(42)


def test_pseudo_id_differs_per_activity() -> None:
    p = Privacy("strict")
    assert p.public_id(1) != p.public_id(2)


def test_normal_keeps_id_and_precise_time_but_not_location() -> None:
    out = Privacy("normal").activity(make_activity())
    assert out["id"] == "123456789"
    assert ":" in str(out["when"])
    assert "start" not in out
    assert "location_name" not in out


def test_raw_exposes_everything() -> None:
    out = Privacy("raw").activity(make_activity())
    assert out["id"] == "123456789"
    assert out["location_name"] == "某市"
    assert out["start"] == [30.0, 120.0]
    assert out["end"] == [30.1, 120.1]
    assert out["device_id"] == 999
    assert out["user_profile_id"] == 111


@pytest.mark.parametrize("level", ["strict", "normal", "raw"])
def test_core_metrics_survive_every_level(level: str) -> None:
    out = Privacy(level).activity(make_activity())
    assert out["sport"] == "running"
    assert out["distance_km"] == pytest.approx(10.0)
    assert out["duration_s"] == 3600
    assert out["pace_s_per_km"] == pytest.approx(360.0)
    assert out["avg_hr"] == 150


def test_none_fields_are_dropped() -> None:
    out = Privacy("strict").activity(make_activity(maxHr=None, calories=None, vO2MaxValue=None))
    assert "max_hr" not in out
    assert "calories" not in out
    assert "vo2max" not in out


def test_empty_hr_zones_are_dropped() -> None:
    out = Privacy("strict").activity(make_activity(hrTimeInZone_3=0, hrTimeInZone_4=0))
    assert "hr_zones_s" not in out


def test_time_coarsening_handles_missing_time() -> None:
    p = Privacy("strict")
    assert p.when(None, None) is None


def test_no_coordinate_value_leaks_into_serialized_output() -> None:
    """兜底：strict 输出里不应出现任何 30.0 这样的坐标值。"""
    out = Privacy("strict").activity(make_activity())
    assert "30.0" not in repr(out)
    assert "120.0" not in repr(out)


def test_when_uses_local_wall_clock() -> None:
    act = make_activity()
    out = Privacy("normal").activity(act)
    assert act.local_start is not None
    assert out["when"] == act.local_start.replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def test_utc_start_timezone_is_aware() -> None:
    act = make_activity()
    assert act.utc_start is not None
    assert act.utc_start.tzinfo == timezone.utc
