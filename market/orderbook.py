"""内存订单簿 + 撮合引擎（P0）。

结构：玩家委托为订单簿主体；做市商（MarketMaker）在玩家盘口之外补充五档流动性。
撮合规则：价格优先、时间优先；玩家单先与玩家对手单撮合，剩余与做市商成交。
"""

from __future__ import annotations

import itertools
import random
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import CFG


@dataclass
class BookOrder:
    order_id: int
    user_id: str
    side: str          # "buy" / "sell"
    price: float
    remaining: int     # 剩余数量
    seq: int           # 时间优先序号


@dataclass
class Fill:
    """一笔成交。"""
    taker_order_id: int
    taker_user_id: str
    maker_order_id: int | None     # None = 对手是做市商
    maker_user_id: str | None
    price: float
    quantity: int


class OrderBook:
    """单个股票的订单簿。bids 降序、asks 升序。"""

    def __init__(self, code: str):
        self.code = code
        self.bids: list[BookOrder] = []
        self.asks: list[BookOrder] = []

    def add(self, o: BookOrder) -> None:
        book = self.bids if o.side == "buy" else self.asks
        book.append(o)
        # 价格优先，同价时间优先
        book.sort(key=lambda x: (-x.price if o.side == "buy" else x.price, x.seq))

    def remove(self, order_id: int) -> BookOrder | None:
        for book in (self.bids, self.asks):
            for i, o in enumerate(book):
                if o.order_id == order_id:
                    return book.pop(i)
        return None

    def best_bid(self) -> BookOrder | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> BookOrder | None:
        return self.asks[0] if self.asks else None

    def depth(self, side: str, levels: int = 5) -> list[tuple[float, int]]:
        """聚合五档：[(价格, 总量), ...]"""
        book = self.bids if side == "buy" else self.asks
        agg: dict[float, int] = defaultdict(int)
        for o in book:
            agg[o.price] += o.remaining
        prices = sorted(agg, reverse=(side == "buy"))[:levels]
        return [(p, agg[p]) for p in prices]


class MarketMaker:
    """虚拟做市商：围绕最新价挂五档，保证流动性。"""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def quote(self, mid: float) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
        """返回 (买盘五档, 卖盘五档)。"""
        bids, asks = [], []
        for lv in range(1, 6):
            off = CFG.mm_spread * lv
            bp = round(mid * (1 - off), 2)
            ap = round(mid * (1 + off), 2)
            qty = int(CFG.mm_depth / lv * self.rng.uniform(0.7, 1.3))
            bids.append((bp, qty))
            asks.append((ap, qty))
        return bids, asks

    def mm_price_for(self, side: str, mid: float, book: OrderBook) -> float:
        """与做市商成交的价格：取玩家最优对手价，无对手则 mid±spread。"""
        if side == "buy":
            best = book.best_ask()
            return best.price if best else round(mid * (1 + CFG.mm_spread), 2)
        best = book.best_bid()
        return best.price if best else round(mid * (1 - CFG.mm_spread), 2)


class Matcher:
    """撮合器。持有所有股票的订单簿，统计净压力供价格引擎反馈。"""

    def __init__(self):
        self.books: dict[str, OrderBook] = {}
        self.mm = MarketMaker(random.Random())
        self._seq = itertools.count(1)
        # 供需压力累计：每股 (净买入股数)（买为正）
        self.pressure: dict[str, int] = defaultdict(int)

    def book(self, code: str) -> OrderBook:
        if code not in self.books:
            self.books[code] = OrderBook(code)
        return self.books[code]

    def place(self, order_id: int, user_id: str, code: str, side: str,
              price: float, quantity: int, mid: float) -> list[Fill]:
        """挂入委托并立即撮合。返回成交列表。"""
        book = self.book(code)
        order = BookOrder(order_id, user_id, side, price, quantity, next(self._seq))
        fills: list[Fill] = []

        opposing = book.asks if side == "buy" else book.bids
        # 1) 与玩家对手单撮合
        while order.remaining > 0 and opposing:
            best = opposing[0]
            crossed = (side == "buy" and order.price >= best.price) or \
                      (side == "sell" and order.price <= best.price)
            if not crossed:
                break
            qty = min(order.remaining, best.remaining)
            fills.append(Fill(order.order_id, order.user_id,
                              best.order_id, best.user_id, best.price, qty))
            order.remaining -= qty
            best.remaining -= qty
            self.pressure[code] += qty if side == "buy" else -qty
            if best.remaining == 0:
                opposing.pop(0)

        # 2) 剩余与做市商成交（限价保护：做市商价不得劣于委托价）
        if order.remaining > 0:
            mm_price = self.mm.mm_price_for(side, mid, book)
            ok = (side == "buy" and mm_price <= order.price) or \
                 (side == "sell" and mm_price >= order.price)
            if ok:
                # 做市商有深度上限，一次性吃掉压力太大则部分成交
                qty = min(order.remaining, CFG.mm_depth)
                fills.append(Fill(order.order_id, order.user_id,
                                  None, None, mm_price, qty))
                order.remaining -= qty
                self.pressure[code] += qty if side == "buy" else -qty

        # 3) 剩余挂单
        if order.remaining > 0:
            book.add(order)
        return fills

    def cancel(self, code: str, order_id: int) -> BookOrder | None:
        return self.book(code).remove(order_id)

    def restore(self, order_id: int, user_id: str, code: str, side: str,
                price: float, remaining: int) -> None:
        """重启恢复：把数据库里的未成交委托重新挂回订单簿（不撮合）。"""
        self.book(code).add(BookOrder(order_id, user_id, side, price, remaining,
                                      next(self._seq)))

    def five_levels(self, code: str, mid: float):
        """玩家盘口叠加做市商档位的五档展示。"""
        book = self.book(code)
        mm_bids, mm_asks = self.mm.quote(mid)
        return book.depth("buy") + mm_bids, book.depth("sell") + mm_asks

    def take_pressure(self, code: str) -> int:
        """取走并清零某股净压力（价格引擎每 tick 调用）。"""
        p = self.pressure.get(code, 0)
        self.pressure[code] = 0
        return p
