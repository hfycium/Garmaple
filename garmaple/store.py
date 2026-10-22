"""SQLite 索引。

把解析结果落到本地缓存，避免每次查询都去解压并重读 15MB 的 JSON。

设计取舍：
- 主键用原始 activityId，配合 UPSERT 实现幂等——重复 index 不会产生重复记录
- 敏感字段（坐标、地点、设备 ID）**照常入库**，因为缓存目录已被 .gitignore 排除，
  且本地库不联网；是否对外输出由 privacy 层在工具返回时决定
- 日期用本地钟面时间，因为用户问「上周跑了多少」时指的是本地的那一天
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .parser.activities import Activity
from .parser.daily import DailyStat
from .parser.metrics import RacePrediction, Vo2maxSample
from .parser.records import Gear, GearUsage, PersonalRecord, SportUsage
from .parser.sleep import STATUS_OK, SleepRecord
from .parser.training import TrainingDay
from .privacy import pseudo_activity_id

SCHEMA_VERSION = 10

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id      INTEGER PRIMARY KEY,
    pseudo_id        TEXT NOT NULL,
    sport            TEXT NOT NULL,
    name             TEXT,
    utc_start        TEXT,
    local_start      TEXT,
    date             TEXT,
    distance_m       REAL,
    duration_s       REAL,
    moving_s         REAL,
    elapsed_s        REAL,
    avg_hr           INTEGER,
    max_hr           INTEGER,
    calories         REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    aerobic_te       REAL,
    anaerobic_te     REAL,
    vo2max           REAL,
    avg_speed_mps    REAL,
    stride_length_cm REAL,
    cadence_spm      REAL,
    lap_count        INTEGER,
    hr_zone_0        INTEGER,
    hr_zone_1        INTEGER,
    hr_zone_2        INTEGER,
    hr_zone_3        INTEGER,
    hr_zone_4        INTEGER,
    hr_zone_5        INTEGER,
    hr_zone_6        INTEGER,
    location_name    TEXT,
    start_lat        REAL,
    start_lon        REAL,
    end_lat          REAL,
    end_lon          REAL,
    device_id        INTEGER,
    user_profile_id  INTEGER,
    source           TEXT
);

CREATE INDEX IF NOT EXISTS idx_activities_date   ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_sport  ON activities(sport);
CREATE INDEX IF NOT EXISTS idx_activities_pseudo ON activities(pseudo_id);
"""

# 每日汇总（UDS）。主键就是日历日，与 activities.date 同一套本地日期口径，
# 因此「某天有没有记录」可以直接和活动表对照。
_DAYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date                 TEXT PRIMARY KEY,
    steps                INTEGER,
    step_goal            INTEGER,
    distance_m           REAL,
    moderate_min         INTEGER,
    vigorous_min         INTEGER,
    intensity_goal_min   INTEGER,
    resting_hr           INTEGER,
    min_hr               INTEGER,
    max_hr               INTEGER,
    calories_total       REAL,
    calories_active      REAL,
    calories_bmr         REAL,
    stress_avg           INTEGER,
    stress_max           INTEGER,
    stress_off_wrist_min INTEGER,
    stress_rest_s        INTEGER,
    stress_low_s         INTEGER,
    stress_medium_s      INTEGER,
    stress_high_s        INTEGER,
    bb_charged           INTEGER,
    bb_drained           INTEGER,
    bb_high              INTEGER,
    bb_low               INTEGER,
    bb_start             INTEGER,
    bb_end               INTEGER,
    bb_high_at           TEXT,
    bb_low_at            TEXT,
    utc_offset_hours     REAL,
    user_profile_id      INTEGER,
    source               TEXT
);
"""

_DAY_COLUMNS = (
    "date", "steps", "step_goal", "distance_m",
    "moderate_min", "vigorous_min", "intensity_goal_min",
    "resting_hr", "min_hr", "max_hr",
    "calories_total", "calories_active", "calories_bmr",
    "stress_avg", "stress_max", "stress_off_wrist_min",
    "stress_rest_s", "stress_low_s", "stress_medium_s", "stress_high_s",
    "bb_charged", "bb_drained", "bb_high", "bb_low", "bb_start", "bb_end",
    "bb_high_at", "bb_low_at", "utc_offset_hours", "user_profile_id", "source",
)

# 睡眠。主键同样是日历日，但口径是**本地醒来日**：一晚的睡眠记在醒来那天。
# 与 days 表可以按 date 直接对照（实测两边都是 608 天、日期完全重合）。
# asleep_min 不入库——它是「窗口 − 清醒 − 测不到」推出来的，存一份等于留两份真相；
# 睡眠数据只有几百条，读取时算一次不影响性能。
_SLEEPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sleeps (
    date            TEXT PRIMARY KEY,
    start_at        TEXT,
    end_at          TEXT,
    in_bed_min      INTEGER,
    deep_min        INTEGER,
    light_min       INTEGER,
    rem_min         INTEGER,
    awake_min       INTEGER,
    unmeasured_min  INTEGER,
    respiration_avg REAL,
    respiration_low REAL,
    respiration_high REAL,
    status          TEXT NOT NULL,
    retro           INTEGER,
    source          TEXT
);
"""

