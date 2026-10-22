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
from .parser.sleep import SleepRecord

# 固定盐值：让伪 ID 在多次会话间保持稳定，又不直接暴露 activityId
_ID_SALT = "garmaple-v1"

VALID_LEVELS = ("strict", "normal", "raw")


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
