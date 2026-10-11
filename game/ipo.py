"""新股申购（打新，P2）：发行 → 申购冻结 → 摇号配售上市。

对齐 DESIGN.md §9（发行价申购、每签 500/1000 股、中签率模拟真实）与
§2 新股首日涨跌幅（主板首日 +44% 上限；创业/科创/北交所前 5 日无涨跌幅）。

流程三阶段（账本 ipo.status 状态机）：
- **publish** 只登记发行信息（status=open）：申购期内该股不在行情中、不可交易。
- **subscribe** 申购全额预冻结：quantity×发行价 从 cash_available 转入
  cash_frozen（复用挂单冻结池；申购单不进 orders 表，与 dayclose 的挂单
  过期解冻互不干扰）。每账户累计上限 = 发行量的 1%（向下取整到整签、
  至少 1 签；真实为千分之一，游戏发行量小故放宽，见下方差异记录）。
- **draw** 摇号+缴款+上市一次完成：全部申购签号入签池，
  中签率 = min(1, 发行总签数 ÷ 总申购签数)，用 random.Random(seed).sample
  无放回抽取中签签号——与"每签独立抽签"同分布（超购时真实亦是按中签率
  比例配号摇号），结果由 seed 完全确定（默认取 publish 时记录的稳定种子），
  可复现、可测试，不靠概率碰运气。中签股按中签签数×签额划入持仓
  （sellable=0，沿用 T+1，日终 unlock_all_sellable 解锁），未中签部分
  立即从 cash_frozen 退回 cash_available。抽签后即刻上市：
  engine.register(stock, price)（market/engine.py 公开入口，幂等）。

**与 DESIGN §9 的差异（明确记录，未默默改规则）**：
1. DESIGN §9 为"中签缴款"（先摇号、后按中签结果付款）；本实现为申购时
   全额预冻结、摇号后退还未中签部分——最终资金结果与中签付款一致，但
   资金占用时点提前。理由：现有 db API 无跨表事务，"先配售后收款"在崩溃
   时会产生无款中签的悬空账（凭空销账风险）；预冻结保证任何中断点上钱
   股守恒。
2. 每账户申购上限取发行量 1%（真实 0.1%），避免小盘发行下上限不足 1 签。
3. 主板首日涨跌幅按 ±44% 对称简化（真实为 +44% 及不对称开盘集合竞价
   规则）；创业/科创/北交所"前 5 日无涨跌幅"以 ±1000% 覆盖带宽近似
   （tick 内价格仍被钳制在带宽里，实务等同无限制），到期由 on_day_start
   解除覆盖、恢复常规涨跌幅。
4. 中签不收佣金/印花税（真实打新亦无）；退款进 cash_available（当日可
   用，次日 settle_withdrawable 转可取），与撤买单退款同口径。

持久化与崩溃恢复：全部状态是 meta 表 JSON 账本 `ipo.ledger.v1`（现有
DB API，不改 storage/db.py；真实 SQLite 落盘、重启可恢复；账本损坏则
拒绝一切打新操作，防凭空销账）。所有资金/账本变动都在 trade_lock() 内
与交易、日终清算串行。两处"先落盘意图、再动钱、再落盘完成"的推进点，
中断后重入按账本恢复（还原 pre-image → 整笔重放一次），保证既不双退款
也不丢退款、不重复配股：
- 申购：freezing（记 pending 增量 + 现金/持仓 pre-image）→ active。
- 结算：inflight（记 pre-image）→ settled。
pre-image 还原假定崩溃与恢复之间该账户无其他资金操作（窗口为两次相邻
commit，极小；恢复发生时打 WARNING，出现即应核查该账户）。

接线（供后续集成 t4）：main.py 构造 `IPOService(db, engine)`；
`/打新` 指令调 status_text()/subscribe()；管理员 publish()/draw()；
启动时 `await ipo.restore()` 重登记已上市新股；日终清算里调用
`await ipo.on_day_start(day_key)` 推进首日涨跌幅到期。
"""

from __future__ import annotations

import json
import math
import random
import time
import zlib

