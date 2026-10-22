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
from datetime import date
from pathlib import Path
from typing import Iterable

from .parser.activities import Activity
from .privacy import pseudo_activity_id

SCHEMA_VERSION = 2

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


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_META_SCHEMA)
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """索引随时可从 data/ 重建，因此版本不匹配时直接丢弃旧表，不做增量迁移。"""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row and row["value"] != str(SCHEMA_VERSION):
            self._conn.executescript("DROP TABLE IF EXISTS activities;")
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
