"""排行榜（P0）：收益率为主、总资产为辅（D8）；破产玩家墓碑展示。

t1：新增「全体榜」口径——`top(include_npc=True)` 把 `npc:` 前缀的
模拟股民警账户与真实玩家混入同一排序，`render()` 用 🤖 标记区分。
边界：`snapshot_day()` / `render_bankrupt()` 仍只统计真实玩家
（is_npc=False），历史曲线与破产叙事是玩家自己的故事。
"""

from __future__ import annotations

from ..storage.db import Database
# 复用 npc 模块的 user_id 前缀判定（单一事实源，避免两处硬编码 "npc:"）
from .npc import is_npc as _is_npc


class LeaderboardService:
    def __init__(self, db: Database):
        self.db = db

    async def top(self, price_of, limit: int = 10,
                  include_npc: bool = False) -> list[dict]:
        """榜单计算：收益率为主、总资产为辅（D8）。

        include_npc=False（默认）=「玩家榜」：只统计真实玩家，NPC 账户
        被 `list_accounts(is_npc=False)` 过滤掉，不污染玩家口径。
        include_npc=True =「全体榜」：真实玩家与模拟股民警（`npc:` 前缀）
        混入同一排序，每条 NPC 条目带 `is_npc: True`，由 render() 加 🤖
        标记区分。排序口径两者一致（active 按收益率，bankrupt 垫底）。
        注意全体榜必须取**全表**（list_accounts(is_npc=None)），只传
        is_npc=True 会反过来只剩 NPC——这正是 t1 第一轮踩的坑。

        边界（t1）：`snapshot_day()` 与 `render_bankrupt()` 仍是
        is_npc=False——历史曲线与破产叙事是玩家的故事，见各自 docstring。
        """
        rows = await self.db.list_accounts(
            is_npc=None if include_npc else False)
        entries = []
        for acc in rows:
            positions = await self.db.get_positions(acc["user_id"])
            mv = 0.0
            for pos in positions:
                p = price_of(pos["code"])
                if p is not None:
                    mv += p * pos["quantity"]
            total = acc["cash_available"] + mv
            initial = acc["initial_cash"] or 1.0
            entries.append({
                "user_id": acc["user_id"],
                "name": acc["user_name"],
                "total": total,
                "return_pct": (total - initial) / initial * 100,
                "status": acc["status"],
                "rebirth": acc["rebirth_count"] or 0,
                "is_npc": _is_npc(acc["user_id"]),
            })
        actives = [e for e in entries if e["status"] == "active"]
        bankrupted = [e for e in entries if e["status"] == "bankrupt"]
        actives.sort(key=lambda e: (-e["return_pct"], -e["total"]))
        bankrupted.sort(key=lambda e: -e["total"])
        return (actives + bankrupted)[:limit]

    async def period_top(self, period: str, price_of, limit: int = 10):
        """日榜/周榜（DESIGN §15 周期维度）：窗口收益率 = 当前实盘 vs 快照基线。

        period="day"  日榜：基线 = 最近一次日终快照（盘中看即昨收基准；
                      收盘后实盘与快照相等、收益归零属正常）。
        period="week" 周榜：基线 = 近 7 个快照日窗口内该玩家**最早出现**的
                      那天（窗口做单调键窗口，不日历算术，与 render_history
                      的口径一致）。
        当前总资产用 price_of 实时计算，与总榜 top() 同口径；真实玩家
        （is_npc=False）——NPC 不入快照也不入本榜。
        无基线（窗口内未出现，新开户）→ 基线退回 initial_cash 即累计收益，
        对当日新开户者累计恰好等于当日收益；基线 total<=0（异常账面）同样
        退回累计，避免除零。
        返回 None = 还没有任何日终快照（首个虚拟交易日收盘前）。
        排序与 top() 一致：窗口收益率降序、总资产次之，bankrupt 垫底。
        """
        if period not in ("day", "week"):
            raise ValueError(f"unknown leaderboard period: {period!r}")
        window = 1 if period == "day" else 7
        rows = await self.db.list_leaderboard_snapshots(window)
        if not rows:
            return None
        by_day: dict[int, dict[str, dict]] = {}
        for r in rows:
            by_day.setdefault(r["day_ts"], {})[r["user_id"]] = r
        days = sorted(by_day)                    # 升序：days[-1]=最近收盘日
        latest = by_day[days[-1]]
        first: dict[str, dict] = {}
        for d in days:                           # 升序 setdefault = 窗口首现
            for uid, r in by_day[d].items():
                first.setdefault(uid, r)
        entries = []
        for acc in await self.db.list_accounts(is_npc=False):
            uid = acc["user_id"]
            mv = 0.0
            for pos in await self.db.get_positions(uid):
                p = price_of(pos["code"])
                if p is not None:
                    mv += p * pos["quantity"]
            total = acc["cash_available"] + mv
            initial = acc["initial_cash"] or 1.0
            base = latest.get(uid) if period == "day" else first.get(uid)
            if base is None:
                ret = (total - initial) / initial * 100
            elif base["total"] > 0:
                ret = (total - base["total"]) / base["total"] * 100
            else:
                ret = (total - initial) / initial * 100
            entries.append({
                "user_id": uid,
                "name": acc["user_name"],
                "total": total,
                "return_pct": ret,
                "status": acc["status"],
                "rebirth": acc["rebirth_count"] or 0,
                "is_npc": _is_npc(uid),
            })
        actives = [e for e in entries if e["status"] == "active"]
        bankrupted = [e for e in entries if e["status"] == "bankrupt"]
        actives.sort(key=lambda e: (-e["return_pct"], -e["total"]))
        bankrupted.sort(key=lambda e: -e["total"])
        return (actives + bankrupted)[:limit]

    def render(self, entries: list[dict],
               title: str = "🏆 股神排行榜（按收益率）") -> str:
        """渲染榜单文本。

        title 可区分两种榜：默认「🏆 股神排行榜（按收益率）」为玩家榜，
        全体榜传「🏆 全体排行榜（玩家 + 模拟股民警）」。
        每条 is_npc 条目渲染时加 🤖 前缀标记。
        """
        if not entries:
            return "还没有玩家开户，发送 /炒股 开户 成为第一位股民！"
        lines = [title]
        medals = ["🥇", "🥈", "🥉"]
        for i, e in enumerate(entries):
            rank = medals[i] if i < 3 else f"{i + 1}."
            bot = "🤖" if e.get("is_npc") else ""
            rb = e.get("rebirth", 0)
            rb_tag = f"　🔄重生{rb}次" if rb else ""
            if e["status"] == "bankrupt":
                lines.append(f"{rank} {bot}💀 {e['name']} —— 已破产{rb_tag}")
            else:
                sign = "+" if e["return_pct"] >= 0 else ""
                lines.append(
                    f"{rank} {bot}{e['name']}  {sign}{e['return_pct']:.2f}%  "
                    f"总资产 {e['total']:,.0f}{rb_tag}")
        return "\n".join(lines)

    # ── 历史曲线（P3）──────────────────────────────────────
    async def snapshot_day(self, day_ts: int, price_of) -> int:
        """日终落一条排行榜快照（P3 历史曲线数据源）。

        锁兼容性（2026-10-11 由 spec_fixer / hang_analyst 双人只读核实）：
        本方法**自身无锁**——只调 db 的 list_accounts / get_positions /
        insert_leaderboard_snapshot，而 storage/db.py 全库不 import
        trade_lock（db 层锁免疫），db.transaction() 也从不 acquire
        trade_lock。所以它在 DayCloseService._run() 的 trade_lock 锁内
        调用**是安全的**（这正是 game/dayclose.py:44-58 的接线方式），
        在锁外调用亦可，但会读到与日终结算无关的中间态。
        写入整批走 db.transaction()：一条 day_ts 的所有快照要么全落要么
        全不落，否则历史曲线会出现半截日期（参照 margin C1 的教训）。
        day_ts 为当日零点时间戳，由调用方推算；幂等由 PK(day_ts, rank)
        的 INSERT OR REPLACE 保证。返回记录条数。
        NPC 账户（`npc:` 前缀）不入快照——历史曲线是玩家的故事。
        这个边界在 t1 常开改造后**仍然成立**：模拟股民警只在
        `top(include_npc=True)` 的全体榜出现，不写历史快照。
        """
        rows = await self.db.list_accounts(is_npc=False)
        entries = []
        for acc in rows:
            cash = acc["cash_available"]
            mv = 0.0
            for pos in await self.db.get_positions(acc["user_id"]):
                p = price_of(pos["code"])
                if p is not None:
                    mv += p * pos["quantity"]
            total = cash + mv
            initial = acc["initial_cash"] or 1.0
            entries.append({
                "user_id": acc["user_id"],
                "name": acc["user_name"],
                "cash": cash,
                "mv": mv,
                "total": total,
                "return_pct": (total - initial) / initial * 100,
                "status": acc["status"],
                "rebirth": acc["rebirth_count"] or 0,
            })
        actives = [e for e in entries if e["status"] == "active"]
        bankrupted = [e for e in entries if e["status"] == "bankrupt"]
        actives.sort(key=lambda e: (-e["return_pct"], -e["total"]))
        bankrupted.sort(key=lambda e: -e["total"])
        async with self.db.transaction():
            for rank, e in enumerate(actives + bankrupted, 1):
                await self.db.insert_leaderboard_snapshot(
                    day_ts, rank, e["user_id"], e["name"], e["total"],
                    e["cash"], e["mv"], e["return_pct"],
                    e["status"], e["rebirth"])
        return len(entries)

    async def render_history(self, limit_days: int = 14) -> str | None:
        """最近 N 天 rank1 收益率曲线（P3）。

        契约（与 main.py:695-705 的指令实现对齐）：
        - **有 Pillow 时返回 None** —— 调用方（render/chart.py 的
          render_history_chart）负责把曲线画成 PNG 并通过 AstrBot 发图；
        - **没有 Pillow 时返回 ASCII 文本**，作为兜底，保证指令在任何
          环境下都能给出可读回复，不会因为缺图库而静默失败。
        数据源 leaderboard_snapshots 表，只读。
        """
        try:
            import PIL  # noqa: F401
        except ImportError:
            pass
        else:
            return None
        rows = await self.db.list_leaderboard_snapshots(limit_days)
        if not rows:
            return "还没有历史快照，等第一个虚拟交易日结束后再来。"
        by_day: dict[int, list] = {}
        for r in rows:
            by_day.setdefault(r["day_ts"], []).append(r)
        days = sorted(by_day.keys())[-limit_days:]
        lines = ["📈 排行榜历史曲线（状元收益率）"]
        blocks = "▁▂▃▄▅▆▇█"
        vals = []
        for d in days:
            top = [r for r in by_day[d] if r["rank"] == 1]
            vals.append(top[0]["return_pct"] if top else 0.0)
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        # 标签按「距最新快照的序号」定位，不按 day_ts 做日历算术：
        # day_ts 只要求单调递增（演示模式一日可远短于 86400 秒，届时 UTC
        # 零点同一天会撞键被 INSERT OR REPLACE 覆盖，丢历史点）。
        for i, v in enumerate(vals):
            idx = int(round((v - lo) / span * (len(blocks) - 1)))
            age = len(vals) - 1 - i
            ds = "最新" if age == 0 else f"{age}日前"
            lines.append(f"{ds}  {blocks[idx]}  {v:+.2f}%")
        return "\n".join(lines)

    async def render_bankrupt(self, limit: int = 10) -> str:
        """破产榜叙事（P3）：曾破产/正在破产的玩家，语气克制不嘲讽。

        NPC 账户排除（`is_npc=False`）：破产 NPC 由 NpcService 自动重建，
        不是玩家的故事，混进来会让叙事失真。
        """
        rows = await self.db.list_accounts(is_npc=False)
        dead = [a for a in rows if (a["status"] == "bankrupt"
                                    or (a["rebirth_count"] or 0) > 0)]
        if not dead:
            return "还没有人破产过，这个市场目前全员生还。"
        dead.sort(key=lambda a: -(a["rebirth_count"] or 0))
        lines = ["📖 破产榜"]
        for i, a in enumerate(dead[:limit], 1):
            rb = a["rebirth_count"] or 0
            state = "正在破产" if a["status"] == "bankrupt" else "已重生"
            lines.append(f"{i}. {a['user_name']} · 破产 {rb} 次 · {state}")
        return "\n".join(lines)
