"""命令行入口测试。

用合成导出包（临时 zip）验证 index / doctor 的接线：
归档结构照抄真实包，但内容全是手写的小数据，不含任何个人记录。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from garmaple.cli import main
from garmaple.config import Config
from garmaple.store import Store

from .test_activities import make_raw
from .test_daily import make_block


def build_archive(root: Path, activities: int = 2, days: int = 2) -> Path:
    """造一个最小导出包：活动摘要 + 每日汇总。"""
    path = root / "export.zip"
    payload = [{"summarizedActivitiesExport": [
        make_raw(activityId=1000 + i) for i in range(activities)
    ]}]
    daily = [
        make_block(calendarDate=f"2026-07-{20 + i:02d}") for i in range(days)
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "DI_CONNECT/DI-Connect-Fitness/9000_1_summarizedActivities.json",
            json.dumps(payload),
        )
        zf.writestr(
            "DI_CONNECT/DI-Connect-Aggregator/UDSFile_2026-07-20_2026-07-30.json",
            json.dumps(daily),
        )
    return path


def make_config(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data)
    return Config(data_dir=data, cache_dir=tmp_path / "cache", privacy="strict")


def test_index_builds_both_tables(tmp_path: Path, capsys) -> None:
    cfg = make_config(tmp_path)
    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0

    out = capsys.readouterr().out
    assert "活动分片" in out and "日汇总分片" in out
    assert "库内活动   2 条" in out
    assert "库内日汇总 2 天" in out

    with Store(cfg.db_path) as store:
        assert store.count_activities() == 2
        assert store.count_days() == 2


def test_index_does_not_print_shard_filenames(tmp_path: Path, capsys) -> None:
    """分片文件名可能带设备序列号，终端输出里不该出现。"""
    cfg = make_config(tmp_path)
    main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"])
    out = capsys.readouterr().out
    assert "9000_1_summarizedActivities.json" not in out
    assert "UDSFile_" not in out


def test_index_is_idempotent(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    args = ["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]
    main(args)
    main(args)
    with Store(cfg.db_path) as store:
        assert store.count_activities() == 2
        assert store.count_days() == 2


def test_index_without_daily_shard_still_works(tmp_path: Path) -> None:
    """没有 UDS 文件时只跳过每日汇总，不该整体失败。"""
    data = tmp_path / "data"
    data.mkdir()
    path = data / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "DI_CONNECT/DI-Connect-Fitness/9000_1_summarizedActivities.json",
            json.dumps([{"summarizedActivitiesExport": [make_raw()]}]),
        )
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache")

    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0
    with Store(cfg.db_path) as store:
        assert store.count_activities() == 1
        assert store.count_days() == 0


def test_doctor_reports_counts(tmp_path: Path, capsys) -> None:
    cfg = make_config(tmp_path)
    args = ["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir)]
    main(args + ["index"])
    capsys.readouterr()

    assert main(args + ["doctor"]) == 0
    out = capsys.readouterr().out
    assert "活动       2 条" in out
    assert "每日汇总   2 天" in out
    assert "一切就绪" in out


def test_doctor_fails_before_index(tmp_path: Path, capsys) -> None:
    cfg = make_config(tmp_path)
    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "doctor"]) == 1
    assert "尚未建立索引" in capsys.readouterr().out