from ..config import CFG
from ..locks import trade_lock
from ..log import get_logger
from ..market.stocks import (BOARD_BSE, BOARD_GEM, BOARD_MAIN, BOARD_STAR,
                             SECTORS, STOCK_POOL, Stock)
from ..storage.db import Database

log = get_logger("ipo")

_LEDGER_KEY = "ipo.ledger.v1"
_UNLIMITED_PCT = 10.0     # "无涨跌幅"近似：±1000% 覆盖带宽（Quote.pct_override）

# 各板块新股默认每 tick 波动率（与 market/stocks.py 静态池同量级）
_DEFAULT_VOL = {BOARD_MAIN: 0.005, BOARD_GEM: 0.011,
                BOARD_STAR: 0.013, BOARD_BSE: 0.015}


class IpoError(Exception):
    pass


# ── 数值（集成时移入 config.py，同名键即可自动生效）─────────────────
def _sub_cap_ratio() -> float:
    """每账户累计申购上限占发行量比例（真实 0.1%，游戏放宽为 1%）。"""
    return getattr(CFG, "ipo_sub_cap_ratio", 0.01)


def lot_size_of(code: str) -> int:
    """每签股数：沪市（6 开头）1000 股/签，其余 500 股/签（DESIGN §9 只锁 500/1000 两档）。"""
    return 1000 if code.startswith("6") else 500


def first_day_rule(board: str) -> tuple[int, float]:
    """(免常规涨跌幅天数, 期间 Quote.pct_override 覆盖值)。"""
    if board == BOARD_MAIN:
        return 1, 0.44                      # 主板首日 ±44% 对称简化
    return 5, _UNLIMITED_PCT                # 创业/科创/北交所前 5 日无限制（带宽近似）


def _r2(x: float) -> float:
    return round(x + 0.0, 2)


