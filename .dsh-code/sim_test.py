"""P0 核心逻辑离线模拟测试（不依赖 astrbot，用 sqlite3 伪造 aiosqlite 驱动 service 层）。

覆盖 DESIGN.md §18 关键边界：
1 开户唯一性 / 2 买入边界(整手/资金/代码) / 3 涨跌停钳制与越界拒单
4 做市商成交与费用 / 5 T+1 拦截 / 6 解锁后卖出与零股规则
7 供需冲击改价 / 8 玩家间撮合 / 9 撤单释放冻结 / 10 破产与重生
11 排行榜 / 12 日终清算(委托过期+T+1解锁+昨收更新)
"""
import asyncio
import importlib
import logging
import math
import os
import sqlite3
import sys
import time
import types

try:  # Windows 控制台默认 GBK，强制 UTF-8 输出避免 emoji/中文报错
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 用标准库 sqlite3 伪造 aiosqlite（本机未安装 aiosqlite）──────────
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))   # plugins 目录，使包可导入
PKG = os.path.basename(PLUGIN_DIR)

DB_PATH = os.path.join(PLUGIN_DIR, f".sim_test_{int(time.time() * 1000)}.db")


class _Row(dict):
    """模拟 aiosqlite.Row：支持 row["col"] 与 .get()。"""


def _wrap(r):
    return None if r is None else _Row(r)


class _FakeConn:
    def __init__(self, path):
        self._c = sqlite3.connect(path)
        self._c.row_factory = sqlite3.Row

    @property
    def row_factory(self):
        return sqlite3.Row

    @row_factory.setter
    def row_factory(self, _v):
        self._c.row_factory = sqlite3.Row

    async def executescript(self, s):
        self._c.executescript(s)

    async def execute(self, sql, params=()):
        cur = self._c.execute(sql, params)
        return _Cur(cur)

    async def executemany(self, sql, seq):
        # 与 aiosqlite 对齐：业务层用到的连接方法必须都在 shim 里实现
        cur = self._c.executemany(sql, seq)
        return _Cur(cur)

    async def commit(self):
        self._c.commit()

    async def rollback(self):
        self._c.rollback()

    async def close(self):
        self._c.close()


class _Cur:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = cur.lastrowid

    async def fetchone(self):
        return _wrap(self._cur.fetchone())

    async def fetchall(self):
        return [_wrap(r) for r in self._cur.fetchall()]


class _FakeAio(types.ModuleType):
    Row = sqlite3.Row
    OperationalError = sqlite3.OperationalError

    @staticmethod
    async def connect(path):
        return _FakeConn(path)


sys.modules["aiosqlite"] = _FakeAio("aiosqlite")

CFG = importlib.import_module(f"{PKG}.config").CFG
AccountService = importlib.import_module(f"{PKG}.game.account").AccountService
DayCloseService = importlib.import_module(f"{PKG}.game.dayclose").DayCloseService
LeaderboardService = importlib.import_module(f"{PKG}.game.leaderboard").LeaderboardService
_t = importlib.import_module(f"{PKG}.game.trade")
TradeError, TradeService = _t.TradeError, _t.TradeService
PriceEngine = importlib.import_module(f"{PKG}.market.engine").PriceEngine
Matcher = importlib.import_module(f"{PKG}.market.orderbook").Matcher
Database = importlib.import_module(f"{PKG}.storage.db").Database
_stocks_mod = importlib.import_module(f"{PKG}.market.stocks")
STOCK_POOL = _stocks_mod.STOCK_POOL
SECTORS, SECTOR_MEMBERS = _stocks_mod.SECTORS, _stocks_mod.SECTOR_MEMBERS
_ind = importlib.import_module(f"{PKG}.market.indicators")
ma, macd, rsi, sparkline = _ind.ma, _ind.macd, _ind.rsi, _ind.sparkline

# ── 日志捕获：验证关键路径真的埋了日志 ──────────────────────────
LOG_RECORDS: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOG_RECORDS.append(f"{record.levelname}|{record.name}|{record.getMessage()}")


_logmod = importlib.import_module(f"{PKG}.log")
_capture = _Capture()
logging.getLogger(_logmod.ROOT).addHandler(_capture)
logging.getLogger(_logmod.ROOT).setLevel(logging.DEBUG)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