_SLEEP_COLUMNS = (
    "date", "start_at", "end_at", "in_bed_min",
    "deep_min", "light_min", "rem_min", "awake_min", "unmeasured_min",
    "respiration_avg", "respiration_low", "respiration_high",
    "status", "retro", "source",
)

# 训练状态。主键同样是日历日，口径也是本地日（`calendarDate` 本身即本地）。
# load_position（在隧道内 / 低于 / 高于）不入库——它由 weekly_load 与隧道边界
# 推出来，和 sleeps.asleep_min 同理，存一份等于留两份真相。
_TRAINING_SCHEMA = """
CREATE TABLE IF NOT EXISTS training (
    date            TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    weekly_load     INTEGER,
    tunnel_min      INTEGER,
    tunnel_max      INTEGER,
    fitness_trend   TEXT,
    load_trend      TEXT,
    sport           TEXT,
    updated_at      TEXT,
    user_profile_id INTEGER,
    source          TEXT
);
"""

_TRAINING_COLUMNS = (
    "date", "status", "weekly_load", "tunnel_min", "tunnel_max",
    "fitness_trend", "load_trend", "sport", "updated_at",
    "user_profile_id", "source",
)

# 比赛成绩预测。四个距离各一列，缺哪个就留空——不用 0 占位，
# 0 秒与「设备没算这个距离」在分析里是两回事。
# 预测配速不入库：它由时间与标准距离推出来，存一份等于留两份真相。
_RACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS race_predictions (
    date            TEXT PRIMARY KEY,
    time_5k_s       INTEGER,
    time_10k_s      INTEGER,
    time_half_s     INTEGER,
    time_marathon_s INTEGER,
    updated_at      TEXT,
    user_profile_id INTEGER,
    source          TEXT
);
"""

_RACE_COLUMNS = (
    "date", "time_5k_s", "time_10k_s", "time_half_s", "time_marathon_s",
    "updated_at", "user_profile_id", "source",
)

# 最大摄氧量。日期是主键，不是「日期 + 运动项目」：实测同日只会有一个项目，
# 按日期收敛即可；哪天数据里真的并存两个项目，这里会丢掉一条而不是给出两行，
# 所以解析层把 sport 当描述字段，不当维度。
_VO2MAX_SCHEMA = """
CREATE TABLE IF NOT EXISTS vo2max (
    date            TEXT PRIMARY KEY,
    vo2max          REAL NOT NULL,
    sport           TEXT,
    updated_at      TEXT,
    user_profile_id INTEGER,
    source          TEXT
);
"""

_VO2MAX_COLUMNS = (
    "date", "vo2max", "sport", "updated_at", "user_profile_id", "source",
)

# 个人最佳。主键是设备给的 personalRecordId，不是日期——同一天可能刷新多条，
# 同一类型也会有「当前纪录」与「被刷新的旧纪录」并存，按日期做键会互相覆盖。
# 单位、中文标签、分组都不入库：它们由 type 查表推出来，存一份等于留两份真相。
_PERSONAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_records (
    record_id       INTEGER PRIMARY KEY,
    type            TEXT NOT NULL,
    value           REAL NOT NULL,
    date            TEXT,
    current         INTEGER NOT NULL DEFAULT 1,
    user_profile_id INTEGER,
    source          TEXT
);

CREATE INDEX IF NOT EXISTS idx_personal_type ON personal_records(type);
"""

_PERSONAL_COLUMNS = (
    "record_id", "type", "value", "date", "current", "user_profile_id", "source",
)

# 装备。装备本身没有累计里程字段，里程靠 gear_activities 关联活动求和，
# 所以这里不存 distance——活动表变了，里程自然跟着变，不会有两份互相矛盾的数。
_GEAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS gears (
    gear_id         INTEGER PRIMARY KEY,
    name            TEXT,
    gear_type       TEXT,
    status          TEXT,
    started_on      TEXT,
    max_meters      REAL,
    user_profile_id INTEGER,
    source          TEXT
);

CREATE TABLE IF NOT EXISTS gear_activities (
    gear_id   INTEGER NOT NULL,
    pseudo_id TEXT NOT NULL,
    PRIMARY KEY (gear_id, pseudo_id)
);

CREATE INDEX IF NOT EXISTS idx_gear_activities_pseudo ON gear_activities(pseudo_id);
"""

_GEAR_COLUMNS = (
    "gear_id", "name", "gear_type", "status", "started_on", "max_meters",
    "user_profile_id", "source",
)

# 逐秒记录（FIT）的定位表。**只存下标，不存文件名**——归档里的 .fit 文件名
# 形如 `<设备序列号>_<内部编号>.fit`，序列号是身份标识，没有理由落到库里。
# `archive_idx` 是归档在 Dataset.archives 里的序号（discover 按名字排序，
# 顺序稳定）；`entry_idx` 是该归档内嵌 zip 里第几个 .fit（zip 条目顺序同样稳定）。
# 定位方式见 parser/fit.py：file_id.type == "activity" 正好圈出全部活动文件。
#
# time_created 不等于活动开始时间（设备在保存文件时才写），实测落在活动区间
# 附近，因此只用于缩小候选，真正对齐仍要靠 session.start_time（存 utc_start）。
#
# tss / intensity_factor 来自 FIT session 的标准字段（号 35 / 36）。
# 本机设备没写过（全空），但格式里有——有功率计的导出会写满。
# utc_start 是 session.start_time（FIT epoch 转 aware UTC），用于和活动按
# ±1 秒匹配得到本地日期，算 CTL / ATL / TSB 的每日 TSS 序列。
_FIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS fit_files (
    archive_idx       INTEGER NOT NULL,
    entry_idx         INTEGER NOT NULL,
    time_created      TEXT,
    utc_start         TEXT,
    tss               REAL,
    intensity_factor  REAL,
    PRIMARY KEY (archive_idx, entry_idx)
);

CREATE INDEX IF NOT EXISTS idx_fit_files_created ON fit_files(time_created);
"""

