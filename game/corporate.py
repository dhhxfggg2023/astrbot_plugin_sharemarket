"""公司行为与风险警示：分红、送转、ST 戴帽/摘帽、停牌、退市。

日终执行（`DayCloseService` 在委托过期之前调用），因此除权除息发生在
"当日收盘价 → 昨收"之前，玩家看到的是调整后的价格。

资产守恒：除权除息只改变"价格 × 数量"的构成，总资产不变（股息到账、股数增加、
每股价格等比例下调），这是可以断言的性质。
"""

from __future__ import annotations

import random

from ..config import CFG
from ..log import get_logger
from ..market.stocks import STOCK_POOL
from ..storage.db import Database

log = get_logger("corporate")


class CorporateService:
    def __init__(self, db: Database, engine, news=None,
                 rng: random.Random | None = None):
        self.db = db
        self.engine = engine
        self.news = news
        self.rng = rng or random.Random()

    async def run_day(self) -> dict:
        """返回本日事件统计（供日志与快讯）。"""
        stats = {"dividend": 0, "split": 0, "st": 0, "un_st": 0,
                 "suspend": 0, "delist": 0}
        codes = [c for c in STOCK_POOL if not self.engine.is_delisted(c)]
        for code in codes:
            q = self.engine.get(code)
            if q is None:
                continue
            stock = STOCK_POOL[code]

            # ── 分红 / 送转 ──────────────────────────────
            if self.rng.random() < CFG.dividend_prob:
                if self.rng.random() < 0.5:
                    await self._cash_dividend(code, q, stock, stats)
                else:
                    await self._stock_dividend(code, q, stock, stats)

            # ── 停牌 ────────────────────────────────────
            if (self.rng.random() < CFG.suspend_prob
                    and not self.engine.is_suspended(code)):
                ticks = self.rng.randint(*CFG.suspend_ticks)
                gap = self.rng.uniform(-0.05, 0.05)
                if self.engine.suspend(code, ticks, "重大事项", resume_gap=gap):
                    stats["suspend"] += 1
                    await self._announce(
                        "停牌",
                        f"{stock.name}因重大事项停牌，预计 {ticks} 个 tick 后复牌")

            # ── ST 戴帽 / 摘帽 ───────────────────────────
            if not self.engine.is_st(code):
                if (q.price < stock.initial_price * 0.6
                        and self.rng.random() < CFG.st_prob):
                    self.engine.set_st(code, True)
                    stats["st"] += 1
                    await self._announce(
                        "利空", f"{stock.name}触及其他风险警示情形，被实施 ST")
            else:
                if (q.price > stock.initial_price * 1.2
                        and self.rng.random() < CFG.un_st_prob):
                    self.engine.set_st(code, False)
                    stats["un_st"] += 1
                    await self._announce(
                        "利好", f"{stock.name}撤销风险警示，摘帽成功")

            # ── 面值退市 ────────────────────────────────
            if (self.engine.is_st(code) and q.price < CFG.delist_price
                    and self.rng.random() < CFG.delist_prob):
                await self._delist(code, q, stock, stats)

        if any(stats.values()):
            log.info("公司行为：%s", "、".join(
                f"{k}={v}" for k, v in stats.items() if v))
        return stats

    # ── 分红 / 送转 ──────────────────────────────────────
    async def _cash_dividend(self, code, q, stock, stats) -> None:
        """现金分红：除息 + 派现（总资产不变）。"""
        per_share = round(q.price * self.rng.uniform(0.004, 0.02), 2)
        if per_share <= 0:
            return
        self.engine.adjust_price(code, -per_share)
        paid = 0.0
        for pos in await self.db.get_position_holders(code):
            amount = per_share * pos["quantity"]
            acc = await self.db.get_account(pos["user_id"])
            if not acc:
                continue
            await self.db.update_cash_full(
                pos["user_id"], acc["cash_available"] + amount,
                acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
            paid += amount
        stats["dividend"] += 1
        await self._announce(
            "利好", f"{stock.name}实施现金分红 每股 {per_share:.2f} 元"
                    f"（除息，共派现 {paid:,.0f} 元）")
        log.info("现金分红：%s %s 每股 %.2f，派现 %.2f", stock.name, code,
                 per_share, paid)

    async def _stock_dividend(self, code, q, stock, stats) -> None:
        """送股/转增：除权 + 股数按比例增加（总资产不变）。"""
        ratio = self.rng.uniform(0.1, 0.5)
        self.engine.adjust_price(code, None, factor=1.0 / (1.0 + ratio))
        holders = 0
        for pos in await self.db.get_position_holders(code):
            new_qty = int(pos["quantity"] * (1.0 + ratio))
            if new_qty <= pos["quantity"]:
                continue
            new_sellable = min(int(pos["sellable"] * (1.0 + ratio)), new_qty)
            new_cost = pos["cost_price"] / (1.0 + ratio)
            await self.db.upsert_position(pos["user_id"], code, new_qty,
                                          new_sellable, new_cost)
            holders += 1
        stats["split"] += 1
        await self._announce(
            "利好", f"{stock.name}实施每 10 股送转 {ratio * 10:.1f} 股"
                    f"（除权，{holders} 个账户持仓增加）")
        log.info("送转：%s %s 比例 %.2f，影响 %d 个账户", stock.name, code,
                 ratio, holders)

    # ── 退市 ─────────────────────────────────────────────
    async def _delist(self, code, q, stock, stats) -> None:
        """面值退市：按退市整理期折算价强制清算持仓。"""
        price = round(q.price * 0.5, 2)      # 整理期通常继续下跌，按半价清算
        holders = 0
        for pos in await self.db.get_position_holders(code):
            acc = await self.db.get_account(pos["user_id"])
            if not acc:
                continue
            proceeds = price * pos["quantity"]
            await self.db.update_cash_full(
                pos["user_id"], acc["cash_available"] + proceeds,
                acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
            await self.db.delete_position(pos["user_id"], code)
            holders += 1
        self.engine.delist(code)
        stats["delist"] += 1
        await self._announce(
            "利空", f"{stock.name}因连续低于面值被终止上市，"
                    f"持仓按 {price:.2f} 元强制清算（{holders} 个账户受影响）")
        log.warning("退市清算：%s %s 清算价 %.2f，%d 个账户", stock.name,
                    code, price, holders)

    # ── 快讯 ─────────────────────────────────────────────
    async def _announce(self, kind: str, headline: str) -> None:
        if self.news is None:
            return
        from ..events.news import News
        import time as _t
        await self.news.publish(News(int(_t.time()), "stock", "", kind,
                                     headline, 0.0))
