"""汇总计算。

独立于 MCP 与数据库，输入是查询出来的活动行，输出是给人看的聚合结果。
这样聚合逻辑可以单独测试，不必启动服务。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


def human_duration(seconds: float | None, *, precise: bool = False) -> str | None:
    """秒数转可读时长。

    `precise=False`（默认）在超过 1 小时时省掉秒：活动时长与睡眠时长说
    「1h54m」就够，多给的秒只是噪音。

    `precise=True` 一律带秒。个人最佳必须这样——1h54m14s 与 1h54m52s
    是两条不同的纪录，省掉秒会让它们看起来完全一样。
    """
    if not seconds:
        return None
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s" if precise else f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def human_pace(seconds_per_km: float | None) -> str | None:
    if not seconds_per_km or seconds_per_km <= 0:
        return None
    total = int(round(seconds_per_km))
    m, s = divmod(total, 60)
    return f"{m}'{s:02d}\"/km"


def _get(row: Mapping[str, Any] | Any, key: str) -> Any:
    """兼容 sqlite3.Row 与 dict。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def summarize(rows: Iterable[Mapping[str, Any] | Any]) -> dict[str, Any]:
    """把一组活动行聚合成区间概览。

    配速按「总时长 / 总距离」加权，而不是各次配速的算术平均——
    后者会让一次短距离的速度训练拉高整个周期的平均配速。
    """
    items = list(rows)
    if not items:
        return {"count": 0}

    total_distance_m = 0.0
    total_duration_s = 0.0
    total_moving_s = 0.0
    total_elevation_m = 0.0
    hr_values: list[int] = []
    sports: Counter[str] = Counter()
    longest: dict[str, Any] | None = None
    fastest: dict[str, Any] | None = None

    for row in items:
        sport = _get(row, "sport") or "unknown"
        sports[sport] += 1

        dist = _get(row, "distance_m") or 0.0
        dur = _get(row, "duration_s") or 0.0
        total_distance_m += dist
        total_duration_s += dur
        total_moving_s += _get(row, "moving_s") or 0.0
        total_elevation_m += _get(row, "elevation_gain_m") or 0.0

        hr = _get(row, "avg_hr")
        if hr:
            hr_values.append(int(hr))

        if dist > 0 and (longest is None or dist > longest["distance_m"]):
            longest = {
                "date": _get(row, "date"),
                "sport": sport,
                "distance_m": dist,
                "duration_s": round(dur),
            }

        # 只看距离足够的记录，避免几百米的热身被当成最快配速
        if sport.startswith("run") and dist >= 3000 and dur > 0:
            pace = dur / (dist / 1000.0)
            if fastest is None or pace < fastest["pace_s_per_km"]:
                fastest = {
                    "date": _get(row, "date"),
                    "pace_s_per_km": pace,
                    "distance_km": round(dist / 1000.0, 2),
                }

    avg_pace = (
        total_duration_s / (total_distance_m / 1000.0) if total_distance_m > 0 else None
    )

    out: dict[str, Any] = {
        "count": len(items),
        "sports": dict(sports.most_common()),
        "total_distance_km": round(total_distance_m / 1000.0, 2),
        "total_duration_s": int(round(total_duration_s)),
        "total_duration": human_duration(total_duration_s),
        "total_moving": human_duration(total_moving_s),
        "total_elevation_m": round(total_elevation_m),
        "avg_pace_s_per_km": round(avg_pace, 1) if avg_pace else None,
        "avg_pace": human_pace(avg_pace),
    }

    if hr_values:
        out["avg_hr"] = round(sum(hr_values) / len(hr_values))
        out["max_hr"] = max(hr_values)

    if longest:
        longest["distance_km"] = round(longest.pop("distance_m") / 1000.0, 2)
        longest["duration"] = human_duration(longest.get("duration_s"))
        out["longest"] = longest

    if fastest:
        fastest["pace"] = human_pace(fastest["pace_s_per_km"])
        out["fastest"] = fastest

    return {k: v for k, v in out.items() if v is not None}


ZONE_COUNT = 7


def zone_summary(rows: Iterable[Mapping[str, Any] | Any]) -> dict[str, Any]:
    """把一组活动聚合成心率区间分布。

    只统计**有区间数据**的活动，并单独报出没有的那部分。把缺数据的活动
    当成「各区间都是 0」会稀释占比——那等于说「他这几次没花时间在任何区间」，
    而事实是那几次没有区间记录。

    区间编号照搬归档里的 `hrTimeInZone_N`，不做「Z1..Z5」的映射：归档里
    既没有区间上下限（bpm），也没有区间个数的设置，套上 Z 编号等于编造
    一个不存在的对应关系。
    """
    items = list(rows)
    totals = [0] * ZONE_COUNT
    by_sport: dict[str, list[int]] = {}
    sport_counts: Counter[str] = Counter()
    used = 0

    for row in items:
        zones = [_get(row, f"hr_zone_{i}") or 0 for i in range(ZONE_COUNT)]
        if not any(zones):
            continue
        used += 1
        sport = _get(row, "sport") or "unknown"
        sport_counts[sport] += 1
        bucket = by_sport.setdefault(sport, [0] * ZONE_COUNT)
        for i, value in enumerate(zones):
            totals[i] += value
            bucket[i] += value

    total = sum(totals)
    out: dict[str, Any] = {
        "activities": len(items),
        "with_zone_data": used,
        "without_zone_data": len(items) - used,
        "total_s": total,
        "total": human_duration(total),
    }

    if not total:
        return out

    out["zones"] = [
        {
            "zone": i,
            "seconds": seconds,
            "time": human_duration(seconds),
            "pct": round(seconds / total * 100, 1),
        }
        for i, seconds in enumerate(totals)
    ]
    out["dominant_zone"] = totals.index(max(totals))

    if len(by_sport) > 1:
        out["by_sport"] = {
            sport: {
                "activities": sport_counts[sport],
                "total_s": sum(bucket),
                "total": human_duration(sum(bucket)),
                "pct": [round(v / sum(bucket) * 100, 1) for v in bucket],
            }
            for sport, bucket in sorted(by_sport.items())
        }

    return out
