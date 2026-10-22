"""汇总计算。

独立于 MCP 与数据库，输入是查询出来的活动行，输出是给人看的聚合结果。
这样聚合逻辑可以单独测试，不必启动服务。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
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


# 低于这个速度就当作「停下了」。0.3 m/s 约 1.1 km/h，比正常步行（1.1~1.5 m/s）
# 慢得多；用它兜住设备在原地记录的 0 速度与漂移，免得算出 9999"/km 这种配速。
STANDSTILL_MPS = 0.3


def pace_from_speed(speed_mps: float | None) -> int | None:
    """瞬时速度换成秒每公里。停下时返回 None，不要给一个巨大的配速。

    逐秒序列里静止点很常见（等红灯、拍照、暂停），如果照 `1000 / speed`
    算，0.01 m/s 会得到 100000"/km，画出来的配速曲线会被这些点压平。
    """
    if speed_mps is None or speed_mps < STANDSTILL_MPS:
        return None
    return int(round(1000.0 / speed_mps))


# 有氧解耦（配速心率漂移）的最小可用时长。20 分钟以下前后半程各只剩
# 十来分钟，热身还没走完，算出来的「漂移」主要是热身带来的自然上升。
DECOUPLING_MIN_SECONDS = 1200

# 每一半至少要有这么多个有效采样，否则平均值的抖动会盖过真实漂移
DECOUPLING_MIN_POINTS = 30


def pace_hr_drift(samples: Iterable[Any]) -> dict[str, Any]:
    """前后半程的「速度 ÷ 心率」比值变化，也就是有氧解耦。

    口径：

    - 比值取`平均速度 / 平均心率`（m/s per bpm）。同样心率跑得更快，比值就
      更高，所以**比值下降 = 效率下降**。
    - `drift_pct` 取正数表示后半程效率下降（心率漂移），与跑步圈说的
      「解耦 5%」同向：同样配速下心率涨了，或者同样心率下配速掉了。
    - 只用同时有速度与心率的采样点。静止点不参与平均，否则一次等红灯就能
      把前半程的平均速度拉下去，凭空造出一个「漂移」。
    - 没有心率的活动（力量训练之类）直接返回 `available=False`。

    `reliable` **只表示样本条件够不够**（时长与点数），不代表结论成立：
    前半程冲、后半程放松同样会让比值变化，但那不是心率漂移。工具没法从
    数据里区分这两件事，只能在说明里讲清楚看到的是什么。
    """
    usable = [
        s
        for s in samples
        if getattr(s, "hr", None)
        and s.hr > 0
        and getattr(s, "speed_mps", None) is not None
        and s.speed_mps >= STANDSTILL_MPS
    ]
    if not usable:
        return {"available": False, "reason": "no_hr_or_speed"}

    first_ts = usable[0].offset_s
    last_ts = usable[-1].offset_s
    span = last_ts - first_ts
    if span <= 0:
        return {"available": False, "reason": "too_short"}

    mid = first_ts + span / 2
    first_half = [s for s in usable if s.offset_s <= mid]
    second_half = [s for s in usable if s.offset_s > mid]

    if len(first_half) < DECOUPLING_MIN_POINTS or len(second_half) < DECOUPLING_MIN_POINTS:
        return {
            "available": False,
            "reason": "too_few_points",
            "points": len(usable),
            "needed": DECOUPLING_MIN_POINTS * 2,
        }

    def half(points: list[Any]) -> dict[str, Any]:
        speed = sum(s.speed_mps for s in points) / len(points)
        hr = sum(s.hr for s in points) / len(points)
        return {
            "points": len(points),
            "avg_speed_mps": round(speed, 3),
            "avg_hr": round(hr, 1),
            "efficiency": round(speed / hr, 5),
            "avg_pace_s_per_km": round(1000.0 / speed) if speed else None,
        }

    a = half(first_half)
    b = half(second_half)
    drift = (a["efficiency"] - b["efficiency"]) / a["efficiency"] * 100

    return {
        "available": True,
        "first_half": a,
        "second_half": b,
        "drift_pct": round(drift, 1),
        "span_s": round(span),
        "reliable": span >= DECOUPLING_MIN_SECONDS,
    }


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


# CTL / ATL 的指数平滑时间常数（天）。Andy Coggan 经典口径：
# CTL 42 天（约 6 周，衡量「体能」）、ATL 7 天（约 1 周，衡量「疲劳」）。
CTL_DAYS = 42
ATL_DAYS = 7


def training_load_series(
    daily_tss: Iterable[tuple[str, float]],
) -> list[dict[str, Any]]:
    """把每日 TSS 序列展开成 CTL / ATL / TSB 趋势。

    口径：

    - 输入是**按日期升序**的 (date, tss) 对，日期缺口自动补 0（休息日
      TSS 为零，不是「缺失」）。日期是本地日。
    - CTL(d) = CTL(d-1) + (TSS(d) − CTL(d-1)) / 42，ATL 同理但用 7。
      初始值为 0——冷启动时 CTL/ATL 偏低，覆盖到 42 天后就稳了。
    - TSB = CTL − ATL。正数表示恢复充分（体能 > 疲劳），负数表示欠恢复。
    - **不拿训练效果（TE）近似 TSS。** 量纲不同（TE 是 0–5 级），
      拼出来的「CTL」和佳明的不是一回事。

    返回每天一个 dict：date / tss / ctl / atl / tsb，ctl/atl/tsb 保留 1 位小数。
    """
    items = sorted(daily_tss, key=lambda x: x[0])
    if not items:
        return []

    first = date.fromisoformat(items[0][0])
    last = date.fromisoformat(items[-1][0])
    tss_map = {d: t for d, t in items}

    ctl = 0.0
    atl = 0.0
    out: list[dict[str, Any]] = []
    cur = first
    while cur <= last:
        key = cur.isoformat()
        tss = tss_map.get(key, 0.0)
        ctl = ctl + (tss - ctl) / CTL_DAYS
        atl = atl + (tss - atl) / ATL_DAYS
        out.append(
            {
                "date": key,
                "tss": round(tss, 1),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(ctl - atl, 1),
            }
        )
        cur += timedelta(days=1)

    return out
