"""MCP 服务层：把本地索引暴露成 AI 可调用的工具。

所有返回值都必须经过 Privacy，这是唯一的外泄出口。
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, timedelta
from typing import Any

from .archive import ArchiveError, discover
from .config import Config
from .parser.fit import downsample, sample_summary
from .parser.sleep import STATUS_OK
from .parser.metrics import RACE_DISTANCES_KM
from .parser.records import GROUPS, GearUsage
from .privacy import Privacy
from .stats import (
    PACE_KEY,
    human_duration,
    human_pace,
    pace_hr_drift,
    recovery_correlations,
    summarize,
    training_load_series,
    zone_summary,
)
from .streams import find_stream
from .store import (
    Store,
    row_to_activity,
    row_to_daily,
    row_to_gear,
    row_to_personal,
    row_to_race,
    row_to_sleep,
    row_to_training,
    row_to_vo2max,
)

INSTRUCTIONS = """\
本服务读取用户本地的佳明（Garmin）导出数据，不联网、不上传。

数据来自用户手动从 connect.garmin.cn 或 connect.garmin.com 导出的归档包，
因此默认不包含最近一次导出之后的活动。回答时若涉及时间范围，
应说明数据截止日期。

隐私：默认 strict 级别下不提供 GPS 坐标、地点名称与作息时刻，活动 ID 是伪 ID。
不要试图推测用户住址或作息规律。若用户需要更细的数据，请提示其
自行调整 GARMAPLE_PRIVACY 配置，而不是尝试绕过限制。

当前索引包含：活动摘要（含心率区间累计时长）、每日 wellness 汇总（步数、
静息心率、压力、身体电量、强度分钟）、睡眠（时长与分期）、训练状态（状态
判定、周训练负荷、负荷隧道）、比赛成绩预测（5K / 10K / 半马 / 全马）、
最大摄氧量、个人最佳纪录、装备里程、恢复指标与配速的相关性、以及从原始
.fit 解析出的逐秒曲线（心率、配速、海拔、步频；覆盖大部分活动但不是全部）。

九点必须注意：
- 睡眠记录按**醒来日**归档。问「昨晚睡得怎么样」要查今天那条，
  查昨天会拿到前一晚的数据。
- 官方导出包里没有睡眠评分数值。
- CTL / ATL / TSB 用 get_training_load 查询，输入是 FIT session 的
  `training_stress_score`（TSS）。这是 FIT 标准字段，但**不是所有设备都写**
  ——本机设备 304 次活动一次没写过，有功率计的设备才会有。
  查不到 TSS 时 tool 会返回 available: false，不要拿训练效果（TE）或
  周训练负荷去近似——量纲不同，拼出来的值和佳明的不是一回事。
- 逐秒曲线是真实采样，不是插值。但它**只覆盖有 .fit 文件的活动**，
  查不到时不要下结论说「那次没记录心率」——先看 get_activity 的汇总值。
  曲线返回的 `series` 是抽稀后的（默认 300 点），极值以 `summary` 为准。
  配速心率漂移只在稳态有氧上成立，间歇跑与冲刺本来前后就该不一样，
  不要拿它评价这类训练。
- 比赛预测是设备按当前体能**推算**的完赛时间，不是已经跑出来的成绩；
  个人最佳里的 5K / 10K / 半马纪录才是实际达成的。两者不要混着说。
- 最大摄氧量只在设备测出有效数据的日子才有记录，日期很稀疏，
  不要按天插值，也不要把缺记录说成数值下降。
- 个人最佳的 `value` 量纲随类型变：成绩是**秒**、距离是**米**、
  步数是**次数**、连续达标是**天**。转述时必须带上 `unit` 或直接用
  `display`，否则会把 1108 秒的 5K 说成 1108 米。
- 装备里程是由关联活动的距离求和得到的。某双装备没有关联记录
  只代表**归档里没记**，不等于没跑过，不要报 0 公里。
- 心率区间只有**每档累计时长**，没有区间上下限（bpm），也没有区间个数设置。
  因此回答不了「Z3 是多少 bpm」，也不要把 zone_N 直接叫成 Z1-Z5 或有氧区间。
- get_recovery_correlation 只做统计描述。相关不等于因果，配速快慢同时受
  课表安排影响（睡得好、状态好的日子往往也是排强度课的日子）；样本不足
  10 对的项不给系数，也不要替它下结论。
