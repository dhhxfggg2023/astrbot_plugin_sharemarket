"""SQLite 持久化层（aiosqlite）。P0 表：accounts / positions / orders / prices / meta。"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,
    user_name TEXT NOT NULL,
    cash_available REAL NOT NULL,
    cash_withdrawable REAL NOT NULL,
    cash_frozen REAL NOT NULL DEFAULT 0,        -- 挂单冻结
    initial_cash REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active / bankrupt / frozen
    rebirth_count INTEGER NOT NULL DEFAULT 0,
    last_rebirth_ts INTEGER,
    last_bankrupt_ts INTEGER,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    user_id TEXT NOT NULL,
    code TEXT NOT NULL,
    quantity INTEGER NOT NULL,       -- 总持仓
    sellable INTEGER NOT NULL,       -- 当前可卖（T+1 + 挂单冻结后）
    cost_price REAL NOT NULL,        -- 摊薄成本
    PRIMARY KEY (user_id, code)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,              -- buy / sell
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    filled INTEGER NOT NULL DEFAULT 0,
    frozen REAL NOT NULL DEFAULT 0,  -- 该买单当前冻结的资金
    status TEXT NOT NULL,            -- open/partial/filled/cancelled/expired
    phase TEXT NOT NULL DEFAULT 'continuous',  -- continuous / auction_open / auction_close
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_book ON orders(code, side, status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, status);

CREATE TABLE IF NOT EXISTS prices (
    code TEXT PRIMARY KEY,
    price REAL NOT NULL,             -- 最新价
    prev_close REAL NOT NULL,        -- 昨收
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    user_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,              -- buy / sell
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    fee REAL NOT NULL,               -- 该笔成交的费用
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, trade_id);
CREATE INDEX IF NOT EXISTS idx_trades_order ON trades(order_id);

CREATE TABLE IF NOT EXISTS candles (
    code TEXT NOT NULL,
    period TEXT NOT NULL,            -- 1d = 日K（分时只在内存里，日终落库）
    ts INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, period, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles ON candles(code, period, ts);

CREATE TABLE IF NOT EXISTS news (
    news_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    scope TEXT NOT NULL,             -- market / sector / stock
    target TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,              -- 利好 / 利空
    headline TEXT NOT NULL,
    impact REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);
"""

