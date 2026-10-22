"""MCP 服务层：把本地索引暴露成 AI 可调用的工具。

所有返回值都必须经过 Privacy，这是唯一的外泄出口。
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from typing import Any

from .config import Config
from .privacy import Privacy
from .stats import human_duration, human_pace, summarize
from .store import (
    Store,
    row_to_activity,
    row_to_daily,
    row_to_sleep,
    row_to_training,
)

INSTRUCTIONS = """\
本服务读取用户本地的佳明（Garmin）导出数据，不联网、不上传。

数据来自用户手动从 connect.garmin.cn 或 connect.garmin.com 导出的归档包，
因此默认不包含最近一次导出之后的活动。回答时若涉及时间范围，
应说明数据截止日期。

隐私：默认 strict 级别下不提供 GPS 坐标、地点名称与作息时刻，活动 ID 是伪 ID。
不要试图推测用户住址或作息规律。若用户需要更细的数据，请提示其
自行调整 GARMAPLE_PRIVACY 配置，而不是尝试绕过限制。

当前索引包含：活动摘要、每日 wellness 汇总（步数、静息心率、压力、
身体电量、强度分钟）、睡眠（时长与分期）、训练状态（状态判定、
周训练负荷、负荷隧道）。

两点必须注意：
- 睡眠记录按**醒来日**归档。问「昨晚睡得怎么样」要查今天那条，
  查昨天会拿到前一晚的数据。
- 官方导出包里没有睡眠评分数值，也没有 CTL / ATL / TSB 三个体能指标，
  也没有逐秒采样。训练状态与周训练负荷是设备自己判定的结果，
  与 Connect 上显示的一致，但不要把它当成 CTL 用，也不要自行换算。
  被问到时直接说明当前没有这项能力，不要猜测或估算数值。
"""


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
                    "note": "设备判定的结果；不含 CTL / ATL / TSB",
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
        3. 归档里**没有 CTL / ATL / TSB**。`weekly_load` 是佳明的周负荷累计值，
           与 CTL 不是同一个量：不要当作体能值使用，也不要自行换算。
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

    return mcp

def run(cfg: Config) -> int:
    """以 stdio 传输启动服务，供 MCP 客户端拉起。"""
    mcp = build_server(cfg)
    print(f"garmaple MCP 服务已启动（隐私级别 {cfg.privacy}）", file=sys.stderr)
    mcp.run(transport="stdio")
    return 0
