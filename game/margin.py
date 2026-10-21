"""融资融券服务（P3）：抵押资产借款/借券、维持担保比例、每日计息、强平清算。

对齐 DESIGN.md §14.3（130% 维持担保比例平仓线，跌破强平；强平后净资产 < 0 →
L4 冻结，管理员清账）与 §21 P3（融资融券）。

核心规则：
- **维持担保比例** =（现金三本账 + 持仓市值）÷ 负债总额；负债 = 融资本金 +
  融券数量×市价（盯市）+ 已计未付利息。跌破 130% 立即强平。
- **净资产** = 现金 + 持仓市值 − 负债。借入现金同时进入现金与负债，净资产
  不变——借入现金绝不构成利润（测试里有这条断言）。
- **借款上限**：借款后维持担保比例不得低于平仓线
  （X ≤（总资产 − 130%×负债）÷（130% − 1）），模拟真实授信逻辑。
- **每日计息幂等**：每笔负债记 `last_interest_day`；`accrue_day(day)` 对
  day ≤ last_interest_day 直接跳过，重启后同一虚拟日不会重复扣息。
- **强平**：卖持仓（市值大者优先）→ 现金先还息再还本 → 买券还券，循环至
  比例恢复或资产耗尽；净资产 < 0 则冻结账户（L4，等待管理员清账）。

持久化：借贷账本 JSON 存在 meta 表的 `margin.ledger.v1`（现有 DB API，
不改 storage/db.py，真实 SQLite 持久化、重启可恢复）。所有账本变动都在
`trade_lock()` 内完成，与交易/日终清算串行。集成若要规范化表结构，替换
`_load/_save` 两个方法即可，账本结构见 `_empty_ledger`。

费用复用 `trade.fees_buy/fees_sell`；强平是特殊成交路径（无视 T+1 可卖
限制），不经过 TradeService。
"""

from __future__ import annotations

import json
import math
import time

from ..config import CFG
from ..locks import trade_lock
from ..log import get_logger
from ..market.stocks import STOCK_POOL
from ..storage.db import Database
from .trade import fees_buy, fees_sell

log = get_logger("margin")

_LEDGER_KEY = "margin.ledger.v1"
_INTEREST_LOG_MAX = 200       # 利息流水最多保留条数（防无界增长）
_LIQUIDATE_MAX_STEPS = 200    # 强平循环步数上限（防呆）

# 账本单条负债的必备字段（schema 校验用，缺一即视为账本损坏）
_LOAN_FIELDS = ("id", "user_id", "kind", "code", "quantity", "principal",
                "borrow_value", "interest_accrued", "last_interest_day",
                "status", "opened_ts", "closed_ts")


# ── 数值（集成时移入 config.py，同名键即可自动生效）─────────────────
def _maintenance_ratio() -> float:
    """维持担保比例平仓线（DESIGN §14.3：130%）。"""
    return getattr(CFG, "margin_maintenance_ratio", 1.30)


def _cash_rate_daily() -> float:
    """融资日利率 = 年利率 / 365。"""
    return getattr(CFG, "margin_cash_rate_annual", 0.08) / 365.0


def _stock_rate_daily() -> float:
    """融券日费率 = 年费率 / 365。"""
    return getattr(CFG, "margin_stock_rate_annual", 0.10) / 365.0


def _check_amount(value, label: str) -> float:
    """金额入口防线：有限实数且为正（C2）。"""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise MarginError(f"{label}格式不正确。")
    if not math.isfinite(amount):
        raise MarginError(f"{label}必须为有限数字（收到 {value!r}）。")
    if amount <= 0:
        raise MarginError(f"{label}必须为正数。")
    return amount


class MarginError(Exception):
    pass


