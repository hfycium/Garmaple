"""解析 `DI-Connect-Metrics/TrainingHistory_*.json` 中的训练状态快照。

佳明把训练判定结果按日写进这份文件：当天的训练状态、周训练负荷、
负荷隧道（最优负荷区间）的上下界，以及体能水平与负荷水平两条趋势。

三个实测出来的坑：

**同一天有多条记录。** 设备每次同步都会追加一条，一天能写十几条
（实测 937 条落在 604 天上）。它们不是重复数据，数值随时间往前走：
同一天里周负荷会从 341 涨到 462 再到 564。所以去重不能按「内容相同」判断，
要按 `timestamp` 取当日最后一条——那才是这一天结束时的定稿值。

**`timestamp` 是 UTC 钟面。** 实测 `calendarDate: 2026-07-09` 对应的
`timestamp` 是 `2026-07-08T16:03:47`，差 8 小时。它只用来排序，
不参与展示，也就不需要借日汇总的时区偏移去换算。

**`sport` 字段时有时无。** 937 条里 673 条带 `sport: RUNNING`、264 条不带，
同一天两种都出现过（实测 10 天）。这里不假装能拆成「整体状态」和
「分项状态」两套口径——数据不支持，硬拆会得到两列互相矛盾的结论。
`sport` 只如实记录这条快照的来处。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

# 佳明的训练状态枚举。实测归档里出现的是这 8 种，没有别的值。
# 中文标签不是为了翻译好看，而是因为 PRODUCTIVE / UNPRODUCTIVE 在英文里
# 只差一个前缀，模型很容易读反，直接给结论性标签更稳。
STATUS_LABELS = {
    "NO_STATUS": "无状态",
    "RECOVERY": "恢复中",
    "MAINTAINING": "维持中",
    "PRODUCTIVE": "有成效",
    "PEAKING": "巅峰",
    "OVERREACHING": "过度训练",
    "UNPRODUCTIVE": "无成效",
    "DETRAINING": "训练不足",
}

TREND_LABELS = {
    "INCREASING": "上升",
    "DECREASING": "下降",
    "NO_CHANGE": "持平",
    "NO_RESULT": "无结果",
}


def _int(value: Any) -> int | None:
    """把数值字段转成 int；空值与负数当缺失。

    这里不能写成 `value or None` —— 周训练负荷 **0 是有效值**
    （实测 108 天是 0，表示这段时间没有训练负荷），用 `or` 会把 0 吞成缺失。
    """
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


@dataclass
class TrainingDay:
    """一天的训练状态快照。缺失字段一律为 None，不用 0 冒充。"""

    date: str
    status: str
    # 佳明口径的周训练负荷。字段名是 weeklyTrainingLoadSum，与 Connect 上
    # 「本周负荷」显示的是同一个数；不在这里重算，因为单次训练负荷不在归档里。
    weekly_load: int | None = None
    # 负荷隧道的上下界：设备当前认为的「最优负荷区间」
    tunnel_min: int | None = None
    tunnel_max: int | None = None
    fitness_trend: str | None = None
    load_trend: str | None = None
    # 这条快照的来处运动项目，不是每天都有
    sport: str | None = None
    # 当日最后一次写入的 UTC 时刻，仅用于同日去重排序
    updated_at: datetime | None = None
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    user_profile_id: int | None = None

    @property
    def status_label(self) -> str | None:
        return STATUS_LABELS.get(self.status)

    @property
    def fitness_trend_label(self) -> str | None:
        return TREND_LABELS.get(self.fitness_trend or "")

    @property
    def load_trend_label(self) -> str | None:
        return TREND_LABELS.get(self.load_trend or "")

    @property
    def load_position(self) -> str | None:
        """周负荷相对负荷隧道的位置：below / in / above。

        隧道边界缺失时返回 None 而不是猜一个——没有区间就谈不上位置。
        """
        if self.weekly_load is None:
            return None
        if self.tunnel_min is None or self.tunnel_max is None:
            return None
        if self.weekly_load < self.tunnel_min:
            return "below"
        if self.weekly_load > self.tunnel_max:
            return "above"
        return "in"


def parse_training(raw: Any) -> TrainingDay | None:
    """解析单条快照；`calendarDate` 缺失或非法、或没有训练状态时返回 None。"""
    if not isinstance(raw, dict):
        return None

    day = raw.get("calendarDate")
    try:
        date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None

    status = _text(raw.get("trainingStatus"))
    if status is None:
        return None

    return TrainingDay(
        date=str(day),
        status=status,
        weekly_load=_int(raw.get("weeklyTrainingLoadSum")),
        tunnel_min=_int(raw.get("loadTunnelMin")),
        tunnel_max=_int(raw.get("loadTunnelMax")),
        fitness_trend=_text(raw.get("fitnessLevelTrend")),
        load_trend=_text(raw.get("loadLevelTrend")),
        sport=_text(raw.get("sport")),
        updated_at=_parse_ts(raw.get("timestamp")),
        user_profile_id=_int(raw.get("userProfilePK")),
    )


def _first_list(payload: dict[str, Any]) -> list[Any]:
    """顶层被包一层时，取第一个元素是对象的数组。

    与日汇总解析器同一策略：不枚举候选键名，按结构找，免得漏掉固件差异。
    """
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(i, dict) for i in value):
            return value
    return []


def parse_trainings(payload: Any) -> list[TrainingDay]:
    """解析一个分片。实测顶层是数组；若被包了一层，退回按结构查找。"""
    if isinstance(payload, dict):
        payload = _first_list(payload)
    if not isinstance(payload, list):
        return []

    out: list[TrainingDay] = []
    for raw in payload:
        rec = parse_training(raw)
        if rec is not None:
            out.append(rec)
    return out


def dedupe(records: Iterable[TrainingDay]) -> list[TrainingDay]:
    """同一天只留 `updated_at` 最晚的一条，再按日期排序。

    `updated_at` 缺失的记录排在最后——它的新鲜度无从判断，
    不该盖掉一条有时间戳的记录。
    """
    best: dict[str, TrainingDay] = {}
    for rec in records:
        current = best.get(rec.date)
        if current is None:
            best[rec.date] = rec
            continue
        if _is_newer(rec, current):
            best[rec.date] = rec
    return [best[k] for k in sorted(best)]


def _is_newer(candidate: TrainingDay, current: TrainingDay) -> bool:
    if candidate.updated_at is None:
        return False
    if current.updated_at is None:
        return True
    return candidate.updated_at > current.updated_at
