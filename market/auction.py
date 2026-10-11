"""集合竞价订单簿与定价（纯逻辑，不碰数据库）。

真实规则对应：
- 竞价期间委托只收集、不连续撮合；
- 结束时按**最大成交量原则**定出唯一清算价：在该价位上能成交的量最大，
  并列时取**未成交量最小**（供需最平衡）者，再并列取更接近参考价（昨收/最新价）者；
- 所有买价 ≥ 清算价的买单、卖价 ≤ 清算价的卖单，**统一按清算价成交**；
- 同价内按挂单时间优先分配可成交量。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass


@dataclass
class AucOrder:
    order_id: int
    user_id: str
    side: str          # buy / sell
    price: float
    qty: int
    seq: int


class AuctionBook:
    def __init__(self, code: str):
        self.code = code
        self.orders: list[AucOrder] = []
        self._seq = itertools.count(1)

    def add(self, order_id: int, user_id: str, side: str, price: float,
            qty: int) -> AucOrder:
        o = AucOrder(order_id, user_id, side, price, qty, next(self._seq))
        self.orders.append(o)
        return o

    def remove(self, order_id: int) -> AucOrder | None:
        for i, o in enumerate(self.orders):
            if o.order_id == order_id:
                return self.orders.pop(i)
        return None

    def get(self, order_id: int) -> AucOrder | None:
        return next((o for o in self.orders if o.order_id == order_id), None)

    def pending_qty(self, user_id: str, code: str | None = None,
                    side: str | None = None) -> int:
        return sum(o.qty for o in self.orders
                   if o.user_id == user_id
                   and (side is None or o.side == side))

    def clearing(self, ref_price: float) -> tuple[float, int]:
        """最大成交量定价：返回 (清算价, 可成交量)。无委托时返回 (ref_price, 0)。"""
        if not self.orders:
            return ref_price, 0
        candidates = sorted({o.price for o in self.orders} | {ref_price})
        best_key = None
        best_price, best_vol = ref_price, 0
        for p in candidates:
            demand = sum(o.qty for o in self.orders
                         if o.side == "buy" and o.price >= p - 1e-9)
            supply = sum(o.qty for o in self.orders
                         if o.side == "sell" and o.price <= p + 1e-9)
            vol = min(demand, supply)
            imbalance = abs(demand - supply)
            key = (vol, -imbalance, -abs(p - ref_price))
            if best_key is None or key > best_key:
                best_key, best_price, best_vol = key, p, vol
        return round(best_price, 2), best_vol

    def match(self, ref_price: float) -> tuple[float, list[tuple[AucOrder, int]]]:
        """按清算价撮合：返回 (清算价, [(订单, 成交数量), ...])。"""
        price, volume = self.clearing(ref_price)
        if volume <= 0:
            return price, []
        buys = sorted((o for o in self.orders
                       if o.side == "buy" and o.price >= price - 1e-9),
                      key=lambda o: (-o.price, o.seq))
        sells = sorted((o for o in self.orders
                        if o.side == "sell" and o.price <= price + 1e-9),
                       key=lambda o: (o.price, o.seq))
        fills: list[tuple[AucOrder, int]] = []
        i = j = 0
        left_buy = buys[0].qty if buys else 0
        left_sell = sells[0].qty if sells else 0
        remaining = volume
        while i < len(buys) and j < len(sells) and remaining > 0:
            take = min(left_buy, left_sell, remaining)
            if take > 0:
                fills.append((buys[i], take))
                fills.append((sells[j], take))
                remaining -= take
                left_buy -= take
                left_sell -= take
            if left_buy == 0:
                i += 1
                left_buy = buys[i].qty if i < len(buys) else 0
            if left_sell == 0:
                j += 1
                left_sell = sells[j].qty if j < len(sells) else 0
        return price, fills

    def apply_fills(self, fills: list[tuple[AucOrder, int]]) -> None:
        """把已成交数量从订单上扣掉，成交完的移出竞价簿。"""
        done: set[int] = set()
        for order, qty in fills:
            order.qty -= qty
            if order.qty <= 0:
                done.add(order.order_id)
        self.orders = [o for o in self.orders if o.order_id not in done]
