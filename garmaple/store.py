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
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .parser.activities import Activity
from .parser.daily import DailyStat
from .parser.metrics import RacePrediction, Vo2maxSample
from .parser.sleep import STATUS_OK, SleepRecord
from .parser.training import TrainingDay
from .privacy import pseudo_activity_id

SCHEMA_VERSION = 6

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
        self._conn.commit()

    # 结构自愈的判据：每张表缺了这些列就视为旧表
    _REQUIRED_COLUMNS = {
        "activities": frozenset({"pseudo_id", "sport", "date", "distance_m"}),
        "days": frozenset({"date", "steps", "resting_hr", "stress_avg", "utc_offset_hours"}),
        "sleeps": frozenset({"date", "in_bed_min", "status", "deep_min"}),
        "training": frozenset({"date", "status", "weekly_load", "tunnel_min"}),
        "race_predictions": frozenset({"date", "time_5k_s", "time_half_s"}),
        "vo2max": frozenset({"date", "vo2max"}),
    }

    def _migrate(self) -> None:
        """索引随时可从 data/ 重建，因此结构不符时直接丢弃旧表，不做增量迁移。

        判据故意用「实际列结构」而不是 meta 里的版本号：版本号可能被中途写入，
        一旦元数据写着新版本而表仍是旧的，只查版本号就永远修不好
        （曾因此出现 `no such column: pseudo_id`）。
        """
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

    def count_days(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]

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
