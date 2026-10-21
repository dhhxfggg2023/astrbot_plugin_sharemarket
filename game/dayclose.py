"""日终清算（P0）：委托过期解冻、T+1 解锁、昨收更新、可取资金结算。

触发时机：
- 247 演示模式：每 CFG.day_seconds 秒一个虚拟交易日
- realtime 模式：真实收盘（open→closed 转变）时

与下单共用全局交易锁，避免清算与成交交叉改账本。

执行顺序（不可随意调整）：
0   收盘集合竞价 → 0.5 公司行为（除权除息）→ 0.6 融资计息 + 强平扫描
1   委托过期解冻 → 2 T+1 解锁 → 3 行情日切 → 4 IPO 首日涨跌幅到期
5   资金结算（可用→可取）
其中 0.6 必须在 2 之前：强平可卖全部持仓（无视 T+1），若先解锁
强平与 T+1 的语义边界会重叠。
"""

from __future__ import annotations

import time

from ..locks import trade_lock
from ..log import get_logger
from ..market.engine import PriceEngine
from ..market.orderbook import Matcher
from ..storage.db import Database

log = get_logger("dayclose")


class DayCloseService:
    def __init__(self, db: Database, engine: PriceEngine, matcher: Matcher,
                 auction=None, corporate=None, news=None,
                 margin=None, ipo=None):
        self.db = db
        self.engine = engine
        self.matcher = matcher
        self.auction = auction      # AuctionService（可选，用于收盘集合竞价）
        self.corporate = corporate  # CorporateService（可选，分红/ST/停牌/退市）
        self.news = news            # NewsService（可选，用于每日快讯重置）
        self.margin = margin        # MarginService（可选，日终计息 + 强平扫描）
        self.ipo = ipo              # IPOService（可选，新股首日涨跌幅到期）
        self._day_seq = 0           # 虚拟交易日序号（计息/日切 key）

    async def run(self, leaderboard=None, price_of=None) -> int:
        """执行日终清算，返回过期委托数。

        leaderboard / price_of 可选：传入则在 **trade_lock 锁外** 落一条
        排行榜快照（t1 死锁教训——锁边界越短越安全；锁内 O(N) 次 await 会
        阻塞期间所有下单/撤单指令）。快照失败只记日志，不回滚日终主流程。
        """
        async with trade_lock():
            expired = await self._run()
        # 锁外落快照：db.transaction() 不 acquire trade_lock（已验证），
        # 锁外 O(N) 次 await 不会阻塞交易指令。
        if leaderboard is not None and price_of is not None:
            day_ts = int(time.time() // 86400 * 86400)
            try:
                n = await leaderboard.snapshot_day(day_ts, price_of)
                log.info("排行榜快照：day_ts=%d 写入 %d 条", day_ts, n)
            except Exception:
                log.exception("排行榜快照失败（不影响日终主流程）")
        return expired

    async def _run(self) -> int:
        self._day_seq += 1
        day_key = self._day_seq

        # 0) 收盘集合竞价：必须在委托过期之前清算，否则竞价单会被当作普通挂单过期
        if self.auction is not None and self.auction.has_orders():
            await self.auction.run("收盘集合竞价")

        # 0.5) 公司行为：分红送转（除权除息）、ST、停牌、退市
        #      放在"收盘价→昨收"之前，玩家看到的是调整后的价格
        if self.corporate is not None:
            await self.corporate.run_day()

        # 0.6) 融资融券：先计息、后强平扫描
        #      必须先计息再强平：利息计入负债后维持担保比例才会反映当日真实
        #      风险；且两步都必须在 unlock_all_sellable 之前——强平可卖全部
        #      持仓（无视 T+1），若先解锁强平与 T+1 的语义边界会重叠。
        #      这里必须调 *_nolock 变体：run() 已持全局锁，锁非重入。
        if self.margin is not None:
            accrued = await self.margin._accrue_day_nolock(day_key)
            if accrued.get("interest", 0.0) > 0:
                log.info("日终计息：%d 笔负债，利息 %.2f 元",
                         accrued.get("charged", 0),
                         accrued.get("interest", 0.0))
            reports = await self.margin._check_margin_calls_nolock()
            if reports:
                for r in reports:
                    log.info("日终强平：%s", r.replace("\n", " | "))

        rows = await self.db.get_all_open_orders()
        log.info("日终清算开始：待处理未成交委托 %d 笔", len(rows))
        released = 0.0

        # 1) 未成交委托全部过期（当日有效），释放买单冻结资金
        for r in rows:
            self.matcher.cancel(r["code"], r["order_id"])
            if r["side"] == "buy" and (r["frozen"] or 0) > 0:
                acc = await self.db.get_account(r["user_id"])
                if acc:
                    await self.db.update_cash_full(
                        r["user_id"],
                        acc["cash_available"] + r["frozen"],
                        acc["cash_withdrawable"],
                        max((acc["cash_frozen"] or 0) - r["frozen"], 0.0))
                    released += r["frozen"]
        await self.db.expire_open_orders()

        # 2) T+1 解锁：所有持仓恢复可卖（卖单股份冻结一并解除）
        await self.db.unlock_all_sellable()

        # 3) 行情日切：落日 K → 收盘价转昨收 → 重置日内数据
        day_rows = self.engine.day_close()
        await self.db.insert_candles(day_rows)
        await self.db.day_close_prices()

        # 4) IPO：新股首日涨跌幅到期推进（解除覆盖、恢复常规涨跌幅）
        #     run() 已持 trade_lock（:46），锁非重入，必须调无锁变体。
        if self.ipo is not None:
            cleared = await self.ipo._on_day_start_nolock(day_key)
            if cleared:
                log.info("IPO 日切：%d 只新股免涨跌幅期满", cleared)

        # 5) 资金结算：可用 → 可取
        await self.db.settle_withdrawable()

        await self.db.set_meta("last_day_close", str(int(time.time())))
        if self.news is not None:
            self.news.reset_day()
        stats = await self.db.stats()
        log.info("日终清算完成：%d 笔委托过期（释放冻结 %.2f 元），"
                 "T+1 已解锁，昨收与 %d 根日 K 已落库；"
                 "账户 %d（活跃 %d / 破产 %d），持仓 %d 条",
                 len(rows), released, len(day_rows), stats["accounts"],
                 stats["active"], stats["bankrupt"], stats["positions"])
        return len(rows)
