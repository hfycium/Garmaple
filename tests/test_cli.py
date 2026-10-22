"""命令行入口测试。

用合成导出包（临时 zip）验证 index / doctor 的接线：
归档结构照抄真实包，但内容全是手写的小数据，不含任何个人记录。
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garmaple.cli import main
from garmaple.config import Config
from garmaple.store import Store

from .fit_factory import build_fit, sample_at
from .test_activities import make_raw
from .test_daily import make_block
from .test_metrics import make_prediction, make_vo2max
from .test_records import make_gear, make_gear_payload, make_raw as make_record
from .test_sleep import make_sleep
from .test_training import make_block as make_training

# make_raw 默认的开始时间；造 FIT 时必须与它对齐，否则定位不上
_ACTIVITY_START_MS = 1_740_000_000_000


def sleep_block(wake_day: str) -> dict[str, object]:
    """醒来日为 wake_day 的一晚：前一晚 23:20 入睡，当日 10:21 起床（本地 +8）。"""
    prev = (date.fromisoformat(wake_day) - timedelta(days=1)).isoformat()
    return make_sleep(
        calendarDate=wake_day,
        sleepStartTimestampGMT=f"{prev}T15:20:00.0",
        sleepEndTimestampGMT=f"{wake_day}T02:21:00.0",
    )


def build_archive(
    root: Path,
    activities: int = 2,
    days: int = 2,
    sleeps: int | None = None,
    training: int | None = None,
    races: int | None = None,
    vo2max: int | None = None,
    personal: int | None = None,
    gears: int | None = None,
    superseded: bool = False,
    fits: int = 0,
    workout_rpe: int | None = None,
    workout_feel: int | None = None,
    power_w: int | None = None,
) -> Path:
    """造一个最小导出包：活动 + 日汇总 + 睡眠 + 训练 + 预测 + 摄氧量 + 纪录 + 装备。

    `sleeps` / `training` / `races` / `vo2max` 为 None 时与 `days` 同天数；
    `personal` / `gears` 为 None 时各写 2 条。显式传 0 则不写对应分片，
    用于验证「缺这份数据也能正常索引」。

    `superseded=True` 时额外写一条被刷新过的旧纪录（current=false），
    用于验证「当前纪录」与「历史纪录」不会混在一起。
    """
    path = root / "export.zip"
    payload = [{"summarizedActivitiesExport": [
        # 每个活动相差 1 小时：这样第 i 个 FIT 能且只能对上第 i 个活动，
        # 「有活动没有 FIT」才测得出来
        make_raw(
            activityId=1000 + i,
            beginTimestamp=_ACTIVITY_START_MS + i * 3_600_000,
            startTimeLocal=_ACTIVITY_START_MS + i * 3_600_000 + 8 * 3_600_000,
        )
        for i in range(activities)
    ]}]
    daily = [
        make_block(calendarDate=f"2026-07-{20 + i:02d}") for i in range(days)
    ]
    sleep_count = days if sleeps is None else sleeps
    sleep = [
        sleep_block(f"2026-07-{20 + i:02d}") for i in range(sleep_count)
    ]
    train_count = days if training is None else training
    # 同一天写两条快照，复刻真实设备一天多次同步：晚的那条应当胜出
    train = [
        item
        for i in range(train_count)
        for item in (
            make_training(
                calendarDate=f"2026-07-{20 + i:02d}",
                timestamp=f"2026-07-{20 + i:02d}T00:00:00.0",
                weeklyTrainingLoadSum=100,
            ),
            make_training(
                calendarDate=f"2026-07-{20 + i:02d}",
                timestamp=f"2026-07-{20 + i:02d}T12:00:00.0",
                weeklyTrainingLoadSum=300,
            ),
        )
    ]
    race_count = days if races is None else races
    # 与训练状态同样的情形：同一天两条预测，数值不同，晚的那条才是定稿
    race = [
        item
        for i in range(race_count)
        for item in (
            make_prediction(
                calendarDate=f"2026-07-{20 + i:02d}",
                timestamp=f"2026-07-{20 + i:02d}T00:00:00.0",
                raceTime5K=1092,
            ),
            make_prediction(
                calendarDate=f"2026-07-{20 + i:02d}",
                timestamp=f"2026-07-{20 + i:02d}T12:00:00.0",
                raceTime5K=1086,
            ),
        )
    ]
    vo2_count = days if vo2max is None else vo2max
    vo2 = [
        make_vo2max(
            calendarDate=f"2026-07-{20 + i:02d}",
            updateTimestamp=f"2026-07-{20 + i:02d}T07:00:00.0",
        )
        for i in range(vo2_count)
    ]
    personal_count = 2 if personal is None else personal
    personal_items = [
        make_record(
            recordId=900001, recordType="Best 5km Run", value=1108.3,
            activityId=1000, created="2026-07-20",
        ),
        make_record(
            recordId=900002, recordType="Farthest Run", value=21116.4,
            activityId=1001, created="2026-07-21",
        ),
    ][:personal_count]
    if superseded:
        # 同类型的旧成绩：已经被上面那条 5K 刷新掉了
        personal_items.append(
            make_record(
                recordId=900003, recordType="Best 5km Run", value=1194.2,
                activityId=1000, created="2026-07-20", current=False,
            )
        )
    personal_block = [{"personalRecords": personal_items}]
    gear_count = 2 if gears is None else gears
    # 一双有关联、一双没有——复刻实测情形：有装备的关联键整个不存在
    gear_block = make_gear_payload(
        [
            make_gear(gearPk=700001, name="测试跑鞋甲"),
            make_gear(gearPk=700002, name="测试跑鞋乙"),
        ][:gear_count],
        {
            "700001": [
                {"gearPk": 700001, "activityId": 1000},
                {"gearPk": 700001, "activityId": 1001},
            ]
        },
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "DI_CONNECT/DI-Connect-Fitness/9000_1_summarizedActivities.json",
            json.dumps(payload),
        )
        zf.writestr(
            "DI_CONNECT/DI-Connect-Aggregator/UDSFile_2026-07-20_2026-07-30.json",
            json.dumps(daily),
        )
        if sleep:
            zf.writestr(
                "DI_CONNECT/DI-Connect-Wellness/2026-07-20_2026-07-30_9000_sleepData.json",
                json.dumps(sleep),
            )
        if train:
            zf.writestr(
                "DI_CONNECT/DI-Connect-Metrics/TrainingHistory_2026-07-20_2026-07-30_9000.json",
                json.dumps(train),
            )
        if race:
            zf.writestr(
                "DI_CONNECT/DI-Connect-Metrics/RunRacePredictions_2026-07-20_2026-07-30_999.json",
                json.dumps(race),
            )
        if vo2:
            zf.writestr(
                "DI_CONNECT/DI-Connect-Metrics/MetricsMaxMetData_2026-07-20_2026-07-30_999.json",
                json.dumps(vo2),
            )
        if personal_count:
            zf.writestr(
                "DI_CONNECT/DI-Connect-Fitness/9000_personalRecord.json",
                json.dumps(personal_block),
            )
        if gear_count:
            zf.writestr(
                "DI_CONNECT/DI-Connect-Fitness/9000_gear.json",
                json.dumps(gear_block),
            )
        if fits:
            # 内嵌 zip 里放逐秒记录。第 i 个 FIT 的开始时间与第 i 个活动对齐，
            # 因此 fits 小于活动数时，剩下的活动就是「有活动没有 FIT」的情形。
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as izf:
                for i in range(fits):
                    base = datetime.fromtimestamp(
                        (_ACTIVITY_START_MS + i * 3_600_000) / 1000, tz=timezone.utc
                    )
                    izf.writestr(
                        f"0000000000000_{i}.fit",
                        build_fit(
                            start=base,
                            workout_rpe=workout_rpe,
                            workout_feel=workout_feel,
                            samples=[
                                sample_at(
                                    base,
                                    sec,
                                    # 心率随时间缓慢上升（每 20 秒 +1），
                                    # 配速恒定——正是「配速没变但心率涨了」
                                    # 的有氧解耦形态
                                    hr=120 + sec // 20,
                                    speed_mps=3.0,
                                    altitude_m=20.0 + sec / 100,
                                    cadence=85,
                                    distance_m=3.0 * sec,
                                    power_w=power_w,
                                )
                                for sec in range(0, 1300, 2)
                            ],
                        ),
                    )
            zf.writestr(
                "DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip",
                inner.getvalue(),
            )
    return path


def make_config(tmp_path: Path, **kwargs: object) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, **kwargs)
    return Config(data_dir=data, cache_dir=tmp_path / "cache", privacy="strict")


def test_index_builds_both_tables(tmp_path: Path, capsys) -> None:
    cfg = make_config(tmp_path)
    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0

    out = capsys.readouterr().out
    assert "活动分片" in out and "日汇总分片" in out and "睡眠分片" in out
    assert "训练分片" in out and "比赛预测" in out and "最大摄氧量" in out
    assert "个人最佳" in out and "装备" in out
    assert "库内活动   2 条" in out
    assert "库内日汇总 2 天" in out
    assert "库内睡眠   2 晚" in out
    assert "库内训练   2 天" in out
    assert "库内预测   2 天" in out
    assert "库内摄氧量 2 天" in out
    assert "库内纪录   2 条" in out
    assert "库内装备   2 双" in out

    with Store(cfg.db_path) as store:
        assert store.count_activities() == 2
        assert store.count_days() == 2
        assert store.count_sleeps() == 2
        assert store.count_training() == 2
        assert store.count_race_predictions() == 2
        assert store.count_vo2max() == 2
        assert store.count_personal_records() == 2
        assert store.count_gears() == 2


def test_index_computes_gear_mileage_from_activities(tmp_path: Path) -> None:
    """装备里程是关联活动求和：两条各 10km，合计 20km。"""
    cfg = make_config(tmp_path)
    main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"])
    with Store(cfg.db_path) as store:
        usage = store.gear_usage()
    assert usage[700001].activity_count == 2
    assert usage[700001].distance_m == 20000.0
    # 第二双没有关联键，里程必须是「没有」而不是 0
    assert usage[700002].activity_count == 0
    assert usage[700002].distance_m is None


def test_index_without_personal_and_gear_shards_still_works(
    tmp_path: Path, capsys
) -> None:
    """没有纪录与装备时只跳过，不该整体失败。"""
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, personal=0, gears=0)
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache")

    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0
    out = capsys.readouterr().out
    assert "归档中没有 *personalRecord.json" in out
    assert "归档中没有 *gear.json" in out
    with Store(cfg.db_path) as store:
        assert store.count_personal_records() == 0
        assert store.count_gears() == 0
        assert store.count_activities() == 2


def test_index_merges_same_day_race_snapshots(tmp_path: Path) -> None:
    """同一天两条预测，入库只留一条，且是时间更晚的那条。"""
    cfg = make_config(tmp_path)
    main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"])
    with Store(cfg.db_path) as store:
        assert store.count_race_predictions() == 2
        assert [r["time_5k_s"] for r in store.query_race_predictions()] == [1086, 1086]


def test_index_without_metrics_shards_still_works(tmp_path: Path, capsys) -> None:
    """没有预测与摄氧量数据时只跳过，不该整体失败。"""
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, races=0, vo2max=0)
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache")

    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0
    out = capsys.readouterr().out
    assert "归档中没有 RunRacePredictions_*.json" in out
    assert "归档中没有 MetricsMaxMetData_*.json" in out
    with Store(cfg.db_path) as store:
        assert store.count_race_predictions() == 0
        assert store.count_vo2max() == 0
        assert store.count_activities() == 2


def test_index_merges_same_day_training_snapshots(tmp_path: Path) -> None:
    """同一天两条快照，入库只留一条，且是时间更晚的那条。"""
    cfg = make_config(tmp_path)
    main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"])
    with Store(cfg.db_path) as store:
        assert store.count_training() == 2
        assert [r["weekly_load"] for r in store.query_training()] == [300, 300]


def test_index_without_training_shard_still_works(tmp_path: Path, capsys) -> None:
    """没有训练状态数据时只跳过它，不该整体失败。"""
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, training=0)
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache")

    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0
    out = capsys.readouterr().out
    assert "归档中没有 TrainingHistory_*.json" in out
    with Store(cfg.db_path) as store:
        assert store.count_training() == 0
        assert store.count_days() == 2


def test_index_without_sleep_shard_still_works(tmp_path: Path, capsys) -> None:
    """没有睡眠数据时只跳过睡眠，不该整体失败。"""
    data = tmp_path / "data"
    data.mkdir()
    build_archive(data, sleeps=0)
    cfg = Config(data_dir=data, cache_dir=tmp_path / "cache")

    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "index"]) == 0
    out = capsys.readouterr().out
    assert "归档中没有 *sleepData.json" in out
    with Store(cfg.db_path) as store:
        assert store.count_sleeps() == 0
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
    assert "心率区间   2/2 条活动有区间数据" in out
    assert "每日汇总   2 天" in out
    assert "睡眠       2 晚" in out
    assert "训练状态   2 天" in out
    assert "比赛预测   2 天" in out
    assert "最大摄氧量 2 天" in out
    assert "个人最佳   2 条" in out
    assert "装备       2 双" in out
    assert "逐秒记录   无" in out
    assert "一切就绪" in out


def test_index_and_doctor_report_fit_coverage(tmp_path: Path, capsys) -> None:
    """归档里带逐秒记录时，index 与 doctor 都要报出覆盖了多少次活动。"""
    cfg = make_config(tmp_path, fits=1)
    args = ["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir)]

    assert main(args + ["index"]) == 0
    out = capsys.readouterr().out
    assert "逐秒记录   1 个活动 FIT" in out
    assert "逐秒记录   1 次活动可查" in out

    assert main(args + ["doctor"]) == 0
    assert "逐秒记录   1/2 条活动有 FIT 文件" in capsys.readouterr().out


def test_doctor_fails_before_index(tmp_path: Path, capsys) -> None:
    cfg = make_config(tmp_path)
    assert main(["--data", str(cfg.data_dir), "--cache", str(cfg.cache_dir), "doctor"]) == 1
    assert "尚未建立索引" in capsys.readouterr().out