_COLUMNS = (
    "activity_id", "pseudo_id", "sport", "name", "utc_start", "local_start", "date",
    "distance_m", "duration_s", "moving_s", "elapsed_s",
    "avg_hr", "max_hr", "calories",
    "elevation_gain_m", "elevation_loss_m",
    "aerobic_te", "anaerobic_te", "vo2max",
    "avg_speed_mps", "stride_length_cm", "cadence_spm", "lap_count",
    "hr_zone_0", "hr_zone_1", "hr_zone_2", "hr_zone_3",
    "hr_zone_4", "hr_zone_5", "hr_zone_6",
    "location_name", "start_lat", "start_lon", "end_lat", "end_lon",
    "device_id", "user_profile_id", "source",
)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def row_to_activity(row: sqlite3.Row) -> Activity:
    """把索引里的行还原成 Activity，交给 privacy 层统一格式化。

    多绕一步而不是直接拼 dict，是为了让脱敏规则只有一处实现。
    """
    return Activity(
        activity_id=row["activity_id"],
        sport=row["sport"],
        name=row["name"],
        utc_start=_parse_dt(row["utc_start"]),
        local_start=_parse_dt(row["local_start"]),
        distance_m=row["distance_m"],
        duration_s=row["duration_s"],
        moving_s=row["moving_s"],
        elapsed_s=row["elapsed_s"],
        avg_hr=row["avg_hr"],
        max_hr=row["max_hr"],
        calories=row["calories"],
        elevation_gain_m=row["elevation_gain_m"],
        elevation_loss_m=row["elevation_loss_m"],
        aerobic_te=row["aerobic_te"],
        anaerobic_te=row["anaerobic_te"],
        vo2max=row["vo2max"],
        avg_speed_mps=row["avg_speed_mps"],
        stride_length_cm=row["stride_length_cm"],
        cadence_spm=row["cadence_spm"],
        lap_count=row["lap_count"],
        hr_zone_seconds=[row[f"hr_zone_{i}"] or 0 for i in range(7)],
        location_name=row["location_name"],
        start_lat=row["start_lat"],
        start_lon=row["start_lon"],
        end_lat=row["end_lat"],
        end_lon=row["end_lon"],
        device_id=row["device_id"],
        user_profile_id=row["user_profile_id"],
    )


def _row_tuple(act: Activity, source: str) -> tuple[object, ...]:
    local_date = act.local_start.date().isoformat() if act.local_start else None
    zones = (act.hr_zone_seconds + [None] * 7)[:7]
    return (
        act.activity_id, pseudo_activity_id(act.activity_id), act.sport, act.name,
        _iso(act.utc_start), _iso(act.local_start), local_date,
        act.distance_m, act.duration_s, act.moving_s, act.elapsed_s,
        act.avg_hr, act.max_hr, act.calories,
        act.elevation_gain_m, act.elevation_loss_m,
        act.aerobic_te, act.anaerobic_te, act.vo2max,
        act.avg_speed_mps, act.stride_length_cm, act.cadence_spm, act.lap_count,
        *zones,
        act.location_name, act.start_lat, act.start_lon, act.end_lat, act.end_lon,
        act.device_id, act.user_profile_id, source,
    )


def _day_tuple(day: DailyStat, source: str) -> tuple[object, ...]:
    return (
        day.date, day.steps, day.step_goal, day.distance_m,
        day.moderate_min, day.vigorous_min, day.intensity_goal_min,
        day.resting_hr, day.min_hr, day.max_hr,
        day.calories_total, day.calories_active, day.calories_bmr,
        day.stress_avg, day.stress_max, day.stress_off_wrist_min,
        day.stress_rest_s, day.stress_low_s, day.stress_medium_s, day.stress_high_s,
        day.bb_charged, day.bb_drained, day.bb_high, day.bb_low,
        day.bb_start, day.bb_end,
        _iso(day.bb_high_at), _iso(day.bb_low_at),
        day.utc_offset_hours,
        day.user_profile_id, source,
    )


def _sleep_tuple(rec: SleepRecord, source: str) -> tuple[object, ...]:
    return (
        rec.date, _iso(rec.start_at), _iso(rec.end_at), rec.in_bed_min,
        rec.deep_min, rec.light_min, rec.rem_min, rec.awake_min, rec.unmeasured_min,
        rec.respiration_avg, rec.respiration_low, rec.respiration_high,
        rec.status, 1 if rec.retro else 0, source,
    )


