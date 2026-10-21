"""成就系统（P3）：只读推导，不建表、不写账本。

设计约束（沿用 DESIGN.md:316「成就/称号：翻倍王、回撤控制、打板高手」）：
- **纯只读**：每个成就都从既有事实推导（accounts / positions / trades /
  prices / meta 账本），不写任何表、不调用任何写 dao、不持 trade_lock。
  冷启动只看数据库也能算出同一份结果，不需要额外落地一张成就表。
- **拿不到精确数据时用近似并在 desc 里注明**（含「（近似）」字样），
  绝不静默降级成 False；数据源不可读时在 progress 里说明需人工核查。
- 不 import main.py，不 import astrbot。

近似点集中在这里，改动前先读：
1. 回撤控制：没有历史总资产快照表，用成交记录回放现金+持仓（成交价计价）
   重建峰值；不含分红送转/融资负债/挂单冻结；重生后以「初始资金×重生
   折扣」为基线，且只回放 last_rebirth_ts 之后的成交。
2. 打板高手：prices 表只有「当前昨收」快照，跨日成交与 ST 摘戴帽、
   新股首日涨跌幅覆盖（±44%/±1000%）都无法还原，故为近似。
3. 板块赢家：持仓成本用 positions.cost_price（摊薄成本），非每日快照。
4. 成交笔数：用 get_user_trades 扫描最近 _TRADE_SCAN_LIMIT 条，超出记 ≥。
"""

from __future__ import annotations

import json
import time

from ..config import CFG
from ..market.stocks import STOCK_POOL
from ..storage.db import Database

_MARGIN_LEDGER_KEY = "margin.ledger.v1"   # game/margin.py 的 meta 账本键
_IPO_LEDGER_KEY = "ipo.ledger.v1"         # game/ipo.py 的 meta 账本键
_APPROX_LIMIT_PCT = 0.095   # 拿不到板块涨跌幅时的近似涨停线（昨收×1.095）
_TRADE_SCAN_LIMIT = 5000    # 成交扫描上限（读保护，防一次拉全表）
_SURVIVOR_DAYS = 90         # 「老股民」开户天数
_DRAWDOWN_LIMIT = 0.10      # 「回撤控制」允许的最大回撤
_SECTOR_HOT = 0.20          # 「板块赢家」板块收益率阈值


