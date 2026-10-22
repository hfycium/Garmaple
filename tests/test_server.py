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
    assert len(names) == 6


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
        ("get_period_summary", {"start_date": "2026-07-22", "end_date": "2026-07-20"}),
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