def row_to_daily(row: sqlite3.Row) -> DailyStat:
    """把 days 表的行还原成 DailyStat，供 privacy 层统一格式化。"""
    return DailyStat(
        date=row["date"],
        steps=row["steps"],
        step_goal=row["step_goal"],
        distance_m=row["distance_m"],
        moderate_min=row["moderate_min"],
        vigorous_min=row["vigorous_min"],
        intensity_goal_min=row["intensity_goal_min"],
        resting_hr=row["resting_hr"],
        min_hr=row["min_hr"],
        max_hr=row["max_hr"],
        calories_total=row["calories_total"],
        calories_active=row["calories_active"],
        calories_bmr=row["calories_bmr"],
        stress_avg=row["stress_avg"],
        stress_max=row["stress_max"],
        stress_off_wrist_min=row["stress_off_wrist_min"],
        stress_rest_s=row["stress_rest_s"],
        stress_low_s=row["stress_low_s"],
        stress_medium_s=row["stress_medium_s"],
        stress_high_s=row["stress_high_s"],
        bb_charged=row["bb_charged"],
        bb_drained=row["bb_drained"],
        bb_high=row["bb_high"],
        bb_low=row["bb_low"],
        bb_start=row["bb_start"],
        bb_end=row["bb_end"],
        bb_high_at=_parse_dt(row["bb_high_at"]),
        bb_low_at=_parse_dt(row["bb_low_at"]),
        utc_offset_hours=row["utc_offset_hours"],
        user_profile_id=row["user_profile_id"],
    )


def row_to_sleep(row: sqlite3.Row) -> SleepRecord:
    """把 sleeps 表的行还原成 SleepRecord。

    `asleep_min` 不在表里，由 SleepRecord 自己从窗口与清醒时长推出来——
    所以这里必须把分段值原样带上，不能省。
    """
    rec = SleepRecord(
        date=row["date"],
        start_at=_parse_dt(row["start_at"]),
        end_at=_parse_dt(row["end_at"]),
        in_bed_min=row["in_bed_min"],
        deep_min=row["deep_min"],
        light_min=row["light_min"],
        rem_min=row["rem_min"],
        awake_min=row["awake_min"],
        unmeasured_min=row["unmeasured_min"],
        respiration_avg=row["respiration_avg"],
        respiration_low=row["respiration_low"],
        respiration_high=row["respiration_high"],
        status=row["status"],
        retro=bool(row["retro"]),
    )
    # 睡着时长走的是秒级口径，入库时没存；用分钟级字段重建即可，
    # 误差不超过各字段取整的累积（实测 ≤1 分钟），不影响展示
    if rec.status == STATUS_OK and rec.in_bed_min is not None:
        rec._asleep_seconds = int(
            round((rec.in_bed_min - (rec.awake_min or 0) - (rec.unmeasured_min or 0)) * 60)
        )
    return rec


def _training_tuple(rec: TrainingDay, source: str) -> tuple[object, ...]:
    return (
        rec.date, rec.status, rec.weekly_load, rec.tunnel_min, rec.tunnel_max,
        rec.fitness_trend, rec.load_trend, rec.sport, _iso(rec.updated_at),
        rec.user_profile_id, source,
    )


def row_to_training(row: sqlite3.Row) -> TrainingDay:
    """把 training 表的行还原成 TrainingDay，供 privacy 层统一格式化。

    `status_label` / `load_position` 都是属性，不在这里补——派生值只留一处实现。
    """
    return TrainingDay(
        date=row["date"],
        status=row["status"],
        weekly_load=row["weekly_load"],
        tunnel_min=row["tunnel_min"],
        tunnel_max=row["tunnel_max"],
        fitness_trend=row["fitness_trend"],
        load_trend=row["load_trend"],
        sport=row["sport"],
        updated_at=_parse_dt(row["updated_at"]),
        user_profile_id=row["user_profile_id"],
    )


def _race_tuple(rec: RacePrediction, source: str) -> tuple[object, ...]:
    return (
        rec.date, rec.time_5k_s, rec.time_10k_s, rec.time_half_s,
        rec.time_marathon_s, _iso(rec.updated_at), rec.user_profile_id, source,
    )


def row_to_race(row: sqlite3.Row) -> RacePrediction:
    """把 race_predictions 表的行还原成 RacePrediction。

    配速是 `pace_s_per_km()` 属性算的，不入库、也不在这里补。
    """
    return RacePrediction(
        date=row["date"],
        time_5k_s=row["time_5k_s"],
        time_10k_s=row["time_10k_s"],
        time_half_s=row["time_half_s"],
        time_marathon_s=row["time_marathon_s"],
        updated_at=_parse_dt(row["updated_at"]),
        user_profile_id=row["user_profile_id"],
    )


def _vo2max_tuple(rec: Vo2maxSample, source: str) -> tuple[object, ...]:
    return (
        rec.date, rec.vo2max, rec.sport, _iso(rec.updated_at),
        rec.user_profile_id, source,
    )


def row_to_vo2max(row: sqlite3.Row) -> Vo2maxSample:
    """把 vo2max 表的行还原成 Vo2maxSample。"""
    return Vo2maxSample(
        date=row["date"],
        vo2max=row["vo2max"],
        sport=row["sport"],
        updated_at=_parse_dt(row["updated_at"]),
        user_profile_id=row["user_profile_id"],
    )