class MarginService:
    def __init__(self, db: Database, engine, trade=None):
        self.db = db
        self.engine = engine
        self.trade = trade       # TradeService（强平前撤卖单用；main 装配时回填）

    # ── 账本读写 ──────────────────────────────────────────
    @staticmethod
    def _empty_ledger() -> dict:
        return {"version": 1, "next_id": 1, "loans": {}, "interest_log": []}

    @staticmethod
    def _validate_loan(loan) -> bool:
        """单条负债结构校验：必备字段齐全且数值类型正确。"""
        if not isinstance(loan, dict):
            return False
        if any(f not in loan for f in _LOAN_FIELDS):
            return False
        if not isinstance(loan["user_id"], str):
            return False
        if loan["kind"] not in ("cash", "stock"):
            return False
        for f in ("quantity", "principal", "borrow_value",
                  "interest_accrued", "last_interest_day"):
            if not isinstance(loan[f], (int, float)) \
                    or isinstance(loan[f], bool):
                return False
        return True

    def _validate_ledger(self, data) -> None:
        """账本 schema 校验：结构损坏一律拒业务（宁可停服不凭空销账）。"""
        if not isinstance(data, dict):
            raise MarginError("融资账本损坏（顶层不是对象），已拒绝融资操作。")
        if not isinstance(data.get("loans"), dict):
            raise MarginError("融资账本损坏（loans 不是映射），已拒绝融资操作。")
        if not isinstance(data.get("next_id"), int) \
                or isinstance(data.get("next_id"), bool):
            raise MarginError("融资账本损坏（next_id 非整数），已拒绝融资操作。")
        for key, loan in data["loans"].items():
            if not isinstance(key, str) or not self._validate_loan(loan):
                raise MarginError(
                    f"融资账本损坏（单条负债 #{key} 结构非法），"
                    f"已拒绝融资操作，请联系管理员核查。")

    async def _load(self) -> dict:
        """从 meta 表读账本（每次操作前调用，避免内存缓存与磁盘漂移）。"""
        raw = await self.db.get_meta(_LEDGER_KEY)
        if not raw:
            return self._empty_ledger()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 账本损坏时拒绝继续融资业务：宁可停服也不凭空销账（= 刷钱漏洞）
            raise MarginError("融资融券账本损坏（meta 表 JSON 解析失败），"
                              "已拒绝所有融资操作，请联系管理员核查。")
        try:
            self._validate_ledger(data)
        except MarginError:
            raise
        data.setdefault("version", 1)
        data.setdefault("next_id", 1)
        data.setdefault("loans", {})
        data.setdefault("interest_log", [])
        return data

    async def _save(self, ledger: dict) -> None:
        await self.db.set_meta(_LEDGER_KEY,
                               json.dumps(ledger, ensure_ascii=False))

    def _open_loans(self, ledger: dict, user_id: str | None = None) -> list[dict]:
        loans = [l for l in ledger["loans"].values() if l["status"] == "open"]
        if user_id is not None:
            loans = [l for l in loans if l["user_id"] == user_id]
        return sorted(loans, key=lambda l: int(l["id"]))

    # ── 资产与负债计算 ────────────────────────────────────
    async def _assets(self, user_id: str) -> tuple[float, float, float]:
        """(现金, 持仓市值, 总资产)。

        现金 = 可用 + 冻结（与 account.bankruptcy_level 同一口径）；
        cash_withdrawable 是日终快照列（日终 = available 的拷贝），
        重复计入会双算初始资金——开户时 available 与 withdrawable 同为初始值。
        """
        acc = await self.db.get_account(user_id)
        if not acc:
            return 0.0, 0.0, 0.0
        cash = acc["cash_available"] + (acc["cash_frozen"] or 0.0)
        mv = 0.0
        for pos in await self.db.get_positions(user_id):
            q = self.engine.get(pos["code"])
            if q is not None:
                mv += q.price * pos["quantity"]
        return cash, mv, cash + mv

    def _stock_debt(self, loans: list[dict]) -> float:
        """融券负债 = 借入数量 × 当前市价（盯市）。"""
        total = 0.0
        for l in loans:
            if l["kind"] != "stock":
                continue
            q = self.engine.get(l["code"])
            if q is None:
                log.warning("融券负债盯市失败：%s 无行情，按 0 计（账目可能失真）",
                            l["code"])
                continue
            total += q.price * l["quantity"]
        return total

    def _debt(self, ledger: dict, user_id: str | None = None) -> float:
        """负债总额 = 融资本金 + 融券盯市值 + 已计未付利息。"""
        loans = self._open_loans(ledger, user_id)
        cash_debt = sum(l["principal"] for l in loans if l["kind"] == "cash")
        interest = sum(l["interest_accrued"] for l in loans)
        return round(cash_debt + self._stock_debt(loans) + interest, 2)

    async def _net_worth(self, user_id: str) -> float:
        _, _, assets = await self._assets(user_id)
        ledger = await self._load()
        return round(assets - self._debt(ledger, user_id), 2)

    async def _margin_ratio(self, user_id: str) -> float | None:
        """维持担保比例；无负债返回 None。"""
        ledger = await self._load()
        if not self._open_loans(ledger, user_id):
            return None
        debt = self._debt(ledger, user_id)
        if debt <= 0:
            return None
        _, _, assets = await self._assets(user_id)
        return assets / debt

    def _borrow_cap(self, assets: float, debt: float) -> float:
        """借款上限：借 X 后（资产+X）/（负债+X）≥ 平仓线
        → X ≤（资产 − 平仓线×负债）/（平仓线 − 1）。"""
        m = _maintenance_ratio()
        if m <= 1.0:
            return float("inf")
        return max(0.0, (assets - m * debt) / (m - 1.0))

    async def _get_active_account(self, user_id: str):
        acc = await self.db.get_account(user_id)
        if not acc:
            raise MarginError("你还没开户，先 /炒股 开户。")
        if acc["status"] == "frozen":
            raise MarginError("账户已冻结（融资爆仓负债中，L4），"
                              "请联系管理员清账后再操作。")
        if acc["status"] != "active":
            raise MarginError("账户状态异常，暂不可使用融资融券。")
        return acc

    # ── 对外查询 ──────────────────────────────────────────
    async def net_worth(self, user_id: str) -> float:
        """净资产 = 现金 + 持仓市值 − 负债（借入现金不计入利润）。"""
        async with trade_lock():
            return await self._net_worth(user_id)

    async def debt(self, user_id: str) -> float:
        async with trade_lock():
            ledger = await self._load()
            return self._debt(ledger, user_id)

    async def margin_ratio(self, user_id: str) -> float | None:
        async with trade_lock():
            return await self._margin_ratio(user_id)

    async def has_debt(self, user_id: str) -> bool:
        async with trade_lock():
            ledger = await self._load()
            return bool(self._open_loans(ledger, user_id))

    # ── 融资借款 ──────────────────────────────────────────
    async def borrow_cash(self, user_id: str, amount: float) -> str:
        """融资借款：借入现金进入可用资金，同时记入负债。"""
        async with trade_lock():
            async with self.db.transaction():
                return await self._borrow_cash(user_id, amount)

    async def _borrow_cash(self, user_id: str, amount: float) -> str:
        amount = _check_amount(amount, "借款金额")
        acc = await self._get_active_account(user_id)
        ledger = await self._load()
        debt = self._debt(ledger, user_id)
        _, _, assets = await self._assets(user_id)
        cap = self._borrow_cap(assets, debt)
        if amount > cap + 1e-9:
            raise MarginError(
                f"借款超限：按当前抵押物最多可借 {cap:,.2f} 元"
                f"（借款后维持担保比例不得低于 {_maintenance_ratio():.0%}）。")

        loan_id = str(ledger["next_id"])
        ledger["next_id"] += 1
        ledger["loans"][loan_id] = {
            "id": loan_id, "user_id": user_id, "kind": "cash", "code": None,
            "quantity": 0, "principal": amount, "borrow_value": 0.0,
            "interest_accrued": 0.0, "last_interest_day": -1,
            "status": "open", "opened_ts": int(time.time()),
            "closed_ts": None,
        }
        await self.db.update_cash_full(
            user_id, acc["cash_available"] + amount,
            acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
        await self._save(ledger)
        ratio = await self._margin_ratio(user_id)
        log.info("融资借款 %s %.2f 元（单号 %s，维持担保比例 %s）",
                 user_id, amount, loan_id,
                 f"{ratio:.2f}" if ratio is not None else "-")
        return (f"💰 融资成功：借入 {amount:,.2f} 元已到账（单号 {loan_id}）。\n"
                f"年利率 {getattr(CFG, 'margin_cash_rate_annual', 0.08):.1%}，"
                f"每日计息；当前维持担保比例 "
                f"{ratio:.1%}（平仓线 {_maintenance_ratio():.0%}）。")

    # ── 融券借券（借入即卖出）─────────────────────────────
    async def borrow_stock(self, user_id: str, code: str, quantity: int) -> str:
        """融券借券：借入股票立即按市价卖出，所得进可用资金，负债=数量×市价。"""
        async with trade_lock():
            async with self.db.transaction():
                return await self._borrow_stock(user_id, code, quantity)

    async def _borrow_stock(self, user_id: str, code: str, quantity: int) -> str:
        if code not in STOCK_POOL:
            raise MarginError(f"股票 {code} 不存在，可用 /炒股 搜 <关键词> 查询。")
        ok, why = self.engine.can_trade(code)
        if not ok:
            raise MarginError(f"{code} {why}，不可融券。")
        quantity = _check_amount(quantity, "融券数量")
        quantity = int(quantity)
        if quantity <= 0 or quantity % CFG.lot_size != 0:
            raise MarginError(f"融券数量必须为 {CFG.lot_size} 股的整数倍。")
        acc = await self._get_active_account(user_id)
        q = self.engine.get(code)
        price = q.price
        gross = round(price * quantity, 2)
        fee = round(fees_sell(gross), 2)
        proceeds = round(gross - fee, 2)
        ledger = await self._load()
        debt = self._debt(ledger, user_id)
        _, _, assets = await self._assets(user_id)
        cap = self._borrow_cap(assets, debt)
        if gross > cap + 1e-9:
            raise MarginError(
                f"融券超限：当前最多可融 {cap:,.2f} 元的券"
                f"（借入后维持担保比例不得低于 {_maintenance_ratio():.0%}）。")

        loan_id = str(ledger["next_id"])
        ledger["next_id"] += 1
        ledger["loans"][loan_id] = {
            "id": loan_id, "user_id": user_id, "kind": "stock", "code": code,
            "quantity": quantity, "principal": 0.0, "borrow_value": gross,
            "interest_accrued": 0.0, "last_interest_day": -1,
            "status": "open", "opened_ts": int(time.time()),
            "closed_ts": None,
        }
        await self.db.update_cash_full(
            user_id, acc["cash_available"] + proceeds,
            acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
        await self.db.insert_trade(None, user_id, code, "sell", price,
                                   quantity, fee, int(time.time()))
        self.engine.on_fill(code, quantity)
        await self._save(ledger)
        log.info("融券卖出 %s %s %d股 价格%.2f 所得%.2f（单号 %s）",
                 user_id, code, quantity, price, proceeds, loan_id)
        return (f"📉 融券成功：借入 {STOCK_POOL[code].name}（{code}）"
                f"{quantity} 股并以 {price:.2f} 元卖出，到账 {proceeds:,.2f} 元"
                f"（单号 {loan_id}，费用 {fee:,.2f} 元）。\n"
                f"还券：/炒股 还券 {code} {quantity}。股价每涨 1%，"
                f"负债约增 {gross * 0.01:,.2f} 元。")

    # ── 还款 / 还券 ───────────────────────────────────────
    async def repay_cash(self, user_id: str, amount: float) -> str:
        """归还融资（先息后本，按单号先后）。"""
        async with trade_lock():
            async with self.db.transaction():
                return await self._repay_cash(user_id, amount)

    async def _repay_cash(self, user_id: str, amount: float) -> str:
        amount = _check_amount(amount, "还款金额")
        acc = await self._get_active_account(user_id)
        if amount > acc["cash_available"] + 1e-9:
            raise MarginError(
                f"可用资金不足：需 {amount:,.2f} 元，"
                f"可用 {acc['cash_available']:,.2f} 元。")
        ledger = await self._load()
        loans = self._open_loans(ledger, user_id)
        if not loans:
            raise MarginError("你没有融资融券负债。")
        owed = sum(l["principal"] for l in loans if l["kind"] == "cash") \
            + sum(l["interest_accrued"] for l in loans)
        if owed <= 0:
            raise MarginError("融资本息已结清；融券负债请用 "
                              "/炒股 还券 <代码> <数量> 归还。")
        if amount > owed + 1e-9:
            amount = owed
        paid = await self._repay_debt(user_id, amount, ledger)
        return f"✅ 已还款 {paid:,.2f} 元（先息后本）。"

    async def _repay_debt(self, user_id: str, amount: float,
                          ledger: dict) -> float:
        """锁内用现金偿还利息 + 融资本金，返回实际还款额。"""
        acc = await self.db.get_account(user_id)
        amount = min(amount, acc["cash_available"])
        if amount <= 0:
            return 0.0
        paid = 0.0
        for l in self._open_loans(ledger, user_id):
            if amount <= 0:
                break
            take = min(l["interest_accrued"], amount)      # 先还息
            l["interest_accrued"] = round(l["interest_accrued"] - take, 2)
            amount = round(amount - take, 2)
            paid += take
            if l["kind"] == "cash":                        # 再还本金
                take = min(l["principal"], amount)
                l["principal"] = round(l["principal"] - take, 2)
                amount = round(amount - take, 2)
                paid += take
            debt_free = (l["kind"] != "cash" or l["principal"] <= 0)
            if debt_free and l["interest_accrued"] <= 0 and not (
                    l["kind"] == "stock" and l["quantity"] > 0):
                l["status"] = "closed"
                l["closed_ts"] = int(time.time())
        if paid > 0:
            await self.db.update_cash_full(
                user_id, acc["cash_available"] - paid,
                acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
            await self._save(ledger)
            log.info("还款 %s %.2f 元", user_id, paid)
        return paid

    async def cover(self, user_id: str, code: str, quantity: int) -> str:
        """买券还券：按市价买回股票归还融券负债。"""
        async with trade_lock():
            async with self.db.transaction():
                return await self._cover(user_id, code, quantity)

    async def _cover(self, user_id: str, code: str, quantity: int) -> str:
        quantity = _check_amount(quantity, "还券数量")
        quantity = int(quantity)
        if quantity <= 0:
            raise MarginError("还券数量必须为正数。")
        acc = await self._get_active_account(user_id)
        ledger = await self._load()
        loans = [l for l in self._open_loans(ledger, user_id)
                 if l["kind"] == "stock" and l["code"] == code]
        owed = sum(l["quantity"] for l in loans)
        if owed <= 0:
            raise MarginError(f"你没有 {code} 的融券负债。")
        if quantity > owed:
            raise MarginError(f"还券数量超过负债：最多可还 {owed} 股。")
        q = self.engine.get(code)
        price = q.price
        gross = round(price * quantity, 2)
        fee = round(fees_buy(gross), 2)
        cost = round(gross + fee, 2)
        if cost > acc["cash_available"] + 1e-9:
            raise MarginError(
                f"可用资金不足：买券还券需 {cost:,.2f} 元（含费用），"
                f"可用 {acc['cash_available']:,.2f} 元。")
        remaining = quantity
        for l in loans:
            if remaining <= 0:
                break
            take = min(l["quantity"], remaining)
            l["quantity"] -= take
            remaining -= take
            if l["quantity"] <= 0:
                l["status"] = "closed"
                l["closed_ts"] = int(time.time())
        await self.db.update_cash_full(
            user_id, acc["cash_available"] - cost,
            acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
        await self.db.insert_trade(None, user_id, code, "buy", price,
                                   quantity, fee, int(time.time()))
        self.engine.on_fill(code, quantity)
        await self._save(ledger)
        log.info("买券还券 %s %s %d股 价格%.2f 花费%.2f",
                 user_id, code, quantity, price, cost)
        return (f"✅ 已买券还券：{code} {quantity} 股，价格 {price:.2f} 元，"
                f"花费 {cost:,.2f} 元（含费用 {fee:,.2f} 元）。")

    # ── 每日计息（幂等）───────────────────────────────────
    async def accrue_day(self, day: int) -> dict:
        """每日计息。day 为虚拟交易日序号；每笔负债逐日补计息
        （last_interest_day+1 … day），期间停服不会漏息；已计息日
        直接跳过，重启后重复调用同一 day 不会重复扣息。返回统计。"""
        async with trade_lock():
            async with self.db.transaction():
                return await self._accrue_day_nolock(day)

    async def _accrue_day_nolock(self, day: int) -> dict:
        """无锁变体：调用方须已持 trade_lock 且在事务内（日终锁内防死锁）。"""
        ledger = await self._load()
        stats = {"charged": 0, "interest": 0.0, "skipped": 0}
        loans = self._open_loans(ledger)
        if not loans:
            return stats
        for l in loans:
            if l["last_interest_day"] >= day:
                stats["skipped"] += 1
                continue
            base = l["principal"] if l["kind"] == "cash" \
                else l["borrow_value"]
            rate = _cash_rate_daily() if l["kind"] == "cash" \
                else _stock_rate_daily()
            daily = round(base * rate, 2)
            if daily <= 0:
                l["last_interest_day"] = day
                continue
            # 逐日补计：停服几天就补几天（I1）。last_interest_day < 0 表示
            # 该笔负债从未计息，首日只计 1 天——不能按 day-(-1) 算，否则
            # 开户当日借款再 accrue_day(1) 会一次性扣 2 天息。
            if l["last_interest_day"] < 0:
                days = 1
            else:
                days = day - l["last_interest_day"]
            days = max(1, min(days, 365))
            amount = round(daily * days, 2)
            acc = await self.db.get_account(l["user_id"])
            if acc is None:
                l["last_interest_day"] = day
                continue
            pay = min(amount, max(acc["cash_available"], 0.0))
            # 应付全额入账；现金够的部分实付，不够的转为欠息（仍计负债）
            l["interest_accrued"] = round(
                l["interest_accrued"] + amount - pay, 2)
            if pay > 0:
                await self.db.update_cash_full(
                    l["user_id"], acc["cash_available"] - pay,
                    acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
            l["last_interest_day"] = day
            stats["charged"] += 1
            stats["interest"] = round(stats["interest"] + amount, 2)
            ledger["interest_log"].append({
                "loan_id": l["id"], "day": day, "days": days,
                "amount": amount, "paid": round(pay, 2),
                "ts": int(time.time())})
            log.info("计息 单号%s %s %.2f 元 × %d 天（现金实付 %.2f）",
                     l["id"], l["user_id"], daily, days, pay)
        ledger["interest_log"] = ledger["interest_log"][-_INTEREST_LOG_MAX:]
        await self._save(ledger)
        return stats

    # ── 强平清算 ──────────────────────────────────────────
    async def check_margin_calls(self) -> list[str]:
        """扫描全部负债账户，维持担保比例跌破平仓线的立即强平。
        集成：日终（DayCloseService，锁内调 _check_margin_calls_nolock）
        与定时器调用。"""
        async with trade_lock():
            return await self._check_margin_calls_nolock()

    async def _check_margin_calls_nolock(self) -> list[str]:
        """无锁变体：调用方须已持 trade_lock（日终锁内防死锁）。"""
        async with self.db.transaction():
            reports: list[str] = []
            ledger = await self._load()
            users = {l["user_id"] for l in self._open_loans(ledger)}
            for user_id in sorted(users):
                acc = await self.db.get_account(user_id)
                if not acc or acc["status"] != "active":
                    continue
                ratio = await self._margin_ratio(user_id)
                if ratio is not None and ratio < _maintenance_ratio():
                    reports.append(await self._force_liquidate(user_id))
            return reports

    async def force_liquidate(self, user_id: str) -> str:
        """强平：卖持仓 → 还本息 → 买券还券，至比例恢复或资产耗尽。"""
        async with trade_lock():
            return await self._force_liquidate(user_id)

    async def _force_liquidate(self, user_id: str) -> str:
        acc = await self.db.get_account(user_id)
        if not acc:
            raise MarginError("账户不存在。")
        if acc["status"] != "active":
            return f"{user_id} 账户非活跃（{acc['status']}），跳过强平。"
        ledger = await self._load()
        if not self._open_loans(ledger, user_id):
            return "无融资融券负债，无需强平。"
        ratio = await self._margin_ratio(user_id)
        if ratio is None or ratio >= _maintenance_ratio():
            return (f"维持担保比例 {ratio:.2%} ≥ "
                    f"{_maintenance_ratio():.0%}，无需强平。")

        # C5：强平前撤销该用户全部挂卖单（DB+撮合簿+竞价簿），
        # 防止强平卖出后委托簿残留幽灵卖单凭空增发股票
        if self.trade is not None:
            cancelled = await self.trade.cancel_user_orders(user_id, "sell")
            if cancelled:
                log.info("强平前撤单 %s：撤销 %d 笔卖单", user_id, cancelled)

        sold, covered, skipped = 0, 0, []
        steps = 0
        while steps < _LIQUIDATE_MAX_STEPS:
            steps += 1
            ratio = await self._margin_ratio(user_id)
            if ratio is None or ratio >= _maintenance_ratio():
                break
            ledger = await self._load()
            progress = False

            # 1) 卖持仓（市值大者优先；强平无视 T+1 可卖限制与涨跌停
            #    ——交易所强制平仓性质；但停牌/退市股无法成交，跳过）
            positions = await self.db.get_positions(user_id)
            if positions:
                def _mv(p):
                    q = self.engine.get(p["code"])
                    return (q.price if q else 0.0) * p["quantity"]
                pos = max(positions, key=_mv)
                q = self.engine.get(pos["code"])
                ok, why = self.engine.can_trade(pos["code"])
                if q is not None and ok:
                    price = q.price
                    gross = round(price * pos["quantity"], 2)
                    fee = round(fees_sell(gross), 2)
                    acc = await self.db.get_account(user_id)
                    await self.db.update_cash_full(
                        user_id, acc["cash_available"] + gross - fee,
                        acc["cash_withdrawable"], acc["cash_frozen"] or 0.0)
                    await self.db.delete_position(user_id, pos["code"])
                    await self.db.insert_trade(
                        None, user_id, pos["code"], "sell", price,
                        pos["quantity"], fee, int(time.time()))
                    self.engine.on_fill(pos["code"], pos["quantity"])
                    sold += 1
                    progress = True
                elif not ok:
                    if pos["code"] not in skipped:
                        skipped.append(pos["code"])

            # 2) 现金还息还本
            acc = await self.db.get_account(user_id)
            if acc["cash_available"] > 0.005:
                ledger = await self._load()
                if self._debt(ledger, user_id) > 0:
                    if await self._repay_debt(
                            user_id, acc["cash_available"], ledger) > 0:
                        progress = True

            # 3) 买券还券（现金够则整笔核销）
            ledger = await self._load()
            stock_loans = [l for l in self._open_loans(ledger, user_id)
                           if l["kind"] == "stock"]
            if stock_loans:
                l = stock_loans[0]
                q = self.engine.get(l["code"])
                acc = await self.db.get_account(user_id)
                if q is not None:
                    price = q.price
                    gross = round(price * l["quantity"], 2)
                    fee = round(fees_buy(gross), 2)
                    cost = round(gross + fee, 2)
                    if cost <= acc["cash_available"] + 1e-9:
                        await self.db.update_cash_full(
                            user_id, acc["cash_available"] - cost,
                            acc["cash_withdrawable"],
                            acc["cash_frozen"] or 0.0)
                        qty = l["quantity"]
                        l["quantity"] = 0
                        l["status"] = "closed"
                        l["closed_ts"] = int(time.time())
                        await self.db.insert_trade(
                            None, user_id, l["code"], "buy", price,
                            qty, fee, int(time.time()))
                        self.engine.on_fill(l["code"], qty)
                        covered += 1
                        progress = True
                        # 覆盖后立即落盘：下一轮 _margin_ratio 从库读账本，
                        # 不落盘会按旧负债重复还券
                        await self._save(ledger)

            if not progress:
                break
        if steps >= _LIQUIDATE_MAX_STEPS:
            log.warning("强平步数超限 %s，强制收尾", user_id)

        ledger = await self._load()
        await self._save(ledger)
        ratio = await self._margin_ratio(user_id)
        net = await self._net_worth(user_id)
        if net < 0:
            await self.db.set_status(user_id, "frozen", int(time.time()))
            log.warning("强平后净资产为负 %s：%.2f 元，账户冻结（L4），"
                        "等待管理员清账", user_id, net)
            return (f"🚨 强平完成：卖出持仓 {sold} 只、买券还券 {covered} 笔。"
                    f"强平后净资产 {net:,.2f} 元 < 0，账户已冻结（L4），"
                    f"请联系管理员清账。")
        ratio_txt = f"{ratio:.1%}" if ratio is not None else "负债已清零"
        log.info("强平完成 %s：卖 %d 只、还券 %d 笔，比例 %s，净资产 %.2f",
                 user_id, sold, covered, ratio_txt, net)
        return (f"⚠️ 强平完成：卖出持仓 {sold} 只、买券还券 {covered} 笔。"
                f"当前维持担保比例 {ratio_txt}，净资产 {net:,.2f} 元。")

    # ── 展示 ──────────────────────────────────────────────
    async def render(self, user_id: str) -> str:
        """信用账户一览（集成：/炒股 融资 子命令）。"""
        async with trade_lock():
            ledger = await self._load()
            loans = self._open_loans(ledger, user_id)
            cash, mv, assets = await self._assets(user_id)
            debt = self._debt(ledger, user_id)
            net = round(assets - debt, 2)
            lines = [
                "🏦 信用账户",
                f"现金 {cash:,.2f} 元 + 持仓 {mv:,.2f} 元 = 总资产 "
                f"{assets:,.2f} 元",
                f"总负债 {debt:,.2f} 元，净资产 {net:,.2f} 元",
            ]
            ratio = await self._margin_ratio(user_id)
            if ratio is None:
                lines.append("维持担保比例：无负债")
            else:
                lines.append(f"维持担保比例 {ratio:.1%}"
                             f"（平仓线 {_maintenance_ratio():.0%}）")
            for l in loans:
                if l["kind"] == "cash":
                    lines.append(
                        f"  融资 #{l['id']}：本金 {l['principal']:,.2f} 元，"
                        f"未付利息 {l['interest_accrued']:,.2f} 元")
                else:
                    q = self.engine.get(l["code"])
                    p = q.price if q else 0.0
                    lines.append(
                        f"  融券 #{l['id']}：{l['code']} {l['quantity']} 股，"
                        f"现价 {p:.2f} 元（盯市 {p * l['quantity']:,.2f} 元），"
                        f"未付利息 {l['interest_accrued']:,.2f} 元")
            lines.append("指令：/炒股 融资 <金额> 借款 · /炒股 还钱 <金额> · "
                         "/炒股 融券 <代码> <数量> · /炒股 还券 <代码> <数量>")
            return "\n".join(lines)
