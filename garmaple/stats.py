"""汇总计算。

独立于 MCP 与数据库，输入是查询出来的活动行，输出是给人看的聚合结果。
这样聚合逻辑可以单独测试，不必启动服务。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


def human_duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
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
