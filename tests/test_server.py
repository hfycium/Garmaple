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

from .test_cli import build_archive


def prepared(tmp_path: Path, days: int = 3, privacy: str = "strict") -> Config:
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, activities=2, days=days)
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
    assert len(names) == 9


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
