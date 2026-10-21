"""交易服务（P0）：下单校验、费用、资金/股份冻结、成交入账、撤单、T+1。

资金账本（三本账）：
- cash_available   可用资金（下单即扣/冻结）
- cash_frozen      挂单冻结资金（买单未成交部分；成交消耗、撤单/过期释放）
- cash_withdrawable 可取资金（卖出所得当日不可取，日终结算）

股份冻结：卖单未成交部分从 sellable 中预扣；撤单/过期归还；日终 T+1 统一解锁。

委托类型（DESIGN.md §4）：
- 限价单：指定价格，未成交挂单排队。
- 市价单：price=None 时生效——买入委托价取涨停价、卖出取跌停价，
  立即可吃穿对手方最优档位成交（成交价不会劣于该保护价），未成交
  部分按保护价挂单排队；集合竞价阶段市价单明确拒绝（只收限价委托）。
"""

from __future__ import annotations

import time

from ..config import CFG
from ..locks import trade_lock
from ..log import get_logger
from ..market import calendar
from ..market.engine import PriceEngine
from ..market.orderbook import Fill, Matcher
from ..market.stocks import STOCK_POOL
from ..storage.db import Database

log = get_logger("trade")


def fees_buy(amount: float) -> float:
    return max(amount * CFG.commission_rate, CFG.commission_min) \
        + amount * CFG.transfer_fee_rate


def fees_sell(amount: float) -> float:
    return max(amount * CFG.commission_rate, CFG.commission_min) \
        + amount * CFG.transfer_fee_rate + amount * CFG.stamp_tax_rate


def freeze_estimate(price: float, qty: int) -> float:
    """挂单冻结金额：成交成本 + 分笔最低佣金缓冲。"""
    return round(price * qty * 1.001 + CFG.commission_min * 4, 2)


class TradeError(Exception):
    pass


