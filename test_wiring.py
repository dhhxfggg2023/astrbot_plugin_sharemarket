"""Quick wiring test - uses sim_test infrastructure but runs in workspace root where code_verify can write DB."""
import asyncio
import importlib
import os
import sys

# Add .dsh-code to path for sim_test
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dsh-code"))
import sim_test as _st  # noqa: E402 注入伪造 aiosqlite + 插件包导入

PKG = _st.PKG
Database, PriceEngine = _st.Database, _st.PriceEngine
AccountService = _st.AccountService
_i = importlib.import_module(f"{PKG}.game.ipo")
IPOService = _i.IPOService
_m = importlib.import_module(f"{PKG}.game.margin")
MarginService = _m.MarginService
_d = importlib.import_module(f"{PKG}.game.dayclose")
DayCloseService = _d.DayCloseService
_trade = importlib.import_module(f"{PKG}.game.trade")
TradeService = _trade.TradeService
_auction = importlib.import_module(f"{PKG}.game.auction")
AuctionService = _auction.AuctionService
_market_ob = importlib.import_module(f"{PKG}.market.orderbook")
Matcher = _market_ob.Matcher
_stocks = _st.STOCK_POOL

# Override for deterministic test
_m._cash_rate_daily = lambda: 0.001
_m._stock_rate_daily = lambda: 0.001

check = _st.check

async def main():
    # Per project ledger note: code_verify channel cannot create sqlite files
    # inside .dsh-code/ or plugin root dir; use temp dir at system temp instead.
    import tempfile
    DB_PATH = os.path.join(tempfile.gettempdir(), "test_wiring.db")
    try:
        os.remove(DB_PATH)
    except OSError:
        pass

    db = Database(DB_PATH)
    await db.open()
    engine = PriceEngine(seed=42)
    matcher = Matcher()
    acct = AccountService(db)
    trade = TradeService(db, engine, matcher)
    auction = AuctionService(db, engine, trade)
    trade.auction = auction
    ipo = IPOService(db, engine)
    margin = MarginService(db, engine, trade)
    dayclose = DayCloseService(db, engine, matcher, auction, None, None,
                               margin=margin, ipo=ipo)

    print("== 1. IPO restore on fresh DB (should be 0) ==")
    n = await ipo.restore()
    check("fresh restore = 0", n == 0)

    print("== 2. IPO publish + subscribe + draw + restore ==")
    await ipo.publish("600999", "测试新股", "主板", "周期", "测试", 10.00, 20000)
    ok, _ = await acct.open_account("u1", "玩家1")
    check("u1 open", ok)
    await ipo.subscribe("u1", "600999", 1)
    await ipo.draw("600999")

    # Restart with new engine
    engine2 = PriceEngine(seed=7)
    ipo2 = IPOService(db, engine2)
    n = await ipo2.restore()
    check("restore after draw = 1", n == 1)
    check("quote exists after restore", engine2.quotes.get("600999") is not None)
    check("pct_override=0.44 (main board day 1)", engine2.quotes["600999"].pct_override == 0.44)

    print("== 3. Margin accrue_day + force_liquidate flow ==")
    ok, _ = await acct.open_account("u2", "玩家2")
    check("u2 open", ok)
    await margin.borrow_cash("u2", 300_000)
    check("debt after borrow", await margin.debt("u2") == 300_000)
    stats = await margin.accrue_day(1)
    check("accrue_day day 1 charged", stats["charged"] == 1)
    check("accrue_day day 2 same day skipped", (await margin.accrue_day(1))["charged"] == 0)

    print("== 4. DayCloseService wiring: margin _check_margin_calls_nolock called ==")
    # Setup a margin call scenario
    await margin.borrow_cash("u2", 2_800_000)
    qty = int(3_100_000 / engine.get("600001").price // 100) * 100
    await db.upsert_position("u2", "600001", qty, 0, engine.get("600001").price)
    await db.update_cash_full("u2", 1_000_000, 0, 0)
    # crash price
    engine.quotes["600001"].price = round(engine.get("600001").price * 0.7, 2)
    # Run dayclose - should trigger margin calls
    await dayclose.run()
    check("u2 debt cleared after liquidation", await margin.debt("u2") == 0)

    print("== 5. DayCloseService without margin/ipo behaves same ==")
    dayclose2 = DayCloseService(db, engine, matcher, auction, None, None)
    await dayclose2.run()  # should not crash

    await db.close()
    try:
        os.remove(DB_PATH)
    except OSError:
        pass

    print(f"\n==== 结果: {_st.PASS} 通过, {_st.FAIL} 失败 ====")
    return _st.FAIL

if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)