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
    assert len(names) == 5


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
    with pytest.raises(Exception):
        call(
            prepared(tmp_path),
            "get_daily_summary",
            {"start_date": "2026-07-22", "end_date": "2026-07-20"},
        )


def test_daily_summary_rejects_bad_date_format(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        call(prepared(tmp_path), "get_daily_summary", {"start_date": "2026/07/20"})


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