class IPOService:
    def __init__(self, db: Database, engine):
        self.db = db
        self.engine = engine

    # ── 账本读写（meta 表 JSON，仿 margin.ledger.v1 模式）──────────
    @staticmethod
    def _empty_ledger() -> dict:
        return {"version": 1, "next_id": 1, "last_limit_day": None,
                "ipos": {}, "subs": {}}

    async def _load(self) -> dict:
        """从 meta 表读账本（每次操作前调用，避免内存缓存与磁盘漂移）。"""
        raw = await self.db.get_meta(_LEDGER_KEY)
        if not raw:
            return self._empty_ledger()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 账本损坏时拒绝一切打新业务：宁可停服也不凭空销账（= 刷钱漏洞）
            raise IpoError("打新账本损坏（meta 表 JSON 解析失败），"
                           "已拒绝所有申购/摇号操作，请联系管理员核查。")
        if (not isinstance(data, dict)
                or not isinstance(data.get("ipos"), dict)
                or not isinstance(data.get("subs"), dict)):
            # 结构损坏（缺表/类型错）：拒绝 setdefault 凭空重建——否则已存在
            # 的发行与申购记录会被静默销掉（= 刷钱/丢账漏洞）
            raise IpoError("打新账本结构损坏（缺少 ipos/subs 发行与申购表），"
                           "已拒绝所有申购/摇号操作，请联系管理员核查。")
        try:
            max_id = max((int(k) for k in data["ipos"]), default=0)
        except (TypeError, ValueError):
            raise IpoError("打新账本损坏（ipos 键非数字），请联系管理员核查。")
        if not isinstance(data.get("next_id"), int) or data["next_id"] <= max_id:
            raise IpoError(f"打新账本损坏（next_id={data.get('next_id')!r} "
                           f"不大于最大发行 id {max_id}），请联系管理员核查。")
        data.setdefault("last_limit_day", None)
        return data

    async def _save(self, ledger: dict) -> None:
        await self.db.set_meta(_LEDGER_KEY,
                               json.dumps(ledger, ensure_ascii=False))

    @staticmethod
    def _sub_key(ipo_id: int, user_id: str) -> str:
        return f"{ipo_id}:{user_id}"

    @staticmethod
    def _ipo_of(ledger: dict, code: str) -> dict | None:
        for ipo in ledger["ipos"].values():
            if ipo["code"] == code:
                return ipo
        return None

    def _subs_of(self, ledger: dict, ipo_id: int) -> list[dict]:
        return sorted((s for s in ledger["subs"].values()
                       if s["ipo_id"] == ipo_id),
                      key=lambda s: s["user_id"])

    # ── 资金/持仓底层（口径与 game/trade.py 一致）──────────────────
    async def _move_cash(self, user_id: str, available: float = 0.0,
                         frozen: float = 0.0) -> None:
        acc = await self.db.get_account(user_id)
        if not acc:
            return
        await self.db.update_cash_full(
            user_id,
            acc["cash_available"] + available,
            acc["cash_withdrawable"],
            (acc["cash_frozen"] or 0.0) + frozen)

    async def _pre_image(self, user_id: str, code: str) -> dict:
        """现金 + 该股持仓快照（新股代码结算前不存在其他来源的持仓）。"""
        acc = await self.db.get_account(user_id)
        pos = await self.db.get_position(user_id, code)
        pre_pos = None
        if pos:
            pre_pos = [pos["quantity"], pos["sellable"], pos["cost_price"]]
        return {"cash": [acc["cash_available"], acc["cash_withdrawable"],
                         acc["cash_frozen"] or 0.0],
                "pos": pre_pos}

    async def _restore_pre(self, user_id: str, code: str, pre: dict) -> None:
        """把账户现金/持仓还原到 pre-image（重放前撤销半截应用）。"""
        a, w, f = pre["cash"]
        await self.db.update_cash_full(user_id, a, w, f)
        if pre["pos"] is None:
            await self.db.delete_position(user_id, code)
        else:
            q, s, c = pre["pos"]
            await self.db.upsert_position(user_id, code, q, s, c)

    async def _resume_freezing(self, sub: dict, ipo: dict) -> bool:
        """恢复一笔半截冻结（freezing）：还原 pre-image → 重放冻结增量。

        返回 False 表示崩溃窗口内该账户现金被其他操作动过、无法补冻，
        已放弃该笔订阅（退掉已确认冻结的部分，不白送签号）。
        """
        code = ipo["code"]
        uid = sub["user_id"]
        log.warning("恢复半截申购冻结：%s %s（崩溃窗口内该账户不应有其他资金操作）",
                    code, uid)
        await self._restore_pre(uid, code, sub["pre"])
        pend = sub.get("pending") or {}
        need = pend.get("amount", 0.0)
        acc = await self.db.get_account(uid)
        if need > 0 and (not acc or acc["cash_available"] < need):
            # 现金被动过：放弃整笔（退掉已确认冻结的部分）
            prior = _r2(sub["amount"] - 0.0)     # amount 尚未含 pending
            if acc and prior > 0:
                await self._move_cash(uid, available=prior, frozen=-prior)
            elif acc and prior < 0:              # 防御：账目异常不放大
                log.warning("放弃申购时账目异常 %s %s prior=%.2f", code, uid, prior)
            sub.update(quantity=0, lots=0, amount=0.0,
                       state="settled", win_lots=sub.get("win_lots") or 0,
                       pending=None, pre=None)
            return False
        if need > 0:
            await self._move_cash(uid, available=-need, frozen=need)
        sub["quantity"] += pend.get("quantity", 0)
        sub["lots"] += pend.get("lots", 0)
        sub["amount"] = _r2(sub["amount"] + need)
        sub["pending"] = None
        sub["state"] = "active"
        sub["pre"] = None
        return True

    # ── 发行 ────────────────────────────────────────────────────────
    async def publish(self, code: str, name: str, board: str, sector: str,
                      industry: str, issue_price: float, total_shares: int,
                      volatility: float | None = None, beta: float = 1.0,
                      sector_beta: float = 0.8, seed: int | None = None) -> str:
        """登记一只新股发行（status=open，进入申购期）。返回播报文案。"""
        code = str(code).strip()
        if not code or not code.isdigit():
            raise IpoError(f"股票代码无效：{code!r}")
        if board not in (BOARD_MAIN, BOARD_GEM, BOARD_STAR, BOARD_BSE):
            raise IpoError(f"板块无效：{board}")
        if sector not in SECTORS:
            # 必须校验：tick 的板块因子按 SECTORS 取值，野板块会直接 KeyError
            raise IpoError(f"板块联动单位无效：{sector}"
                           f"（可选：{'/'.join(SECTORS)}）")
        if not name or not industry:
            raise IpoError("名称与行业不能为空")
        try:
            issue_price = float(issue_price)
            total_shares = int(total_shares)
            if seed is not None:
                seed = int(seed)
        except (TypeError, ValueError, OverflowError):
            raise IpoError("发行价/发行量/随机种子必须是数字")
        # NaN/Inf 能穿过 <= 0 比较（nan <= 0 为 False）——必须先过有限性
        if not math.isfinite(issue_price) or issue_price <= 0:
            raise IpoError("发行价必须是有限正数")
        issue_price = _r2(issue_price)
        lot = lot_size_of(code)
        if total_shares < lot:
            raise IpoError(f"发行量至少一个整签（{lot} 股）")
        if volatility is not None and volatility <= 0:
            raise IpoError("波动率必须为正")
        async with trade_lock():
            ledger = await self._load()
            if code in STOCK_POOL:
                raise IpoError(f"{code} 已在股票池中，不能重复发行")
            if self._ipo_of(ledger, code) is not None:
                raise IpoError(f"{code} 已有发行记录")
            ipo_id = ledger["next_id"]
            ledger["next_id"] += 1
            ledger["ipos"][str(ipo_id)] = {
                "id": ipo_id, "code": code, "name": name, "board": board,
                "sector": sector, "industry": industry,
                "issue_price": issue_price, "lot_size": lot,
                "total_shares": total_shares,
                "total_lots": total_shares // lot,
                "volatility": float(volatility
                                    or _DEFAULT_VOL.get(board, 0.006)),
                "beta": float(beta), "sector_beta": float(sector_beta),
                "status": "open", "seed": int(seed if seed is not None
                                              else zlib.crc32(code.encode())),
                "ts": int(time.time()), "days_listed": 0, "listed": False,
                "draw_info": None,
            }
            await self._save(ledger)
        cap = self._cap_lots(total_shares, lot)
        log.info("发行登记：%s %s 发行价 %.2f 量 %d 股（%s，每签 %d 股）",
                 code, name, issue_price, total_shares, board, lot)
        return (f"🆕 新股发行：{name}（{code}，{board}）\n"
                f"发行价 {issue_price:.2f} 元，发行量 {total_shares:,} 股，"
                f"每签 {lot} 股，每账户上限 {cap} 签。\n"
                f"发送 /打新 申购 {code} <签数>。")

    @staticmethod
    def _cap_lots(total_shares: int, lot: int) -> int:
        return max(1, int(total_shares * _sub_cap_ratio()) // lot)

    # ── 申购（冻结资金）─────────────────────────────────────────────
    async def subscribe(self, user_id: str, code: str, lots: int) -> str:
        """申购：lots×lot_size×发行价 从可用转冻结。返回播报文案。"""
        if not isinstance(lots, int) or isinstance(lots, bool) or lots < 1:
            raise IpoError("签数必须是正整数")
        async with trade_lock():
            ledger = await self._load()
            ipo = self._ipo_of(ledger, code)
            if ipo is None:
                raise IpoError(f"没有发行中的新股 {code}")
            if ipo["status"] != "open":
                raise IpoError(f"{ipo['name']}（{code}）已结束申购")
            acc = await self.db.get_account(user_id)
            if not acc:
                raise IpoError("请先开户")
            if acc["status"] != "active":
                raise IpoError(f"账户状态异常（{acc['status']}），不能申购")
            lot, price = ipo["lot_size"], ipo["issue_price"]
            key = self._sub_key(ipo["id"], user_id)
            sub = ledger["subs"].get(key)
            if sub is not None and sub["state"] == "freezing":
                await self._resume_freezing(sub, ipo)   # 崩溃恢复（放弃时 sub 清零）
                sub = ledger["subs"][key]
            held = sub["quantity"] if sub else 0
            cap = self._cap_lots(ipo["total_shares"], lot)
            if (held // lot) + lots > cap:
                raise IpoError(f"超出申购上限：每账户最多 {cap} 签"
                               f"（已申购 {held // lot} 签）")
            qty = lots * lot
            amount = _r2(qty * price)
            if acc["cash_available"] < amount:
                raise IpoError(f"可用资金不足：需 {amount:,.2f} 元，"
                               f"可用 {acc['cash_available']:,.2f} 元")
            pre = await self._pre_image(user_id, code)
            if sub is None:
                sub = {"ipo_id": ipo["id"], "user_id": user_id,
                       "quantity": 0, "lots": 0, "amount": 0.0,
                       "state": "freezing", "win_lots": None,
                       "pending": None, "pre": None}
                ledger["subs"][key] = sub
            # 意图（增量）先落盘，再动钱，最后并入正式计数
            sub.update(state="freezing", pre=pre,
                       pending={"quantity": qty, "lots": lots,
                                "amount": amount})
            await self._save(ledger)
            await self._move_cash(user_id, available=-amount, frozen=amount)
            sub["quantity"] += qty
            sub["lots"] += lots
            sub["amount"] = _r2(sub["amount"] + amount)
            sub.update(state="active", pending=None, pre=None)
            await self._save(ledger)
            total_lots, total_amount = sub["lots"], sub["amount"]
        log.info("申购 %s：%s %d 签（累计 %d 签，冻结 %.2f 元）",
                 code, user_id, lots, total_lots, amount)
        return (f"✅ 申购成功：{ipo['name']}（{code}）{lots} 签"
                f"（{qty:,} 股），冻结 {amount:,.2f} 元；"
                f"累计 {total_lots} 签（{total_amount:,.2f} 元），待摇号。")

    # ── 摇号 + 缴款 + 上市 ─────────────────────────────────────────
    async def draw(self, code: str, seed: int | None = None) -> dict:
        """完成一次摇号：确定性抽签 → 中签划股、退款 → 即刻上市。

        幂等可重入：中断后再次调用按账本恢复（不双退款、不重复配股）；
        已摇号的发行再次调用直接报错。返回 draw_info 汇总。
        """
        async with trade_lock():
            ledger = await self._load()
            ipo = self._ipo_of(ledger, code)
            if ipo is None:
                raise IpoError(f"没有新股发行记录 {code}")
            if ipo["status"] == "drawn":
                raise IpoError(f"{ipo['name']}（{code}）已完成摇号，不能重复摇号")
            if ipo["status"] == "open":
                if seed is not None:           # 显式种子只在开摇时写入
                    ipo["seed"] = int(seed)
                ipo["status"] = "drawing"
                await self._save(ledger)
            # 恢复路径：status=drawing（无论新摇还是崩溃重入）
            subs = self._subs_of(ledger, ipo["id"])
            # 1) 半截冻结（freezing）先还原再补冻，之后才计入签池
            for sub in subs:
                if sub["state"] == "freezing":
                    await self._resume_freezing(sub, ipo)
                    await self._save(ledger)
            subs = self._subs_of(ledger, ipo["id"])
            to_settle = [s for s in subs if s["state"] in ("active", "inflight")]
            # 2) 确定性抽签（签号 = 全部有效申购签的序号空间；seed 来自账本）
            if any(s["win_lots"] is None for s in to_settle):
                t_total = sum(s["lots"] for s in to_settle)
                k = min(ipo["total_lots"], t_total)
                rng = random.Random(ipo["seed"])
                winners = set(rng.sample(range(t_total), k)) if k > 0 else set()
                offset = 0
                for sub in to_settle:
                    sub["win_lots"] = sum(1 for i in winners
                                          if offset <= i < offset + sub["lots"])
                    offset += sub["lots"]
                await self._save(ledger)       # 中签结果先落盘，再动钱/股
            # 3) 逐笔结算：pre-image → 退/付 + 划股 → settled（可安全重放）
            lot = ipo["lot_size"]
            for sub in to_settle:
                if sub["state"] == "settled":
                    continue
                uid = sub["user_id"]
                if sub["state"] == "inflight":  # 崩溃恢复：先还原再整笔重放
                    log.warning("摇号结算恢复重放：%s %s（崩溃窗口内该账户不应有其他资金操作）",
                                code, uid)
                    await self._restore_pre(uid, code, sub["pre"])
                sub["pre"] = await self._pre_image(uid, code)
                sub["state"] = "inflight"
                await self._save(ledger)
                pay = _r2((sub["win_lots"] or 0) * lot * ipo["issue_price"])
                refund = _r2(sub["amount"] - pay)
                await self._move_cash(uid, available=refund,
                                      frozen=-sub["amount"])
                win_qty = (sub["win_lots"] or 0) * lot
                if win_qty > 0:
                    pos = await self.db.get_position(uid, code)
                    if pos:
                        q0, c0 = pos["quantity"], pos["cost_price"]
                        qty = q0 + win_qty
                        cost = _r2((q0 * c0 + win_qty * ipo["issue_price"]) / qty)
                    else:
                        qty, cost = win_qty, ipo["issue_price"]
                    await self.db.upsert_position(uid, code, qty,
                                                  pos["sellable"] if pos else 0,
                                                  cost)
                sub.update(state="settled", pre=None)
                await self._save(ledger)
            # 4) 收尾：统计 + 登记上市 + 首日涨跌幅覆盖
            subs = self._subs_of(ledger, ipo["id"])
            subscribed = sum(s["quantity"] for s in subs)
            subscribed_lots = sum(s["lots"] for s in subs)
            winning_lots = sum(s.get("win_lots") or 0 for s in subs)
            winners = sum(1 for s in subs if (s.get("win_lots") or 0) > 0)
            refunded = _r2(sum(_r2(s["amount"]
                                  - (s.get("win_lots") or 0) * lot
                                  * ipo["issue_price"]) for s in subs))
            info = {"total_subscribed": subscribed,
                    "winning_lots": winning_lots,
                    "total_lots": ipo["total_lots"],
                    "rate": (winning_lots / subscribed_lots)
                    if subscribed_lots else 0.0,
                    "winners": winners,
                    "allotted_shares": winning_lots * lot,
                    "refunded": refunded, "seed": ipo["seed"]}
            ipo.update(status="drawn", draw_info=info, days_listed=0,
                       listed=True)
            await self._save(ledger)
            self._register_listing(ipo)
        log.info("摇号完成 %s %s：申购 %d 股、中签 %d 股（%d/%d 签，"
                 "中签率 %.4f，退款 %.2f 元，seed=%s）",
                 code, ipo["name"], subscribed, info["allotted_shares"],
                 winning_lots, ipo["total_lots"], info["rate"], refunded,
                 info["seed"])
        return info

    def _stock_of(self, ipo: dict) -> Stock:
        return Stock(code=ipo["code"], name=ipo["name"], board=ipo["board"],
                     sector=ipo["sector"], industry=ipo["industry"],
                     initial_price=ipo["issue_price"],
                     volatility=ipo["volatility"], beta=ipo["beta"],
                     sector_beta=ipo["sector_beta"], is_st=False)

    def _register_listing(self, ipo: dict, price: float | None = None) -> None:
        """上市登记（幂等）：行情接入 + 未到期的首日涨跌幅覆盖。"""
        if self.engine is None:
            return
        self.engine.register(self._stock_of(ipo),
                             price if price is not None else ipo["issue_price"])
        free_days, override = first_day_rule(ipo["board"])
        q = self.engine.quotes.get(ipo["code"])
        if q is not None:
            q.pct_override = (override if ipo["days_listed"] < free_days
                              else None)

    # ── 重启恢复 / 日切 ────────────────────────────────────────────
    async def restore(self) -> int:
        """启动时调用：重新登记全部已上市新股（不重复配股、不重复退款）。

        价格取 prices 表快照（若日终落过库），否则按发行价；未到期的新股
        恢复首日涨跌幅覆盖。返回登记只数。发现半截状态（freezing/inflight）
        只打 WARNING，由下一次 draw/subscribe 走恢复重放。
        """
        if self.engine is None:
            return 0
        count = 0
        ledger = await self._load()
        for ipo in ledger["ipos"].values():
            if not ipo.get("listed") or ipo["status"] != "drawn":
                continue
            row = await self.db.get_price(ipo["code"])
            self._register_listing(ipo, row["price"] if row else None)
            count += 1
            stuck = [s["user_id"] for s in self._subs_of(ledger, ipo["id"])
                     if s["state"] in ("freezing", "inflight")]
            if stuck:
                log.warning("打新 %s 存在半截订阅（%s），等待下一次 draw/subscribe 恢复重放",
                            ipo["code"], ",".join(stuck))
        if count:
            log.info("打新恢复：已重新登记 %d 只已上市新股", count)
        return count

    async def on_day_start(self, day_key: int) -> int:
        """每个虚拟交易日开始时调用一次（日终清算接线，t4）。

        推进已上市新股的免涨跌幅天数，到期解除 Quote.pct_override、
        恢复常规涨跌幅。day_key 为单调日键（如当日零点时间戳）；同一天
        重复调用幂等。返回本次解除覆盖的股票数。
        """
        async with trade_lock():
            ledger = await self._load()
            if ledger["last_limit_day"] == day_key:
                return 0
            cleared = 0
            for ipo in ledger["ipos"].values():
                if ipo["status"] != "drawn":
                    continue
                free_days, _ = first_day_rule(ipo["board"])
                if ipo["days_listed"] >= free_days:
                    continue
                ipo["days_listed"] += 1
                if ipo["days_listed"] >= free_days:
                    q = (self.engine.quotes.get(ipo["code"])
                         if self.engine else None)
                    if q is not None:
                        q.pct_override = None
                    cleared += 1
                    log.info("新股 %s %s 免涨跌幅期满，恢复常规涨跌幅",
                             ipo["code"], ipo["name"])
            ledger["last_limit_day"] = day_key
            await self._save(ledger)
            return cleared

    # ── 查询/播报 ──────────────────────────────────────────────────
    async def status_text(self, user_id: str | None = None) -> str:
        """打新面板：发行列表 +（可选）该用户的申购与中签结果。"""
        ledger = await self._load()
        lines: list[str] = []
        open_any = False
        for ipo in sorted(ledger["ipos"].values(), key=lambda i: i["id"]):
            if ipo["status"] != "open":
                continue
            open_any = True
            oversold = sum(s["quantity"] for s in
                           self._subs_of(ledger, ipo["id"]))
            extra = ""
            if oversold > ipo["total_shares"]:
                extra = f"（超购 {oversold / ipo['total_shares']:.1f}×）"
            lines.append(
                f"· {ipo['name']}（{ipo['code']}，{ipo['board']}）"
                f"发行价 {ipo['issue_price']:.2f} 元，每签 {ipo['lot_size']} 股，"
                f"发行 {ipo['total_shares']:,} 股，已申购 {oversold:,} 股{extra}")
        if not open_any:
            lines.append("（暂无发行中的新股）")
        if user_id:
            for ipo in sorted(ledger["ipos"].values(), key=lambda i: i["id"]):
                sub = ledger["subs"].get(self._sub_key(ipo["id"], user_id))
                if not sub:
                    continue
                if ipo["status"] == "drawn" and sub.get("win_lots") is not None:
                    win = sub["win_lots"] or 0
                    pay = _r2(win * ipo["lot_size"] * ipo["issue_price"])
                    lines.append(
                        f"· {ipo['name']}：申购 {sub['lots']} 签，"
                        f"中签 {win} 签（{win * ipo['lot_size']:,} 股，"
                        f"{pay:,.2f} 元）")
                elif ipo["status"] == "open" and sub["lots"] > 0:
                    lines.append(
                        f"· {ipo['name']}：已申购 {sub['lots']} 签"
                        f"（冻结 {sub['amount']:,.2f} 元，待摇号）")
        return "📌 新股申购（打新）\n" + "\n".join(lines)