def _personal_tuple(rec: PersonalRecord, source: str) -> tuple[object, ...]:
    return (
        rec.record_id, rec.type, rec.value, rec.date,
        1 if rec.current else 0, rec.user_profile_id, source,
    )


def row_to_personal(row: sqlite3.Row) -> PersonalRecord:
    """把 personal_records 表的行还原成 PersonalRecord。

    `unit` / `label` / `group` 是由 type 查表得到的属性，不在这里补。
    """
    return PersonalRecord(
        record_id=row["record_id"],
        type=row["type"],
        value=row["value"],
        date=row["date"],
        current=bool(row["current"]),
        user_profile_id=row["user_profile_id"],
    )


def _gear_tuple(gear: Gear, source: str) -> tuple[object, ...]:
    return (
        gear.gear_id, gear.name, gear.gear_type, gear.status, gear.started_on,
        gear.max_meters, gear.user_profile_id, source,
    )


def row_to_gear(row: sqlite3.Row) -> Gear:
    """把 gears 表的行还原成 Gear。

    关联的 activityId 不回填：查询里程走 `gear_usage()` 的聚合，
    把几百个 ID 读进内存再重算一遍没有意义。
    """
    return Gear(
        gear_id=row["gear_id"],
        name=row["name"],
        gear_type=row["gear_type"],
        status=row["status"],
        started_on=row["started_on"],
        max_meters=row["max_meters"],
        user_profile_id=row["user_profile_id"],
    )


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_META_SCHEMA)
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_DAYS_SCHEMA)
        self._conn.executescript(_SLEEPS_SCHEMA)
        self._conn.executescript(_TRAINING_SCHEMA)
        self._conn.executescript(_RACE_SCHEMA)
        self._conn.executescript(_VO2MAX_SCHEMA)
        self._conn.executescript(_PERSONAL_SCHEMA)
        self._conn.executescript(_GEAR_SCHEMA)
        self._conn.executescript(_FIT_SCHEMA)
        self._conn.commit()

    # 结构自愈的判据：每张表缺了这些列就视为旧表
    _REQUIRED_COLUMNS = {
        "activities": frozenset({"pseudo_id", "sport", "date", "distance_m"}),
        "days": frozenset({"date", "steps", "resting_hr", "stress_avg", "utc_offset_hours"}),
        "sleeps": frozenset({"date", "in_bed_min", "status", "deep_min"}),
        "training": frozenset({"date", "status", "weekly_load", "tunnel_min"}),
        "race_predictions": frozenset({"date", "time_5k_s", "time_half_s"}),
        "vo2max": frozenset({"date", "vo2max"}),
        "personal_records": frozenset({"record_id", "type", "value", "current"}),
        "gears": frozenset({"gear_id", "name", "max_meters"}),
        "gear_activities": frozenset({"gear_id", "pseudo_id"}),
        "fit_files": frozenset({"archive_idx", "entry_idx", "time_created", "utc_start", "tss", "intensity_factor"}),
    }

    def _migrate(self) -> None:
        """索引随时可从 data/ 重建，因此结构不符时直接丢弃旧表，不做增量迁移。

        判据有两条，缺一不可：

        1. **实际列结构**（`PRAGMA table_info`）。版本号可能被中途写入，一旦
           元数据写着新版本而表仍是旧的，只查版本号就永远修不好
           （曾因此出现 `no such column: pseudo_id`）。
        2. **版本号本身**。列结构没变但「列里存的东西」变了时，只有版本号能
           发现：hr_zone_* 从毫秒改存秒就是这种情况，改名、改列都没发生，
           不按版本号重建就会继续把旧单位当成新单位读。

        丢弃后需要重新跑 `garmaple index`——工具输出里的 hint 会提示这一点。
        """
        stored = self.get_meta("schema_version")
        if stored is not None and stored != str(SCHEMA_VERSION):
            for table in self._REQUIRED_COLUMNS:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")

        for table, required in self._REQUIRED_COLUMNS.items():
            exists = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                continue
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(cols):
                self._conn.executescript(f"DROP TABLE IF EXISTS {table};")
        self._conn.commit()
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 写入 ----------

    def upsert_activities(self, activities: Iterable[Activity], source: str) -> int:
        """幂等写入，返回实际写入的行数。"""
        placeholders = ", ".join("?" * len(_COLUMNS))
        sql = (
            f"INSERT INTO activities ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(activity_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "activity_id")
        )
        rows = [_row_tuple(a, source) for a in activities]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_days(self, days: Iterable[DailyStat], source: str) -> int:
        """幂等写入每日汇总，返回写入的行数。"""
        placeholders = ", ".join("?" * len(_DAY_COLUMNS))
        sql = (
            f"INSERT INTO days ({', '.join(_DAY_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(date) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _DAY_COLUMNS if c != "date")
        )
        rows = [_day_tuple(d, source) for d in days]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_sleeps(self, records: Iterable[SleepRecord], source: str) -> int:
        """幂等写入睡眠记录，返回写入的行数。"""
        placeholders = ", ".join("?" * len(_SLEEP_COLUMNS))
        sql = (
            f"INSERT INTO sleeps ({', '.join(_SLEEP_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(date) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _SLEEP_COLUMNS if c != "date")
        )
        rows = [_sleep_tuple(r, source) for r in records]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_training(self, records: Iterable[TrainingDay], source: str) -> int:
        """幂等写入训练状态，返回写入的行数。"""
        placeholders = ", ".join("?" * len(_TRAINING_COLUMNS))
        sql = (
            f"INSERT INTO training ({', '.join(_TRAINING_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(date) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _TRAINING_COLUMNS if c != "date")
        )
        rows = [_training_tuple(r, source) for r in records]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_race_predictions(
        self, records: Iterable[RacePrediction], source: str
    ) -> int:
        """幂等写入比赛预测，返回写入的行数。"""
        placeholders = ", ".join("?" * len(_RACE_COLUMNS))
        sql = (
            f"INSERT INTO race_predictions ({', '.join(_RACE_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(date) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _RACE_COLUMNS if c != "date")
        )
        rows = [_race_tuple(r, source) for r in records]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_vo2max(self, records: Iterable[Vo2maxSample], source: str) -> int:
        """幂等写入最大摄氧量，返回写入的行数。"""
        placeholders = ", ".join("?" * len(_VO2MAX_COLUMNS))
        sql = (
            f"INSERT INTO vo2max ({', '.join(_VO2MAX_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(date) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _VO2MAX_COLUMNS if c != "date")
        )
        rows = [_vo2max_tuple(r, source) for r in records]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_personal_records(
        self, records: Iterable[PersonalRecord], source: str
    ) -> int:
        """幂等写入个人最佳，返回写入的行数。"""
        placeholders = ", ".join("?" * len(_PERSONAL_COLUMNS))
        sql = (
            f"INSERT INTO personal_records ({', '.join(_PERSONAL_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT(record_id) DO UPDATE SET "
            + ", ".join(
                f"{c}=excluded.{c}" for c in _PERSONAL_COLUMNS if c != "record_id"
            )
        )
        rows = [_personal_tuple(r, source) for r in records]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def upsert_gears(self, gears: Iterable[Gear], source: str) -> int:
        """幂等写入装备与它的活动关联，返回写入的装备数。

        关联表用伪 ID 而不是原始 activityId：与 activities 表同一套键，
        求里程时可以直接 join，库里也不必再留一份真实 ID。

        每双鞋的关联先清空再写回。装备的关联会变（换鞋、解绑、重新绑定），
        只增不减会让里程永远偏高，而这种偏差没有任何地方会报错。
        """
        items = list(gears)
        if not items:
            return 0

        placeholders = ", ".join("?" * len(_GEAR_COLUMNS))
        sql = (
            f"INSERT INTO gears ({', '.join(_GEAR_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(gear_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _GEAR_COLUMNS if c != "gear_id")
        )
        rows = [_gear_tuple(g, source) for g in items]

        with self._conn:
            self._conn.executemany(sql, rows)
            for gear in items:
                self._conn.execute(
                    "DELETE FROM gear_activities WHERE gear_id=?", (gear.gear_id,)
                )
                links = [
                    (gear.gear_id, pseudo_activity_id(aid))
                    for aid in gear.activity_ids
                ]
                if links:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO gear_activities (gear_id, pseudo_id) "
                        "VALUES (?, ?)",
                        links,
                    )
        return len(items)

    def upsert_fit_files(
        self,
        rows: Iterable[tuple[int, int, str | None, str | None, float | None, float | None]],
    ) -> int:
        """写入逐秒记录的定位与负荷表。

        元素为 (archive_idx, entry_idx, time_created, utc_start, tss, intensity_factor)。
        整表重建而不是增量：条目下标只在当次扫描里有意义，多个归档包的顺序
        也可能因为新增包而变化，留着旧行会指到错误的文件上。
        """
        items = list(rows)
        with self._conn:
            self._conn.execute("DELETE FROM fit_files")
            if items:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO fit_files "
                    "(archive_idx, entry_idx, time_created, utc_start, tss, intensity_factor) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    items,
                )
        return len(items)

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ---------- 查询 ----------

    def count_activities(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"]

    def count_activities_with_zones(self) -> int:
        """有任意一个心率区间时长的活动数。

        用于回答「区间分析覆盖了多少次训练」——分母是它，不是活动总数。
        """
        cols = " + ".join(f"COALESCE(hr_zone_{i}, 0)" for i in range(7))
        return self._conn.execute(
            f"SELECT COUNT(*) AS n FROM activities WHERE {cols} > 0"
        ).fetchone()["n"]

    def count_days(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]

    def count_fit_files(self) -> int:
        """有逐秒记录的活动文件数。用来判断要不要提示重跑 index。"""
        return self._conn.execute("SELECT COUNT(*) AS n FROM fit_files").fetchone()["n"]

    def count_fit_loads(self) -> int:
        """有 TSS 数据的 FIT 文件数。用来判断 get_training_load 是否有输入。"""
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM fit_files WHERE tss IS NOT NULL"
        ).fetchone()["n"]

    def daily_tss(self) -> list[tuple[str, float]]:
        """按本地日期汇总 TSS，用于 CTL / ATL / TSB 计算。

        FIT 的 `session.start_time`（存 utc_start）与活动摘要的 `utc_start`
        按 ±1 秒容差匹配（`_same_moment` 同款逻辑），取活动的本地 `date` 做键。
        匹配不上的 TSS 用 UTC 日期兜底——宁可日期略有偏差也不要丢负荷。
        """
        fits = self._conn.execute(
            "SELECT utc_start, tss FROM fit_files "
            "WHERE tss IS NOT NULL AND utc_start IS NOT NULL"
        ).fetchall()
        if not fits:
            return []

        acts = self._conn.execute(
            "SELECT utc_start, date FROM activities WHERE utc_start IS NOT NULL"
        ).fetchall()

        def _epoch(text: str | None) -> int | None:
            if not text:
                return None
            try:
                dt = datetime.fromisoformat(str(text))
            except ValueError:
                return None
            return int(dt.timestamp())

        act_map: dict[int, str] = {}
        for a in acts:
            ts = _epoch(a["utc_start"])
            if ts is not None and a["date"]:
                act_map[ts] = a["date"]

        by_date: dict[str, float] = {}
        for f in fits:
            ts = _epoch(f["utc_start"])
            if ts is None:
                continue
            date_str = (
                act_map.get(ts)
                or act_map.get(ts - 1)
                or act_map.get(ts + 1)
            )
            if date_str is None:
                # 兜底：用 UTC 日期。比丢掉负荷好
                date_str = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            by_date[date_str] = by_date.get(date_str, 0.0) + (f["tss"] or 0.0)

        return sorted(by_date.items())

    def fit_candidates(
        self, time_from: datetime, time_to: datetime
    ) -> list[sqlite3.Row]:
        """取 time_created 落在区间内的活动 FIT 条目。

        `time_created` 是设备保存文件的时刻，不是开始时间：实测它总是落在
        活动区间附近（最晚比结束晚 37 分钟，最早在活动进行中），所以窗口要
        留足余量，命中后再用 `session.start_time` 精确确认。
        """
        return self._conn.execute(
            "SELECT archive_idx, entry_idx FROM fit_files "
            "WHERE time_created IS NOT NULL AND time_created BETWEEN ? AND ? "
            "ORDER BY time_created",
            (_iso(time_from), _iso(time_to)),
        ).fetchall()

    def fit_entries(self) -> list[sqlite3.Row]:
        """全部活动 FIT 条目，候选窗口没命中时用来兜底全扫。"""
        return self._conn.execute(
            "SELECT archive_idx, entry_idx FROM fit_files ORDER BY archive_idx, entry_idx"
        ).fetchall()

    def count_sleeps(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM sleeps").fetchone()["n"]

    def count_training(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM training").fetchone()["n"]

    def count_race_predictions(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM race_predictions"
        ).fetchone()["n"]

    def count_vo2max(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM vo2max").fetchone()["n"]

    def count_personal_records(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM personal_records"
        ).fetchone()["n"]

    def count_gears(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM gears").fetchone()["n"]

    def _query_by_date(
        self,
        table: str,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[sqlite3.Row]:
        """按日期区间（含端点）取行，表必须是「date 为主键」的两张之一。

        `order="desc"` 配合 limit 时先取最近 N 行再翻回正序，
        语义才是「最近 N 天」按时间正序排列。
        """
        clauses: list[str] = []
        params: list[object] = []
        if start is not None:
            clauses.append("date >= ?")
            params.append(str(start))
        if end is not None:
            clauses.append("date <= ?")
            params.append(str(end))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        descending = order.lower() == "desc"
        sql = f"SELECT * FROM {table} {where} ORDER BY date {'DESC' if descending else 'ASC'}"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        # 只有「降序 + 限条数」才翻回正序：此时取的是「最近 N 天」，
        # 调用方要的是这 N 天按时间排列。单纯 desc 仍返回降序。
        return list(reversed(rows)) if descending and limit else rows

    def query_days(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[sqlite3.Row]:
        """按日期区间（含端点）取每日汇总。

        默认升序：日汇总的主要用途是看趋势，时间正序才读得出来。
        """
        return self._query_by_date("days", start, end, limit, order)

    def query_sleeps(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[sqlite3.Row]:
        """按日期区间取睡眠记录。日期口径是本地**醒来日**。"""
        return self._query_by_date("sleeps", start, end, limit, order)

    def query_training(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[sqlite3.Row]:
        """按日期区间取训练状态。一天至多一行（同日多快照已在解析层去掉）。"""
        return self._query_by_date("training", start, end, limit, order)

    def query_race_predictions(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[sqlite3.Row]:
        """按日期区间取比赛预测。一天至多一行。"""
        return self._query_by_date("race_predictions", start, end, limit, order)

    def query_vo2max(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[sqlite3.Row]:
        """按日期区间取最大摄氧量。日期很稀疏，不要按天补值。"""
        return self._query_by_date("vo2max", start, end, limit, order)

    def query_personal_records(self, current: bool | None = None) -> list[sqlite3.Row]:
        """取个人最佳。`current` 为 None 时全给，True/False 只取相应的一类。

        不按日期排序——这些纪录没有统一的日期口径，排序交给调用方按类别做。
        """
        sql = "SELECT * FROM personal_records"
        params: list[object] = []
        if current is not None:
            sql += " WHERE current = ?"
            params.append(1 if current else 0)
        sql += " ORDER BY type, value"
        return self._conn.execute(sql, params).fetchall()

    def query_gears(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM gears ORDER BY gear_id").fetchall()

    def gear_usage(self) -> dict[int, GearUsage]:
        """每双装备的累计使用情况。

        LEFT JOIN 让没有关联活动的装备仍然出现。这时 `activity_count` 是 0、
        `distance_m` 是 None——「归档里没有关联记录」与「累计跑了 0 公里」
        必须分得开，实测确实有一双鞋整个关联键都不存在。

        `assigned_count` 是关联表里声明的条数。它与 `activity_count` 不等时
        说明有关联的活动不在活动摘要里（导出包不完整或活动已删），
        此时里程是偏小的，输出层要能说出来。
        """
        usage: dict[int, GearUsage] = {
            row["gear_id"]: GearUsage() for row in self.query_gears()
        }

        for row in self._conn.execute(
            "SELECT gear_id, COUNT(*) AS n FROM gear_activities GROUP BY gear_id"
        ):
            gear = usage.get(row["gear_id"])
            if gear is not None:
                gear.assigned_count = row["n"]

        for row in self._conn.execute(
            """
            SELECT ga.gear_id,
                   COUNT(a.activity_id) AS n,
                   SUM(a.distance_m)    AS d,
                   MIN(a.date)          AS lo,
                   MAX(a.date)          AS hi
            FROM gear_activities ga
            JOIN activities a ON a.pseudo_id = ga.pseudo_id
            GROUP BY ga.gear_id
            """
        ):
            gear = usage.get(row["gear_id"])
            if gear is None:
                continue
            gear.activity_count = row["n"]
            gear.distance_m = row["d"]
            gear.first_date = row["lo"]
            gear.last_date = row["hi"]

        for row in self._conn.execute(
            """
            SELECT ga.gear_id, a.sport, COUNT(*) AS n, SUM(a.distance_m) AS d
            FROM gear_activities ga
            JOIN activities a ON a.pseudo_id = ga.pseudo_id
            GROUP BY ga.gear_id, a.sport
            ORDER BY n DESC
            """
        ):
            gear = usage.get(row["gear_id"])
            if gear is None:
                continue
            gear.by_sport[row["sport"]] = SportUsage(
                count=row["n"], distance_m=row["d"] or 0.0
            )

        return usage

    def _range(self, table: str) -> tuple[str | None, str | None]:
        """取某张日期表的最小 / 最大日期。

        五张表都是「date 为主键 + 本地日」，覆盖范围问的是同一件事，
        所以做成一处实现——复制五遍迟早有一处写错表名。
        """
        row = self._conn.execute(
            f"SELECT MIN(date) AS lo, MAX(date) AS hi FROM {table}"
        ).fetchone()
        return (row["lo"], row["hi"]) if row else (None, None)

    def day_range(self) -> tuple[str | None, str | None]:
        return self._range("days")

    def sleep_range(self) -> tuple[str | None, str | None]:
        return self._range("sleeps")

    def training_range(self) -> tuple[str | None, str | None]:
        return self._range("training")

    def race_range(self) -> tuple[str | None, str | None]:
        return self._range("race_predictions")

    def vo2max_range(self) -> tuple[str | None, str | None]:
        return self._range("vo2max")

    def query_activities(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        sport: str | None = None,
        limit: int | None = None,
        order: str = "desc",
    ) -> list[sqlite3.Row]:
        """按日期区间（含端点）与运动类型筛选，返回原始行。"""
        clauses: list[str] = []
        params: list[object] = []
        if start is not None:
            clauses.append("date >= ?")
            params.append(str(start))
        if end is not None:
            clauses.append("date <= ?")
            params.append(str(end))
        if sport:
            clauses.append("sport = ?")
            params.append(sport.lower())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "DESC" if order.lower() == "desc" else "ASC"
        sql = f"SELECT * FROM activities {where} ORDER BY date {direction}, activity_id {direction}"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self._conn.execute(sql, params).fetchall()

    def distinct_sports(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT sport, COUNT(*) AS n FROM activities GROUP BY sport ORDER BY n DESC"
        ).fetchall()
        return [r["sport"] for r in rows]

    def find_activity(self, public_id: str) -> sqlite3.Row | None:
        """按工具返回给 AI 的 ID 查找活动。

        strict 下 AI 拿到的是伪 ID（`act_xxxxxxxx`），normal/raw 下可能是原始数字 ID，
        两种都接受，避免切换隐私级别后工具调用失效。
        """
        pid = str(public_id).strip()
        if pid.startswith("act_"):
            return self._conn.execute(
                "SELECT * FROM activities WHERE pseudo_id=?", (pid,)
            ).fetchone()
        try:
            aid = int(pid)
        except ValueError:
            return None
        return self._conn.execute(
            "SELECT * FROM activities WHERE activity_id=?", (aid,)
        ).fetchone()

    def date_range(self) -> tuple[str | None, str | None]:
        row = self._conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM activities"
        ).fetchone()
        return (row["lo"], row["hi"]) if row else (None, None)
