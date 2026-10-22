"""解析 `DI-Connect-Fitness/` 下的个人最佳与装备。

两份文件都是「设备已经算好结论、本模块只负责还原」，不做任何推算。

**个人最佳**（`*_personalRecord.json`）实测 28 条，其中 20 条为当前有效纪录。
顶层形如 ``[{"personalRecords": [...]}]``。

三处必须按类型分支，否则会给出错误结论：

1. `value` 的单位随类型变，同一个字段四种量纲：
   跑步 / 游泳 / 骑行成绩是**秒**（`Best 5km Run` = 1108.3），
   距离类是**米**（`Farthest Run` = 21116.4），步数是**次数**
   （`Most Steps in a Day` = 36434），连续达标是**天数**。
   把 5K 的成绩当距离用，会得到「5 公里跑了 1108 米」。
   单位不靠猜：见 `RECORD_KINDS`，表里没有的类型不标单位。

2. `prStartTimeGMT` 语义不统一：成绩类给的是活动开始时刻，
   步数与连续达标类给的是**区间起点**（`Most Steps in a Day` 是当日 00:00）。
   它又是纯 GMT 钟面，换算本地日要借时区偏移。因此这里不解析这个字段，
   达成日改用 `createdDate`——实测 23 条成绩记录的 `createdDate`
   与对应活动的**本地日期完全一致**，且不带时刻。

3. `activityId` 为 0 的记录（步数、连续达标）没有对应活动，
   它们的 `createdDate` 只是建档日（实测 5 条全在同一天），
   当成达成日会被读成「一天之内创下 5 项纪录」。所以这类记录 date 留空。

`current` 是设备认定的当前有效纪录；`false` 表示已被刷新的旧纪录。
实测 28 条里 20 条 current、8 条被刷新——旧纪录不是脏数据，是进步的证据。

**装备**（`*_gear.json`）顶层形如::

    [{"gearDTOS": [...],
      "gearActivityDTOs": {"<gearPk>": [{"gearPk": .., "activityId": ..}]}}]

装备本身**没有累计里程字段**，里程只能由关联活动的距离求和得到
（见 `GearUsage` / `Store.gear_usage`）。

实测一个必须留意的空洞：关联表按 `gearPk` 分键，**某些装备整个键都不存在**
（实测 2 双鞋只有 1 双有键，另一双 0 次关联）。这不是「跑量为 0」，
而是归档里没有这段关联，输出时必须区分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Iterator

# 四种量纲。用短标识而不是中文，便于上层程序判断后再格式化
SECONDS = "s"
METERS = "m"
COUNT = "count"
DAYS = "day"

# 展示方式。`unit` 说的是量纲，`metric` 说的是「该按哪种习惯写给人看」——
# 同为米，水平距离超过 1 公里用 km（21.12 km），爬升则一律用米（1347 m）：
# 「单次爬升 1.35 km」看起来像距离，而且不符合跑者读爬升的习惯。
METRIC_DURATION = "duration"
METRIC_DISTANCE = "distance"
METRIC_ELEVATION = "elevation"

# 分组标识。与运动类型同名的组可直接对上 activities.sport
GROUP_RUN = "run"
GROUP_SWIM = "swim"
GROUP_CYCLE = "cycle"
GROUP_GENERAL = "general"
GROUPS = (GROUP_RUN, GROUP_SWIM, GROUP_CYCLE, GROUP_GENERAL)


@dataclass(frozen=True)
class RecordKind:
    """一个个人最佳类型的元数据：量纲、展示方式、中文标签、所属运动组。"""

    unit: str | None
    label: str
    group: str
    metric: str | None = None


# 实测归档里出现的全部类型（20 种）。新增类型时在这里补一行即可；
# 漏补不会崩，只是没有单位和中文标签，输出仍带原类型名与数值。
RECORD_KINDS: dict[str, RecordKind] = {
    # 跑步成绩：时间
    "Best 1km Run": RecordKind(SECONDS, "1 公里跑最快", GROUP_RUN, METRIC_DURATION),
    "Best 1mile Run": RecordKind(SECONDS, "1 英里跑最快", GROUP_RUN, METRIC_DURATION),
    "Best 5km Run": RecordKind(SECONDS, "5 公里跑最快", GROUP_RUN, METRIC_DURATION),
    "Best 10km Run": RecordKind(SECONDS, "10 公里跑最快", GROUP_RUN, METRIC_DURATION),
    "Best Half Marathon": RecordKind(
        SECONDS, "半程马拉松最快", GROUP_RUN, METRIC_DURATION
    ),
    "Farthest Run": RecordKind(METERS, "单次跑得最远", GROUP_RUN, METRIC_DISTANCE),
    # 游泳：成绩是时间，距离类是米
    "Best 100m Pool Swim": RecordKind(
        SECONDS, "100 米池泳最快", GROUP_SWIM, METRIC_DURATION
    ),
    "Best 400m Pool Swim": RecordKind(
        SECONDS, "400 米池泳最快", GROUP_SWIM, METRIC_DURATION
    ),
    "Best 750m Pool Swim": RecordKind(
        SECONDS, "750 米池泳最快", GROUP_SWIM, METRIC_DURATION
    ),
    "Best 1000m Pool Swim": RecordKind(
        SECONDS, "1000 米池泳最快", GROUP_SWIM, METRIC_DURATION
    ),
    "Best 1500m Pool Swim": RecordKind(
        SECONDS, "1500 米池泳最快", GROUP_SWIM, METRIC_DURATION
    ),
    "Longest Pool Swim": RecordKind(
        METERS, "单次游泳最远", GROUP_SWIM, METRIC_DISTANCE
    ),
    # 骑行
    "Best 40km Cycle": RecordKind(
        SECONDS, "40 公里骑行最快", GROUP_CYCLE, METRIC_DURATION
    ),
    "Farthest Cycle": RecordKind(
        METERS, "单次骑得最远", GROUP_CYCLE, METRIC_DISTANCE
    ),
    # 与具体运动无关的纪录
    "Max Elevation Gain": RecordKind(
        METERS, "单次爬升最多", GROUP_GENERAL, METRIC_ELEVATION
    ),
    "Most Steps in a Day": RecordKind(COUNT, "单日步数最多", GROUP_GENERAL),
    "Most Steps in a Week": RecordKind(COUNT, "单周步数最多", GROUP_GENERAL),
    "Most Steps in a Month": RecordKind(COUNT, "单月步数最多", GROUP_GENERAL),
    "Current Goal Streak": RecordKind(DAYS, "当前连续达标天数", GROUP_GENERAL),
    "Longest Goal Streak": RecordKind(DAYS, "最长连续达标天数", GROUP_GENERAL),
}

# 表里没有的类型按命名推断分组。`Best X` 在佳明里一律是成绩（秒）、
# `Farthest X` 一律是水平距离（米），实测无一例外，所以这两个前缀
# 连量纲与展示方式一起补；其余只推分组，单位留空表示不敢确定。
_PREFIX_HINTS = (
    ("Best ", SECONDS, METRIC_DURATION),
    ("Farthest ", METERS, METRIC_DISTANCE),
    ("Most Steps", COUNT, None),
)


def _group_for(name: str) -> str:
    low = name.lower()
    if "swim" in low:
        return GROUP_SWIM
    if "cycle" in low or "ride" in low or "bike" in low:
        return GROUP_CYCLE
    if "run" in low or "marathon" in low:
        return GROUP_RUN
    return GROUP_GENERAL


def kind_for(name: str) -> RecordKind:
    """取类型元数据。表里没有的类型按命名前缀推断，标签留空。"""
    kind = RECORD_KINDS.get(name)
    if kind is not None:
        return kind
    unit: str | None = None
    metric: str | None = None
    for prefix, candidate_unit, candidate_metric in _PREFIX_HINTS:
        if name.startswith(prefix):
            unit, metric = candidate_unit, candidate_metric
            break
    if unit is None and "streak" in name.lower():
        unit = DAYS
    return RecordKind(unit, "", _group_for(name), metric)


@dataclass
class PersonalRecord:
    """一条个人最佳。`value` 的量纲由 `unit` 说明，不要跨类型直接比较。"""

    record_id: int
    type: str
    value: float
    # 达成日（本地）。步数与连续达标类为 None——见模块说明第 3 点
    date: str | None = None
    current: bool = True
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    user_profile_id: int | None = None

    @property
    def kind(self) -> RecordKind:
        return kind_for(self.type)

    @property
    def unit(self) -> str | None:
        return self.kind.unit

    @property
    def group(self) -> str:
        return self.kind.group

    @property
    def label(self) -> str:
        """中文标签；未知类型回落到原始类型名，不编造译名。"""
        return self.kind.label or self.type

    @property
    def metric(self) -> str | None:
        """展示方式。只有成绩、水平距离、爬升三类需要特判，其余按 `unit` 走。"""
        return self.kind.metric


@dataclass
class SportUsage:
    """某双装备在某一运动上的累计。"""

    count: int
    distance_m: float


@dataclass
class GearUsage:
    """一双装备的累计使用情况。

    `activity_count == 0` 表示**归档里没有关联到任何活动**，不是「累计 0 公里」：
    实测确实有一双鞋整个关联键都不存在。两者在输出里必须能分辨，
    所以 `distance_m` 宁可留 None 也不填 0。
    """

    # 出现在活动摘要里、能被距离求和的关联数
    activity_count: int = 0
    distance_m: float | None = None
    first_date: str | None = None
    last_date: str | None = None
    # 关联表里声明的条数；与 activity_count 不等说明有活动不在摘要里
    assigned_count: int = 0
    by_sport: dict[str, SportUsage] = field(default_factory=dict)

    @property
    def has_activities(self) -> bool:
        return self.activity_count > 0

    @property
    def unmatched_count(self) -> int:
        return max(0, self.assigned_count - self.activity_count)


@dataclass
class Gear:
    """一双装备（跑鞋、骑行鞋等）。

    `activity_ids` 是原始 activityId，只在入库时用来换算伪 ID，
    **任何输出路径都不该带它**——与活动 ID 同一处理。
    """

    gear_id: int
    name: str | None = None
    gear_type: str | None = None
    status: str | None = None
    started_on: str | None = None
    max_meters: float | None = None
    activity_ids: list[int] = field(default_factory=list)
    # --- 敏感字段：由 privacy 层决定是否输出 ---
    user_profile_id: int | None = None


# ---------- 小工具 ----------


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    # NaN / inf 会让后续求和与排序全部失真
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def _positive(value: Any) -> float | None:
    n = _number(value)
    return n if n is not None and n > 0 else None


def _int_or_none(value: Any) -> int | None:
    n = _number(value)
    return int(n) if n is not None else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


# ---------- 个人最佳 ----------


def _iter_blocks(payload: Any) -> Iterator[dict[str, Any]]:
    """顶层可能是单块 dict，也可能被包成一元数组，两种都接受。"""
    if isinstance(payload, list):
        yield from (b for b in payload if isinstance(b, dict))
    elif isinstance(payload, dict):
        yield payload


def parse_record(raw: Any) -> PersonalRecord | None:
    """解析单条纪录。缺 ID、类型非法、数值非正时返回 None。

    0 与负数在这里按无效丢弃：所有实测类型都是正数，
    而「0 秒的 5K」与「缺这条纪录」在分析里是两回事，混在一起会给出假结论。
    主键同样要求为正——`personalRecordId: 0` 是占位值，留着会让幂等入库互相覆盖。
    """
    if not isinstance(raw, dict):
        return None

    record_id = _int_or_none(raw.get("personalRecordId"))
    name = _text(raw.get("personalRecordType"))
    value = _positive(raw.get("value"))
    # 0 不是有效主键（和 activityId 一样当占位值），缺了它就没法幂等入库
    if record_id is None or record_id <= 0 or name is None or value is None:
        return None

    # activityId 为 0 的是汇总类纪录，createdDate 只是建档日，不能当达成日
    has_activity = (_int_or_none(raw.get("activityId")) or 0) > 0

    return PersonalRecord(
        record_id=record_id,
        type=name,
        value=value,
        date=_iso_date(raw.get("createdDate")) if has_activity else None,
        current=bool(raw.get("current", True)),
        user_profile_id=_int_or_none(raw.get("userProfilePK")),
    )


def parse_records(payload: Any) -> list[PersonalRecord]:
    """解析一个分片。归档里这一类只有一个文件，多包合并时按 record_id 去重。"""
    out: list[PersonalRecord] = []
    for block in _iter_blocks(payload):
        items = block.get("personalRecords")
        if not isinstance(items, list):
            continue
        for raw in items:
            rec = parse_record(raw)
            if rec is not None:
                out.append(rec)
    return out


def dedupe_records(records: Iterable[PersonalRecord]) -> list[PersonalRecord]:
    """按 `personalRecordId` 去重，再按类别与数值排序。

    多次导出可能包含同一批纪录，主键相同即视为同一条。
    """
    unique: dict[int, PersonalRecord] = {}
    for rec in records:
        unique.setdefault(rec.record_id, rec)
    return sorted(
        unique.values(),
        key=lambda r: (GROUPS.index(r.group), r.type, r.value),
    )


# ---------- 装备 ----------


def _link_ids(links: Any, gear_id: int) -> list[int]:
    """取某双装备关联的 activityId 列表。

    JSON 对象的键一定是字符串，但分片被其它工具重新序列化后可能变成数字键，
    两种都试一次——猜错键名的代价是整双鞋的里程凭空消失。
    """
    if not isinstance(links, dict):
        return []
    raw = links.get(str(gear_id))
    if raw is None:
        raw = links.get(gear_id)  # type: ignore[call-overload]
    if not isinstance(raw, list):
        return []

    out: list[int] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        aid = _int_or_none(item.get("activityId"))
        if aid is not None and aid > 0:
            out.append(aid)
    return out


def parse_gear(raw: Any, links: Any = None) -> Gear | None:
    """解析单双装备。缺 `gearPk` 时返回 None。"""
    if not isinstance(raw, dict):
        return None
    gear_id = _int_or_none(raw.get("gearPk"))
    if gear_id is None:
        return None

    return Gear(
        gear_id=gear_id,
        name=_text(raw.get("customMakeModel")),
        gear_type=_text(raw.get("gearTypeName")),
        status=_text(raw.get("gearStatusName")),
        started_on=_iso_date(raw.get("dateBegin")),
        max_meters=_positive(raw.get("maximumMeters")),
        activity_ids=_link_ids(links, gear_id),
        user_profile_id=_int_or_none(raw.get("userProfilePk")),
    )


def parse_gears(payload: Any) -> list[Gear]:
    """解析一个分片。

    关联表 `gearActivityDTOs` 与装备列表在同一个块里，必须一起读——
    分开解析就只能拿到装备名和「不知道跑过几次」。
    """
    out: list[Gear] = []
    for block in _iter_blocks(payload):
        items = block.get("gearDTOS")
        if not isinstance(items, list):
            continue
        links = block.get("gearActivityDTOs")
        for raw in items:
            gear = parse_gear(raw, links)
            if gear is not None:
                out.append(gear)
    return out


def dedupe_gears(gears: Iterable[Gear]) -> list[Gear]:
    """按 `gearPk` 去重。关联列表取并集，不取其中一份。"""
    merged: dict[int, Gear] = {}
    for gear in gears:
        current = merged.get(gear.gear_id)
        if current is None:
            merged[gear.gear_id] = gear
            continue
        for aid in gear.activity_ids:
            if aid not in current.activity_ids:
                current.activity_ids.append(aid)
    return [merged[k] for k in sorted(merged)]


__all__ = [
    "COUNT",
    "DAYS",
    "GROUP_CYCLE",
    "GROUP_GENERAL",
    "GROUP_RUN",
    "GROUP_SWIM",
    "GROUPS",
    "METERS",
    "METRIC_DISTANCE",
    "METRIC_DURATION",
    "METRIC_ELEVATION",
    "RECORD_KINDS",
    "SECONDS",
    "Gear",
    "GearUsage",
    "PersonalRecord",
    "RecordKind",
    "SportUsage",
    "dedupe_gears",
    "dedupe_records",
    "kind_for",
    "parse_gear",
    "parse_gears",
    "parse_record",
    "parse_records",
]
