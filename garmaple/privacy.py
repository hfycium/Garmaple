"""脱敏层：MCP 工具返回值必须经过这里。

为什么需要它：工具本身不联网，但**返回值会进入 AI 上下文并发送给模型服务商**。
数据一旦离开本机就收不回来，因此脱敏只能发生在返回之前，不能指望 AI 侧保护。

三档策略（见 README）：
    strict  默认。无坐标、无地点名、无原始 ID、时间只到日期
    normal  无坐标，保留原始 ID 与精确时间
    raw     全字段，需显式开启

原则是「聚合优先、默认粗化」：宁可少给 AI 一点，也不要默认交出住址。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .parser.activities import Activity
from .parser.daily import DailyStat
from .parser.fit import FitSample
from .parser.metrics import RACE_DISTANCES_KM, RacePrediction, Vo2maxSample
from .parser.records import (
    COUNT,
    DAYS,
    METERS,
    METRIC_DISTANCE,
    METRIC_DURATION,
    METRIC_ELEVATION,
    SECONDS,
    Gear,
    GearUsage,
    PersonalRecord,
)
from .parser.sleep import SleepRecord
from .parser.training import TrainingDay
from .stats import human_duration, human_pace, pace_from_speed

# 固定盐值：让伪 ID 在多次会话间保持稳定，又不直接暴露 activityId
_ID_SALT = "garmaple-v1"

VALID_LEVELS = ("strict", "normal", "raw")

# 装备状态。实测归档里只出现 active；retired 是佳明另一端的取值，
# 一起译好，免得换一份导出包就冒出一个没有中文的枚举。
_GEAR_STATUS_LABELS = {"active": "使用中", "retired": "已退役"}


def _distance_text(meters: float) -> str:
    """距离的可读写法。小于 1 公里用米，否则用公里——21.12 km 比 21116 m 好读。"""
    if meters >= 1000:
        return f"{meters / 1000:.2f} km"
    return f"{meters:.0f} m"


def _elevation_text(meters: float) -> str:
    """爬升量的可读写法，一律用米。

    不能走 `_distance_text`：爬升 1350 米写成 1.35 km 会被读成
    「爬升了一公里多的坡」，量纲对但直觉反了。爬升本来就用米说。
    """
    return f"{meters:,.0f} m"


def pseudo_activity_id(activity_id: int) -> str:
    """把原始 activityId 映射为稳定的伪 ID。

    与 privacy 级别无关——索引层需要用它建索引，
    因此做成模块级纯函数，保证写入与查询两端算法一致。
    """
    digest = hashlib.blake2s(
        f"{_ID_SALT}:{activity_id}".encode("utf-8"), digest_size=4
    ).hexdigest()
    return f"act_{digest}"


@dataclass(frozen=True)
class Privacy:
    level: str = "strict"

    def __post_init__(self) -> None:
        if self.level not in VALID_LEVELS:
            raise ValueError(f"未知隐私级别 {self.level!r}，可选：{', '.join(VALID_LEVELS)}")

    # ---------- 判定 ----------

    @property
    def is_strict(self) -> bool:
        return self.level == "strict"

    @property
    def allows_location(self) -> bool:
        """地点名与坐标只在 raw 下可用。"""
        return self.level == "raw"

    @property
    def allows_precise_time(self) -> bool:
        return self.level != "strict"

    @property
    def allows_raw_id(self) -> bool:
        return self.level != "strict"

    @property
    def allows_identity(self) -> bool:
        """账号与设备标识（userProfilePK 等）只在 raw 下返回。"""
        return self.level == "raw"

    # ---------- 转换 ----------

    def public_id(self, activity_id: int) -> str:
        """strict 下把 activityId 换成不可反查的伪 ID。

        原始 ID 是公开活动页 URL 的一部分，泄露即可反查到用户身份，
        因此默认不外露。
        """
        if self.allows_raw_id:
            return str(activity_id)
        return pseudo_activity_id(activity_id)

    def when(self, utc: datetime | None, local: datetime | None) -> str | None:
        """strict 下只到日期；更高档位给出本地完整时间。"""
        dt = local or utc
        if dt is None:
            return None
        if self.allows_precise_time:
            return dt.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        return dt.date().isoformat()

    def clock(self, dt: datetime | None) -> str | None:
        """日汇总里的「几点几分」：strict 下一律不给。

        身体电量的峰值时刻会暴露作息（几点起床、几点最累），
        和精确开始时间是同一类信息，因此与 `allows_precise_time` 同档处理。
        """
        if dt is None or not self.allows_precise_time:
            return None
        return dt.strftime("%H:%M")

    def _moment(self, dt: datetime | None) -> str | None:
        """睡眠的入睡 / 醒来时刻，精确到分钟。

        这是**作息规律**的直接刻画（几点睡、几点起），比活动的开始时间
        更敏感，因此与 `allows_precise_time` 同档：strict 下一律不给。
        """
        if dt is None or not self.allows_precise_time:
            return None
        return dt.replace(tzinfo=None).isoformat(sep=" ", timespec="minutes")

    def _safe_name(self, act: Activity) -> str | None:
        """strict/normal 下剥离活动名称里的地点。

        佳明会自动把活动命名为「城市 + 运动类型」（实测形如「深圳市 跑步」）。
        如果只过滤 location_name 而放过 name，等于绕过了这道限制——
        这是实测真实数据时才发现的。
        """
        name = act.name
        if not name:
            return None
        if self.allows_location:
            return name
        if act.location_name and act.location_name in name:
            cleaned = name.replace(act.location_name, "").strip(" -·—,，")
            return cleaned or None
        return name

    def _strip_none(self, data: dict[str, object]) -> dict[str, object]:
        """去掉 None 与空列表，避免返回值被大量 null 撑大。"""
        return {
            k: v
            for k, v in data.items()
            if v is not None and not (isinstance(v, list) and not v)
        }

    # ---------- 实体转换 ----------

    def activity(self, act: Activity) -> dict[str, object]:
        dist_km = round(act.distance_m / 1000.0, 3) if act.distance_m is not None else None
        pace = round(act.pace_s_per_km, 1) if act.pace_s_per_km is not None else None
        zones = act.hr_zone_seconds if any(act.hr_zone_seconds) else None

        out: dict[str, object] = {
            "id": self.public_id(act.activity_id),
            "sport": act.sport,
            "name": self._safe_name(act),
            "when": self.when(act.utc_start, act.local_start),
            "distance_km": dist_km,
            "duration_s": round(act.duration_s) if act.duration_s is not None else None,
            "moving_s": round(act.moving_s) if act.moving_s is not None else None,
            "pace_s_per_km": pace,
            "avg_speed_mps": round(act.avg_speed_mps, 2) if act.avg_speed_mps else None,
            "avg_hr": act.avg_hr,
            "max_hr": act.max_hr,
            "calories": round(act.calories) if act.calories is not None else None,
            "elevation_gain_m": (
                round(act.elevation_gain_m) if act.elevation_gain_m is not None else None
            ),
            "aerobic_te": round(act.aerobic_te, 1) if act.aerobic_te is not None else None,
            "anaerobic_te": (
                round(act.anaerobic_te, 1) if act.anaerobic_te is not None else None
            ),
            "vo2max": act.vo2max,
            "cadence_spm": act.cadence_spm,
            "stride_length_cm": (
                round(act.stride_length_cm, 1) if act.stride_length_cm else None
            ),
            "lap_count": act.lap_count,
            "hr_zones_s": zones,
        }

        if self.allows_location:
            out.update(
                {
                    "location_name": act.location_name,
                    "start": [act.start_lat, act.start_lon],
                    "end": [act.end_lat, act.end_lon],
                    "device_id": act.device_id,
                    "user_profile_id": act.user_profile_id,
                }
            )

        return self._strip_none(out)

    def stream(
        self,
        act: Activity,
        samples: list[FitSample],
        total: int,
        summary: dict[str, object],
        workout_rpe: float | None = None,
        workout_feel: int | None = None,
    ) -> dict[str, object]:
        """单次活动的逐秒曲线。

        用**列式**（每个指标一个数组）而不是数组套对象：同样 300 个点，列式
        能省掉大半体积，模型按列读也不更难。

        不含坐标。FIT 里其实有 `position_lat` / `position_long`，但逐秒轨迹能
        完整还原住址与常去地点，比活动摘要里的起终点敏感得多，因此解析层根本
        不读这两列——三档隐私在这里一视同仁，不是靠这一层过滤。

        也没有绝对时刻，只有相对开始的秒偏移，所以不存在「几点出门」的信息。
        """
        offsets = [s.offset_s for s in samples]
        columns: dict[str, list[object]] = {
            "hr": [s.hr for s in samples],
            "pace_s_per_km": [pace_from_speed(s.speed_mps) for s in samples],
            "altitude_m": [
                round(s.altitude_m, 1) if s.altitude_m is not None else None for s in samples
            ],
            "cadence": [s.cadence for s in samples],
            "power_w": [s.power_w for s in samples],
        }
        # 整列都是 None 就不输出。室内活动只有心率，把 pace/altitude/cadence
        # 三列的空数组一起塞进去既浪费体积，也容易被读成「配速为零」。
        present = {k: v for k, v in columns.items() if any(x is not None for x in v)}
        absent = [k for k in columns if k not in present]

        out: dict[str, object] = {
            "id": self.public_id(act.activity_id),
            "sport": act.sport,
            "when": self.when(act.utc_start, act.local_start),
            "duration_s": round(act.duration_s) if act.duration_s else None,
            "recorded_s": offsets[-1] if offsets else None,
            "points": len(samples),
            "total_points": total,
            "downsampled": len(samples) < total,
            "interval_s": (
                round(offsets[-1] / max(len(offsets) - 1, 1)) if len(offsets) > 1 else None
            ),
            "series": {"offset_s": offsets, **present},
            "summary": summary,
        }
        if absent:
            out["series_missing"] = absent
        # 主观评价是用户在手表上自己填的，不是设备测的。本机 304 次活动里只有
        # 9 次填过，缺是常态，所以单独成块而不是平铺进 summary——避免被读成
        # 「RPE 为 0」这种不存在的结论。没填的那个键直接省略。
        perceived = {
            k: v
            for k, v in (("rpe", workout_rpe), ("feel", workout_feel))
            if v is not None
        }
        if perceived:
            out["perceived"] = perceived
            out["perceived_note"] = (
                "rpe 是 Borg CR10 量表（0–10，数字越大越吃力）；"
                "feel 是 0–100 分，低=当时感觉差、高=感觉好。"
                "两个都是运动后自己填的主观感受，不是仪器测量值。"
            )
        return self._strip_none(out)

    def drift(self, act: Activity, result: dict[str, object]) -> dict[str, object]:
        """配速心率漂移（有氧解耦）。

        数值口径由 `stats.pace_hr_drift` 决定，这里只补可读文本。两个变化量
        都按「后半减前半」给，所以**配速正数=变慢**、**心率正数=升高**，
        与 `drift_pct` 正数=效率下降是同一个方向，不会互相矛盾。
        """
        out: dict[str, object] = {
            "id": self.public_id(act.activity_id),
            "sport": act.sport,
            "when": self.when(act.utc_start, act.local_start),
            "duration_s": round(act.duration_s) if act.duration_s else None,
        }

        if not result.get("available"):
            out["available"] = False
            out["reason"] = result.get("reason")
            if "points" in result:
                out["points"] = result["points"]
                out["needed"] = result.get("needed")
            return self._strip_none(out)

        first = result["first_half"]
        second = result["second_half"]
        pace_change = None
        if first.get("avg_pace_s_per_km") and second.get("avg_pace_s_per_km"):
            pace_change = second["avg_pace_s_per_km"] - first["avg_pace_s_per_km"]

        out.update(
            {
                "available": True,
                "first_half": {
                    **first,
                    "avg_pace": human_pace(first.get("avg_pace_s_per_km")),
                },
                "second_half": {
                    **second,
                    "avg_pace": human_pace(second.get("avg_pace_s_per_km")),
                },
                "drift_pct": result["drift_pct"],
                "pace_change_s_per_km": pace_change,
                "hr_change": round(second["avg_hr"] - first["avg_hr"], 1),
                "span_s": result.get("span_s"),
                "reliable": result.get("reliable"),
            }
        )
        return self._strip_none(out)

    def day(self, d: DailyStat) -> dict[str, object]:
        """单日 wellness 汇总。

        strict 下只丢「时刻」，不丢数值——步数、静息心率、压力、身体电量
        都是聚合量，不含位置与身份信息，分析本身需要它们。
        """
        out: dict[str, object] = {
            "date": d.date,
            "steps": d.steps,
            "step_goal": d.step_goal,
            "distance_km": d.distance_km,
            "intensity_minutes": d.intensity_minutes,
            "intensity_goal": d.intensity_goal_min,
            "moderate_min": d.moderate_min,
            "vigorous_min": d.vigorous_min,
            "resting_hr": d.resting_hr,
            "hr_min": d.min_hr,
            "hr_max": d.max_hr,
            "calories_total": (
                round(d.calories_total) if d.calories_total is not None else None
            ),
            "calories_active": (
                round(d.calories_active) if d.calories_active is not None else None
            ),
            "stress_avg": d.stress_avg,
            "stress_max": d.stress_max,
            "stress_off_wrist_min": d.stress_off_wrist_min,
            "stress_rest_s": d.stress_rest_s,
            "stress_high_s": d.stress_high_s,
            "bb_charged": d.bb_charged,
            "bb_drained": d.bb_drained,
            "bb_high": d.bb_high,
            "bb_low": d.bb_low,
            "bb_start": d.bb_start,
            "bb_end": d.bb_end,
            "bb_high_at": self.clock(d.bb_high_at),
            "bb_low_at": self.clock(d.bb_low_at),
        }

        if self.allows_identity:
            out["user_profile_id"] = d.user_profile_id

        return self._strip_none(out)

    def sleep(self, rec: SleepRecord) -> dict[str, object]:
        """单晚睡眠。

        `date` 是**醒来那天**：一晚的睡眠记在醒来日，问「昨晚睡得怎样」
        要查今天的记录。这个语义由工具说明负责讲清楚，这里只如实输出。

        strict 下丢掉入睡 / 醒来时刻——那是最直接的作息画像；时长与分期
        是聚合量，保留。没有有效分期的那一晚只返回 `date` 与 `status`，
        时长留空：摘表（off_wrist）和未确认（unconfirmed）都不是「睡了 0 分钟」。
        """
        asleep_min = rec.asleep_min
        out: dict[str, object] = {
            "date": rec.date,
            "status": rec.status,
            "asleep_min": asleep_min,
            "asleep_h": round(asleep_min / 60.0, 2) if asleep_min is not None else None,
            "in_bed_min": rec.in_bed_min,
            "deep_min": rec.deep_min,
            "light_min": rec.light_min,
            "rem_min": rec.rem_min,
            "awake_min": rec.awake_min,
            "respiration_avg": (
                round(rec.respiration_avg, 1) if rec.respiration_avg is not None else None
            ),
        }
        # 只有确认有分期的记录才给时刻：摘表那晚的 start/end 其实是「摘下手表」
        # 与「戴回手表」的时间，未确认睡眠窗的是固定占位值，都会被误读成作息。
        if rec.has_stages:
            out["fell_asleep_at"] = self._moment(rec.start_at)
            out["woke_at"] = self._moment(rec.end_at)
        out["retro"] = rec.retro or None
        return self._strip_none(out)

    def training(self, rec: TrainingDay) -> dict[str, object]:
        """单日训练状态。

        状态与趋势都给中文标签，不只是翻译：`PRODUCTIVE` 与 `UNPRODUCTIVE`
        只差一个前缀、`DETRAINING` 又容易被读成「正在训练」，模型按英文枚举
        直接推结论会反向。标签让结论只有一种读法。

        `updated_at` 不输出——它是设备同步快照的时刻，不是训练时刻，
        对分析没有价值，只在同日去重时用过。
        """
        out: dict[str, object] = {
            "date": rec.date,
            "status": rec.status,
            "status_label": rec.status_label,
            "weekly_load": rec.weekly_load,
            "load_position": rec.load_position,
            "fitness_trend": rec.fitness_trend,
            "fitness_trend_label": rec.fitness_trend_label,
            "load_trend": rec.load_trend,
            "load_trend_label": rec.load_trend_label,
            "sport": rec.sport,
        }
        # 隧道边界缺失时整块不给，而不是给一个 {min: null, max: null}
        if rec.tunnel_min is not None and rec.tunnel_max is not None:
            out["load_tunnel"] = {"min": rec.tunnel_min, "max": rec.tunnel_max}

        if self.allows_identity:
            out["user_profile_id"] = rec.user_profile_id

        return self._strip_none(out)

    def prediction(self, rec: RacePrediction) -> dict[str, object]:
        """单日比赛成绩预测。

        每个距离给三样东西：秒数（便于做差）、可读时间、预测配速。
        秒数与可读时间是同一事实的两种写法，不算冗余——模型算差值要秒数，
        转述给用户要可读时间，两边都现算容易算错。

        配速由「时间 ÷ 标准距离」推出来，不是库里存的：全马按 42.195km，
        用 42km 会把配速算快 0.46%。
        """
        predicted: dict[str, object] = {}
        for key in RACE_DISTANCES_KM:
            seconds = rec.times.get(key)
            if seconds is None:
                continue
            predicted[key] = {
                "seconds": seconds,
                "time": human_duration(seconds),
                "pace": human_pace(rec.pace_s_per_km(key)),
            }

        out: dict[str, object] = {"date": rec.date, "predicted": predicted}

        if self.allows_identity:
            out["user_profile_id"] = rec.user_profile_id

        return self._strip_none(out)

    def vo2max(self, rec: Vo2maxSample) -> dict[str, object]:
        """单次最大摄氧量。

        只给数值与来源项目。`fitnessAge` 不在解析层，也不在这里补——
        归档里它恒为占位值 "20"，输出出来就会被当成真实体能年龄。
        """
        out: dict[str, object] = {
            "date": rec.date,
            "vo2max": round(rec.vo2max, 1),
            "sport": rec.sport,
        }

        if self.allows_identity:
            out["user_profile_id"] = rec.user_profile_id

        return self._strip_none(out)

    def record(self, rec: PersonalRecord) -> dict[str, object]:
        """一条个人最佳。

        `value` 的量纲由 `unit` 说明，**不要跨类型比较**：同样是「最好的跑步」，
        `Best 5km Run` 是秒而 `Farthest Run` 是米。`display` 是按量纲格式化过的
        写法，转述给用户时直接用它，别自己拼单位。

        计数与天数给整数，成绩与距离保留一位小数——成绩精确到 0.1 秒已经够用，
        而 1108.3089599609375 这种原值读出来只会让人分心。

        `date` 是达成日（本地）。步数与连续达标类没有这个字段：归档里它们只有
        区间起点，拿它当达成日会读成「一天之内创下多项纪录」。
        """
        if rec.unit in (COUNT, DAYS):
            value: float = float(int(round(rec.value)))
        else:
            value = round(rec.value, 1)

        out: dict[str, object] = {
            "type": rec.type,
            "label": rec.label,
            "group": rec.group,
            "value": value,
            "unit": rec.unit,
            "display": self._record_display(rec),
            "date": rec.date,
            "current": rec.current,
        }

        if self.allows_identity:
            out["user_profile_id"] = rec.user_profile_id

        return self._strip_none(out)

    def _record_display(self, rec: PersonalRecord) -> str | None:
        """按 `metric` 而不是 `unit` 选格式。

        只用 `unit` 分不出「水平距离」与「爬升」——两者的 `unit` 都是米，
        但爬升 1350 米写成 1.35 km 会被读成「爬升了一公里多的坡」，
        而距离 21116 米写成 21.12 km 才是正常的。所以按 metric 分三支。
        """
        unit = rec.unit
        metric = rec.metric
        if unit == SECONDS or metric == METRIC_DURATION:
            # 个人最佳要秒：1h54m14s 与 1h54m52s 是两条不同的纪录，
            # 省略秒会让它们看起来一样。
            return human_duration(rec.value, precise=True)
        if metric == METRIC_ELEVATION:
            return _elevation_text(rec.value)
        if unit == METERS:
            return _distance_text(rec.value)
        if unit == DAYS:
            return f"{int(round(rec.value))} 天"
        if unit == COUNT:
            return f"{int(round(rec.value)):,}"
        # 单位未知就给数值本身，不猜量纲
        return f"{rec.value:g}"

    def gear(self, gear: Gear, usage: GearUsage) -> dict[str, object]:
        """一双装备的累计使用情况。

        里程是**关联活动的距离之和**，不是设备给的累计值——归档里没有那个字段。
        因此这里同时给出 `activity_count` 与按运动的明细：跑鞋的里程里会混进
        步行记录（实测 268 次里有 5 次），只给一个总数没法解释。

        没有关联记录时给 `note` 而不是 0 公里。实测确实有一双鞋整个关联键都
        不存在，「跑了 0 公里」与「归档里没记」是两回事，混起来会得出
        「这双鞋买了没穿过」这种并不成立的结论。

        `remaining_km` 可能为负：它表示已超出设备里设置的建议里程，
        是跑鞋超期服役的事实，不是数据错误，不要截成 0。

        `id` 只在 normal / raw 下给。strict 下不给原始 gearPk——装备靠名称就能
        认出来，不需要暴露内部主键。
        """
        out: dict[str, object] = {
            "name": gear.name,
            "type": gear.gear_type,
            "status": gear.status,
            "status_label": _GEAR_STATUS_LABELS.get(gear.status or "", None),
            "started_on": gear.started_on,
        }

        if usage.has_activities and usage.distance_m is not None:
            km = usage.distance_m / 1000.0
            out["distance_km"] = round(km, 1)
            out["activity_count"] = usage.activity_count
            out["first_activity"] = usage.first_date
            out["last_activity"] = usage.last_date
            if gear.max_meters:
                limit_km = gear.max_meters / 1000.0
                out["life_limit_km"] = round(limit_km, 1)
                out["remaining_km"] = round(limit_km - km, 1)
                out["used_pct"] = round(km / limit_km * 100, 1)
            out["by_sport"] = {
                sport: {
                    "count": item.count,
                    "distance_km": round(item.distance_m / 1000.0, 1),
                }
                for sport, item in usage.by_sport.items()
            }
        else:
            # 有建议里程却没关联记录时也要给出上限，否则这双鞋看上去像没有配置
            if gear.max_meters:
                out["life_limit_km"] = round(gear.max_meters / 1000.0, 1)
            out["note"] = "归档里没有这双装备的关联活动记录，无法计算里程"

        # 关联表里提到但活动摘要里没有的那几次：里程偏低的原因，得说出来
        if usage.unmatched_count:
            out["unmatched_links"] = usage.unmatched_count

        if self.allows_raw_id:
            out["id"] = str(gear.gear_id)
        if self.allows_identity:
            out["user_profile_id"] = gear.user_profile_id

        return self._strip_none(out)
