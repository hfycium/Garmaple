"""MCP 工具层测试。

走真实调用路径（build_server → call_tool），而不是直接调内部函数，
这样才能确认工具确实注册上了、返回值确实过了 privacy。
数据是合成的，见 tests/test_cli.build_archive。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from garmaple.cli import main
from garmaple.config import Config
from garmaple.server import build_server
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from garmaple.privacy import pseudo_activity_id

from .test_cli import build_archive


def prepared(tmp_path: Path, days: int = 3, privacy: str = "strict", fits: int = 0) -> Config:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    build_archive(data, activities=2, days=days, fits=fits)
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache", privacy=privacy)
    main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"])
    return cfg


def call(cfg: Config, name: str, arguments: dict | None = None) -> dict:
    mcp = build_server(cfg)
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    return json.loads(result.content[0].text)


def tool_names(cfg: Config) -> list[str]:
    return [t.name for t in asyncio.run(build_server(cfg).list_tools())]


def test_daily_summary_tool_is_registered(tmp_path: Path) -> None:
    names = tool_names(prepared(tmp_path))
    assert "get_daily_summary" in names
    assert "get_training_status" in names
    assert "get_race_predictions" in names
    assert "get_vo2max_trend" in names
    assert "get_personal_records" in names
    assert "get_gear_mileage" in names
    assert "get_hr_zones" in names
    assert "get_activity_streams" in names
    assert "get_pace_hr_drift" in names
    assert len(names) == 14


def test_hr_zones_distribution(tmp_path: Path) -> None:
    """两条活动各 1800s/900s，合并后 3600s 与 1800s，占比 66.7 / 33.3。"""
    out = call(prepared(tmp_path), "get_hr_zones", {})
    assert out["activities"] == 2
    assert out["with_zone_data"] == 2
    assert out["without_zone_data"] == 0
    assert out["total_s"] == 5400
    zones = {z["zone"]: z["seconds"] for z in out["zones"]}
    assert zones[3] == 3600 and zones[4] == 1800
    assert out["dominant_zone"] == 3
    pct = {z["zone"]: z["pct"] for z in out["zones"]}
    assert pct[3] == 66.7 and pct[4] == 33.3


def test_hr_zones_says_boundaries_are_missing(tmp_path: Path) -> None:
    """必须随结果说明「没有 bpm 边界」。

    归档里没有区间定义，模型看到 zone_2 很容易自行说成「Z3 / 有氧区间」，
    那不是数据里的东西。
    """
    out = call(prepared(tmp_path), "get_hr_zones", {})
    assert "bpm" in out["zones_note"]
    assert "Z1-Z5" in out["zones_note"]


def test_hr_zones_empty_range_gives_hint(tmp_path: Path) -> None:
    out = call(
        prepared(tmp_path),
        "get_hr_zones",
        {"start_date": "2030-01-01", "end_date": "2030-01-02"},
    )
    assert out["activities"] == 0
    assert "hint" in out


def test_daily_summary_defaults_to_recent_days_in_order(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_daily_summary", {"limit": 2})
    assert out["count"] == 2
    assert [d["date"] for d in out["days"]] == ["2026-07-21", "2026-07-22"]
    assert out["range"] == {"from": "2026-07-21", "to": "2026-07-22"}


def test_daily_summary_returns_all_days_by_default(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_daily_summary", {})
    assert [d["date"] for d in out["days"]] == ["2026-07-20", "2026-07-21", "2026-07-22"]


def test_daily_summary_date_range_is_inclusive(tmp_path: Path) -> None:
    out = call(
        prepared(tmp_path),
        "get_daily_summary",
        {"start_date": "2026-07-21", "end_date": "2026-07-22"},
    )
    assert [d["date"] for d in out["days"]] == ["2026-07-21", "2026-07-22"]


def test_daily_summary_truncates_to_most_recent(tmp_path: Path) -> None:
    out = call(
        prepared(tmp_path),
        "get_daily_summary",
        {"start_date": "2026-07-20", "end_date": "2026-07-22", "limit": 2},
    )
    assert out["truncated"] is True
    assert [d["date"] for d in out["days"]] == ["2026-07-21", "2026-07-22"]
    assert "最近" in out["note"]


def test_daily_summary_empty_range_gives_hint(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_daily_summary", {"start_date": "2099-01-01"})
    assert out["count"] == 0
    assert out["days"] == []
    assert "get_data_overview" in out["hint"]


def test_daily_summary_rejects_reversed_range(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        call(
            prepared(tmp_path),
            "get_daily_summary",
            {"start_date": "2026-07-22", "end_date": "2026-07-20"},
        )
    assert "不能晚于" in str(excinfo.value)


def test_daily_summary_rejects_bad_date_format(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        call(prepared(tmp_path), "get_daily_summary", {"start_date": "2026/07/20"})
    assert "YYYY-MM-DD" in str(excinfo.value)


def test_daily_summary_is_privacy_filtered(tmp_path: Path) -> None:
    """strict 下不得出现身体电量峰谷时刻，也不得出现账号标识。"""
    out = call(prepared(tmp_path), "get_daily_summary", {})
    text = json.dumps(out, ensure_ascii=False)
    assert "bb_high_at" not in text
    assert "user_profile_id" not in text
    assert out["days"][0]["steps"] == 9523  # 聚合数值仍然保留


def test_daily_summary_raw_exposes_times(tmp_path: Path) -> None:
    out = call(prepared(tmp_path, privacy="raw"), "get_daily_summary", {"limit": 1})
    day = out["days"][0]
    assert day["bb_high_at"] == "10:57"
    assert day["user_profile_id"] == 999


def test_overview_reports_daily_coverage(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["daily_summary"] == {
        "days": 3,
        "date_from": "2026-07-20",
        "date_to": "2026-07-22",
    }


def test_overview_omits_daily_block_when_absent(tmp_path: Path) -> None:
    cfg = prepared(tmp_path, days=0)
    out = call(cfg, "get_data_overview")
    assert "daily_summary" not in out
    # 活动工具照常可用
    assert out["activities"] == 2


# ---------------- get_sleep ----------------

def test_sleep_returns_nights_in_order(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_sleep", {})
    assert out["count"] == 3
    assert [n["date"] for n in out["nights"]] == [
        "2026-07-20", "2026-07-21", "2026-07-22",
    ]
    assert out["nights"][0]["status"] == "ok"
    assert out["nights"][0]["asleep_min"] == 661 - 52
    assert "status_note" not in out  # 全都有分期时不必解释


def test_sleep_date_range(tmp_path: Path) -> None:
    out = call(
        prepared(tmp_path), "get_sleep",
        {"start_date": "2026-07-21", "end_date": "2026-07-22"},
    )
    assert [n["date"] for n in out["nights"]] == ["2026-07-21", "2026-07-22"]


def test_sleep_default_limit_returns_most_recent(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_sleep", {"limit": 1})
    assert out["count"] == 1
    assert out["nights"][0]["date"] == "2026-07-22"


def test_sleep_range_is_not_cut_by_the_recent_default(tmp_path: Path) -> None:
    """给了区间就尊重区间：默认「最近 14 晚」不能把区间查询截掉。

    真实数据上是 608 晚，若默认 limit 同时管区间，问「这个月睡得怎样」
    会静默只拿到两周，看起来像中间缺数据。
    """
    out = call(
        prepared(tmp_path), "get_sleep",
        {"start_date": "2026-07-20", "end_date": "2026-07-22"},
    )
    assert out["count"] == 3
    assert "truncated" not in out


def test_sleep_strict_withholds_bed_and_wake_times(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_sleep", {})
    text = json.dumps(out, ensure_ascii=False)
    assert "fell_asleep_at" not in text
    assert "woke_at" not in text


def test_sleep_normal_exposes_times_in_local_clock(tmp_path: Path) -> None:
    """醒来时刻必须落在 calendarDate 当天——这是「归到醒来日」的直接证据。"""
    out = call(prepared(tmp_path, privacy="normal"), "get_sleep", {"limit": 1})
    night = out["nights"][0]
    assert night["date"] == "2026-07-22"
    assert night["woke_at"].startswith("2026-07-22 ")
    assert night["fell_asleep_at"].startswith("2026-07-21 ")  # 入睡在前一晚


def test_sleep_without_stages_only_has_date_and_status(tmp_path: Path) -> None:
    """摘表那晚不能给出时长——否则会被读成「睡了 0 分钟」。"""
    from garmaple.parser.sleep import parse_sleep
    from garmaple.store import Store

    from .test_sleep import make_sleep

    cfg = prepared(tmp_path)
    off = parse_sleep(
        make_sleep(
            calendarDate="2026-07-22",
            sleepWindowConfirmationType="OFF_WRIST",
            deepSleepSeconds=0, lightSleepSeconds=0, remSleepSeconds=0,
            awakeSleepSeconds=0, unmeasurableSeconds=0, averageRespiration=None,
        ),
        offset_hours=8.0,
    )
    assert off is not None
    with Store(cfg.db_path) as store:
        store.upsert_sleeps([off], source="test")

    out = call(cfg, "get_sleep", {"start_date": "2026-07-22"})
    assert out["nights"][0] == {"date": "2026-07-22", "status": "off_wrist"}
    assert "off_wrist" in out["status_note"]


def test_sleep_empty_range_gives_hint(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_sleep", {"start_date": "2099-01-01"})
    assert out["count"] == 0
    assert out["nights"] == []
    assert "get_data_overview" in out["hint"]


def test_sleep_rejects_bad_date_format(tmp_path: Path) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        call(prepared(tmp_path), "get_sleep", {"start_date": "2026/07/20"})
    assert "YYYY-MM-DD" in str(excinfo.value)


def test_overview_reports_sleep_coverage(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["sleep"]["nights"] == 3
    assert out["sleep"]["date_from"] == "2026-07-20"
    assert out["sleep"]["date_to"] == "2026-07-22"
    assert "不含睡眠评分" in out["sleep"]["note"]


# ---------------- get_training_status ----------------

def test_training_returns_days_in_order(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_training_status", {})
    assert out["count"] == 3
    assert [d["date"] for d in out["days"]] == [
        "2026-07-20", "2026-07-21", "2026-07-22",
    ]
    assert out["days"][0]["status"] == "PRODUCTIVE"
    assert out["days"][0]["status_label"] == "有成效"


def test_training_latest_matches_the_last_day(tmp_path: Path) -> None:
    """`latest` 存在的意义是回答「我现在的状态」时不用翻数组。"""
    out = call(prepared(tmp_path), "get_training_status", {})
    assert out["latest"] == out["days"][-1]


def test_training_merges_same_day_snapshots(tmp_path: Path) -> None:
    """同一天两条快照只出一天，且取时间更晚那条（周负荷 300 而非 100）。"""
    out = call(prepared(tmp_path), "get_training_status", {})
    assert out["count"] == 3
    assert {d["weekly_load"] for d in out["days"]} == {300}


def test_training_reports_load_position_against_tunnel(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_training_status", {"limit": 1})
    day = out["days"][0]
    assert day["load_tunnel"] == {"min": 281, "max": 640}
    assert day["load_position"] == "in"  # 300 落在 281~640


def test_training_counts_are_provided_for_ranges(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_training_status", {})
    assert out["status_counts"] == {"PRODUCTIVE": 3}
    assert out["load_position_counts"] == {"in": 3}


def test_training_single_day_has_no_counts(tmp_path: Path) -> None:
    """只有一天时计数是噪音，不给。"""
    out = call(prepared(tmp_path), "get_training_status", {"limit": 1})
    assert "status_counts" not in out
    assert "load_position_counts" not in out


def test_training_truncates_to_most_recent(tmp_path: Path) -> None:
    out = call(
        prepared(tmp_path), "get_training_status",
        {"start_date": "2026-07-20", "end_date": "2026-07-22", "limit": 2},
    )
    assert out["truncated"] is True
    assert [d["date"] for d in out["days"]] == ["2026-07-21", "2026-07-22"]
    assert "最近" in out["note"]


def test_training_empty_range_gives_hint(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_training_status", {"start_date": "2099-01-01"})
    assert out["count"] == 0
    assert out["days"] == []
    assert "get_data_overview" in out["hint"]


def test_training_is_privacy_filtered(tmp_path: Path) -> None:
    """strict 下不给账号标识，也不给快照时刻；负荷与状态照常给。"""
    out = call(prepared(tmp_path), "get_training_status", {})
    text = json.dumps(out, ensure_ascii=False)
    assert "user_profile_id" not in text
    assert "updated_at" not in text
    assert out["days"][0]["weekly_load"] == 300


def test_training_raw_exposes_user_profile(tmp_path: Path) -> None:
    out = call(prepared(tmp_path, privacy="raw"), "get_training_status", {"limit": 1})
    assert out["days"][0]["user_profile_id"] == 999


def test_overview_reports_training_coverage(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["training_status"]["days"] == 3
    assert out["training_status"]["date_from"] == "2026-07-20"
    assert out["training_status"]["date_to"] == "2026-07-22"
    assert "CTL" in out["training_status"]["note"]


# ---------------- get_race_predictions ----------------

def test_race_predictions_returns_days_in_order(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_race_predictions", {})
    assert out["count"] == 3
    assert [d["date"] for d in out["days"]] == [
        "2026-07-20", "2026-07-21", "2026-07-22",
    ]


def test_race_predictions_latest_is_the_last_day(tmp_path: Path) -> None:
    """latest 与 days 的末项指同一天，但 latest 带格式化信息。"""
    out = call(prepared(tmp_path), "get_race_predictions", {})
    assert out["latest"]["date"] == out["days"][-1]["date"]
    assert out["latest"]["predicted"]["5k"]["seconds"] == out["days"][-1]["5k"]


def test_race_predictions_merge_same_day_snapshots(tmp_path: Path) -> None:
    """同一天两条预测只出一天，取时间更晚那条（1086 而非 1092）。"""
    out = call(prepared(tmp_path), "get_race_predictions", {})
    assert out["count"] == 3
    assert {d["5k"] for d in out["days"]} == {1086}


def test_race_predictions_trend_gives_seconds_only(tmp_path: Path) -> None:
    """趋势数组只给秒数：120 天 × 4 距离都带格式化文本会把上下文撑满。"""
    out = call(prepared(tmp_path), "get_race_predictions", {"limit": 1})
    assert out["days"][0] == {
        "date": "2026-07-22", "5k": 1086, "10k": 2233, "half": 6540, "marathon": 14040,
    }


def test_race_predictions_latest_gives_time_and_pace(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_race_predictions", {"limit": 1})
    five = out["latest"]["predicted"]["5k"]
    assert five["time"] == "18m06s"
    assert five["pace"] == "3'37\"/km"


def test_race_predictions_have_no_change_for_single_day(tmp_path: Path) -> None:
    """只有一天时没有「变化」可言，不要凭空给 0。"""
    out = call(prepared(tmp_path), "get_race_predictions", {"limit": 1})
    assert "change" not in out


def test_race_predictions_change_is_positive_when_faster(tmp_path: Path) -> None:
    """同值的数据变化为 0——这里只锁住字段形状与「正数=变快」的方向。"""
    out = call(prepared(tmp_path), "get_race_predictions", {})
    change = out["change"]["5k"]
    assert change["faster_by_s"] == 0
    assert change["change_pct"] == 0
    assert "正表示预测变快" in out["change_note"]


def test_race_predictions_truncate_to_most_recent(tmp_path: Path) -> None:
    out = call(
        prepared(tmp_path), "get_race_predictions",
        {"start_date": "2026-07-20", "end_date": "2026-07-22", "limit": 2},
    )
    assert out["truncated"] is True
    assert [d["date"] for d in out["days"]] == ["2026-07-21", "2026-07-22"]


def test_race_predictions_empty_range_gives_hint(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_race_predictions", {"start_date": "2099-01-01"})
    assert out["count"] == 0
    assert "get_data_overview" in out["hint"]


def test_race_predictions_are_privacy_filtered(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_race_predictions", {})
    text = json.dumps(out, ensure_ascii=False)
    assert "user_profile_id" not in text


def test_race_predictions_raw_exposes_user_profile(tmp_path: Path) -> None:
    out = call(prepared(tmp_path, privacy="raw"), "get_race_predictions", {"limit": 1})
    assert out["latest"]["user_profile_id"] == 999


def test_overview_reports_race_coverage(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["race_predictions"]["days"] == 3
    assert "不是已完成的比赛成绩" in out["race_predictions"]["note"]


# ---------------- get_vo2max_trend ----------------

def test_vo2max_returns_samples_in_order(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_vo2max_trend", {})
    assert out["count"] == 3
    assert [s["date"] for s in out["samples"]] == [
        "2026-07-20", "2026-07-21", "2026-07-22",
    ]
    assert out["samples"][0]["vo2max"] == 62.0


def test_vo2max_gives_lowest_and_highest(tmp_path: Path) -> None:
    """样本全同值时最低与最高都指向同一次，不能报成缺失。"""
    out = call(prepared(tmp_path), "get_vo2max_trend", {})
    assert out["lowest"]["vo2max"] == 62.0
    assert out["highest"]["vo2max"] == 62.0


def test_vo2max_reports_change_between_first_and_last(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_vo2max_trend", {})
    assert out["change"]["delta"] == 0
    assert "正数表示提高" in out["change"]["note"]


def test_vo2max_warns_about_sparse_dates(tmp_path: Path) -> None:
    """样本天然稀疏，必须明说空缺不等于数值下降。"""
    out = call(prepared(tmp_path), "get_vo2max_trend", {})
    assert "不代表数值下降" in out["sparsity_note"]


def test_vo2max_never_mentions_fitness_age(tmp_path: Path) -> None:
    """归档里 fitnessAge 恒为占位值 "20"，任何档位都不该出现在输出里。"""
    base = prepared(tmp_path)
    for level in ("strict", "normal", "raw"):
        cfg = Config(data_dir=base.data_dir, cache_dir=base.cache_dir, privacy=level)
        out = call(cfg, "get_vo2max_trend", {})
        assert "fitness_age" not in json.dumps(out, ensure_ascii=False)


def test_vo2max_empty_range_gives_hint(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_vo2max_trend", {"start_date": "2099-01-01"})
    assert out["count"] == 0
    assert "get_data_overview" in out["hint"]


def test_overview_reports_vo2max_coverage(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["vo2max"]["days"] == 3
    assert "稀疏" in out["vo2max"]["note"]


# ---------------- get_personal_records ----------------


def test_personal_records_group_by_sport(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_personal_records", {})
    assert out["count"] == 2
    assert set(out["groups"]) == {"run"}
    assert out["group_counts"] == {"run": 2}


def test_personal_records_keep_units_apart(tmp_path: Path) -> None:
    """5K 是秒、Farthest Run 是米——同组不同类型，量纲不能混。

    把 1108 秒当成距离，就会说出「5 公里跑了 1108 米」。
    """
    out = call(prepared(tmp_path), "get_personal_records", {})
    by_type = {r["type"]: r for r in out["groups"]["run"]}
    five = by_type["Best 5km Run"]
    farthest = by_type["Farthest Run"]
    assert five["unit"] == "s"
    assert five["display"] == "18m28s"
    assert farthest["unit"] == "m"
    assert farthest["display"] == "21.12 km"


def test_personal_records_superseded_is_flagged(tmp_path: Path) -> None:
    """被刷新的旧纪录要单独分组说明，不能与当前纪录并列。"""
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, days=3, superseded=True)
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache", privacy="strict")
    main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"])

    out = call(cfg, "get_personal_records", {})
    assert out["count"] == 2
    assert len(out["superseded"]) == 1
    assert out["superseded"][0]["value"] == 1194.2
    assert out["superseded"][0]["current"] is False
    assert "旧纪录" in out["superseded_note"]


def test_personal_records_filter_by_group(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_personal_records", {"group": "swim"})
    assert out["count"] == 0
    assert "没有 swim" in out["hint"]


def test_personal_records_unknown_group_is_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        call(prepared(tmp_path), "get_personal_records", {"group": "yoga"})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "run" in str(excinfo.value)


def test_overview_reports_personal_records(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["personal_records"]["total"] == 2
    assert out["personal_records"]["current"] == 2


# ---------------- get_gear_mileage ----------------


def test_gear_mileage_sums_linked_activities(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_gear_mileage", {})
    assert out["count"] == 2
    first = out["gears"][0]
    assert first["distance_km"] == 20.0  # 两条 10km 的活动
    assert first["activity_count"] == 2
    assert first["by_sport"]["running"]["distance_km"] == 20.0


def test_gear_without_links_reports_note_not_zero(tmp_path: Path) -> None:
    """空关联必须说「没有记录」，报 0 公里会被读成「买了没穿过」。"""
    out = call(prepared(tmp_path), "get_gear_mileage", {})
    second = out["gears"][1]
    assert "distance_km" not in second
    assert "无法计算里程" in second["note"]
    assert "missing_note" in out


def test_gear_mileage_mentions_source_of_number(tmp_path: Path) -> None:
    """里程是加出来的，不是设备给的——这句必须随结果一起返回。"""
    out = call(prepared(tmp_path), "get_gear_mileage", {})
    assert "关联活动" in out["mileage_note"]


def test_gear_id_hidden_in_strict_but_present_in_normal(tmp_path: Path) -> None:
    strict = call(prepared(tmp_path / "s"), "get_gear_mileage", {})
    assert all("id" not in g for g in strict["gears"])

    normal = call(prepared(tmp_path / "n", privacy="normal"), "get_gear_mileage", {})
    assert normal["gears"][0]["id"] == "700001"


def test_overview_reports_gear_coverage(tmp_path: Path) -> None:
    out = call(prepared(tmp_path), "get_data_overview")
    assert out["gear"]["items"] == 2
    assert out["gear"]["with_activities"] == 1


# ---------- 预期内失败：必须让模型看见原因 ----------
#
# 为什么这几条要单独测：SDK 只把 ToolError 的文本透给模型；其他异常被当成
# crash，模型只看到 `Error executing tool <name>`，原因只留在服务端日志里。
# 注意 UnexpectedToolError 是 ToolError 的子类，光断言 ToolError 抓不住退化。


def test_expected_failures_are_not_wrapped_as_crash(tmp_path: Path) -> None:
    cfg = prepared(tmp_path)
    cases = [
        ("get_daily_summary", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
        ("get_daily_summary", {"start_date": "2026/07/20"}),
        ("list_activities", {"start_date": "13-01-2026"}),
        ("get_period_summary", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
        ("get_training_status", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
        ("get_training_status", {"start_date": "2026-13-01"}),
        ("get_race_predictions", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
        ("get_race_predictions", {"start_date": "2026/07/20"}),
        ("get_vo2max_trend", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
        ("get_vo2max_trend", {"end_date": "not-a-date"}),
        ("get_personal_records", {"group": "not-a-group"}),
        ("get_hr_zones", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
        ("get_hr_zones", {"start_date": "2026/07/20"}),
    ]
    for name, args in cases:
        with pytest.raises(ToolError) as excinfo:
            call(cfg, name, args)
        assert not isinstance(excinfo.value, UnexpectedToolError), (
            f"{name} 的参数错误退化成了 crash，原因会被吞掉"
        )


def test_reason_reaches_the_model_over_stdio(tmp_path: Path) -> None:
    """端到端：真实 stdio 子进程 + 官方 client，看客户端最终拿到什么。

    单测里 call_tool 是内存调用，抛的是异常对象；真实客户端收到的是
    CallToolResult，两者形状不同。这条走完整链路，锁住用户实际看到的内容。
    """
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cfg = prepared(tmp_path)
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "GARMAPLE_DATA": str(cfg.data_dir),
        "GARMAPLE_CACHE": str(cfg.cache_dir),
        "GARMAPLE_PRIVACY": "strict",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "garmaple.cli", "serve"],
        env=env,
    )

    async def ask() -> object:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "get_daily_summary",
                    {"start_date": "2026-07-22", "end_date": "2026-07-20"},
                )

    result = asyncio.run(ask())
    assert result.is_error is True
    text = result.content[0].text
    # 关键断言：原因必须到达客户端，而不只是 `Error executing tool ...`
    assert "不能晚于" in text


def test_missing_index_says_how_to_fix(tmp_path: Path) -> None:
    """没建索引是预期内的情况，应当告诉模型去跑 index，而不是只报崩溃。"""
    cfg = Config(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        privacy="strict",
    )
    with pytest.raises(ToolError) as excinfo:
        call(cfg, "get_data_overview")
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "garmaple index" in str(excinfo.value)


# ---------------- 逐秒曲线 ----------------


def test_activity_streams_is_registered(tmp_path: Path) -> None:
    assert "get_activity_streams" in tool_names(prepared(tmp_path))


def test_activity_streams_returns_second_by_second_series(tmp_path: Path) -> None:
    """有 FIT 时返回逐点序列，且极值来自全量而不是抽稀后的点。"""
    cfg = prepared(tmp_path, fits=1)
    out = call(cfg, "get_activity_streams", {"activity_id": pseudo_activity_id(1000)})

    assert out.get("available") is not False
    assert out["total_points"] == 650  # 0,2,...,1298
    assert out["points"] == 300  # 默认抽稀到 300
    assert out["downsampled"] is True
    assert out["series"]["offset_s"][0] == 0
    assert out["series"]["offset_s"][-1] == 1298
    assert out["series"]["hr"][0] == 120
    assert out["summary"]["hr_max"] == 184  # 120 + 1298//20


def test_activity_streams_downsamples_but_keeps_full_summary(tmp_path: Path) -> None:
    """抽稀只影响 series，不影响 summary 里的极值。"""
    cfg = prepared(tmp_path, fits=1)
    out = call(
        cfg,
        "get_activity_streams",
        {"activity_id": pseudo_activity_id(1000), "max_points": 20},
    )
    assert out["points"] == 20
    assert out["total_points"] == 650
    assert out["downsampled"] is True
    assert out["summary"]["hr_max"] == 184


def test_activity_streams_without_fit_files_explains(tmp_path: Path) -> None:
    """归档里没有 .fit 时给说明，不要当成错误。"""
    cfg = prepared(tmp_path)  # fits=0
    out = call(cfg, "get_activity_streams", {"activity_id": pseudo_activity_id(1000)})
    assert out["available"] is False
    assert "garmaple index" in out["hint"]


def test_activity_streams_reports_activity_without_fit(tmp_path: Path) -> None:
    """有 FIT 但只有第一个活动能对上，第二个应给「没有对应文件」而不是空序列。

    build_archive 里第 i 个 FIT 的开始时间比活动晚 i 小时，因此只有第 0 个
    能对齐——这正好复刻真实数据里「305 个活动、304 个 FIT」的情形。
    """
    cfg = prepared(tmp_path, fits=1)
    out = call(cfg, "get_activity_streams", {"activity_id": pseudo_activity_id(1001)})
    assert out["available"] is False
    assert "没有对应的 FIT" in out["hint"]


def test_activity_streams_unknown_activity(tmp_path: Path) -> None:
    cfg = prepared(tmp_path, fits=1)
    out = call(cfg, "get_activity_streams", {"activity_id": "act_不存在"})
    assert out["error"] == "未找到该活动"


def test_activity_streams_rejects_absurd_max_points(tmp_path: Path) -> None:
    cfg = prepared(tmp_path, fits=1)
    with pytest.raises(ToolError) as excinfo:
        call(cfg, "get_activity_streams", {"activity_id": pseudo_activity_id(1000),
                                           "max_points": 5})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "max_points" in str(excinfo.value)


def test_activity_streams_never_exposes_coordinates(tmp_path: Path) -> None:
    """逐秒曲线在三档隐私下都不含坐标——解析层就没读 position 字段。"""
    for level in ("strict", "normal", "raw"):
        cfg = prepared(tmp_path / level, fits=1, privacy=level)
        out = call(cfg, "get_activity_streams", {"activity_id": pseudo_activity_id(1000)})
        flat = json.dumps(out, ensure_ascii=False)
        assert "lat" not in flat and "lon" not in flat
        assert "location" not in flat


# ---------------- 配速心率漂移 ----------------


def test_pace_hr_drift_is_registered(tmp_path: Path) -> None:
    assert "get_pace_hr_drift" in tool_names(prepared(tmp_path))


def test_pace_hr_drift_detects_cardiac_drift(tmp_path: Path) -> None:
    """合成数据是「配速恒定、心率每 20 秒 +1」，正是纯心率漂移的形态。

    前后半平均心率约 135.8 与 168.2，效率比值降 19.3%，因此 drift_pct 为正。
    """
    cfg = prepared(tmp_path, fits=1)
    out = call(cfg, "get_pace_hr_drift", {"activity_id": pseudo_activity_id(1000)})

    assert out["available"] is True
    assert out["drift_pct"] == pytest.approx(19.3, abs=0.3)
    assert out["drift_pct"] > 0  # 正数 = 后半程效率下降
    assert out["pace_change_s_per_km"] == 0  # 配速没变
    assert out["hr_change"] == pytest.approx(32.4, abs=0.3)  # 心率升了
    assert out["reliable"] is True
    assert out["first_half"]["avg_pace"] and out["second_half"]["avg_pace"]


def test_pace_hr_drift_without_fit_files_explains(tmp_path: Path) -> None:
    cfg = prepared(tmp_path)  # fits=0
    out = call(cfg, "get_pace_hr_drift", {"activity_id": pseudo_activity_id(1000)})
    assert out["available"] is False
    assert "garmaple index" in out["hint"]


def test_pace_hr_drift_unknown_activity(tmp_path: Path) -> None:
    cfg = prepared(tmp_path, fits=1)
    out = call(cfg, "get_pace_hr_drift", {"activity_id": "act_不存在"})
    assert out["error"] == "未找到该活动"
