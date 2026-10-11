"""账户服务（P0）：开户、资产计算、破产判定、重生。"""

from __future__ import annotations

import time

from ..config import CFG
from ..log import get_logger
from ..storage.db import Database

log = get_logger("account")

# 破产级别
L1 = "L1"   # 轻度亏损：可用资金不足最小下单，但有持仓
L2 = "L2"   # 资金枯竭：可用≈0，持仓>0
L3 = "L3"   # 实质破产：可用=0 且持仓=0
OK = "OK"


def min_order_amount(price: float) -> float:
    """最小下单金额（1 手 + 费用余量）。"""
    return price * CFG.lot_size * 1.001 + CFG.commission_min


class AccountService:
    def __init__(self, db: Database):
        self.db = db

    async def open_account(self, user_id: str, user_name: str) -> tuple[bool, str]:
        existing = await self.db.get_account(user_id)
        if existing:
            if existing["status"] == "bankrupt":
                return False, "你已破产，请使用 /炒股 重生"
            return False, "你已开过户了（每 QQ 号限一户）"
        await self.db.create_account(user_id, user_name, CFG.initial_cash,
                                     int(time.time()))
        log.info("开户 %s(%s) 初始资金 %.2f", user_name, user_id,
                 CFG.initial_cash)
        return True, (f"开户成功！初始资金 {CFG.initial_cash:,.0f} 元已到账。\n"
                      f"发送 /炒股 行情 查看大盘，/炒股 帮助 查看全部指令。")

    async def total_assets(self, user_id: str, price_of) -> float:
        """总资产 = 可用资金 + 持仓市值。price_of(code) -> 现价。"""
        acc = await self.db.get_account(user_id)
        if not acc:
            return 0.0
        total = acc["cash_available"]
        for pos in await self.db.get_positions(user_id):
            p = price_of(pos["code"])
            if p is not None:
                total += p * pos["quantity"]
        return total

    async def bankruptcy_level(self, user_id: str, price_of) -> str:
        acc = await self.db.get_account(user_id)
        if not acc or acc["status"] != "active":
            return OK
        cash = acc["cash_available"] + (acc["cash_frozen"] or 0)
        positions = await self.db.get_positions(user_id)
        market_value = 0.0
        min_price = None
        for pos in positions:
            p = price_of(pos["code"])
            if p is None:
                continue
            market_value += p * pos["quantity"]
            min_price = p if min_price is None else min(min_price, p)
        if market_value <= 0 and cash < 0.01:
            return L3
        if cash < 0.01 and market_value > 0:
            return L2
        if min_price is not None and cash < min_order_amount(min_price) \
                and market_value > 0:
            return L1
        return OK

    async def mark_bankrupt_if_l3(self, user_id: str, price_of) -> bool:
        if await self.bankruptcy_level(user_id, price_of) == L3:
            await self.db.set_status(user_id, "bankrupt", int(time.time()))
            log.warning("破产判定 L3：%s 可用资金与持仓均已归零，账户冻结，"
                        "可申请重生", user_id)
            return True
        return False

    async def rebirth(self, user_id: str) -> tuple[bool, str]:
        acc = await self.db.get_account(user_id)
        if not acc:
            return False, "你还没开户，直接 /炒股 开户 即可。"
        if acc["status"] != "bankrupt":
            return False, "只有破产（可用资金与持仓全空）后才能重生。"
        now = int(time.time())
        if acc["rebirth_count"] >= CFG.rebirth_max_times:
            return False, (f"终身重生次数（{CFG.rebirth_max_times} 次）已用完，"
                           f"账户永久封盘。感谢参与。")
        last = acc["last_bankrupt_ts"] or 0
        remain = CFG.rebirth_cooldown_hours * 3600 - (now - last)
        if remain > 0:
            h, m = divmod(remain // 60, 60)
            return False, f"重生冷却中，还需等待 {h} 小时 {m} 分。"
        cash = CFG.initial_cash * CFG.rebirth_cash_factor
        await self.db.do_rebirth(user_id, cash, now)
        left = CFG.rebirth_max_times - acc["rebirth_count"] - 1
        log.info("重生 %s 第 %d 次，资金 %.2f，剩余次数 %d",
                 user_id, acc["rebirth_count"] + 1, cash, left)
        return True, (f"🔄 重生成功！资金 {cash:,.0f} 元（初始资金的 "
                      f"{CFG.rebirth_cash_factor:.0%}）。剩余重生次数：{left}。")