class AchievementService:
    """成就列表与文本渲染（只读）。"""

    def __init__(self, db: Database):
        self.db = db

    # ── 只读取数 ──────────────────────────────────────────────
    async def _trades(self, user_id: str) -> list:
        """该用户成交流水（按 trade_id 升序；只读 dao）。"""
        rows = await self.db.get_user_trades(user_id, _TRADE_SCAN_LIMIT)
        return list(reversed(rows))          # dao 返回是按 trade_id 降序

    async def _ledger(self, key: str):
        """读 meta 账本（只读）。返回 (data, ok)；ok=False 表示账本损坏。"""
        raw = await self.db.get_meta(key)
        if not raw:
            return {}, True                  # 空账本 = 没有任何记录
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None, False
        if not isinstance(data, dict):
            return None, False
        return data, True

    async def _total_assets(self, acc, positions, price_of) -> float:
        """总资产 = 可用资金 + 持仓市值（口径同 game/account.py）。"""
        total = acc["cash_available"] or 0.0
        for pos in positions:
            p = price_of(pos["code"])
            if p is not None:
                total += p * pos["quantity"]
        return total

    # ── 派生判定 ──────────────────────────────────────────────
    @staticmethod
    def _limit_pct_of(code: str) -> float:
        """该股标准涨跌幅（ST/板块）。未知代码返回 None → 走近似线。"""
        stock = STOCK_POOL.get(code)
        return stock.limit_pct if stock is not None else None

    def _limit_hit(self, trades: list, prices: dict) -> tuple[bool, str]:
        """是否有一笔买入成交在当日涨停价（近似）。

        prices 表只存当前昨收快照，跨日成交会失真；ST 摘戴帽与新股首日
        涨跌幅覆盖也无法还原，故 desc 必须注明「近似」。
        """
        best_pct = None
        for t in trades:
            if t["side"] != "buy":
                continue
            row = prices.get(t["code"])
            if not row or not row["prev_close"] or row["prev_close"] <= 0:
                continue
            prev = float(row["prev_close"])
            pct = self._limit_pct_of(t["code"])
            line = _APPROX_LIMIT_PCT if pct is None else pct
            up = round(prev * (1 + line), 2)
            if t["price"] >= up - 0.011:             # 留一个 tick 容差
                return True, f"有一笔买入价 {t['price']:.2f} 达到涨停线 {up:.2f}"
            got = (t["price"] - prev) / prev
            if best_pct is None or got > best_pct:
                best_pct = got
        if best_pct is None:
            return False, "暂无可比对昨收的买入成交"
        return False, f"最高买入涨幅 {best_pct * 100:+.1f}%（涨停线约 +10%）"

    async def _equity_peak(self, acc, trades: list) -> float:
        """重建历史总资产峰值（近似，只读回放）。"""
        baseline = acc["initial_cash"] or CFG.initial_cash
        start_ts = 0
        if (acc["rebirth_count"] or 0) > 0:
            # 重生会清仓并把 initial_cash 重置为「初始资金×折扣」，之前的
            # 成交与旧本金已不对应，故基线取重生本金、只回放重生后成交。
            baseline = CFG.initial_cash * CFG.rebirth_cash_factor
            start_ts = acc["last_rebirth_ts"] or 0
        cash = float(baseline)
        qty: dict[str, int] = {}
        price: dict[str, float] = {}
        peak = 0.0
        for t in trades:
            if (t["ts"] or 0) < start_ts:
                continue
            code, p, q = t["code"], t["price"], t["quantity"]
            fee = t["fee"] or 0.0
            if t["side"] == "buy":
                cash -= p * q + fee
                qty[code] = qty.get(code, 0) + q
            else:
                cash += p * q - fee
                qty[code] = qty.get(code, 0) - q
            price[code] = p
            total = cash + sum(n * price.get(c, 0.0) for c, n in qty.items())
            peak = max(peak, total)
        return peak

    async def _ipo_facts(self, user_id: str) -> tuple[bool, bool, bool]:
        """(是否中过签, 是否参与过打新, 账本可读)。只读 ipo.ledger.v1。"""
        data, ok = await self._ledger(_IPO_LEDGER_KEY)
        if not ok:
            return False, False, False
        subs = data.get("subs") or {}
        if not isinstance(subs, dict):
            return False, False, False
        mine = [s for s in subs.values()
                if isinstance(s, dict) and s.get("user_id") == user_id]
        won = any((s.get("win_lots") or 0) > 0 for s in mine)
        joined = any((s.get("lots") or 0) > 0 for s in mine)
        return won, joined, True

    async def _margin_facts(self, user_id: str) -> tuple[bool, bool]:
        """(是否有过融资融券负债, 账本可读)。只读 margin.ledger.v1。"""
        data, ok = await self._ledger(_MARGIN_LEDGER_KEY)
        if not ok:
            return False, False
        loans = data.get("loans") or {}
        if not isinstance(loans, dict):
            return False, False
        borrowed = any(isinstance(l, dict) and l.get("user_id") == user_id
                       for l in loans.values())
        return borrowed, True

    # ── 对外接口 ──────────────────────────────────────────────
    async def list_achievements(self, user_id: str, price_of) -> list[dict]:
        """只读推导全部成就。price_of(code) -> 现价或 None。"""
        acc = await self.db.get_account(user_id)
        if not acc:
            return []
        positions = await self.db.get_positions(user_id)
        trades = await self._trades(user_id)
        prices = {row["code"]: row
                  for row in await self.db.get_all_prices()}
        total = await self._total_assets(acc, positions, price_of)
        initial = acc["initial_cash"] or 1.0
        out: list[dict] = []

        def add(aid, name, desc, done, progress):
            out.append({"id": aid, "name": name, "desc": desc,
                        "done": bool(done), "progress": progress})

        n_trade = len(trades)
        capped = n_trade >= _TRADE_SCAN_LIMIT
        cnt = f"≥{n_trade}" if capped else f"{n_trade}"
        add("first_trade", "股市初体验", "完成第一笔成交",
            n_trade >= 1, f"成交 {cnt} 笔")
        add("ten_trades", "百笔成交", "累计成交 10 笔",
            n_trade >= 10, f"成交 {cnt}/10 笔")
        add("hundred_trades", "千笔成交", "累计成交 100 笔",
            n_trade >= 100, f"成交 {cnt}/100 笔")

        mult = total / initial if initial > 0 else 0.0
        add("double", "翻倍王", "总资产达到初始资金的 2 倍",
            mult >= 2.0, f"总资产 {mult:.2f}×/2.00×")
        add("triple", "三倍牛人", "总资产达到初始资金的 3 倍",
            mult >= 3.0, f"总资产 {mult:.2f}×/3.00×")

        peak = await self._equity_peak(acc, trades)
        if n_trade == 0:
            add("drawdown_control", "回撤控制",
                "从总资产峰值回撤不超过 10%（近似）", False,
                "暂无成交记录，无法评估峰值")
        else:
            peak = max(peak, total)
            dd = (peak - total) / peak if peak > 0 else 0.0
            add("drawdown_control", "回撤控制",
                "从总资产峰值回撤不超过 10%（近似）", dd <= _DRAWDOWN_LIMIT,
                f"峰值 {peak:,.0f} → 现值 {total:,.0f}"
                f"（回撤 {dd * 100:.1f}%）")

        hit, detail = self._limit_hit(trades, prices)
        add("limit_hitter", "打板高手",
            "有一笔买入成交在当日涨停价（近似）", hit, detail)

        won, joined, ipo_ok = await self._ipo_facts(user_id)
        if not ipo_ok:
            add("ipo_winner", "中签达人",
                "至少中签 1 次（近似：来源打新账本）", False,
                "打新账本不可读，未静默判否，需人工核查")
        else:
            add("ipo_winner", "中签达人",
                "至少中签 1 次（近似：来源打新账本 win_lots）", won,
                f"申购记录 {joined and '有' or '无'}，中签 "
                f"{won and '有' or '无'}")

        borrowed, margin_ok = await self._margin_facts(user_id)
        if not margin_ok:
            add("margin_user", "信用玩家",
                "曾发生融资融券借款（近似：来源融资账本）", False,
                "融资账本不可读，未静默判否，需人工核查")
        else:
            add("margin_user", "信用玩家",
                "曾发生融资融券借款（含已还清，来源融资账本）", borrowed,
                f"账本记录：{borrowed and '有' or '无'}")

        rb = acc["rebirth_count"] or 0
        add("bankrupt", "浴火重生", "破产后完成至少 1 次重生",
            rb >= 1, f"重生 {rb} 次")

        days = (time.time() - (acc["created_ts"] or 0)) / 86400.0
        add("survivor", "老股民", f"开户满 {_SURVIVOR_DAYS} 天",
            days >= _SURVIVOR_DAYS, f"开户 {max(int(days), 0)} 天")

        sec_cost: dict[str, float] = {}
        sec_mv: dict[str, float] = {}
        for pos in positions:
            p = price_of(pos["code"])
            if p is None:
                continue
            stock = STOCK_POOL.get(pos["code"])
            sec = stock.sector if stock is not None else "未知"
            sec_cost[sec] = sec_cost.get(sec, 0.0) + pos["cost_price"] * pos["quantity"]
            sec_mv[sec] = sec_mv.get(sec, 0.0) + p * pos["quantity"]
        best_sec, best_ret = None, 0.0
        for sec, cost in sec_cost.items():
            if cost <= 0:
                continue
            ret = (sec_mv[sec] - cost) / cost
            if best_sec is None or ret > best_ret:
                best_sec, best_ret = sec, ret
        if best_sec is None:
            add("sector_hot", "板块赢家",
                "单一板块持仓收益率超过 20%（近似：按摊薄成本）", False,
                "当前无持仓")
        else:
            add("sector_hot", "板块赢家",
                "单一板块持仓收益率超过 20%（近似：按摊薄成本）",
                best_ret > _SECTOR_HOT,
                f"最强板块 {best_sec} {best_ret * 100:+.1f}%"
                f"（阈值 +{_SECTOR_HOT * 100:.0f}%）")

        return out

    async def render(self, user_id: str, price_of) -> str:
        """中文文本：每行一个成就，≤12 行（与成就数一致，无表头）。"""
        items = await self.list_achievements(user_id, price_of)
        if not items:
            return "还没有成就数据（先 /炒股 开户）。"
        lines = []
        for it in items:
            mark = "✅" if it["done"] else "⬜"
            tail = "" if it["done"] else f"（进度：{it['progress']}）"
            lines.append(f"{mark} {it['name']} — {it['desc']}{tail}")
        return "\n".join(lines)
