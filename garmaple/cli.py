"""命令行入口。

    garmaple index    解析导出包，建立本地索引
    garmaple doctor   自检：数据、索引、隐私配置
    garmaple serve    启动 MCP 服务（stdio）
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile

from . import __version__
from .archive import ArchiveError, discover
from .config import Config, ConfigError
from .parser.activities import parse_activities
from .parser.daily import DailyStat
from .parser.daily import dedupe as dedupe_days
from .parser.daily import parse_days
from .parser.metrics import RacePrediction, Vo2maxSample
from .parser.metrics import dedupe_predictions, dedupe_vo2max
from .parser.metrics import parse_race_predictions, parse_vo2max_samples
from .parser.records import Gear, PersonalRecord
from .parser.records import dedupe_gears, dedupe_records
from .parser.records import parse_gears, parse_records
from .parser.sleep import STATUS_OK, SleepRecord
from .parser.sleep import dedupe as dedupe_sleeps
from .parser.sleep import parse_sleeps
from .parser.training import TrainingDay
from .parser.training import dedupe as dedupe_training
from .parser.training import parse_trainings
from .store import Store
from .streams import scan_activity_fits

ACTIVITY_GLOB = "*summarizedActivities.json"
DAILY_GLOB = "UDSFile_*.json"
SLEEP_GLOB = "*sleepData.json"
TRAINING_GLOB = "TrainingHistory_*.json"
RACE_GLOB = "RunRacePredictions_*.json"
VO2MAX_GLOB = "MetricsMaxMetData_*.json"
PERSONAL_GLOB = "*personalRecord.json"
GEAR_GLOB = "*gear.json"


def _force_utf8() -> None:
    """Windows 终端默认编码未必是 UTF-8，中文输出会炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _hr(title: str) -> None:
    print(f"\n{title}")
    print("-" * max(12, len(title)))


