"""自选股（P3）：收藏 / 移除 / 列表 / 文本渲染。不涉资金，写操作不加 trade_lock。

约定：
- code 校验走 ``market.stocks.STOCK_POOL``（唯一股票真相来源）；
- 只读查询走 db 方法；写操作直接调 ``db.watchlist_*``，不套 transaction()
  （单条 INSERT/DELETE 本身原子，且自选不涉资金，无需与资金变动同生共死）；
- ``price_of(code)`` 由调用方注入（与 leaderboard / account 同签名），返回现价或 None
  （None 表示暂停交易/无行情）；涨跌幅另取 ``db.get_price`` 的昨收自行计算，避免
  把两套价格口径混在一个回调里。
"""

from __future__ import annotations

import time

from ..market.stocks import STOCK_POOL


class WatchlistService:
    def __init__(self, db):
        self.db = db

    # ── 校验 ──────────────────────────────────────────────
    def _validate(self, code: str) -> str | None:
        """返回错误提示；None 表示 code 合法。"""
        if not code or not isinstance(code, str):
            return "股票代码不能为空"
        code = code.strip()
        if code not in STOCK_POOL:
            return (
                f"股票 {code} 不在股票池里，用 /炒股 行情 看所有可交易代码。"
            )
        return None

    # ── 写 ────────────────────────────────────────────────
    async def add(self, user_id: str, code: str) -> tuple[bool, str]:
        err = self._validate(code)
        if err:
            return False, err
        code = code.strip()
        await self.db.watchlist_add(user_id, code, int(time.time()))
        return True, f"已将 {STOCK_POOL[code].name}({code}) 加入自选。"

    async def remove(self, user_id: str, code: str) -> tuple[bool, str]:
        err = self._validate(code)
        if err:
            return False, err
        code = code.strip()
        if not await self.db.watchlist_contains(user_id, code):
            return False, f"{code} 不在你的自选里，无需移除。"
        await self.db.watchlist_remove(user_id, code)
        return True, f"已将 {code} 从自选移除。"

    # ── 读 ────────────────────────────────────────────────
    async def list_codes(self, user_id: str) -> list[str]:
        rows = await self.db.watchlist_list(user_id)
        return [r["code"] for r in rows]

    async def _change_pct(self, code: str, price: float | None) -> float:
        """用 db 的昨收算涨跌幅；无昨收或价格缺失时返回 0.0。"""
        if price is None:
            return 0.0
        row = await self.db.get_price(code)
        prev = row["prev_close"] if row else None
        if not prev or prev <= 0:
            return 0.0
        return (price - prev) / prev * 100.0

    # ── 渲染 ──────────────────────────────────────────────
    async def render(self, user_id: str, price_of) -> str:
        codes = await self.list_codes(user_id)
        if not codes:
            return "你的自选还是空的，用 /炒股 自选加 <代码> 收藏。"

        lines = [f"⭐ 我的自选（{len(codes)} 只）"]
        # 总行数 ≤10：标题 1 行 + 最多 9 行正文（有溢出时末行改为提示行）
        shown = codes if len(codes) <= 9 else codes[:8]
        for code in shown:
            stock = STOCK_POOL[code]
            price = price_of(code)
            if price is None:
                lines.append(f"· {stock.name}({code}) 暂停交易")
                continue
            pct = await self._change_pct(code, price)
            sign = "+" if pct >= 0 else ""
            lines.append(
                f"· {stock.name}({code}) {price:.2f} {sign}{pct:.2f}%")
        extra = len(codes) - len(shown)
        if extra > 0:
            lines.append(f"　… 还有 {extra} 只，用 /炒股 自选 查看全部。")
        return "\n".join(lines)