"""


def _prediction_change(
    first: dict[str, Any], last: dict[str, Any]
) -> dict[str, Any]:
    """两次预测的差值：正数表示变快。

    只给「变快为正」这一个方向。跑步里说的进步是时间变短，如果输出带符号的
    原始差值，模型很容易把负数读成退步、把「变快 40 秒」说成「慢了 40 秒」。
    """
    out: dict[str, Any] = {}
    for key in RACE_DISTANCES_KM:
        a = first.get("predicted", {}).get(key, {}).get("seconds")
        b = last.get("predicted", {}).get(key, {}).get("seconds")
        if a is None or b is None or a <= 0:
            continue
        out[key] = {
            "faster_by_s": a - b,
            "change_pct": round((a - b) / a * 100, 2),
        }
    return out


def build_server(cfg: Config) -> Any:
    from mcp.server.mcpserver import MCPServer
    # 工具里「预期内的失败」必须抛 ToolError，不能抛 ValueError / RuntimeError：
    # 只有 ToolError 的文本会透给模型。其余异常被 SDK 当成 crash，模型只看到
    # `Error executing tool <name>`，真实原因只进服务端日志——AI 既不知道错在哪，
    # 也就无法改参数重试，只能原样再撞一次。
    from mcp.server.mcpserver.exceptions import ToolError

    privacy = Privacy(cfg.privacy)
    mcp = MCPServer(name="garmaple", instructions=INSTRUCTIONS)

    def _store() -> Store:
        if not cfg.db_path.exists():
            # 没建索引是预期内的情况，不是崩溃：把「去跑 index」这句话送到模型面前
            raise ToolError(
                "尚未建立索引。请先在终端运行 `garmaple index`，"
                f"预期数据库位置：{cfg.db_path}"
            )
        return Store(cfg.db_path)

    def _date(value: str | None, field: str) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        raw = str(value).strip()
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise ToolError(
                f"{field} 需为 YYYY-MM-DD 格式，收到 {raw!r}"
            ) from exc

    # ---------------- 工具 ----------------

    @mcp.tool()
    def get_data_overview() -> dict[str, Any]:
        """查看本地佳明数据的覆盖范围。

        回答与训练相关的问题前，先调用它确认数据的时间范围与运动类型，
        避免把「没有记录」误判为「没有运动」。
        """
        with _store() as store:
            total = store.count_activities()
            lo, hi = store.date_range()
            result = {
                "activities": total,
                "date_from": lo,
                "date_to": hi,
                "sports": store.distinct_sports(),
                "indexed_at": store.get_meta("indexed_at"),
                "privacy_level": privacy.level,
                "note": "本地导出归档，不含最近一次导出之后的数据",
            }
            # 每日汇总可能为空（旧索引，或导出包里没有 UDS 文件），单独给出覆盖范围
            day_count = store.count_days()
            if day_count:
                dlo, dhi = store.day_range()
                result["daily_summary"] = {
                    "days": day_count,
                    "date_from": dlo,
                    "date_to": dhi,
                }
            # 逐秒曲线只覆盖有 .fit 文件的活动，先在这里报出覆盖量，
            # 免得模型以为每次活动都能画曲线
            fit_count = store.count_fit_files()
            if fit_count:
                result["activity_streams"] = {
                    "activities_with_streams": fit_count,
                    "note": "逐秒心率 / 配速 / 海拔 / 步频，用 get_activity_streams 取",
                }
            sleep_count = store.count_sleeps()
            if sleep_count:
                slo, shi = store.sleep_range()
                result["sleep"] = {
                    "nights": sleep_count,
                    "date_from": slo,
                    "date_to": shi,
                    "note": "按醒来日归档；官方导出包不含睡眠评分",
                }
            training_count = store.count_training()
            if training_count:
                tlo, thi = store.training_range()
                result["training_status"] = {
                    "days": training_count,
                    "date_from": tlo,
                    "date_to": thi,
                    "note": "设备判定的结果；CTL/ATL/TSB 另见 get_training_load",
                }
            race_count = store.count_race_predictions()
            if race_count:
                rlo, rhi = store.race_range()
                result["race_predictions"] = {
                    "days": race_count,
                    "date_from": rlo,
                    "date_to": rhi,
                    "note": "设备推算的完赛时间，不是已完成的比赛成绩",
                }
            vo2_count = store.count_vo2max()
            if vo2_count:
                vlo, vhi = store.vo2max_range()
                result["vo2max"] = {
                    "days": vo2_count,
                    "date_from": vlo,
                    "date_to": vhi,
                    "note": "只在测出有效数据的日子有记录，日期稀疏",
                }
            record_count = store.count_personal_records()
            if record_count:
                rows = store.query_personal_records()
                result["personal_records"] = {
                    "total": record_count,
                    "current": sum(1 for r in rows if r["current"]),
                    "note": "设备认定的个人最佳；value 量纲随类型变，见 unit 字段",
                }
            gear_count = store.count_gears()
            if gear_count:
                usage = store.gear_usage()
                result["gear"] = {
                    "items": gear_count,
                    "with_activities": sum(1 for u in usage.values() if u.has_activities),
                    "note": "里程由关联活动求和；没有关联记录不等于没跑过",
                }
            return result

    @mcp.tool()
    def list_activities(
        start_date: str | None = None,
        end_date: str | None = None,
        sport: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """列出活动记录，按日期倒序。

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略
            sport: 例如 running、cycling、swimming、fitness_equipment
            limit: 返回条数，默认 20，上限 100
        """
        capped = max(1, min(int(limit or 20), 100))
        with _store() as store:
            rows = store.query_activities(
                start=_date(start_date, "start_date"),
                end=_date(end_date, "end_date"),
                sport=sport,
                limit=capped,
            )
            return {
                "count": len(rows),
                "activities": [privacy.activity(row_to_activity(r)) for r in rows],
            }

    @mcp.tool()
    def get_activity(activity_id: str) -> dict[str, Any]:
        """按 ID 获取单次活动的完整指标。

        ID 取自 list_activities 的返回结果，形如 act_1a2b3c4d。
        """
        with _store() as store:
            row = store.find_activity(activity_id)
            if row is None:
                return {
                    "error": "未找到该活动",
                    "activity_id": activity_id,
                    "hint": "请先用 list_activities 获取有效 ID",
                }
            return privacy.activity(row_to_activity(row))

    def _activity_stream(store: Store, act: Any) -> tuple[Any, str | None]:
        """按活动取逐秒数据，返回 `(stream, 失败原因)`。

        失败原因分四种，各自的应对方式不一样，所以不在这里统一报错——由调用
        方决定是说「重跑 index」「这次活动没有 FIT」还是「换个工具」。
        """
        if not store.count_fit_files():
            return None, "no_fit_index"
        if act.utc_start is None:
            return None, "no_start_time"
        try:
            data_dir = cfg.require_data()
        except Exception as exc:  # ConfigError：数据目录不可用
            raise ToolError(
                f"逐秒数据需要读原始 .fit 文件，但数据目录不可用：{exc}"
            ) from exc
        try:
            with discover(data_dir) as dataset:
                stream = find_stream(
                    dataset, act.utc_start.isoformat(), act.duration_s, store=store
                )
        except ArchiveError as exc:
            raise ToolError(f"读取归档失败：{exc}") from exc
        return stream, (None if stream is not None else "not_found")

    def _no_stream_response(activity_id: str, reason: str | None) -> dict[str, Any]:
        """逐秒数据取不到时给模型看的话。"""
        if reason == "no_fit_index":
            hint = (
                "索引里没有逐秒记录。归档缺少内嵌的 .fit 文件，"
                "或索引版本过旧——重跑一次 `garmaple index` 即可"
            )
        elif reason == "no_start_time":
            hint = "该活动没有开始时间，无法与 FIT 文件对齐"
        else:
            hint = (
                "这次活动在归档里没有对应的 FIT 文件。导出包里不是每次活动"
                "都带逐秒记录（例如手动添加的记录），可以改用 get_activity "
                "看整段汇总值"
            )
        return {"available": False, "activity_id": activity_id, "hint": hint}

    @mcp.tool()
    def get_activity_streams(activity_id: str, max_points: int = 300) -> dict[str, Any]:
        """单次活动的逐秒曲线：心率、配速、海拔、步频。

        数据来自归档里的原始 `.fit` 文件，是设备每秒记录一次的真实采样，
        不是插值也不是估算。活动摘要里只有整段平均值，要看「第 30 分钟心率
        掉到多少」「后程配速有没有崩」就必须用这个工具。

        结果按列返回（每个指标一个数组，下标对齐 `series.offset_s`）。

        三点要注意：
        - **抽稀不影响极值。** `series` 最多给 `max_points` 个点（默认 300），
          但 `summary` 里的最大 / 最小 / 平均值来自全量采样。不要拿 `series`
          里的数字去说「最高心率」——被跳过的点上可能有更高的值。
        - **没有瞬时最高速度。** `summary` 只给 `speed_avg_mps` 与
          `speed_p95_mps`：GPS 漂移能让一次 6'51"/km 的慢跑冒出 86 km/h 的
          单点，拿最大值当「最快配速」会得出荒谬结论。超过 20 m/s 的采样
          直接按测坏了处理。
        - **`cadence` 是单脚计数。** 跑步时每分钟步数约为它的 2 倍，
          直接说「步频 89」会低得离谱。

        另外两点：
        - **`recorded_s` 可能大于 `duration_s`。** 按下停止后设备仍会继续记录
          一段（实测最长多出 3 分钟），`duration_s` 是活动计时、`recorded_s`
          才是文件的采样跨度。比较活动长度时用 `duration_s`。
        - **`series_missing` 列出整列为空的指标。** 室内跑步机之类没有配速与
          海拔，缺列不等于数值为零。

        曲线不含坐标，也没有绝对时刻（只有相对开始的秒偏移），因此不存在
        泄露路线或作息的问题。

        参数：
            activity_id  取自 list_activities 的 ID
            max_points   series 最多给多少个点，默认 300，范围 20~3000
        """
        if not 20 <= max_points <= 3000:
            raise ToolError(f"max_points 需在 20~3000 之间，收到 {max_points}")

        with _store() as store:
            row = store.find_activity(activity_id)
            if row is None:
                return {
                    "error": "未找到该活动",
                    "activity_id": activity_id,
                    "hint": "请先用 list_activities 获取有效 ID",
                }

            act = row_to_activity(row)
            stream, reason = _activity_stream(store, act)

        if stream is None:
            return _no_stream_response(activity_id, reason)

        picks = downsample(stream.samples, max_points)
        return privacy.stream(
            act,
            picks,
            total=len(stream.samples),
            summary=sample_summary(stream.samples),
            workout_rpe=stream.workout_rpe,
            workout_feel=stream.workout_feel,
        )

    @mcp.tool()
    def get_pace_hr_drift(activity_id: str) -> dict[str, Any]:
        """配速心率漂移（有氧解耦）：后半程是不是用更高的心率换同样的配速。

        算法：把活动按时间切成前后两半，各自算「平均速度 ÷ 平均心率」，
        再看后半比前半低了多少。`drift_pct` **正数表示后半程效率下降**，
        也就是同样配速下心率更高了——跑步圈说的「解耦 5%」就是这个方向。

        读数据时要知道三件事：

        - **`reliable` 只说明样本够不够，不说明结论成立。** 时长不足 20 分钟
          或每半点数不足 30 个时会标 false。样本够了也不代表漂移是「体能有
          问题」——前半程冲太快、后半程放松，同样会让比值变化。
        - **这个指标不区分「配速掉了」和「心率涨了」。** 返回里同时给了
          `pace_change_s_per_km`（正数=变慢）与 `hr_change`（正数=升高），
          要解释成因就看这两个数，不要只凭 `drift_pct` 下结论。
        - **只在配速相对稳定的活动上有意义。** 间歇跑、冲刺训练本来就该前后
          不同，硬套解耦没有意义；长距离有氧跑、骑行才是它的用武之地。

        拿不到数据（没有心率、没有 FIT 文件、采样太少）时会返回
        `available: false` 并说明原因，不是报错。

        参数：
            activity_id  取自 list_activities 的 ID
        """
        with _store() as store:
            row = store.find_activity(activity_id)
            if row is None:
                return {
                    "error": "未找到该活动",
                    "activity_id": activity_id,
                    "hint": "请先用 list_activities 获取有效 ID",
                }
            act = row_to_activity(row)
            stream, reason = _activity_stream(store, act)

        if stream is None:
            return _no_stream_response(activity_id, reason)

        result = pace_hr_drift(stream.samples)
        if not result.get("available"):
            out = privacy.drift(act, result)
            out["hint"] = {
                "no_hr_or_speed": (
                    "这次活动没有同时记录心率与速度（例如力量训练、室内器械），"
                    "算不出配速心率比。可以看 get_activity 的整段汇总值"
                ),
                "too_short": "采样跨度太短，前后半程分不出来",
                "too_few_points": (
                    "有效采样点太少，前后半程各自的平均值会被噪声主导，"
                    "不足以谈漂移。至少要两半各 30 个点"
                ),
            }.get(str(result.get("reason")), "数据不足以计算漂移")
            return out

        return privacy.drift(act, result)

    @mcp.tool()
    def get_period_summary(
        start_date: str,
        end_date: str,
        sport: str | None = None,
    ) -> dict[str, Any]:
        """统计一段时间的训练量：次数、总距离、总时长、加权配速、爬升、心率。

        配速按「总时长 / 总距离」加权，因此不会被单次短距离速度课拉偏。
        参数日期为 YYYY-MM-DD，含端点。
        """
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start is None or end is None:
            raise ToolError("start_date 与 end_date 均为必填")
        if start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            rows = store.query_activities(start=start, end=end, sport=sport)
            result = summarize(rows)
            result["range"] = {"from": start, "to": end}
            if sport:
                result["sport_filter"] = sport
            if not rows:
                result["hint"] = "该区间没有记录，可先用 get_data_overview 确认数据覆盖范围"
            return result

    @mcp.tool()
    def get_hr_zones(
        start_date: str | None = None,
        end_date: str | None = None,
        sport: str | None = None,
    ) -> dict[str, Any]:
        """统计心率区间分布：各区间时长、占比、主要落在哪一档。

        数据来自活动摘要里设备已经算好的每个区间累计时长，不是重新分桶。

        最重要的一点：**归档里没有区间边界（bpm），也没有区间个数的设置。**
        因此本工具只能回答「各区间花了多少时间、占比多少」，回答不了
        「Z3 是 152 到 166 bpm」，也不要自行把 zone_2 翻译成「Z3」或
        「有氧区间」——编号是文件里的 `hrTimeInZone_N`，与手表上显示的分区
        对应关系在导出包里没有定义。被问到边界时就说明归档不含这项数据。

        各区间含义只能按编号顺序描述：编号越大强度越高。zone_0 是强度最低
        的那一档（通常是热身与恢复），编号最大的一档是强度最高的。

        参数：
            start_date / end_date  YYYY-MM-DD，含端点，均可省略；
                两者都省略时统计整个索引范围
            sport                 只看某项运动，例如 running / cycling /
                                  walking / hiking。省略则全部合并

        结果里的 `without_zone_data` 要留意：没有区间记录的活动**不计入**占比，
        因为把缺数据当成「各区间都是 0」会稀释每一档的占比。
        它不为 0 时，占比只代表有记录的那部分活动。
        """
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            rows = store.query_activities(start=start, end=end, sport=sport)
            result = zone_summary(rows)
            result["range"] = {
                "from": start or (rows and rows[-1]["date"]) or None,
                "to": end or (rows and rows[0]["date"]) or None,
            }
            if sport:
                result["sport_filter"] = sport

        if not result.get("activities"):
            return {
                **result,
                "hint": "该区间没有活动记录，可先用 get_data_overview 确认数据覆盖范围",
            }

        if not result.get("total_s"):
            result["hint"] = (
                "这些活动都没有心率区间数据。设备未开启心率监测或归档未包含时"
                "会出现这种情况，不要据此说「没有高强度训练」。"
            )
            return result

        result["zones_note"] = (
            "zone 编号即归档里的 hrTimeInZone_N，编号越大强度越高；"
            "归档不含区间上下限（bpm），因此无法把这七档对应到手表上的 Z1-Z5"
        )
        return result

    @mcp.tool()
    def get_daily_summary(
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 31,
    ) -> dict[str, Any]:
        """查看每日 wellness 汇总：步数、静息心率、压力、身体电量、强度分钟。

        与活动工具互补：活动记录只覆盖练过的日子，日汇总覆盖戴表的日子，
        因此「某天没有训练」用它可以区分「休息」与「没戴表」。

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略；
                两者都省略时返回最近 limit 天
            limit: 最多返回天数，默认 31，上限 120；区间结果超长时只保留最近的部分

        结果按日期正序（便于看趋势）。缺记录的日子直接跳过，
        不补零——补零会把「没戴表」说成「走了 0 步」。
        """
        capped = max(1, min(int(limit or 31), 120))
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            if start is None and end is None:
                rows = store.query_days(limit=capped, order="desc")
                truncated = False
            else:
                rows = store.query_days(start=start, end=end, order="asc")
                truncated = len(rows) > capped
                if truncated:
                    # 超长时留最近的，而不是最早的——用户问的通常是「最近」
                    rows = rows[-capped:]

            days = [privacy.day(row_to_daily(r)) for r in rows]
            if not days:
                return {
                    "count": 0,
                    "days": [],
                    "hint": "该区间没有每日汇总。可用 get_data_overview 确认数据覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }

            result: dict[str, Any] = {
                "count": len(days),
                "range": {"from": days[0]["date"], "to": days[-1]["date"]},
                "days": days,
            }
            if truncated:
                result["truncated"] = True
                result["note"] = f"区间内记录多于 {capped} 天，只返回最近 {capped} 天"
            return result

    @mcp.tool()
    def get_sleep(
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """查看每晚睡眠：时长、分期、呼吸率。

        重要：记录按**醒来日**归档。用户问「昨晚睡得怎么样」要看**今天**这条，
        查昨天会拿到前一晚的数据。这一点答错整段结论都会错位。

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略
            limit: 最多返回晚数，上限 120。
                不给日期时默认最近 14 晚；给了日期则默认返回区间内全部
                （超过 120 晚才截断并置 truncated）

        结果按日期正序。每晚的 status 说明可信度：
            ok          有完整分期
            off_wrist   当晚摘了手表，没有睡眠数据（不是「睡了 0 分钟」）
            no_stages   确认了睡眠窗但设备没记分期
            unconfirmed 睡眠窗未确认

        非 ok 的夜晚只有 date 与 status，不带时长：那些「睡眠窗」分别是
        摘下到戴上的间隔、或固定占位值，当成睡眠时长会被读成睡了十几小时。
        官方导出包里没有睡眠评分数值，被问到时直接说明没有，不要估算。
        """
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        # 没给日期时默认看最近两周；给了日期就尊重区间，只在超过上限时截断——
        # 否则「查这个月的睡眠」会静默只拿到两周，用户还以为中间缺数据
        default_limit = 14 if (start is None and end is None) else 120
        capped = max(1, min(int(limit or default_limit), 120))

        with _store() as store:
            if start is None and end is None:
                rows = store.query_sleeps(limit=capped, order="desc")
                truncated = False
            else:
                rows = store.query_sleeps(start=start, end=end, order="asc")
                truncated = len(rows) > capped
                if truncated:
                    rows = rows[-capped:]

            nights = [privacy.sleep(row_to_sleep(r)) for r in rows]
            if not nights:
                return {
                    "count": 0,
                    "nights": [],
                    "hint": "该区间没有睡眠记录。可用 get_data_overview 确认覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }

            result: dict[str, Any] = {
                "count": len(nights),
                "range": {"from": nights[0]["date"], "to": nights[-1]["date"]},
                "nights": nights,
            }
            missing = {n["status"] for n in nights if n.get("status") != "ok"}
            if missing:
                result["status_note"] = (
                    "这些晚没有有效睡眠数据，原因见 status："
                    "off_wrist=摘表，no_stages=设备未记分期，unconfirmed=睡眠窗未确认。"
                    "它们都不是「睡了 0 分钟」。"
                )
            if truncated:
                result["truncated"] = True
                result["note"] = f"区间内记录多于 {capped} 晚，只返回最近 {capped} 晚"
            return result

    @mcp.tool()
    def get_training_status(
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 31,
    ) -> dict[str, Any]:
        """查看设备判定的训练状态与负荷隧道。

        这些是**手表自己算出的结论**，不是本工具从活动数据推的，因此与
        Connect 上「训练状态」页一致；导出包里没有的，这里也没有。

        读结果前必须知道的四点：

        1. `status` 是训练状态判定。`no_status` 表示**设备数据不足、尚无法判定**，
           不是「没练好」，不要把它归到负面状态里。
        2. `load_position` 是周训练负荷相对 `load_tunnel`（最优负荷区间）的位置，
           取 below / in / above。低于区间不等于退步——恢复周本就该低。
        3. `weekly_load` 是佳明的周负荷累计值，与 CTL 不是同一个量。
           CTL / ATL / TSB 用 get_training_load 查（需要设备写 TSS），
           不要拿 weekly_load 当体能值使用，也不要自行换算。
        4. 一天一条。设备一天会写多条同步快照，这里只留当日最后一次。

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略；
                两者都省略时返回最近 limit 天
            limit: 最多返回天数，默认 31，上限 120；区间结果超长时只保留最近的部分

        结果按日期正序，`latest` 是最后一天那条，回答「我现在的训练状态」直接用它。
        状态与趋势都带中文标签，`status_label` 比英文枚举更不容易读反。
        """
        capped = max(1, min(int(limit or 31), 120))
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            if start is None and end is None:
                rows = store.query_training(limit=capped, order="desc")
                truncated = False
            else:
                rows = store.query_training(start=start, end=end, order="asc")
                truncated = len(rows) > capped
                if truncated:
                    rows = rows[-capped:]

            days = [privacy.training(row_to_training(r)) for r in rows]
            if not days:
                return {
                    "count": 0,
                    "days": [],
                    "hint": "该区间没有训练状态记录。可用 get_data_overview 确认覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }

            result: dict[str, Any] = {
                "count": len(days),
                "range": {"from": days[0]["date"], "to": days[-1]["date"]},
                "latest": days[-1],
                "days": days,
            }
            # 多天时给出计数，省得模型自己数还数错
            if len(days) > 1:
                result["status_counts"] = dict(
                    Counter(d["status"] for d in days).most_common()
                )
                positions = Counter(
                    d["load_position"] for d in days if d.get("load_position")
                )
                if positions:
                    result["load_position_counts"] = dict(positions.most_common())
            if truncated:
                result["truncated"] = True
                result["note"] = f"区间内记录多于 {capped} 天，只返回最近 {capped} 天"
            return result

    @mcp.tool()
    def get_training_load(
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 90,
    ) -> dict[str, Any]:
        """训练负荷趋势：CTL（体能）、ATL（疲劳）、TSB（状态）。

        输入是 FIT `session` 消息的 `training_stress_score`（TSS）。
        逐日 TSS 做指数平滑：CTL 用 42 天 EMA、ATL 用 7 天 EMA、
        TSB = CTL − ATL。正 TSB 表示恢复充分，负 TSB 表示欠恢复。

        四点必须知道：
        - **TSS 是 FIT 标准字段，但不是所有设备都写。** 戴功率计或
          高端手表的导出会写满；本机老设备 304 次活动全空。
          没有 TSS 时 tool 返回 `available: false`，不要拿训练效果（TE）
          或周训练负荷近似——量纲不同。
        - **CTL/ATL 是累积值，冷启动偏低。** 前 42 天 CTL 持续爬升
          （42 天后约到稳态值的 63%），不是「体能在涨」，是历史数据不够。
          要看趋势变化，关注**相对变化**而不是绝对值。
        - **TSB 正数 = 恢复充分，负数 = 欠恢复。** 这是 CTL − ATL 的
          算术差，不是设备的「训练状态」判定（那是 get_training_status）。
        - **日期缺口自动补 0。** 没训练的日子 TSS 为零参与平滑。

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略
            limit: 最多返回天数，默认 90，上限 365

        结果按日期正序：
            days     趋势数组（date / tss / ctl / atl / tsb）
            latest   最后一天，回答「我现在体能怎么样」直接用它
        """
        capped = max(1, min(int(limit or 90), 365))
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            tss_count = store.count_fit_loads()
            if not tss_count:
                return {
                    "available": False,
                    "reason": "no_tss_data",
                    "hint": (
                        "归档里没有 TSS（training_stress_score）数据。"
                        "这是 FIT session 的标准字段，需要设备写入——"
                        "有功率计或高端手表的导出才有。"
                        "本机设备不写 TSS，无法计算 CTL/ATL/TSB。"
                        "可以改用 get_training_status 看训练状态与周训练负荷。"
                    ),
                }

            full_series = training_load_series(store.daily_tss())
            if not full_series:
                return {
                    "available": False,
                    "reason": "no_tss_data",
                    "hint": "TSS 数据存在但无法与活动日期对齐，请重跑 garmaple index",
                }

            # 先按请求窗口过滤，再截断到 limit
            if start:
                full_series = [d for d in full_series if d["date"] >= start]
            if end:
                full_series = [d for d in full_series if d["date"] <= end]

            truncated = len(full_series) > capped
            if truncated:
                full_series = full_series[-capped:]

            if not full_series:
                return {
                    "available": True,
                    "count": 0,
                    "days": [],
                    "hint": "该区间没有 TSS 数据。可用 get_data_overview 确认覆盖范围",
                }

            result: dict[str, Any] = {
                "available": True,
                "count": len(full_series),
                "range": {"from": full_series[0]["date"], "to": full_series[-1]["date"]},
                "latest": full_series[-1],
                "days": full_series,
                "note": (
                    "CTL=42 天 EMA、ATL=7 天 EMA、TSB=CTL−ATL。"
                    "TSS 来自 FIT session 的 training_stress_score 字段。"
                    "前 42 天 CTL 冷启动偏低（约到稳态 63%），看趋势变化别看绝对值。"
                ),
            }
            if truncated:
                result["truncated"] = True
                result["note"] += f" 区间超 {capped} 天，只返回最近 {capped} 天"
            return result

    @mcp.tool()
    def get_recovery_correlation(
        start_date: str | None = None,
        end_date: str | None = None,
        sport: str | None = "running",
        min_distance_km: float = 3.0,
    ) -> dict[str, Any]:
        """恢复与表现的相关性：睡眠 / 压力 / 身体电量 / 静息心率 对 配速。

        纯统计描述工具：把每次训练配成一个样本行——

            y   当天平均配速（秒/公里，数值越小越快）
            x   前一晚睡眠（睡着 / 深睡 / REM，按醒来日取**当天**那条）、
                前一日平均压力、前一日结束时的身体电量、当日静息心率

        对每个 x 算 Pearson 与 Spearman 相关系数，并给出 α=0.05 双侧的
        显著性判定。**相关不是因果**：睡得好、状态好的日子往往也是排
        强度课的日子，配速快可能来自课表而不是恢复，本工具分不开这两件事。

        口径与限制（notes 字段会随结果原样返回）：

        - 每个样本行至少 `min_distance_km` 公里且时长满 5 分钟——热身与
          碎片记录的配速没有分析价值。
        - 同一天多次训练只取**距离最长的一次**：同一晚睡眠不该被算进
          两个样本行，否则 n 是虚的。
        - 各 x 的 n 是「x 与 y 同时有记录」的对数，彼此不同；不足 10 对
          的项不给系数，只给计数。
        - 静息心率是当日（含当夜睡眠期间）的日汇总值，不是晨脉。

        参数：
            start_date / end_date  YYYY-MM-DD，含端点，均可省略
            sport            只看某项运动，默认 running；传空串则合并
                             全部运动（不同运动的配速混在一起没有意义，
                             不建议）
            min_distance_km  样本行的最短距离门槛，默认 3 公里

        睡眠或每日汇总缺失时各项的 n 会明显缩水，先看 sample 里的计数
        再解读，不要把「没记录」当成「睡了 0 分钟」或「没压力」。
        """
        if not 0 < float(min_distance_km) <= 100:
            raise ToolError(f"min_distance_km 需在 0~100 之间，收到 {min_distance_km}")
        min_m = float(min_distance_km) * 1000.0
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        sport_filter = (sport or "").strip().lower() or None

        with _store() as store:
            acts = store.query_activities(
                start=start, end=end, sport=sport_filter, order="asc"
            )

            # 同日多次只留最长的一次。放在距离门槛之后数，multi 才是
            # 「因去重被丢掉的真实训练」而不是「本来就不合格的热身」。
            by_date: dict[str, Any] = {}
            multi_session_days = 0
            for row in acts:
                d = row["date"]
                dist = row["distance_m"] or 0.0
                dur = row["duration_s"] or 0.0
                if not d or dist < min_m or dur < 300:
                    continue
                if d in by_date:
                    multi_session_days += 1
                    if (by_date[d]["distance_m"] or 0) >= dist:
                        continue
                by_date[d] = row

            if not by_date:
                return {
                    "available": False,
                    "reason": "no_eligible_sessions",
                    "sport_filter": sport_filter,
                    "min_distance_km": min_distance_km,
                    "activities_in_range": len(acts),
                    "hint": (
                        "该区间没有满足门槛的训练（距离不足或时长不足 5 分钟），"
                        "可先用 get_data_overview 确认数据覆盖范围，"
                        "或调低 min_distance_km"
                    ),
                }

            # 睡眠按醒来日对齐：活动日 D 配的正是「当天早上醒来」那条，
            # 也就是训练前一夜。days 需要覆盖到最早活动日的前一天
            # （前一日压力 / 身体电量），所以整表读入再按日期查——
            # 两张表都只有几百行，不值得为省内存写区间拼接。
            sleeps = {
                r["date"]: r
                for r in store.query_sleeps()
                if r["status"] == STATUS_OK
            }
            days = {r["date"]: r for r in store.query_days()}

            samples: list[dict[str, Any]] = []
            for d in sorted(by_date):
                row = by_date[d]
                dist = row["distance_m"]
                dur = row["duration_s"]
                pace = dur / (dist / 1000.0) if dist else None

                srow = sleeps.get(d)
                sleep_min = None
                if srow is not None and srow["in_bed_min"] is not None:
                    # 睡着 = 窗口 − 清醒 − 测不到，与索引层同一套口径
                    raw = (
                        srow["in_bed_min"]
                        - (srow["awake_min"] or 0)
                        - (srow["unmeasured_min"] or 0)
                    )
                    sleep_min = raw if raw > 0 else None

                prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
                prow = days.get(prev)
                drow = days.get(d)
                samples.append(
                    {
                        "date": d,
                        PACE_KEY: round(pace, 1) if pace else None,
                        "sleep_min": sleep_min,
                        "deep_min": srow["deep_min"] if srow else None,
                        "rem_min": srow["rem_min"] if srow else None,
                        "prev_stress_avg": prow["stress_avg"] if prow else None,
                        "prev_bb_end": prow["bb_end"] if prow else None,
                        "resting_hr": drow["resting_hr"] if drow else None,
                    }
                )

            result = recovery_correlations(samples)

        dates = sorted(by_date)
        out: dict[str, Any] = {
            "available": result["available"],
            "sport_filter": sport_filter,
            "min_distance_km": min_distance_km,
            "range": {"from": dates[0], "to": dates[-1]},
            "sample": {
                "activities_in_range": len(acts),
                "sessions_used": len(samples),
                "multi_session_days_dropped": multi_session_days,
            },
            "y": result["y"],
            "correlations": result["correlations"],
            "notes": result["notes"],
        }
        if not result["available"]:
            out["hint"] = (
                "样本不足：每个相关系数至少要 10 对同时有记录的样本。"
                "睡眠或每日汇总缺失会让各项 n 缩水，先看 sample 与各项 n "
                "判断缺的是哪部分数据；若索引早于本功能，请重跑 garmaple index"
            )
        return out

    @mcp.tool()
    def get_race_predictions(
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 31,
    ) -> dict[str, Any]:
        """查看设备推算的比赛成绩预测：5K / 10K / 半马 / 全马的预计完赛时间。

        这是手表按当前体能**推算**出来的成绩，**不是已经跑出来的比赛成绩**，
        两者不要混着说。它也不能当 CTL / 体能值用——那是另外一套指标，
        归档里没有。

        想看体能走向时用它比看训练状态更直接：预测时间在变短就是体能变好。
        与 `get_training_status` 的 `weekly_load` 是两套不同的东西，
        后者是负荷，前者是能力。

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略；
                两者都省略时返回最近 limit 天
            limit: 最多返回天数，默认 31，上限 120；区间结果超长时只保留最近的部分

        结果按日期正序：
            latest   最后一天那条，带可读时间与预测配速，回答「我 5K 能跑多少」直接用它
            days     趋势数组，每个距离**只给秒数**（键名 5k / 10k / half / marathon），
                     便于做比较，也免得 120 天 × 4 距离的格式化文本把上下文撑满
            change   对比本次返回的首末两天：
                     faster_by_s  为正表示变快（时间变短），不是原始差值
                     change_pct   变化百分比，同样正数表示变快

        缺某个距离时该键直接不出现，不是 0。
        """
        capped = max(1, min(int(limit or 31), 120))
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            if start is None and end is None:
                rows = store.query_race_predictions(limit=capped, order="desc")
                truncated = False
            else:
                rows = store.query_race_predictions(start=start, end=end, order="asc")
                truncated = len(rows) > capped
                if truncated:
                    rows = rows[-capped:]

            days = [privacy.prediction(row_to_race(r)) for r in rows]
            if not days:
                return {
                    "count": 0,
                    "days": [],
                    "hint": "该区间没有比赛预测。可用 get_data_overview 确认覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }

            # 趋势数组只留秒数：可读时间与配速由 latest 承担，否则 120 天 × 4 距离
            # × 3 个字段能到 40KB，白白吃掉上下文。
            series = [
                {"date": d["date"], **{
                    k: v["seconds"] for k, v in d["predicted"].items()
                }}
                for d in days
            ]

            result: dict[str, Any] = {
                "count": len(days),
                "range": {"from": days[0]["date"], "to": days[-1]["date"]},
                "latest": days[-1],
                "days": series,
            }
            if len(days) > 1:
                change = _prediction_change(days[0], days[-1])
                if change:
                    result["change"] = change
                    result["change_note"] = (
                        "faster_by_s 为正表示预测变快（时间变短）；"
                        "对比的是本次返回区间的首末两天"
                    )
            if truncated:
                result["truncated"] = True
                result["note"] = f"区间内记录多于 {capped} 天，只返回最近 {capped} 天"
            return result

    @mcp.tool()
    def get_vo2max_trend(
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        """查看最大摄氧量（VO2max）的历次测量与变化。

        重要：**只有设备测出有效数据的日子才有记录**，日期天然稀疏
        （可能是隔几天一条，也可能隔几周）。中间的空缺不是「数值下降」，
        也不能按天插值补点——补出来的曲线是编的。

        返回：
            samples  按日期正序的每次测量：date / vo2max / sport
            latest   最后一次测量，回答「我现在多少」用它
            range    最小值、最大值及其日期
            change   首末差值（正数表示提高）

        参数：
            start_date / end_date: YYYY-MM-DD，含端点，均可省略
            limit: 最多返回条数，默认 120（实测全年也不到 100 条，等于全给）。
                给日期时结果超长只保留最近的部分并置 truncated

        归档里没有「体能年龄」的可用数值——设备写的是固定占位值，
        因此本工具不提它。被问到体能年龄时说明没有这项数据。
        """
        capped = max(1, min(int(limit or 120), 120))
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and start > end:
            raise ToolError("start_date 不能晚于 end_date")

        with _store() as store:
            if start is None and end is None:
                rows = store.query_vo2max(limit=capped, order="desc")
                truncated = False
            else:
                rows = store.query_vo2max(start=start, end=end, order="asc")
                truncated = len(rows) > capped
                if truncated:
                    rows = rows[-capped:]

            samples = [privacy.vo2max(row_to_vo2max(r)) for r in rows]
            if not samples:
                return {
                    "count": 0,
                    "samples": [],
                    "hint": "该区间没有最大摄氧量记录。可用 get_data_overview 确认覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }

            values = [(s["date"], s["vo2max"]) for s in samples]
            lowest = min(values, key=lambda p: p[1])
            highest = max(values, key=lambda p: p[1])

            result: dict[str, Any] = {
                "count": len(samples),
                "range": {"from": samples[0]["date"], "to": samples[-1]["date"]},
                "latest": samples[-1],
                "lowest": {"date": lowest[0], "vo2max": lowest[1]},
                "highest": {"date": highest[0], "vo2max": highest[1]},
                "samples": samples,
                "sparsity_note": "记录只出现在设备测出有效数据的日子，"
                                 "样本间的空缺不代表数值下降",
            }
            if len(samples) > 1:
                result["change"] = {
                    "delta": round(samples[-1]["vo2max"] - samples[0]["vo2max"], 1),
                    "note": "正数表示提高；对比的是本次返回区间的首末两次测量",
                }
            if truncated:
                result["truncated"] = True
                result["note"] = f"区间内记录多于 {capped} 条，只返回最近 {capped} 条"
            return result

    @mcp.tool()
    def get_personal_records(group: str | None = None) -> dict[str, Any]:
        """查看设备认定的个人最佳纪录：跑、游、骑的最好成绩与最大量。

        这些是**实际达成**的成绩，与 `get_race_predictions` 的推算值不是一回事，
        回答「我 5K 最好成绩」用它，回答「我 5K 能跑多少」用预测。

        最重要的坑：`value` 的量纲随类型变化，同一个字段四种单位。
            s      成绩，秒            Best 5km Run = 1108.3
            m      距离，米            Farthest Run = 21116.4
            count  计数，次数          Most Steps in a Day = 36434
            day    连续天数            Longest Goal Streak = 9
        转述时必须看 `unit`，或者直接用 `display`（已按量纲格式化）。
        把 1108 秒当成距离，就会说出「5 公里跑了 1108 米」。

        结果结构：
            count        当前有效纪录条数
            groups       按运动分组：run / swim / cycle / general
            group_counts 每组的条数
            superseded   被刷新过的旧纪录（含 date 与 value）
            unit_note    上述单位说明，原样引用即可

        两点边界：
        - 步数与连续达标类**没有 date**：归档里它们只有区间起点，
          拿它当达成日会读成「一天之内创下多项纪录」。
        - `superseded` 里的纪录已经被当前纪录取代，是进步的证据，
          不是并列成绩，不要说成「有两个 5K 最好成绩」。

        参数：
            group  只看某一组，取 run / swim / cycle / general，省略则全给

        归档里没有的类型（例如全马最佳）就是没有记录过——本工具不推算、
        不用预测值顶替。被问到没有的类型时说明归档里没有，不要拿预测值冒充。
        """
        wanted: str | None = None
        if group is not None and str(group).strip():
            wanted = str(group).strip().lower()
            if wanted not in GROUPS:
                raise ToolError(
                    f"group 需为 {', '.join(GROUPS)} 之一，收到 {group!r}"
                )

        with _store() as store:
            rows = store.query_personal_records()
            if not rows:
                return {
                    "count": 0,
                    "groups": {},
                    "hint": "索引里没有个人最佳记录。可用 get_data_overview 确认覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }
            records = [privacy.record(row_to_personal(r)) for r in rows]

        current = [r for r in records if r.get("current")]
        superseded = [r for r in records if not r.get("current")]
        if wanted:
            current = [r for r in current if r["group"] == wanted]
            superseded = [r for r in superseded if r["group"] == wanted]
            if not current and not superseded:
                return {
                    "count": 0,
                    "groups": {},
                    "hint": f"归档里没有 {wanted} 组的纪录。"
                            f"可选分组：{', '.join(GROUPS)}",
                }

        groups: dict[str, list[dict[str, Any]]] = {}
        for rec in current:
            groups.setdefault(rec["group"], []).append(rec)

        result: dict[str, Any] = {
            "count": len(current),
            "groups": groups,
            "group_counts": {k: len(v) for k, v in groups.items()},
            "unit_note": "value 的量纲由 unit 说明：s=秒、m=米、count=次数、day=天；"
                         "display 是格式化后的写法，可直接转述",
        }
        if superseded:
            result["superseded"] = superseded
            result["superseded_note"] = (
                "这些是被刷新过的旧纪录，已被上面的当前纪录取代；"
                "可以用来对比进步，不是并列成绩"
            )
        if any("date" not in r for r in current):
            result["date_note"] = (
                "步数与连续达标类没有 date：归档里它们只有区间起点，"
                "不是达成日"
            )
        return result

    @mcp.tool()
    def get_gear_mileage(include_inactive: bool = False) -> dict[str, Any]:
        """查看装备（跑鞋等）的累计里程与寿命使用比例。

        里程口径：**关联活动的距离之和**。归档里装备本身没有累计里程字段，
        所以这个数是本工具从活动表加出来的，与 Connect 上显示的可能有差异
        （差别通常来自没关联到装备的活动）。

        每双装备给出：
            distance_km     累计里程
            activity_count  参与计算的活动次数
            first_activity / last_activity  首次与最后一次的日期
            life_limit_km   设备里设置的建议里程上限（用户自己填的）
            remaining_km    上限减去已跑；**为负表示已超出**，是超期服役的事实，
                            不是数据错误，不要截成 0，也不要据此说「数据异常」
            used_pct        已用掉上限的百分比
            by_sport        按运动的明细：跑鞋的里程里会混进步行记录，
                            只给总数就没法解释「为什么比我自己记的多」

        重要：`activity_count` 缺失或为 0 时，返回的是 `note` 而不是 0 公里。
        那表示**归档里没有这双装备的关联记录**，可能没同步过、也可能本来就没记，
        不等于「买了没穿过」。被问到时就按 note 的说法讲。

        参数：
            include_inactive  是否包含非使用中（已退役）的装备，默认只看使用中的
        """
        with _store() as store:
            rows = store.query_gears()
            if not rows:
                return {
                    "count": 0,
                    "gears": [],
                    "hint": "索引里没有装备记录。可用 get_data_overview 确认覆盖范围；"
                            "若索引早于本次升级，请重新运行 garmaple index。",
                }
            usage = store.gear_usage()
            rows_usage = {r["gear_id"]: usage.get(r["gear_id"], GearUsage()) for r in rows}
            gears = [
                privacy.gear(row_to_gear(r), rows_usage[r["gear_id"]]) for r in rows
            ]

        excluded = 0
        if not include_inactive:
            kept = [
                g for g in gears
                if g.get("status") in (None, "active")
            ]
            excluded = len(gears) - len(kept)
            gears = kept

        result: dict[str, Any] = {
            "count": len(gears),
            "gears": gears,
            "mileage_note": "里程为关联活动的距离之和，不是设备直接给的累计值",
        }
        if excluded:
            result["excluded_inactive"] = excluded
            result["excluded_note"] = (
                "已排除非使用中的装备；要看全部请传 include_inactive=true"
            )
        if any("note" in g for g in gears):
            result["missing_note"] = (
                "带 note 的装备在归档里没有关联活动记录，因此没有里程可算；"
                "这不等于没跑过"
            )
        return result

    return mcp

def run(cfg: Config) -> int:
    """以 stdio 传输启动服务，供 MCP 客户端拉起。"""
    mcp = build_server(cfg)
    print(f"garmaple MCP 服务已启动（隐私级别 {cfg.privacy}）", file=sys.stderr)
    mcp.run(transport="stdio")
    return 0