def cmd_index(cfg: Config) -> int:
    data_dir = cfg.require_data()
    try:
        dataset = discover(data_dir)
    except ArchiveError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    with dataset:
        print(f"数据目录   {data_dir}")
        print(f"发现归档   {len(dataset)} 个")

        shards = dataset.find_all(ACTIVITY_GLOB)
        if not shards:
            print("错误：归档中没有活动摘要文件（*summarizedActivities.json）", file=sys.stderr)
            return 1

        activities = []
        for archive, name in shards:
            activities.extend(parse_activities(archive.read_json(name)))
        # 分片文件名常带设备序列号（形如 <序列号>_1_summarizedActivities.json），
        # 不逐条打印文件名，只报数量，免得用户把终端输出贴进 issue 时泄出去
        print(f"活动分片   {len(shards)} 个 → {len(activities)} 条")

        if not activities:
            print("错误：解析结果为空", file=sys.stderr)
            return 1

        # 去重：多次导出可能包含同一活动
        unique = {a.activity_id: a for a in activities}
        deduped = sorted(unique.values(), key=lambda a: a.activity_id)

        dates = sorted(a.utc_start for a in deduped if a.utc_start)
        sports = Counter(a.sport for a in deduped)

        # 每日汇总（UDS）：没有这份数据也能用活动工具，因此缺失只提示不报错
        days: list[DailyStat] = []
        daily_shards = dataset.find_all(DAILY_GLOB)
        for archive, name in daily_shards:
            days.extend(parse_days(archive.read_json(name)))
        raw_days = len(days)
        days = dedupe_days(days)
        if daily_shards:
            print(f"日汇总分片 {len(daily_shards)} 个 → {raw_days} 天（去重后 {len(days)} 天）")

        # 睡眠：只带 GMT 时间戳，要靠日汇总的当日偏移换算成本地时刻，
        # 所以必须排在日汇总之后
        sleeps: list[SleepRecord] = []
        sleep_shards = dataset.find_all(SLEEP_GLOB)
        if sleep_shards:
            offsets = {
                d.date: d.utc_offset_hours
                for d in days
                if d.utc_offset_hours is not None
            }
            for archive, name in sleep_shards:
                sleeps.extend(parse_sleeps(archive.read_json(name), offsets=offsets))
            raw_sleeps = len(sleeps)
            sleeps = dedupe_sleeps(sleeps)
            print(f"睡眠分片   {len(sleep_shards)} 个 → {raw_sleeps} 晚（去重后 {len(sleeps)} 晚）")

        # 训练状态：同一天可能有多条同步快照，解析层按时间戳收敛成一条
        training: list[TrainingDay] = []
        training_shards = dataset.find_all(TRAINING_GLOB)
        for archive, name in training_shards:
            training.extend(parse_trainings(archive.read_json(name)))
        raw_training = len(training)
        training = dedupe_training(training)
        if training_shards:
            print(
                f"训练分片   {len(training_shards)} 个 → {raw_training} 条"
                f"（同日合并后 {len(training)} 天）"
            )

        # 比赛预测：同一天设备会重算多次，解析层按时间戳收敛成一条
        races: list[RacePrediction] = []
        race_shards = dataset.find_all(RACE_GLOB)
        for archive, name in race_shards:
            races.extend(parse_race_predictions(archive.read_json(name)))
        raw_races = len(races)
        races = dedupe_predictions(races)
        if race_shards:
            print(
                f"比赛预测   {len(race_shards)} 个 → {raw_races} 条"
                f"（同日合并后 {len(races)} 天）"
            )

        # 最大摄氧量：只有跑出有效数据的日子才有，日期天然稀疏
        vo2maxes: list[Vo2maxSample] = []
        vo2max_shards = dataset.find_all(VO2MAX_GLOB)
        for archive, name in vo2max_shards:
            vo2maxes.extend(parse_vo2max_samples(archive.read_json(name)))
        raw_vo2max = len(vo2maxes)
        vo2maxes = dedupe_vo2max(vo2maxes)
        if vo2max_shards:
            print(
                f"最大摄氧量 {len(vo2max_shards)} 个 → {raw_vo2max} 条"
                f"（同日合并后 {len(vo2maxes)} 天）"
            )

        # 个人最佳：一次导出只有一份，多次导出按 personalRecordId 去重
        personal: list[PersonalRecord] = []
        personal_shards = dataset.find_all(PERSONAL_GLOB)
        for archive, name in personal_shards:
            personal.extend(parse_records(archive.read_json(name)))
        raw_personal = len(personal)
        personal = dedupe_records(personal)
        if personal_shards:
            current = sum(1 for r in personal if r.current)
            print(
                f"个人最佳   {len(personal_shards)} 个 → {raw_personal} 条"
                f"（去重后 {len(personal)} 条，其中当前 {current} 条）"
            )

        # 装备：里程要靠关联活动求和，因此必须在活动之后解析
        gears: list[Gear] = []
        gear_shards = dataset.find_all(GEAR_GLOB)
        for archive, name in gear_shards:
            gears.extend(parse_gears(archive.read_json(name)))
        raw_gears = len(gears)
        gears = dedupe_gears(gears)
        if gear_shards:
            linked = sum(len(g.activity_ids) for g in gears)
            print(
                f"装备       {len(gear_shards)} 个 → {raw_gears} 双"
                f"（去重后 {len(gears)} 双，关联活动 {linked} 次）"
            )

        # 逐秒记录（FIT）：只读每个文件的 file_id 帧，约 2 秒就能拿到全部活动
        # 文件的定位；完整解析要 5 分钟，因此索引阶段不做，查询时按需解析。
        fit_rows: list[tuple[int, int, str | None]] = []
        try:
            fit_rows = scan_activity_fits(dataset)
        except (BadZipFile, ArchiveError):
            fit_rows = []
        if fit_rows:
            print(f"逐秒记录   {len(fit_rows)} 个活动 FIT（仅索引文件头）")
        else:
            print("逐秒记录   归档中没有内嵌的 .fit，逐秒工具将不可用")

        _hr("概览")
        print(f"活动总数   {len(deduped)} 条" + (
            f"（原始 {len(activities)} 条，去重 {len(activities) - len(deduped)} 条）"
            if len(activities) != len(deduped) else ""
        ))
        if dates:
            print(f"时间跨度   {dates[0].date()} → {dates[-1].date()}")
        print("运动类型   " + ", ".join(f"{k} {v}" for k, v in sports.most_common(8)))
        if days:
            print(f"每日汇总   {len(days)} 天（{days[0].date} → {days[-1].date}）")
        elif daily_shards:
            print("每日汇总   解析结果为空，跳过")
        else:
            print("每日汇总   归档中没有 UDSFile_*.json，跳过")
        if sleeps:
            staged = sum(1 for s in sleeps if s.status == STATUS_OK)
            print(f"睡眠记录   {len(sleeps)} 晚（有分期 {staged} 晚，其余为摘表或未确认）")
        elif sleep_shards:
            print("睡眠记录   解析结果为空，跳过")
        else:
            print("睡眠记录   归档中没有 *sleepData.json，跳过")
        if training:
            print(f"训练状态   {len(training)} 天（{training[0].date} → {training[-1].date}）")
        elif training_shards:
            print("训练状态   解析结果为空，跳过")
        else:
            print("训练状态   归档中没有 TrainingHistory_*.json，跳过")
        if races:
            print(f"比赛预测   {len(races)} 天（{races[0].date} → {races[-1].date}）")
        elif race_shards:
            print("比赛预测   解析结果为空，跳过")
        else:
            print("比赛预测   归档中没有 RunRacePredictions_*.json，跳过")
        if vo2maxes:
            print(f"最大摄氧量 {len(vo2maxes)} 天（{vo2maxes[0].date} → {vo2maxes[-1].date}）")
        elif vo2max_shards:
            print("最大摄氧量 解析结果为空，跳过")
        else:
            print("最大摄氧量 归档中没有 MetricsMaxMetData_*.json，跳过")
        if personal:
            print(f"个人最佳   {len(personal)} 条（当前有效 {sum(1 for r in personal if r.current)} 条）")
        elif personal_shards:
            print("个人最佳   解析结果为空，跳过")
        else:
            print("个人最佳   归档中没有 *personalRecord.json，跳过")
        if gears:
            print(f"装备       {len(gears)} 双")
        elif gear_shards:
            print("装备       解析结果为空，跳过")
        else:
            print("装备       归档中没有 *gear.json，跳过")

        with Store(cfg.db_path) as store:
            store.upsert_activities(deduped, source=dataset.archives[0].label)
            if days:
                store.upsert_days(days, source=dataset.archives[0].label)
            if sleeps:
                store.upsert_sleeps(sleeps, source=dataset.archives[0].label)
            if training:
                store.upsert_training(training, source=dataset.archives[0].label)
            if races:
                store.upsert_race_predictions(races, source=dataset.archives[0].label)
            if vo2maxes:
                store.upsert_vo2max(vo2maxes, source=dataset.archives[0].label)
            if personal:
                store.upsert_personal_records(personal, source=dataset.archives[0].label)
            if gears:
                store.upsert_gears(gears, source=dataset.archives[0].label)
            if fit_rows:
                store.upsert_fit_files(fit_rows)
            store.set_meta("indexed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            store.set_meta("source", dataset.archives[0].label)
            total = store.count_activities()
            total_days = store.count_days()
            total_sleeps = store.count_sleeps()
            total_training = store.count_training()
            total_races = store.count_race_predictions()
            total_vo2max = store.count_vo2max()
            total_personal = store.count_personal_records()
            total_gears = store.count_gears()
            total_fits = store.count_fit_files()

        _hr("索引")
        print(f"写入位置   {cfg.db_path}")
        print(f"库内活动   {total} 条")
        print(f"库内日汇总 {total_days} 天")
        print(f"库内睡眠   {total_sleeps} 晚")
        print(f"库内训练   {total_training} 天")
        print(f"库内预测   {total_races} 天")
        print(f"库内摄氧量 {total_vo2max} 天")
        print(f"库内纪录   {total_personal} 条")
        print(f"库内装备   {total_gears} 双")
        print(f"逐秒记录   {total_fits} 次活动可查（其余活动没有对应的 FIT 文件）")

    print(f"\n完成。隐私级别 {cfg.privacy}（strict 下坐标与地点不会进入 AI 上下文）")
    return 0


def cmd_doctor(cfg: Config) -> int:
    ok = True
    print(f"garmaple {__version__}")

    _hr("配置")
    print(f"数据目录   {cfg.data_dir}  {'✓' if cfg.data_dir.exists() else '✗ 不存在'}")
    print(f"缓存目录   {cfg.cache_dir}")
    print(f"隐私级别   {cfg.privacy}")
    ok &= cfg.data_dir.exists()

    _hr("数据")
    try:
        with discover(cfg.data_dir) as dataset:
            print(f"导出包     {len(dataset)} 个")
            shards = dataset.find_all(ACTIVITY_GLOB)
            print(f"活动分片   {len(shards)} 个")
            daily_shards = dataset.find_all(DAILY_GLOB)
            print(f"日汇总分片 {len(daily_shards)} 个")
            sleep_shards = dataset.find_all(SLEEP_GLOB)
            print(f"睡眠分片   {len(sleep_shards)} 个")
            training_shards = dataset.find_all(TRAINING_GLOB)
            print(f"训练分片   {len(training_shards)} 个")
            race_shards = dataset.find_all(RACE_GLOB)
            print(f"预测分片   {len(race_shards)} 个")
            vo2max_shards = dataset.find_all(VO2MAX_GLOB)
            print(f"摄氧量分片 {len(vo2max_shards)} 个")
            personal_shards = dataset.find_all(PERSONAL_GLOB)
            print(f"纪录分片   {len(personal_shards)} 个")
            gear_shards = dataset.find_all(GEAR_GLOB)
            print(f"装备分片   {len(gear_shards)} 个")
            fits = dataset.find_one("UploadedFiles_*.zip")
            if fits:
                with fits[0].nested(fits[1]) as nested:
                    n = len([x for x in nested.names if x.lower().endswith(".fit")])
                print(f"原始 FIT   {n} 个")
            ok &= bool(shards)
    except ArchiveError as exc:
        print(f"错误：{exc}")
        ok = False

    _hr("索引")
    if cfg.db_path.exists():
        with Store(cfg.db_path) as store:
            n = store.count_activities()
            lo, hi = store.date_range()
            print(f"活动       {n} 条")
            print(f"日期范围   {lo} → {hi}")
            nz = store.count_activities_with_zones()
            print(f"心率区间   {nz}/{n} 条活动有区间数据")
            nf = store.count_fit_files()
            if nf:
                print(f"逐秒记录   {nf}/{n} 条活动有 FIT 文件")
            else:
                print("逐秒记录   无（归档不含内嵌 .fit，或索引版本过旧需重跑 index）")
            nd = store.count_days()
            dlo, dhi = store.day_range()
            print(f"每日汇总   {nd} 天" + (f"（{dlo} → {dhi}）" if nd else ""))
            ns = store.count_sleeps()
            slo, shi = store.sleep_range()
            print(f"睡眠       {ns} 晚" + (f"（{slo} → {shi}）" if ns else ""))
            nt = store.count_training()
            tlo, thi = store.training_range()
            print(f"训练状态   {nt} 天" + (f"（{tlo} → {thi}）" if nt else ""))
            nr = store.count_race_predictions()
            rlo, rhi = store.race_range()
            print(f"比赛预测   {nr} 天" + (f"（{rlo} → {rhi}）" if nr else ""))
            nv = store.count_vo2max()
            vlo, vhi = store.vo2max_range()
            print(f"最大摄氧量 {nv} 天" + (f"（{vlo} → {vhi}）" if nv else ""))
            np_ = store.count_personal_records()
            print(f"个人最佳   {np_} 条")
            ng = store.count_gears()
            print(f"装备       {ng} 双")
            print(f"上次索引   {store.get_meta('indexed_at') or '未知'}")
            ok &= n > 0
    else:
        print("尚未建立索引，请先运行 garmaple index")
        ok = False

    print("\n" + ("一切就绪。" if ok else "存在问题，见上。"))
    return 0 if ok else 1


def cmd_serve(cfg: Config) -> int:
    try:
        from .server import run
    except ImportError as exc:
        print(f"无法加载 MCP 服务：{exc}", file=sys.stderr)
        return 1
    return run(cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="garmaple",
        description="读取佳明官方导出归档，在本地建立索引并通过 MCP 提供给 AI。",
    )
    p.add_argument("--version", action="version", version=f"garmaple {__version__}")
    p.add_argument("--data", help="导出包所在目录（默认 ./data，或环境变量 GARMAPLE_DATA）")
    p.add_argument("--cache", help="索引与缓存目录（默认 ./cache，或 GARMAPLE_CACHE）")
    p.add_argument(
        "--privacy",
        choices=["strict", "normal", "raw"],
        help="隐私级别（默认 strict，或 GARMAPLE_PRIVACY）",
    )

    sub = p.add_subparsers(dest="command")
    sub.add_parser("index", help="解析导出包并建立本地索引")
    sub.add_parser("doctor", help="自检数据、索引与隐私配置")
    sub.add_parser("serve", help="启动 MCP 服务（stdio）")
    return p


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)

    try:
        cfg = Config.load(data_dir=args.data, cache_dir=args.cache, privacy=args.privacy)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    command = args.command or "doctor"
    if command == "index":
        return cmd_index(cfg)
    if command == "doctor":
        return cmd_doctor(cfg)
    if command == "serve":
        return cmd_serve(cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
