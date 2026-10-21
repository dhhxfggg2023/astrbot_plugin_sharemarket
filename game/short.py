"""杠杆交易层（P5）：在既有融资融券账本之上叠一层「杠杆倍数额度」。

定位（captain 决策，ledger decisions.md「做空/杠杆走「融券延长线 + 杠杆参数」
而非新账本」）：

- **做空已由 `game/margin.py` 提供**：`MarginService.borrow_stock()`（game/margin.py:297）
  借入即按市价卖出，本质就是卖空；`cover()`（game/margin.py:413）买券还券，
  `accrue_day()`（game/margin.py:465）逐日计息，`check_margin_calls()`（:527）
  与 `force_liquidate()`（:554）负责强平。
- **本模块只补「杠杆倍数」这一个概念**：`game/margin.py:216 _borrow_cap()` 只按
  维持担保比例算额度，没有「按自有资金 N 倍动用」的用户心智。
- **绝不新开第二套账本**：不写 accounts / orders / trades / positions 任何一张表，
  不 import `trade_lock`（db 层锁免疫），不直接碰资金。所有账本变动仍走
  `MarginService.borrow_cash()` / `TradeService.buy/sell()`。

对外 API（全部为纯函数或薄封装，调用方在**不持锁**状态调用）：

- `MAX_LEVERAGE` / `DEFAULT_LEVERAGE`：合法上限与默认倍数。
- `check_leverage(leverage)`：非正整数或越界抛 `LeverageError`。
- `max_leverage()`：返回 `MAX_LEVERAGE`。
- `leverage_cap(assets, debt, leverage)`：杠杆允许的最大买入额
  （= `assets×leverage − debt`，再与 `MarginService._borrow_cap` 取 min，
  取 min 的部分保证「跌破维持担保比例之前就停手」）。
- `ShortService`：仅做**查询与文案**的薄封装（持仓盈亏/可开杠杆额/强平价），
  下单仍由调用方走 margin/trade。
"""

from __future__ import annotations

from ..log import get_logger
from .margin import MarginError, _maintenance_ratio

log = get_logger("short")

#: 杠杆倍数额证区间：1 = 不用杠杆，MAX_LEVERAGE = 上限。
DEFAULT_LEVERAGE = 2
MAX_LEVERAGE = 3


class LeverageError(Exception):
    """杠杆倍数非法（非整数 / 越界）。"""


def check_leverage(leverage: int) -> None:
    """校验杠杆倍数，非法时抛 `LeverageError`（文案含合法区间）。"""
    if not isinstance(leverage, int) or isinstance(leverage, bool):
        raise LeverageError(
            f"杠杆倍数必须是整数，合法区间 1–{MAX_LEVERAGE}。")
    if leverage < 1 or leverage > MAX_LEVERAGE:
        raise LeverageError(f"杠杆倍数超出范围，合法区间 1–{MAX_LEVERAGE}。")


def max_leverage() -> int:
    """当前允许的最大杠杆倍数。"""
    return MAX_LEVERAGE


def leverage_cap(assets: float, debt: float, leverage: int) -> float:
    """杠杆允许的最大买入金额。

    `assets×leverage − debt` 是「自有资金按倍数放大后还能动多少」；
    再与 `_borrow_cap(assets, debt)`（维持担保比例约束）取 min——倍数可以
    更严，但绝不能宽于授信上限，否则会在跌破 130% 之前就把钱花光。
    """
    check_leverage(leverage)
    by_leverage = max(0.0, assets * leverage - debt)
    m = _maintenance_ratio()
    if m <= 1.0:
        by_ratio = float("inf")
    else:
        by_ratio = max(0.0, (assets - m * debt) / (m - 1.0))
    return round(min(by_leverage, by_ratio), 2)


def force_liquidate_price(quantity: int, borrow_value: float,
                          debt: float) -> float | None:
    """融券做空的强平价：负债达到 `debt` 时的价格，`debt<=0` 返回 None。

    负债按 `quantity × price` 盯市（见 game/margin.py 负债口径），所以
    `price_liq = debt / quantity`。仅用于文案展示，不由本模块执行强平。
    """
    if quantity <= 0 or debt <= 0:
        return None
    return round(debt / quantity, 2)


def short_pnl(quantity: int, open_price: float, now_price: float) -> float:
    """做空浮动盈亏（未计费用）：跌了赚钱。"""
    return round((open_price - now_price) * quantity, 2)


class ShortService:
    """做空/杠杆的查询与文案层（不含任何写账本动作）。

    构造时传入既有的 `MarginService` 与 `TradeService`，只读它们的账本；
    任何「真下单」都必须调用方自己走 `margin.borrow_cash()` /
    `trade.buy/sell()`，本类不代理写操作，避免第二真相。
    """

    def __init__(self, db, margin, trade=None):
        self.db = db
        self.margin = margin
        self.trade = trade

    async def open_short_positions(self, user_id: str) -> list[dict]:
        """该用户当前的全部融券负债（kind == "stock" 的 loan）。"""
        ledger = await self.margin._load()
        rows = []
        for loan in self.margin._open_loans(ledger, user_id):
            if loan.get("kind") == "stock":
                rows.append({
                    "code": loan.get("code"),
                    "quantity": int(loan.get("quantity") or 0),
                    "borrow_value": float(loan.get("borrow_value") or 0.0),
                    "last_interest_day": int(loan.get("last_interest_day") or 0),
                })
        return rows

    async def leverage_room(self, user_id: str,
                            leverage: int = DEFAULT_LEVERAGE) -> float:
        """该用户在给定倍数下还能动用多少买入额（调用方不持锁时调）。"""
        check_leverage(leverage)
        _, _, assets = await self.margin._assets(user_id)
        ledger = await self.margin._load()
        debt = self.margin._debt(ledger, user_id)
        return leverage_cap(assets, debt, leverage)

    async def panel(self, user_id: str) -> str:
        """做空/杠杆面板（纯文本）。

        做空盈亏按 `short_pnl()` 估算，强平价由 margin 的维持担保比例逻辑
        实际执行（见 game/margin.py:527/:554），这里只展示参考价。
        """
        positions = await self.open_short_positions(user_id)
        ledger = await self.margin._load()
        debt = self.margin._debt(ledger, user_id)
        ratio = await self.margin._margin_ratio(user_id)
        room = await self.leverage_room(user_id)
        net = await self.margin._net_worth(user_id)
        lines = ["📉 做空/杠杆面板",
                 f"杠杆倍数上限 {MAX_LEVERAGE}×（默认 {DEFAULT_LEVERAGE}×）",
                 f"维持担保比例：{ratio:.2f}" if ratio is not None
                 else "维持担保比例：无负债",
                 f"负债总额：{debt:,.2f} 元｜净资产：{net:,.2f} 元",
                 f"可开杠杆买入额：{room:,.2f} 元"]
        if not positions:
            lines.append("当前无融券做空仓位。")
        for p in positions:
            q = self.margin.engine.get(p["code"])
            if q is None:
                lines.append(f"- {p['code']} 空头 {p['quantity']} 股")
                continue
            liq = force_liquidate_price(p["quantity"], p["borrow_value"],
                                        debt)
            pnl = short_pnl(p["quantity"], p["borrow_value"]
                            / max(p["quantity"], 1), q.price)
            lines.append(
                f"- {p['code']} 空头 {p['quantity']} 股｜"
                f"现价 {q.price:.2f}｜浮动盈亏 {pnl:+,.2f}"
                + (f"｜强平参考价 {liq:.2f}" if liq is not None else ""))
        return "\n".join(lines)