# 已存在的旧库升级（新库 SCHEMA 已含这些列，ALTER 会报 duplicate column，忽略）
MIGRATIONS = [
    "ALTER TABLE accounts ADD COLUMN cash_frozen REAL NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN frozen REAL NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN phase TEXT NOT NULL DEFAULT 'continuous'",
]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._in_txn = False

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                await self.conn.execute(m)
            except sqlite3.OperationalError:
                pass  # 列已存在
        await self._commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ── 事务边界 ───────────────────────────────────────────
    @asynccontextmanager
    async def transaction(self):
        """显式事务：块内所有写方法合并为一次提交，任一步异常整体回滚。

        用于"现金划转 + 账本/委托落盘"这类必须同生共死的多步写入
        （如融资还款、IPO 摇号结算）。嵌套复用外层事务，由最外层统一提交。
        """
        if self._in_txn:
            yield self
            return
        self._in_txn = True
        try:
            yield self
        except BaseException:
            await self.conn.rollback()
            raise
        else:
            # 直接提交连接：不能走 _commit()——此时 _in_txn 仍为 True，
            # 会让提交判断恒为 False，整个事务的写入永不落盘（已提交数据
            # 会在下一次失败事务的 rollback 或 close 时被整体丢弃）。
            await self.conn.commit()
        finally:
            self._in_txn = False

    async def _commit(self) -> None:
        """事务内不提交，由 transaction() 统一提交；事务外立即提交。"""
        if not self._in_txn:
            await self.conn.commit()

    # ── 账户 ──────────────────────────────────────────────
    async def get_account(self, user_id: str):
        cur = await self.conn.execute(
            "SELECT * FROM accounts WHERE user_id=?", (user_id,))
        return await cur.fetchone()

    async def create_account(self, user_id: str, user_name: str,
                             cash: float, ts: int) -> None:
        await self.conn.execute(
            "INSERT INTO accounts(user_id,user_name,cash_available,"
            "cash_withdrawable,cash_frozen,initial_cash,created_ts) "
            "VALUES(?,?,?,?,0,?,?)",
            (user_id, user_name, cash, cash, cash, ts))
        await self._commit()

    async def update_cash(self, user_id: str, available: float,
                          withdrawable: float) -> None:
        # C2 防线：负现金一律拒绝（NaN 比较为 False，会通过下方检查，
        # 故上层还需 isfinite 校验；此处只拦确定性的负值写入）。
        if available < 0 or withdrawable < 0:
            raise ValueError("cash cannot be negative")
        await self.conn.execute(
            "UPDATE accounts SET cash_available=?, cash_withdrawable=? "
            "WHERE user_id=?", (available, withdrawable, user_id))
        await self._commit()

    async def update_cash_full(self, user_id: str, available: float,
                               withdrawable: float, frozen: float) -> None:
        if available < 0 or withdrawable < 0 or frozen < 0:
            raise ValueError("cash cannot be negative")
        await self.conn.execute(
            "UPDATE accounts SET cash_available=?, cash_withdrawable=?, "
            "cash_frozen=? WHERE user_id=?",
            (round(available, 2), round(withdrawable, 2), round(frozen, 2),
             user_id))
        await self._commit()

    async def set_status(self, user_id: str, status: str,
                         ts: int | None = None) -> None:
        if status == "bankrupt":
            await self.conn.execute(
                "UPDATE accounts SET status=?, last_bankrupt_ts=? WHERE user_id=?",
                (status, ts, user_id))
        else:
            await self.conn.execute(
                "UPDATE accounts SET status=? WHERE user_id=?", (status, user_id))
        await self._commit()

    async def do_rebirth(self, user_id: str, cash: float, ts: int) -> None:
        await self.conn.execute(
            "UPDATE accounts SET status='active', cash_available=?, "
            "cash_withdrawable=?, cash_frozen=0, initial_cash=?, "
            "rebirth_count=rebirth_count+1, last_rebirth_ts=? WHERE user_id=?",
            (cash, cash, cash, ts, user_id))
        await self.conn.execute("DELETE FROM positions WHERE user_id=?", (user_id,))
        await self.conn.execute(
            "UPDATE orders SET status='cancelled', frozen=0 WHERE user_id=? AND "
            "status IN ('open','partial')", (user_id,))
        await self._commit()

    async def list_accounts(self):
        cur = await self.conn.execute("SELECT * FROM accounts")
        return await cur.fetchall()

    async def settle_withdrawable(self) -> None:
        """日终：可用资金全部转为可取。"""
        await self.conn.execute(
            "UPDATE accounts SET cash_withdrawable=cash_available")
        await self._commit()

    # ── 持仓 ──────────────────────────────────────────────
    async def get_position(self, user_id: str, code: str):
        cur = await self.conn.execute(
            "SELECT * FROM positions WHERE user_id=? AND code=?", (user_id, code))
        return await cur.fetchone()

    async def get_positions(self, user_id: str):
        cur = await self.conn.execute(
            "SELECT * FROM positions WHERE user_id=? AND quantity>0", (user_id,))
        return await cur.fetchall()

    async def upsert_position(self, user_id: str, code: str, quantity: int,
                              sellable: int, cost_price: float) -> None:
        await self.conn.execute(
            "INSERT INTO positions(user_id,code,quantity,sellable,cost_price) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,code) DO UPDATE SET "
            "quantity=?, sellable=?, cost_price=?",
            (user_id, code, quantity, sellable, cost_price,
             quantity, sellable, cost_price))
        await self._commit()

    async def delete_position(self, user_id: str, code: str) -> None:
        await self.conn.execute(
            "DELETE FROM positions WHERE user_id=? AND code=?", (user_id, code))
        await self._commit()

    async def get_position_holders(self, code: str):
        """持有某只股票的所有账户（分红送股/退市清算用）。"""
        cur = await self.conn.execute(
            "SELECT * FROM positions WHERE code=? AND quantity>0", (code,))
        return await cur.fetchall()

    async def unlock_all_sellable(self) -> None:
        """日终 T+1 解锁：所有持仓全部可卖。"""
        await self.conn.execute("UPDATE positions SET sellable=quantity")
        await self._commit()

    # ── 委托 ──────────────────────────────────────────────
    async def insert_order(self, user_id: str, code: str, side: str,
                           price: float, quantity: int, ts: int,
                           phase: str = "continuous") -> int:
        cur = await self.conn.execute(
            "INSERT INTO orders(user_id,code,side,price,quantity,status,phase,ts) "
            "VALUES(?,?,?,?,?,'open',?,?)",
            (user_id, code, side, price, quantity, phase, ts))
        await self._commit()
        return cur.lastrowid

    async def update_order(self, order_id: int, filled: int, status: str) -> None:
        await self.conn.execute(
            "UPDATE orders SET filled=?, status=? WHERE order_id=?",
            (filled, status, order_id))
        await self._commit()

    async def update_order_frozen(self, order_id: int, frozen: float) -> None:
        await self.conn.execute(
            "UPDATE orders SET frozen=? WHERE order_id=?",
            (round(max(frozen, 0.0), 2), order_id))
        await self._commit()

    async def get_order(self, order_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,))
        return await cur.fetchone()

    async def get_user_open_orders(self, user_id: str):
        cur = await self.conn.execute(
            "SELECT * FROM orders WHERE user_id=? AND status IN ('open','partial') "
            "ORDER BY order_id DESC", (user_id,))
        return await cur.fetchall()

    async def get_all_open_orders(self):
        cur = await self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('open','partial')")
        return await cur.fetchall()

    async def expire_open_orders(self) -> None:
        """日终：未成交委托全部过期（当日有效）。"""
        await self.conn.execute(
            "UPDATE orders SET status='expired', frozen=0 "
            "WHERE status IN ('open','partial')")
        await self._commit()

    async def get_book_orders(self, code: str, side: str):
        """取某股票某方向所有未成交玩家委托，用于重启恢复与盘口展示。"""
        cur = await self.conn.execute(
            "SELECT * FROM orders WHERE code=? AND side=? AND "
            "status IN ('open','partial')", (code, side))
        return await cur.fetchall()

    # ── 价格 ──────────────────────────────────────────────
    async def upsert_price(self, code: str, price: float, prev_close: float,
                           open_: float, high: float, low: float,
                           volume: int) -> None:
        await self.conn.execute(
            "INSERT INTO prices(code,price,prev_close,open,high,low,volume) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
            "price=?, prev_close=?, open=?, high=?, low=?, volume=?",
            (code, price, prev_close, open_, high, low, volume,
             price, prev_close, open_, high, low, volume))

    async def commit_prices(self) -> None:
        await self._commit()

    async def day_close_prices(self) -> None:
        """日终：收盘价转为昨收，重置日内数据。"""
        await self.conn.execute(
            "UPDATE prices SET prev_close=price, open=price, high=price, "
            "low=price, volume=0")
        await self._commit()

    async def get_price(self, code: str):
        cur = await self.conn.execute(
            "SELECT * FROM prices WHERE code=?", (code,))
        return await cur.fetchone()

    async def get_all_prices(self):
        cur = await self.conn.execute("SELECT * FROM prices")
        return await cur.fetchall()

    # ── 成交流水（对账用）────────────────────────────────
    async def insert_trade(self, order_id: int | None, user_id: str, code: str,
                           side: str, price: float, quantity: int, fee: float,
                           ts: int) -> None:
        await self.conn.execute(
            "INSERT INTO trades(order_id,user_id,code,side,price,quantity,fee,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (order_id, user_id, code, side, price, quantity, round(fee, 2), ts))
        await self._commit()

    async def get_user_trades(self, user_id: str, limit: int = 10):
        cur = await self.conn.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY trade_id DESC LIMIT ?",
            (user_id, limit))
        return await cur.fetchall()

    async def stats(self) -> dict:
        """运行统计（心跳 / 状态指令用）。用列别名取值，兼容不同驱动。"""
        async def one(sql: str) -> int:
            cur = await self.conn.execute(sql)
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

        return {
            "accounts": await one("SELECT COUNT(*) AS n FROM accounts"),
            "active": await one(
                "SELECT COUNT(*) AS n FROM accounts WHERE status='active'"),
            "bankrupt": await one(
                "SELECT COUNT(*) AS n FROM accounts WHERE status='bankrupt'"),
            "positions": await one("SELECT COUNT(*) AS n FROM positions"),
            "open_orders": await one(
                "SELECT COUNT(*) AS n FROM orders WHERE status IN ('open','partial')"),
            "trades": await one("SELECT COUNT(*) AS n FROM trades"),
            "frozen_total": await one(
                "SELECT COALESCE(SUM(cash_frozen),0) AS n FROM accounts"),
        }

    # ── K 线 ──────────────────────────────────────────────
    async def insert_candles(self, rows) -> None:
        """rows: (code, period, ts, open, high, low, close, volume) 列表。"""
        if not rows:
            return
        await self.conn.executemany(
            "INSERT INTO candles(code,period,ts,open,high,low,close,volume) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code,period,ts) DO NOTHING",
            rows)
        await self._commit()

    async def get_candles(self, code: str, period: str = "1d", limit: int = 30):
        """最近 limit 根 K 线（按时间升序返回）。"""
        cur = await self.conn.execute(
            "SELECT * FROM candles WHERE code=? AND period=? "
            "ORDER BY ts DESC LIMIT ?", (code, period, limit))
        rows = await cur.fetchall()
        return list(reversed(rows))

    # ── 新闻 ──────────────────────────────────────────────
    async def insert_news(self, ts: int, scope: str, target: str, kind: str,
                          headline: str, impact: float) -> None:
        await self.conn.execute(
            "INSERT INTO news(ts,scope,target,kind,headline,impact) "
            "VALUES(?,?,?,?,?,?)",
            (ts, scope, target, kind, headline, round(impact, 6)))
        await self._commit()

    async def get_news_since(self, ts: int, limit: int = 50):
        cur = await self.conn.execute(
            "SELECT * FROM news WHERE ts>=? ORDER BY news_id DESC LIMIT ?",
            (ts, limit))
        return await cur.fetchall()

    # ── 元数据 ────────────────────────────────────────────
    async def get_meta(self, key: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value))
        await self._commit()