async def main():
    db = Database(DB_PATH)
    await db.open()
    engine = PriceEngine(seed=42)
    matcher = Matcher()
    acct = AccountService(db)
    trade = TradeService(db, engine, matcher)
    board = LeaderboardService(db)
    dayclose = DayCloseService(db, engine, matcher)
    price_of = lambda c: engine.get(c).price if engine.get(c) else None  # noqa: E731
    # 第 1–17 节验证连续竞价；集合竞价在第 18 节单独开
    CFG.auction_enabled = False

    print("== 1. 开户 ==")
    ok, _ = await acct.open_account("u1", "玩家一")
    check("首次开户成功", ok)
    ok, _ = await acct.open_account("u1", "玩家一")
    check("重复开户被拒", not ok)
    await acct.open_account("u2", "玩家二")

    print("== 2. 买入边界 ==")
    for name, code, qty, price in [
        ("非100整数倍拒单", "600001", 150, 4.20),
        ("不存在代码拒单", "999999", 100, 1.00),
        ("资金不足拒单", "600001", 3_000_000, 4.20),
    ]:
        try:
            await trade.buy("u1", code, qty, price)
            check(name, False, "未抛错")
        except TradeError:
            check(name, True)

    print("== 3. 涨跌停 ==")
    q = engine.get("600001")
    s1 = STOCK_POOL["600001"]
    up, down = q.limit_up(s1), q.limit_down(s1)
    check("涨停价=4.62", abs(up - 4.62) < 1e-9, f"up={up}")
    check("跌停价=3.78", abs(down - 3.78) < 1e-9, f"down={down}")
    try:
        await trade.buy("u1", "600001", 100, 5.00)
        check("越涨停价拒单", False)
    except TradeError:
        check("越涨停价拒单", True)

    print("== 4. 正常买入（做市商对手）==")
    before = (await db.get_account("u1"))["cash_available"]
    msg = await trade.buy("u1", "600001", 1000, 4.30)
    acc1 = await db.get_account("u1")
    pos = await db.get_position("u1", "600001")
    check("买入成交", pos is not None and pos["quantity"] == 1000, msg)
    check("资金被扣", acc1["cash_available"] < before)
    check("全部成交无冻结", (acc1["cash_frozen"] or 0) == 0,
          f"frozen={acc1['cash_frozen']}")
    check("T+1 可卖=0", pos["sellable"] == 0, f"sellable={pos['sellable']}")
    print("   回执:", msg.replace("\n", " | "))

    print("== 5. T+1 拦截 ==")
    try:
        await trade.sell("u1", "600001", 1000, 4.30)
        check("当日买入当日卖被拒", False)
    except TradeError as e:
        check("当日买入当日卖被拒", "T+1" in str(e), str(e))

    print("== 6. 解锁后卖出与零股规则 ==")
    await db.upsert_position("u1", "600001", 1000, 1000, pos["cost_price"])
    try:
        await trade.sell("u1", "600001", 2000, None)
        check("超持仓卖出被拒", False)
    except TradeError:
        check("超持仓卖出被拒", True)
    try:
        await trade.sell("u1", "600001", 50, None)
        check("零股非清仓被拒", False)
    except TradeError:
        check("零股非清仓被拒", True)
    cash_before = (await db.get_account("u1"))["cash_available"]
    msg = await trade.sell("u1", "600001", 500, None)
    pos2 = await db.get_position("u1", "600001")
    cash_after = (await db.get_account("u1"))["cash_available"]
    check("部分卖出成交", pos2["quantity"] == 500, msg)
    check("未成交部分不冻结股份(全成)", pos2["sellable"] == 500,
          f"sellable={pos2['sellable']}")
    check("卖出到账", cash_after > cash_before)
    # 清仓剩余
    await trade.sell("u1", "600001", 500, None)
    check("清仓后持仓删除",
          await db.get_position("u1", "600001") is None)

    print("== 7. 供需冲击改价 ==")
    qq = engine.get("600001")
    p0 = qq.price
    for _ in range(30):
        engine.tick({"600001": 60000})
    p1 = engine.get("600001").price
    check("净买压推高价格", p1 >= p0, f"{p0}→{p1}")
    check("钳制在涨停内", p1 <= qq.limit_up(s1) + 1e-9,
          f"{p1} vs up {qq.limit_up(s1)}")

    print("== 8. 玩家间撮合 ==")
    q2 = engine.get("600002")
    rest_price = round(q2.price * 0.98, 2)
    u2_before = await db.get_account("u2")
    msg = await trade.buy("u2", "600002", 100, rest_price)
    u2_after = await db.get_account("u2")
    check("挂单冻结资金", u2_after["cash_frozen"] > 0,
          f"frozen={u2_after['cash_frozen']}")
    check("可用资金减少", u2_after["cash_available"] < u2_before["cash_available"])
    # u1 买入后解锁再卖，与 u2 的挂单撮合
    await trade.buy("u1", "600002", 100, None)
    p = await db.get_position("u1", "600002")
    await db.upsert_position("u1", "600002", p["quantity"], p["quantity"],
                             p["cost_price"])
    msg2 = await trade.sell("u1", "600002", 100, None)
    check("与对手玩家撮合", "对手玩家" in msg2, msg2.replace("\n", " | "))
    u2_pos = await db.get_position("u2", "600002")
    check("对手玩家收到股票", u2_pos is not None and u2_pos["quantity"] == 100,
          f"pos={dict(u2_pos) if u2_pos else None}")
    u2_after2 = await db.get_account("u2")
    check("冻结被成交消耗", u2_after2["cash_frozen"] == 0,
          f"frozen={u2_after2['cash_frozen']}")

    print("== 9. 撤单释放冻结 ==")
    q1 = engine.get("600001")
    far = round(q1.limit_down(s1) * 1.01, 2)
    frozen_before = (await db.get_account("u2"))["cash_frozen"] or 0
    await trade.buy("u2", "600001", 100, far)
    frozen_mid = (await db.get_account("u2"))["cash_frozen"] or 0
    check("挂单后冻结增加", frozen_mid > frozen_before, f"{frozen_before}→{frozen_mid}")
    oid = None
    for r in await db.get_user_open_orders("u2"):
        if abs(r["price"] - far) < 1e-9:
            oid = r["order_id"]
    check("委托可查", oid is not None)
    if oid:
        m = await trade.cancel("u2", oid)
        check("撤单成功", "已撤销" in m, m)
        frozen_after = (await db.get_account("u2"))["cash_frozen"] or 0
        check("撤单释放冻结", abs(frozen_after - frozen_before) < 1e-6,
              f"{frozen_after} vs {frozen_before}")

    print("== 10. 破产与重生 ==")
    for p in await db.get_positions("u2"):
        await db.delete_position("u2", p["code"])
    await db.update_cash_full("u2", 0.0, 0.0, 0.0)
    lv = await acct.bankruptcy_level("u2", price_of)
    check("L3 破产判定", lv == "L3", f"lv={lv}")
    check("破产标记", await acct.mark_bankrupt_if_l3("u2", price_of))
    ok, msg = await acct.rebirth("u2")
    check("冷却期内重生被拒", not ok and "冷却" in msg, msg)
    await db.set_status("u2", "bankrupt", int(time.time()) - 25 * 3600)
    ok, msg = await acct.rebirth("u2")
    check("冷却后重生成功(50%资金)", ok and "500,000" in msg, msg)
    acc2 = await db.get_account("u2")
    check("重生后状态 active 且冻结清零",
          acc2["status"] == "active" and (acc2["cash_frozen"] or 0) == 0)

    print("== 11. 排行榜 ==")
    entries = await board.top(price_of, 10)
    check("排行非空", len(entries) >= 2, f"n={len(entries)}")
    print(board.render(entries))

    print("== 12. 日终清算 ==")
    await trade.buy("u1", "600003", 100, None)
    p3 = await db.get_position("u1", "600003")
    check("买入后不可卖", p3["sellable"] == 0)
    # 挂一笔必然过期的委托
    await trade.buy("u2", "600001", 100, far)
    check("日终前有未成交委托",
          len(await db.get_user_open_orders("u2")) > 0)
    # 落盘价格（模拟主循环每个 tick 做的事）
    for row in engine.snapshot_rows():
        await db.upsert_price(*row)
    await db.commit_prices()
    n = await dayclose.run()
    check("清算处理了委托", n >= 1, f"n={n}")
    check("委托全部过期",
          len(await db.get_user_open_orders("u2")) == 0)
    check("冻结全部释放",
          ((await db.get_account("u2"))["cash_frozen"] or 0) == 0)
    p3b = await db.get_position("u1", "600003")
    check("T+1 解锁全部可卖", p3b["sellable"] == p3b["quantity"],
          f"{p3b['sellable']}/{p3b['quantity']}")
    check("昨收更新为收盘价",
          abs(engine.get("600003").prev_close - engine.get("600003").price) < 1e-9)
    rowp = await db.get_price("600003")
    check("数据库昨收已更新", abs(rowp["prev_close"] - rowp["price"]) < 1e-9)

    print("== 13. 日志埋点与成交流水 ==")
    check("开户有日志", any("开户" in r for r in LOG_RECORDS),
          f"共 {len(LOG_RECORDS)} 条日志")
    check("成交有日志", any("成交" in r for r in LOG_RECORDS))
    check("撤单有日志", any("撤单" in r for r in LOG_RECORDS))
    check("日终清算有日志", any("日终清算" in r for r in LOG_RECORDS))
    check("破产有 WARNING 日志",
          any(r.startswith("WARNING") and "破产" in r for r in LOG_RECORDS))
    check("重生有日志", any("重生" in r for r in LOG_RECORDS))
    tr1 = await db.get_user_trades("u1", 50)
    tr2 = await db.get_user_trades("u2", 50)
    check("成交流水有记录", len(tr1) + len(tr2) > 0,
          f"u1={len(tr1)} u2={len(tr2)}")
    fees = [r["fee"] for r in tr1 if r["side"] == "buy" and r["code"] == "600001"]
    check("买入流水费用=最低佣金5.04", any(abs(f - 5.04) < 0.01 for f in fees),
          str(fees))
    st = await db.stats()
    need = {"accounts", "active", "bankrupt", "positions", "open_orders",
            "trades", "frozen_total"}
    check("stats 字段完整", need <= set(st), str(sorted(st)))
    check("stats 成交数与流水表一致", st["trades"] == len(tr1) + len(tr2),
          f"stats={st['trades']} 表={(len(tr1) + len(tr2))}")
    check("stats 冻结合计归零", st["frozen_total"] == 0, str(st["frozen_total"]))
    check("暂停开关默认关闭", engine.paused is False)

    print("== 14. 板块联动 ==")
    check("板块数为 10", len(SECTORS) == 10, str(SECTORS))
    check("每只股票板块已登记",
          all(s.sector in SECTORS for s in STOCK_POOL.values()),
          str([s.code for s in STOCK_POOL.values() if s.sector not in SECTORS]))
    thin = {k: len(v) for k, v in SECTOR_MEMBERS.items() if len(v) < 5}
    check("每板块成员 ≥5 只", not thin, str(thin))
    check("板块成员覆盖全部股票",
          sum(len(v) for v in SECTOR_MEMBERS.values()) == len(STOCK_POOL))

    eng2 = PriceEngine(seed=99)
    prices = {c: [] for c in STOCK_POOL}
    for _ in range(300):
        eng2.tick({})
        for c, q in eng2.quotes.items():
            prices[c].append(q.price)

    def tick_rets(c):
        p = prices[c]
        return [(p[i] - p[i - 1]) / p[i - 1] for i in range(1, len(p)) if p[i - 1]]

    # 单 tick 收益被 0.01 价格网格的量化噪声主导（每 tick 理论波动不足一格），
    # 衡量板块联动必须看多 tick 聚合收益，否则量化噪声把相关性稀释掉
    BLOCK = 25

    def block_rets(c):
        p = prices[c]
        return [(p[i] - p[i - BLOCK]) / p[i - BLOCK]
                for i in range(BLOCK, len(p), BLOCK) if p[i - BLOCK]]

    def corr(a, b):
        n = min(len(a), len(b))
        if n < 10:
            return 0.0
        a, b = a[:n], b[:n]
        ma, mb = sum(a) / n, sum(b) / n
        sa = math.sqrt(sum((x - ma) ** 2 for x in a))
        sb = math.sqrt(sum((x - mb) ** 2 for x in b))
        if sa == 0 or sb == 0:
            return 0.0
        return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)

    same, cross = [], []
    codes = list(STOCK_POOL)
    for i, c1 in enumerate(codes):
        for c2 in codes[i + 1:]:
            r = corr(block_rets(c1), block_rets(c2))
            if STOCK_POOL[c1].sector == STOCK_POOL[c2].sector:
                same.append(r)
            else:
                cross.append(r)
    avg_same = sum(same) / len(same)
    avg_cross = sum(cross) / len(cross)
    check("同板块相关性显著高于跨板块",
          avg_same > avg_cross + 0.15,
          f"同板块 {avg_same:.3f} vs 跨板块 {avg_cross:.3f}")
    print(f"   同板块 {len(same)} 对 平均相关 {avg_same:+.3f}；"
          f"跨板块 {len(cross)} 对 平均相关 {avg_cross:+.3f}")

    # 价格必须始终落在涨跌停区间内
    bad = []
    for c, q in eng2.quotes.items():
        s = STOCK_POOL[c]
        if not (q.limit_down(s) - 1e-9 <= q.price <= q.limit_up(s) + 1e-9):
            bad.append((c, q.price, q.limit_down(s), q.limit_up(s)))
    check("全部价格在涨跌停区间内", not bad, str(bad[:3]))
    moved = sum(1 for c in STOCK_POOL if prices[c][0] != prices[c][-1])
    check("300 tick 后多数股票价格发生变化", moved > len(STOCK_POOL) // 2,
          f"变动 {moved}/{len(STOCK_POOL)}")

    print("== 15. 凶险度与随机性 ==")
    eng3 = PriceEngine(seed=2024)
    pr = {c: [] for c in STOCK_POOL}
    for _ in range(3000):
        eng3.tick({})
        for c, q in eng3.quotes.items():
            pr[c].append(q.price)

    def blocks(c, n=25):
        p = pr[c]
        return [(p[i] - p[i - n]) / p[i - n] for i in range(n, len(p), n) if p[i - n]]

    allb = [r for c in STOCK_POOL for r in blocks(c)]
    mm = sum(allb) / len(allb)
    vv = sum((x - mm) ** 2 for x in allb) / len(allb)
    kurt = sum((x - mm) ** 4 for x in allb) / len(allb) / (vv * vv) if vv > 0 else 0
    check("收益分布肥尾（峰度>3）", kurt > 3.0, f"峰度={kurt:.2f}")
    worst, best = min(allb), max(allb)
    check("存在极端跌幅（<-5%）", worst < -0.05, f"最差={worst * 100:.1f}%")
    check("存在极端涨幅（>5%）", best > 0.05, f"最好={best * 100:.1f}%")
    check("情绪指数在 0..100 之间", 0 <= eng3.sentiment <= 100,
          f"{eng3.sentiment:.1f}")
    s_before = eng3.sentiment
    for _ in range(400):
        eng3.tick({})
    check("情绪指数会随行情变化", abs(eng3.sentiment - s_before) > 0.5,
          f"{s_before:.1f}→{eng3.sentiment:.1f}")
    states = {eng3.main_force_state(c) for c in STOCK_POOL}
    check("主力状态机在运转（不止观望）", any(s != "观望" for s in states),
          str(states))
    flows = [eng3.fund_flow(c) for c in STOCK_POOL]
    check("主力净流入有正有负",
          any(f > 0 for f in flows) and any(f < 0 for f in flows),
          f"正 {sum(1 for f in flows if f > 0)} / 负 {sum(1 for f in flows if f < 0)}")
    lu, ld = eng3.limit_counts()
    check("涨跌停家数接口正常", lu >= 0 and ld >= 0, f"涨停{lu}/跌停{ld}")

    print("== 16. 排行榜显示重生次数 ==")
    await db.set_status("u2", "bankrupt", int(time.time()) - 25 * 3600)
    ok, msg = await acct.rebirth("u2")
    check("二次重生成功", ok, msg)
    entries = await board.top(price_of, 10)
    check("排行条目带 rebirth 字段", all("rebirth" in e for e in entries))
    rendered = board.render(entries)
    check("排行榜显示重生次数", "重生" in rendered,
          rendered.replace("\n", " | ")[:140])

    print("== 17. K 线与技术指标 ==")
    eng4 = PriceEngine(seed=555)
    for _ in range(50):
        eng4.tick({})
    bars = eng4.intraday_bars("600001", 0)
    expect = max(50 // max(eng4.ticks_per_bar, 1), 1)
    check("分时 K 线已生成", len(bars) >= max(expect - 1, 1),
          f"{len(bars)} 根（期望≈{expect}，每根 {eng4.ticks_per_bar} tick）")
    ok_ohlc = all(b.low <= min(b.open, b.close) + 1e-9
                  and max(b.open, b.close) <= b.high + 1e-9
                  and b.volume >= 0 for b in bars)
    check("每根 K 线 OHLC 自洽", ok_ohlc)
    q1 = eng4.get("600001")
    check("最后一根收盘价=最新价", abs(bars[-1].close - q1.price) < 1e-9,
          f"{bars[-1].close} vs {q1.price}")
    check("分时区间落在当日高低之间",
          bars[-1].high <= q1.high + 1e-9 and bars[-1].low >= q1.low - 1e-9)

    rows = await db.get_candles("600001", "1d", 10)
    check("日 K 已落库（第 12 节日终清算写入）", len(rows) >= 1,
          f"{len(rows)} 根")
    if rows:
        r = rows[-1]
        check("日 K OHLC 自洽",
              r["low"] <= min(r["open"], r["close"]) + 1e-9
              and max(r["open"], r["close"]) <= r["high"] + 1e-9,
              f"O{r['open']} H{r['high']} L{r['low']} C{r['close']}")
        check("日 K 成交量非负", r["volume"] >= 0)
        check("日 K period 字段正确", r["period"] == "1d")

    check("MA 计算正确", abs(ma([1, 2, 3, 4, 5], 5) - 3.0) < 1e-9)
    check("MA 样本不足返回 None", ma([1, 2], 5) is None)
    check("RSI 单边上涨=100",
          abs(rsi(list(range(1, 16)), 14) - 100.0) < 1e-9)
    check("RSI 样本不足返回 None", rsi([1, 2, 3], 14) is None)
    check("MACD 样本不足返回 None", macd([1.0] * 10)[0] is None)
    check("sparkline 长度正确", len(sparkline([1, 2, 3, 4])) == 4)
    check("sparkline 空输入安全", sparkline([]) == "")

    _chart = importlib.import_module(f"{PKG}.render.chart")
    check("图表模块 available() 返回布尔", isinstance(_chart.available(), bool))
    check("图表空数据返回 None（不回退成空图）",
          _chart.render_kline(_chart.Path("_x.png"), "t", []) is None and
          _chart.render_intraday(_chart.Path("_x.png"), "t", []) is None)
    if _chart.available():
        _p = _chart.render_intraday(
            _chart.Path(os.path.join(PLUGIN_DIR, f"_t_chart_{int(time.time())}.png")),
            "自检", eng4.intraday_bars("600001", 30))
        check("分时图能生成且非空",
              _p is not None and os.path.getsize(_p) > 3000,
              str(_p))
        try:
            os.remove(_p)
        except OSError:
            pass
    else:
        print("  SKIP 分时图生成（本解释器没有 Pillow；用 bundled python 跑 "
              ".dsh-code/chart_check.py 验证）")

    print("== 18. 集合竞价 ==")
    AuctionBook = importlib.import_module(f"{PKG}.market.auction").AuctionBook
    AuctionService = importlib.import_module(f"{PKG}.game.auction").AuctionService
    calendars = importlib.import_module(f"{PKG}.market.calendar")

    # 18.1 纯逻辑：最大成交量定价
    bk = AuctionBook("600001")
    bk.add(1, "u1", "buy", 4.30, 1000)
    bk.add(2, "u2", "buy", 4.10, 500)
    bk.add(3, "u3", "sell", 4.05, 800)
    bk.add(4, "u4", "sell", 4.20, 400)
    price, vol = bk.clearing(4.15)
    check("清算价取最大可成交量价位", vol == 1000, f"price={price} vol={vol}")
    check("清算价落在合理区间", 4.05 <= price <= 4.30, f"{price}")
    price2, fills = bk.match(4.15)
    buy_fills = sum(q for o, q in fills if o.side == "buy")
    sell_fills = sum(q for o, q in fills if o.side == "sell")
    check("买卖两方成交量相等", buy_fills == sell_fills == 1000,
          f"buy={buy_fills} sell={sell_fills}")
    check("成交价唯一（等于清算价）", abs(price2 - price) < 1e-9)

    # 18.2 阶段机与撤单窗口
    CFG.auction_enabled = True
    calendars.SESSION.mode = "247"
    calendars.SESSION.reset(time.time())
    check("虚拟交易日开头=开盘集合竞价",
          calendars.SESSION.phase() == "auction_open",
          calendars.SESSION.phase())
    check("开盘竞价前段可撤单", calendars.SESSION.cancellable() is True)
    calendars.SESSION.reset(time.time() - CFG.auction_seconds * 0.8)
    check("开盘竞价后段不可撤单", calendars.SESSION.cancellable() is False)
    calendars.SESSION.reset(time.time() - CFG.auction_seconds - 1)
    check("竞价时长用尽→连续竞价",
          calendars.SESSION.phase() == "continuous",
          calendars.SESSION.phase())
    calendars.SESSION.reset(time.time() - (CFG.day_seconds
                                           - CFG.closing_auction_seconds / 2))
    check("日终前=收盘集合竞价",
          calendars.SESSION.phase() == "auction_close",
          calendars.SESSION.phase())
    check("收盘竞价不可撤单", calendars.SESSION.cancellable() is False)

    # 18.3 端到端：竞价下单 → 清算 → 入账 → 剩余转连续竞价
    auc = AuctionService(db, engine, trade)
    trade.auction = auc
    dayclose.auction = auc
    # 先用连续竞价建仓（竞价阶段禁止市价单，所以这段必须在进入竞价之前做）
    CFG.auction_enabled = False
    await trade.buy("u2", "600002", 300, None)
    p2 = await db.get_position("u2", "600002")
    await db.upsert_position("u2", "600002", p2["quantity"],
                             p2["quantity"], p2["cost_price"])
    CFG.auction_enabled = True
    calendars.SESSION.reset(time.time())        # 进入开盘集合竞价
    q2 = engine.get("600002")
    cash_u1 = (await db.get_account("u1"))["cash_available"]
    msg = await trade.buy("u1", "600002", 200, round(q2.price * 1.02, 2))
    check("竞价买入进入竞价簿（未立即成交）",
          "竞价" in msg and auc.pending_count() >= 1,
          msg.replace("\n", " | ")[:90])
    check("竞价买单冻结资金",
          ((await db.get_account("u1"))["cash_frozen"] or 0) > 0)
    await trade.sell("u2", "600002", 200, round(q2.price * 0.98, 2))
    p2b = await db.get_position("u2", "600002")
    check("竞价卖单冻结股份（sellable 预扣）",
          p2b["sellable"] == p2b["quantity"] - 200,
          f"{p2b['sellable']}/{p2b['quantity']}")
    fills_n, shares, amount = await auc.run("开盘集合竞价")
    check("竞价清算产生成交", fills_n >= 1 and shares == 200,
          f"笔={fills_n} 股={shares} 额={amount:.2f}")
    p1 = await db.get_position("u1", "600002")
    p2c = await db.get_position("u2", "600002")
    check("买方收到股票", p1 is not None and p1["quantity"] >= 200,
          f"u1={dict(p1) if p1 else None}")
    check("卖方减少持仓", p2c is not None and p2c["quantity"] == 100,
          f"u2={dict(p2c) if p2c else None}")
    check("竞价成交后冻结清零",
          ((await db.get_account("u1"))["cash_frozen"] or 0) == 0
          and ((await db.get_account("u2"))["cash_frozen"] or 0) == 0)
    check("竞价成交有流水",
          any(r["code"] == "600002" for r in await db.get_user_trades("u1", 20)))
    # 未成交部分转入连续竞价
    await trade.buy("u1", "600001", 100, round(engine.get("600001").limit_down(
        STOCK_POOL["600001"]) * 1.005, 2))
    check("竞价挂单已入簿", auc.pending_count() >= 1)
    await auc.run("开盘集合竞价")
    book = trade.matcher.book("600001")
    check("未成交竞价单转入连续竞价订单簿",
          any(o.user_id == "u1" for o in book.bids),
          f"bids={[(o.user_id, o.price, o.remaining) for o in book.bids]}")
    CFG.auction_enabled = False                 # 复原，避免影响后续收尾
    calendars.SESSION.reset()

    print(f"\n==== 结果: {PASS} 通过, {FAIL} 失败 ====")
    await db.close()
    try:  # 清理测试库（沙箱下删除可能被拒，忽略）
        os.remove(DB_PATH)
    except OSError:
        print(f"（测试库保留：{os.path.basename(DB_PATH)}）")
    return FAIL


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
