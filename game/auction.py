"""集合竞价服务：收集竞价委托、按最大成交量定价清算、剩余转连续竞价。

与 `TradeService` 的分工：
- `TradeService` 负责校验、冻结、入账（`place_auction` / `settle_auction_fill`）；
- 本服务只负责"什么时候把竞价簿清算掉"以及把未成交部分交给连续竞价订单簿。
构造后需由 main 回填 `trade.auction = self`（两者互相引用）。
"""

from __future__ import annotations

from ..log import get_logger
from ..market.auction import AuctionBook
from ..market.stocks import STOCK_POOL
from ..storage.db import Database

log = get_logger("auction")


class AuctionService:
    def __init__(self, db: Database, engine, trade):
        self.db = db
        self.engine = engine
        self.trade = trade
        self.books: dict[str, AuctionBook] = {}

    def book(self, code: str) -> AuctionBook:
        if code not in self.books:
            self.books[code] = AuctionBook(code)
        return self.books[code]

    def has_orders(self) -> bool:
        return any(b.orders for b in self.books.values())

    def pending_count(self) -> int:
        return sum(len(b.orders) for b in self.books.values())

    async def run(self, label: str = "集合竞价") -> tuple[int, int, float]:
        """清算全部竞价簿，返回 (成交笔数, 成交股数, 成交金额)。"""
        trades = shares = 0
        amount_total = 0.0
        touched = 0
        for code in STOCK_POOL:
            book = self.books.get(code)
            if not book or not book.orders:
                continue
            ref = self.engine.get(code).price
            price, fills = book.match(ref)
            if fills:
                touched += 1
                # match() 返回买卖两侧各一条记录，统计只能算一侧，否则成交量翻倍
                for order, qty in fills:
                    if qty <= 0 or order.side != "buy":
                        continue
                    trades += 1
                    shares += qty
                    amount_total += price * qty
                for order, qty in fills:
                    if qty > 0:
                        await self.trade.settle_auction_fill(code, order, price, qty)
                log.info("%s 清算价 %s %.2f（参考价 %.2f），成交 %d 股",
                         code, STOCK_POOL[code].name, price, ref, shares)
            book.apply_fills(fills)
            # 未成交部分转入连续竞价订单簿（保留限价与时间优先）
            for o in list(book.orders):
                self.trade.matcher.restore(o.order_id, o.user_id, code,
                                           o.side, o.price, o.qty)
                await self.trade._refresh_order(o.order_id)
            book.orders.clear()
        self.books = {c: b for c, b in self.books.items() if b.orders}
        if trades:
            log.info("%s清算完成：%d 只股票、%d 笔成交、%d 股、金额 %.2f",
                     label, touched, trades, shares, amount_total)
        else:
            log.info("%s清算：无成交", label)
        return trades, shares, amount_total