class TradeService:
    def __init__(self, db: Database, engine: PriceEngine, matcher: Matcher,
                 auction=None):
        self.db = db
        self.engine = engine
        self.matcher = matcher
        self.auction = auction     # AuctionService（由 main 回填，两者互相引用）

    # ── 买入 ──────────────────────────────────────────────
    async def buy(self, user_id: str, code: str, quantity: int,
                  price: float | None = None) -> str:
        """买入。price=None 为市价单（DESIGN §4）：委托价取涨停价，
        立即与对手方最优档位成交，未成交部分按涨停价挂单排队；
        带价为限价单。"""
        async with trade_lock():
            return await self._buy(user_id, code, quantity, price)

    async def _buy(self, user_id: str, code: str, quantity: int,
                   price: float | None = None) -> str:
        if calendar.SESSION.is_auction():
            return await self.place_auction(user_id, code, "buy", quantity, price)
        self._check_common(code, quantity)
        acc = await self._get_active_account(user_id)
        stock = STOCK_POOL[code]
        quote = self.engine.get(code)
        # 市价单（DESIGN §4）：price=None → 委托价取涨停价作保护价，保证
        # 能吃穿对手方五档；限价单按用户委托价。成交价均不劣于委托价。
        market = price is None
        limit_price = quote.limit_up(stock) if market else round(price, 2)
        self._check_limit(stock, quote, limit_price, "buy")

        # 资金校验：按限价估算总成本（含费用）
        est_cost = limit_price * quantity + fees_buy(limit_price * quantity)
        if est_cost > acc["cash_available"] + 1e-9:
            raise TradeError(
                f"资金不足：预计需要 {est_cost:,.2f} 元（含佣金），"
                f"可用 {acc['cash_available']:,.2f} 元。")

        order_id = await self.db.insert_order(
            user_id, code, "buy", limit_price, quantity, int(time.time()))
        fills = self.matcher.place(order_id, user_id, code, "buy",
                                   limit_price, quantity, quote.price)

        spent_total = 0.0
        filled_qty = 0
        for f in fills:
            amount = f.price * f.quantity
            fee = fees_buy(amount)
            if f.maker_user_id:  # 对手是玩家挂的卖单
                delivered = await self._settle_sell_maker(
                    f.maker_user_id, f.maker_order_id, code, f.price, f.quantity)
                if not delivered:
                    # 幽灵卖单：对方持仓已被强平/重生删除，本笔作废，
                    # 不扣钱不交货不记流水（委托已在 _settle_sell_maker 撤销）
                    continue
            spent_total += amount + fee
            filled_qty += f.quantity
            self.engine.on_fill(code, f.quantity)
            await self._record_trade(order_id, user_id, code, "buy",
                                     f.price, f.quantity, fee, taker=True)
            await self._apply_buy_fill(user_id, code, f.price, f.quantity)

        # 未成交部分冻结资金
        remaining = quantity - filled_qty
        freeze_amt = freeze_estimate(limit_price, remaining) if remaining > 0 else 0.0
        await self._move_cash(user_id, available=-(spent_total + freeze_amt),
                              withdrawable=-spent_total, frozen=freeze_amt)
        if freeze_amt > 0:
            await self.db.update_order_frozen(order_id, freeze_amt)
        await self._refresh_order(order_id)
        await self._post_trade_bankruptcy_check(user_id)

        price_tag = ("市价" if market else "限价") + f"@{limit_price:.2f}"
        log.info("买入 %s %s %d股 %s 成交%d股 花费%.2f 冻结%.2f 委托#%s",
                 user_id, code, quantity, price_tag, filled_qty, spent_total,
                 freeze_amt, order_id)

        return self._fill_report("买入", code, quantity, filled_qty,
                                 fills, order_id, spent=spent_total,
                                 frozen=freeze_amt, market=market,
                                 limit_price=limit_price)

    # ── 卖出 ──────────────────────────────────────────────
    async def sell(self, user_id: str, code: str, quantity: int,
                   price: float | None = None) -> str:
        """卖出。price=None 为市价单（DESIGN §4）：委托价取跌停价，
        立即与对手方最优档位成交，未成交部分按跌停价挂单排队；
        带价为限价单。"""
        async with trade_lock():
            return await self._sell(user_id, code, quantity, price)

    async def _sell(self, user_id: str, code: str, quantity: int,
                    price: float | None = None) -> str:
        if calendar.SESSION.is_auction():
            return await self.place_auction(user_id, code, "sell", quantity,
                                            price)
        if code not in STOCK_POOL:
            raise TradeError(f"股票 {code} 不存在，可用 /炒股 搜 <关键词> 查询。")
        if quantity <= 0:
            raise TradeError("卖出数量必须为正数。")
        self._ensure_tradable(code)
        if not calendar.is_open():
            raise TradeError("当前非交易时段，无法下单。")
        await self._get_active_account(user_id)

        pos = await self.db.get_position(user_id, code)
        if not pos or pos["quantity"] <= 0:
            raise TradeError(f"你没有持有 {code}。")
        if quantity > pos["quantity"]:
            raise TradeError(f"持仓仅 {pos['quantity']} 股。")
        if quantity % CFG.lot_size != 0 and quantity != pos["quantity"]:
            raise TradeError(
                f"卖出数量须为 {CFG.lot_size} 股整数倍，"
                f"或一次性清仓全部 {pos['quantity']} 股（含零股）。")
        if quantity > pos["sellable"]:
            raise TradeError(
                f"可卖数量不足：持仓 {pos['quantity']} 股，当前可卖 "
                f"{pos['sellable']} 股（T+1：当日买入次日才可卖；"
                f"挂单中的股份已冻结）。")

        stock = STOCK_POOL[code]
        quote = self.engine.get(code)
        # 市价单（DESIGN §4）：price=None → 委托价取跌停价作保护价，保证
        # 能吃穿对手方五档买盘；限价单按用户委托价。成交价均不劣于委托价。
        market = price is None
        limit_price = quote.limit_down(stock) if market else round(price, 2)
        self._check_limit(stock, quote, limit_price, "sell")

        order_id = await self.db.insert_order(
            user_id, code, "sell", limit_price, quantity, int(time.time()))
        fills = self.matcher.place(order_id, user_id, code, "sell",
                                   limit_price, quantity, quote.price)

        proceeds = 0.0
        filled_qty = 0
        for f in fills:
            amount = f.price * f.quantity
            fee = fees_sell(amount)
            proceeds += amount - fee
            filled_qty += f.quantity
            self.engine.on_fill(code, f.quantity)
            await self._record_trade(order_id, user_id, code, "sell",
                                     f.price, f.quantity, fee, taker=True)
            if f.maker_user_id:  # 对手是玩家挂的买单
                await self._settle_buy_maker(
                    f.maker_user_id, f.maker_order_id, code, f.price, f.quantity)

        # 扣持仓；未成交部分冻结股份（从 sellable 预扣）
        remaining = quantity - filled_qty
        new_qty = pos["quantity"] - filled_qty
        new_sellable = pos["sellable"] - filled_qty - remaining
        if new_qty <= 0:
            await self.db.delete_position(user_id, code)
        else:
            await self.db.upsert_position(user_id, code, new_qty,
                                          max(new_sellable, 0), pos["cost_price"])
        await self._move_cash(user_id, available=proceeds)
        await self._refresh_order(order_id)
        await self._post_trade_bankruptcy_check(user_id)

        price_tag = ("市价" if market else "限价") + f"@{limit_price:.2f}"
        log.info("卖出 %s %s %d股 %s 成交%d股 到账%.2f 委托#%s",
                 user_id, code, quantity, price_tag, filled_qty, proceeds,
                 order_id)

        return self._fill_report("卖出", code, quantity, filled_qty,
                                 fills, order_id, gained=proceeds,
                                 market=market, limit_price=limit_price)

    # ── 撤单 ──────────────────────────────────────────────
    async def cancel(self, user_id: str, order_id: int) -> str:
        async with trade_lock():
            return await self._cancel(user_id, order_id)

    async def _cancel(self, user_id: str, order_id: int) -> str:
        row = await self.db.get_order(order_id)
        if not row:
            raise TradeError(f"委托 {order_id} 不存在。")
        if row["user_id"] != user_id:
            raise TradeError("只能撤销自己的委托。")
        if row["status"] not in ("open", "partial"):
            raise TradeError(f"委托 {order_id} 已是 {row['status']} 状态，不可撤。")
        # 竞价委托：撤单窗口规则（开盘竞价后段与收盘竞价不可撤）
        auc = self.auction.book(row["code"]).get(order_id) if self.auction else None
        if auc is not None:
            if not calendar.SESSION.cancellable():
                raise TradeError(
                    "集合竞价后段不可撤单（对应真实规则 9:20 后不可撤），"
                    "要撤请等竞价结束转入连续竞价后再撤。")
            self.auction.book(row["code"]).remove(order_id)
            if row["side"] == "buy":
                if row["frozen"] > 0:
                    await self._move_cash(user_id, available=row["frozen"],
                                          frozen=-row["frozen"])
                    await self.db.update_order_frozen(order_id, 0.0)
            else:
                pos = await self.db.get_position(user_id, row["code"])
                if pos:
                    await self.db.upsert_position(
                        user_id, row["code"], pos["quantity"],
                        min(pos["sellable"] + auc.qty, pos["quantity"]),
                        pos["cost_price"])
            await self.db.update_order(order_id, row["filled"], "cancelled")
            log.info("撤销竞价委托 %s #%s %s %s %d 股",
                     user_id, order_id, row["side"], row["code"], auc.qty)
            return f"已撤销竞价委托 #{order_id}（{auc.qty} 股，冻结已释放）。"
        self.matcher.cancel(row["code"], order_id)
        remaining = row["quantity"] - row["filled"]
        if row["side"] == "buy":
            if row["frozen"] > 0:
                await self._move_cash(user_id, available=row["frozen"],
                                      frozen=-row["frozen"])
                await self.db.update_order_frozen(order_id, 0.0)
        else:
            pos = await self.db.get_position(user_id, row["code"])
            if pos and remaining > 0:
                await self.db.upsert_position(
                    user_id, row["code"], pos["quantity"],
                    min(pos["sellable"] + remaining, pos["quantity"]),
                    pos["cost_price"])
        await self.db.update_order(order_id, row["filled"], "cancelled")
        log.info("撤单 %s 委托#%s %s %s 未成交%d股 释放冻结%.2f",
                 user_id, order_id, row["side"], row["code"], remaining,
                 row["frozen"] or 0.0)
        return f"已撤销委托 #{order_id}（未成交 {remaining} 股，冻结已释放）。"

    # ── 批量撤单（强平/重生前调用，防幽灵挂单）─────────────
    async def cancel_user_orders(self, user_id: str,
                                 side: str | None = None) -> int:
        """撤销某用户全部（或指定方向）未成交委托：撮合簿 + 竞价簿 +
        订单表 + 冻结释放。**无锁内部变体**，调用方须已持 trade_lock
        （强平清算/重生流程），否则请逐笔用 cancel()。
        返回撤销笔数。"""
        cancelled = 0
        for row in await self.db.get_user_open_orders(user_id):
            if side is not None and row["side"] != side:
                continue
            await self._cancel_order_row(row)
            cancelled += 1
        return cancelled

    async def _cancel_order_row(self, row) -> None:
        """撤销单笔委托（行数据），撮合簿/竞价簿/冻结/订单表一并处理。
        无锁变体，供 cancel_user_orders 调用。"""
        order_id = row["order_id"]
        user_id = row["user_id"]
        code = row["code"]
        remaining = row["quantity"] - row["filled"]
        if row["side"] == "buy":
            if (row["frozen"] or 0) > 0:
                await self._move_cash(user_id, available=row["frozen"],
                                      frozen=-row["frozen"])
                await self.db.update_order_frozen(order_id, 0.0)
        else:
            pos = await self.db.get_position(user_id, code)
            if pos and remaining > 0:
                await self.db.upsert_position(
                    user_id, code, pos["quantity"],
                    min(pos["sellable"] + remaining, pos["quantity"]),
                    pos["cost_price"])
        self.matcher.cancel(code, order_id)
        if self.auction:
            self.auction.book(code).remove(order_id)
        await self.db.update_order(order_id, row["filled"], "cancelled")
        log.info("批量撤单 %s 委托#%s %s %s 未成交%d股",
                 user_id, order_id, row["side"], code, remaining)

    # ── 集合竞价 ──────────────────────────────────────────
    async def place_auction(self, user_id: str, code: str, side: str,
                            quantity: int, price: float | None) -> str:
        """竞价阶段下单：只收集不撮合，竞价结束时统一按清算价成交。"""
        if code not in STOCK_POOL:
            raise TradeError(f"股票 {code} 不存在，可用 /炒股 搜 <关键词> 查询。")
        if quantity <= 0:
            raise TradeError("数量必须为正整数。")
        if price is None:
            raise TradeError(
                "集合竞价阶段只接受限价委托，市价单会被拒绝（竞价不连续"
                "撮合，无法保证对手价）。请带上委托价，例如 "
                "/炒股 买入 600001 1000 10.50。")
        acc = await self._get_active_account(user_id)
        stock = STOCK_POOL[code]
        quote = self.engine.get(code)
        limit_price = round(price, 2)
        self._check_limit(stock, quote, limit_price, side)
        phase = calendar.SESSION.phase()
        ts = int(time.time())

        if side == "buy":
            if quantity % CFG.lot_size != 0:
                raise TradeError(
                    f"买入数量必须是 {CFG.lot_size} 股（1 手）的整数倍。")
            est = limit_price * quantity + fees_buy(limit_price * quantity)
            if est > acc["cash_available"] + 1e-9:
                raise TradeError(
                    f"资金不足：预计需要 {est:,.2f} 元（含佣金），"
                    f"可用 {acc['cash_available']:,.2f} 元。")
            order_id = await self.db.insert_order(user_id, code, "buy",
                                                  limit_price, quantity, ts, phase)
            lock = freeze_estimate(limit_price, quantity)
            await self._move_cash(user_id, available=-lock, frozen=lock)
            await self.db.update_order_frozen(order_id, lock)
            frozen_note = f"冻结 {lock:,.2f} 元"
        else:
            # 停牌/退市校验：与买入 _check_common → _ensure_tradable 同源。
            # 集合竞价同样不可挂卖单（corporate 停牌/退市日终生效）。
            self._ensure_tradable(code)
            pos = await self.db.get_position(user_id, code)
            if not pos or pos["quantity"] <= 0:
                raise TradeError(f"你没有持有 {code}。")
            if quantity > pos["quantity"]:
                raise TradeError(f"持仓仅 {pos['quantity']} 股。")
            if quantity % CFG.lot_size != 0 and quantity != pos["quantity"]:
                raise TradeError(
                    f"卖出数量须为 {CFG.lot_size} 股整数倍，"
                    f"或一次性清仓全部 {pos['quantity']} 股（含零股）。")
            if quantity > pos["sellable"]:
                raise TradeError(
                    f"可卖数量不足：持仓 {pos['quantity']} 股，当前可卖 "
                    f"{pos['sellable']} 股（T+1：当日买入次日才可卖；"
                    f"挂单中的股份已冻结）。")
            order_id = await self.db.insert_order(user_id, code, "sell",
                                                  limit_price, quantity, ts, phase)
            await self.db.upsert_position(user_id, code, pos["quantity"],
                                          pos["sellable"] - quantity,
                                          pos["cost_price"])
            frozen_note = f"冻结 {quantity} 股"

        label = calendar.SESSION.auction_label()
        self.auction.book(code).add(order_id, user_id, side, limit_price, quantity)
        log.info("%s委托 %s #%s %s %s %d股 @%.2f（%s）", label, user_id,
                 order_id, side, code, quantity, limit_price, frozen_note)
        side_cn = "买入" if side == "buy" else "卖出"
        return (f"📋 {label}委托 #{order_id}：{stock.name}（{code}）\n"
                f"{side_cn} {quantity} 股 @ {limit_price:.2f} 已进入竞价簿"
                f"（{frozen_note}）。\n"
                f"竞价结束按清算价统一成交，未成交部分自动转入连续竞价；"
                f"撤单窗口见 /炒股 帮助。")

    async def settle_auction_fill(self, code: str, order, price: float,
                                  qty: int) -> None:
        """竞价成交入账（order 为 market.auction.AucOrder）。"""
        acc = await self.db.get_account(order.user_id)
        if not acc:
            return
        amount = price * qty
        consumed = 0.0
        if order.side == "buy":
            fee = fees_buy(amount)
            total = amount + fee
            # 结算口径只认**订单级**冻结（orders.frozen，place_auction 逐笔冻结）。
            # 账户级 cash_frozen 是所有在途委托的合集：拿它结算一笔竞价成交，
            # 多笔委托并存时会把别人的冻结当成自己的，再回头从可用的补差额，
            # 而买单成交前可用早已被冻结清空 → cash cannot be negative
            # （2026-10-12 生产日志 3689 次，日终清完整条链被它掐断）。
            row0 = await self.db.get_order(order.order_id)
            held = float((row0["frozen"] if row0 else 0.0) or 0.0)
            pay = min(total, held)
            need = round(total - pay, 2)
            avail = float(acc["cash_available"] or 0.0)
            from_avail = min(need, max(avail, 0.0))
            if need - from_avail > 0.005:
                log.error("竞价结算资金缺口：%s 委托#%s 需 %.2f，订单冻结 %.2f，"
                          "可用 %.2f，差额 %.2f 未扣（按上限结算，对账排查）",
                          order.user_id, order.order_id, total, held, avail,
                          need - from_avail)
            consumed = pay
            withdrawable = float(acc["cash_withdrawable"] or 0.0)
            wd_pay = min(round(pay + from_avail, 2), max(withdrawable, 0.0))
            await self._move_cash(order.user_id, available=-from_avail,
                                  withdrawable=-wd_pay, frozen=-pay)
            await self._apply_buy_fill(order.user_id, code, price, qty)
        else:
            fee = fees_sell(amount)
            delivered = await self._apply_sell_fill(order.user_id, code, qty)
            if not delivered:
                # 幽灵卖单：持仓已在挂单期间被删除（强平/重生），
                # 买方不交货，卖方不入账，委托作废
                await self._cancel_ghost_order(order.order_id)
                log.warning("竞价幽灵卖单作废：委托#%s %s %s %d股",
                            order.order_id, order.user_id, code, qty)
                return
            await self._move_cash(order.user_id, available=amount - fee)
        self.engine.on_fill(code, qty)
        await self._record_trade(order.order_id, order.user_id, code, order.side,
                                 price, qty, fee)
        row = await self.db.get_order(order.order_id)
        if row:
            filled = row["filled"] + qty
            status = "filled" if filled >= row["quantity"] else "partial"
            await self.db.update_order(order.order_id, filled, status)
            if order.side == "buy":
                # 订单级冻结随成交递减；全部成交后把剩余（含估算缓冲）退回可用，
                # 注意别把"账户级冻结"和"订单级冻结"混成一个数重算，否则双重释放
                held = row["frozen"] or 0.0
                left = round(max(held - consumed, 0.0), 2)
                if status == "filled" and left > 0:
                    await self._move_cash(order.user_id, available=left,
                                          frozen=-left)
                    left = 0.0
                await self.db.update_order_frozen(order.order_id, left)
        await self._post_trade_bankruptcy_check(order.user_id)

    # ── 内部：撮合入账 ────────────────────────────────────
    async def _settle_sell_maker(self, user_id: str, order_id: int | None,
                                 code: str, price: float, qty: int) -> bool:
        """对手方（挂卖单的玩家）被撮合：交货、收钱。

        返回 False 表示对方持仓不足（幽灵挂单——持仓被强平/重生删除后
        委托簿残留）：撤销该委托并作废本笔成交，调用方必须回滚买入方
        入账（防股票凭空增发）。绝不静默交付。
        """
        pos = await self.db.get_position(user_id, code)
        if not pos or pos["quantity"] < qty:
            held = pos["quantity"] if pos else 0
            log.error("幽灵卖单：%s 委托#%s %s 持仓 %d < %d，"
                      "撤销该委托并作废本笔成交（对账排查）",
                      user_id, order_id, code, held, qty)
            if order_id:
                await self._cancel_ghost_order(order_id)
            return False
        proceeds = price * qty - fees_sell(price * qty)
        await self._record_trade(order_id, user_id, code, "sell", price, qty,
                                 fees_sell(price * qty))
        new_qty = pos["quantity"] - qty
        new_sellable = min(pos["sellable"], max(new_qty, 0))
        if new_qty <= 0:
            await self.db.delete_position(user_id, code)
        else:
            await self.db.upsert_position(user_id, code, new_qty,
                                          new_sellable, pos["cost_price"])
        await self._move_cash(user_id, available=proceeds)
        if order_id:
            await self._refresh_order(order_id)
        return True

    async def _cancel_ghost_order(self, order_id: int) -> None:
        """撤销幽灵委托（委托簿 + 竞价簿 + 订单表），释放卖单冻结股份。"""
        row = await self.db.get_order(order_id)
        if not row or row["status"] not in ("open", "partial"):
            return
        self.matcher.cancel(row["code"], order_id)
        if self.auction:
            self.auction.book(row["code"]).remove(order_id)
        remaining = row["quantity"] - row["filled"]
        if remaining > 0:
            pos = await self.db.get_position(row["user_id"], row["code"])
            if pos:
                await self.db.upsert_position(
                    row["user_id"], row["code"], pos["quantity"],
                    min(pos["sellable"] + remaining, pos["quantity"]),
                    pos["cost_price"])
        await self.db.update_order(order_id, row["filled"], "cancelled")

    async def _settle_buy_maker(self, user_id: str, order_id: int | None,
                                code: str, price: float, qty: int) -> None:
        """对手方（挂买单的玩家）被撮合：从冻结资金扣款、收货。"""
        amount = price * qty
        cost = amount + fees_buy(amount)
        await self._record_trade(order_id, user_id, code, "buy", price, qty,
                                 fees_buy(amount))
        acc = await self.db.get_account(user_id)
        if acc:
            pay = min(cost, acc["cash_frozen"])
            short = cost - pay  # 理论上为 0（冻结有缓冲）
            await self._move_cash(user_id, available=-short,
                                  withdrawable=-cost, frozen=-pay)
        await self._apply_buy_fill(user_id, code, price, qty)
        if order_id:
            row = await self.db.get_order(order_id)
            if row:
                await self.db.update_order_frozen(order_id, row["frozen"] - cost)
            await self._refresh_order(order_id)

    async def _apply_sell_fill(self, user_id: str, code: str, qty: int) -> bool:
        """成交后扣持仓：sellable 已在挂单时冻结，这里只减数量。
        持仓不足（幽灵挂单）返回 False，由调用方决定处置。"""
        pos = await self.db.get_position(user_id, code)
        if not pos or pos["quantity"] < qty:
            held = pos["quantity"] if pos else 0
            log.error("幽灵卖单（竞价）：%s %s 持仓 %d < %d，"
                      "本笔成交作废不交货（对账排查）", user_id, code, held, qty)
            return False
        new_qty = pos["quantity"] - qty
        if new_qty <= 0:
            await self.db.delete_position(user_id, code)
        else:
            await self.db.upsert_position(user_id, code, new_qty,
                                          min(pos["sellable"], new_qty),
                                          pos["cost_price"])
        return True

    async def _apply_buy_fill(self, user_id: str, code: str,
                              price: float, qty: int) -> None:
        pos = await self.db.get_position(user_id, code)
        if pos:
            new_qty = pos["quantity"] + qty
            new_cost = (pos["cost_price"] * pos["quantity"] + price * qty) / new_qty
            # T+1：今日买入不可卖，sellable 不变
            await self.db.upsert_position(user_id, code, new_qty,
                                          pos["sellable"], new_cost)
        else:
            await self.db.upsert_position(user_id, code, qty, 0, price)

    # ── 内部：校验 ────────────────────────────────────────
    def _ensure_tradable(self, code: str) -> None:
        """停牌/退市校验（公司行为会动态改变可交易状态）。"""
        ok, msg = self.engine.can_trade(code)
        if not ok:
            raise TradeError(f"{STOCK_POOL[code].name} {msg}。")

    def _check_common(self, code: str, quantity: int) -> None:
        if code not in STOCK_POOL:
            raise TradeError(f"股票 {code} 不存在，可用 /炒股 搜 <关键词> 查询。")
        if quantity <= 0 or quantity % CFG.lot_size != 0:
            raise TradeError(f"买入数量必须是 {CFG.lot_size} 股（1 手）的整数倍。")
        self._ensure_tradable(code)
        if not calendar.is_open():
            raise TradeError("当前非交易时段，无法下单。")

    async def _get_active_account(self, user_id: str):
        acc = await self.db.get_account(user_id)
        if not acc:
            raise TradeError("你还没开户，发送 /炒股 开户 开始。")
        if acc["status"] == "bankrupt":
            raise TradeError("你已破产，可发送 /炒股 重生 东山再起。")
        if acc["status"] != "active":
            raise TradeError("账户状态异常，无法交易。")
        return acc

    def _check_limit(self, stock, quote, price: float, side: str) -> None:
        up, down = quote.limit_up(stock), quote.limit_down(stock)
        if price > up or price < down:
            raise TradeError(
                f"委托价 {price:.2f} 超出涨跌幅限制"
                f"（{down:.2f} ~ {up:.2f}，±{stock.limit_pct:.0%}）。")
        # 涨停封板不可买、跌停封板不可卖
        if side == "buy" and quote.price >= up and price >= up:
            raise TradeError(f"{stock.name} 已涨停（{up:.2f}），买单无法成交。")
        if side == "sell" and quote.price <= down and price <= down:
            raise TradeError(f"{stock.name} 已跌停（{down:.2f}），卖单无法成交。")

    # ── 内部：账本 ────────────────────────────────────────
    async def _record_trade(self, order_id: int | None, user_id: str, code: str,
                            side: str, price: float, qty: int, fee: float,
                            taker: bool = False) -> None:
        """成交流水落库 + 日志（对账与排查都靠它）。"""
        await self.db.insert_trade(order_id, user_id, code, side, price, qty,
                                   fee, int(time.time()))
        if CFG.log_trades:
            log.info("成交 %s %s %s %d股 @%.2f 费%.2f 委托#%s 角色=%s",
                     user_id, side, code, qty, price, fee, order_id,
                     "主动" if taker else "被动")

    async def _move_cash(self, user_id: str, available: float = 0.0,
                         withdrawable: float = 0.0, frozen: float = 0.0) -> None:
        acc = await self.db.get_account(user_id)
        if not acc:
            return
        await self.db.update_cash_full(
            user_id,
            acc["cash_available"] + available,
            acc["cash_withdrawable"] + withdrawable,
            (acc["cash_frozen"] or 0) + frozen)

    async def _refresh_order(self, order_id: int) -> None:
        row = await self.db.get_order(order_id)
        if not row:
            return
        book = self.matcher.book(row["code"])
        remaining = None
        for o in book.bids + book.asks:
            if o.order_id == order_id:
                remaining = o.remaining
                break
        filled = row["quantity"] - (remaining if remaining is not None else 0)
        if filled >= row["quantity"]:
            status = "filled"
        elif filled > 0:
            status = "partial"
        else:
            status = "open"
        await self.db.update_order(order_id, filled, status)
        # 全部成交后释放冻结资金余量（冻结估算含分笔佣金缓冲）
        if status == "filled" and (row["frozen"] or 0) > 0:
            await self._move_cash(row["user_id"], available=row["frozen"],
                                  frozen=-row["frozen"])
            await self.db.update_order_frozen(order_id, 0.0)

    async def _post_trade_bankruptcy_check(self, user_id: str) -> None:
        from .account import AccountService
        svc = AccountService(self.db)
        await svc.mark_bankrupt_if_l3(
            user_id,
            lambda c: self.engine.get(c).price if self.engine.get(c) else None)

    # ── 内部：回执 ────────────────────────────────────────
    def _fill_report(self, action: str, code: str, quantity: int,
                     filled_qty: int, fills: list[Fill], order_id: int,
                     spent: float = 0.0, gained: float = 0.0,
                     frozen: float = 0.0, market: bool = False,
                     limit_price: float | None = None) -> str:
        name = STOCK_POOL[code].name
        kind = "市价" if market else ""
        lines = [f"📋 {kind}{action}委托 #{order_id}：{name}（{code}）"]
        if filled_qty == 0:
            if market and limit_price is not None:
                lines.append(f"市价单未成交，{quantity} 股已按保护价 "
                             f"{limit_price:.2f} 挂单排队。")
            else:
                lines.append(f"未成交，{quantity} 股已挂单排队。")
            if frozen > 0:
                lines.append(f"已冻结资金 {frozen:,.2f} 元，撤单可释放。")
        else:
            avg = sum(f.price * f.quantity for f in fills) / filled_qty
            lines.append(f"成交 {filled_qty}/{quantity} 股，均价 {avg:.2f} 元")
            for f in fills[:3]:
                who = "做市商" if f.maker_user_id is None else "对手玩家"
                lines.append(f"  · {f.quantity} 股 @ {f.price:.2f}（对手：{who}）")
            if action == "买入":
                lines.append(f"支付 {spent:,.2f} 元（含佣金过户费）")
            else:
                lines.append(f"到账 {gained:,.2f} 元（已扣印花税佣金）")
            if filled_qty < quantity:
                lines.append(f"剩余 {quantity - filled_qty} 股挂单中，"
                             f"/炒股 撤单 {order_id} 可撤销。")
                if market and limit_price is not None:
                    lines.append(f"市价单剩余按保护价 {limit_price:.2f} 排队。")
                if frozen > 0:
                    lines.append(f"挂单部分已冻结 {frozen:,.2f} 元。")
        return "\n".join(lines)
