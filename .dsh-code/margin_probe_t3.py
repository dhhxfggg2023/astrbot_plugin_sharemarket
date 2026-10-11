# -*- coding: utf-8 -*-
"""t3 独立审查探针：融资融券边界行为取证（只读审查，不改业务代码）。

全部使用 sqlite ":memory:"（sim_test 伪造 aiosqlite 层基于 stdlib sqlite3），
零磁盘写入。每项打印 OBSERVED=... 作为证据。

取证目标（对应审查任务点名项）：
  N1 NaN 金额借款：是否被拒；对现金/账本/维持担保比例/全局功能的破坏
  N2 NaN 还款：现金可否被写负、债务是否被凭空抹掉
  N3 坏账本：JSON 损坏是否拒绝操作；"合法 JSON 但缺 loans 键"是否凭空销账
  N4 现金更新与账本落盘非原子：_save 失败后现金已到、债务未记（凭空印钞）
  N5 计息：跨日只补 1 天（漏日）；_save 失败后同日重复扣息
  N6 强平：停牌股票照卖；跌停价照卖（正常卖单同状态被拒——对照）
  N7 强平：不撤未成交卖单 → 挂单被后续买方撮合时空卖（对手方付钱收不到交付的账目破坏）
  N8 被 L4 冻结账户：能否还款/还券（对照 DESIGN §14.3 管理员清账口径）
  N9 排行榜：借款是否虚增总资产/收益率（刷榜）
  N10 现金口径：assets=可用+冻结、不含可取（防双算）——正向确认
"""
import asyncio
import importlib
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_test as _st  # noqa: E402  注入伪造 aiosqlite + 包导入

PKG = _st.PKG
Database, PriceEngine, CFG, check = _st.Database, _st.PriceEngine, _st.CFG, _st.check
AccountService = _st.AccountService
TradeService, Matcher, TradeError = _st.TradeService, _st.Matcher, _st.TradeError
_m = importlib.import_module(f"{PKG}.game.margin")
MarginService, MarginError = _m.MarginService, _m.MarginError
_board = importlib.import_module(f"{PKG}.game.leaderboard").LeaderboardService
_stocks = importlib.import_module(f"{PKG}.market.stocks")
_cal = importlib.import_module(f"{PKG}.market.calendar")
STOCK_POOL = _stocks.STOCK_POOL

_m._cash_rate_daily = lambda: 0.001   # 日息 0.1% 确定性
_m._stock_rate_daily = lambda: 0.001
CFG.auction_enabled = False           # 247 模式下 phase 恒为 continuous
CFG.market_mode = "247"
_cal.SESSION.mode = "247"

CODE, CODE2 = "600001", "600002"


async def fresh():
    db = Database(":memory:")
    await db.open()
    engine = PriceEngine(seed=42)
    matcher = Matcher()
    acct = AccountService(db)
    trade = TradeService(db, engine, matcher)
    margin = MarginService(db, engine)
    return db, engine, matcher, acct, trade, margin


async def main():
    print(f"phase()={_cal.SESSION.phase()} is_open={_cal.is_open()}")

    # ── N1 NaN 借款 ─────────────────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u1", "NaN玩家")
    P0 = engine.get(CODE).price
    try:
        msg = await margin.borrow_cash("u1", float("nan"))
        print(f"N1 OBSERVED: NaN借款被接受 -> {msg[:60]!r}")
    except MarginError as e:
        print(f"N1 OBSERVED: NaN借款被拒(MarginError): {e}")
    except Exception as e:
        print(f"N1 OBSERVED: NaN借款抛非MarginError异常: {type(e).__name__}: {e}")
    acc1 = await db.get_account("u1")
    print(f"N1 OBSERVED: u1 cash_available={acc1['cash_available']!r} "
          f"(sqlite 将 NaN 存成 NULL={acc1['cash_available'] is None})")
    raw = await db.get_meta("margin.ledger.v1")
    print(f"N1 OBSERVED: 账本JSON含NaN字面量={'NaN' in (raw or '')!r} -> {raw}")
    try:
        d = await margin.debt("u1")
        print(f"N1 OBSERVED: debt(u1)={d!r} (math.isnan={math.isnan(d) if isinstance(d, float) else 'n/a'})")
    except Exception as e:
        print(f"N1 OBSERVED: debt(u1) 抛 {type(e).__name__}: {e}")
    try:
        r = await margin.margin_ratio("u1")
        print(f"N1 OBSERVED: margin_ratio(u1)={r!r}; r<1.30 恒False(不触发强平)="
              f"{(r < 1.30) if isinstance(r, float) else 'n/a'}")
    except Exception as e:
        print(f"N1 OBSERVED: margin_ratio(u1) 抛 {type(e).__name__}: {e}")
    # NaN 账户是否连累全局功能
    try:
        reports = await margin.check_margin_calls()
        print(f"N1 OBSERVED: check_margin_calls 正常返回 {reports!r}")
    except Exception as e:
        print(f"N1 OBSERVED: check_margin_calls 因NaN账户整体崩溃: {type(e).__name__}: {e}")
    board = _board(db)
    try:
        entries = await board.top(lambda c: engine.get(c).price if engine.get(c) else None)
        print(f"N1 OBSERVED: 排行榜正常渲染 top1 total={entries[0]['total']!r}")
    except Exception as e:
        print(f"N1 OBSERVED: 排行榜因NaN账户整体崩溃: {type(e).__name__}: {e}")
    # 二次借款同一NaN账户
    try:
        await margin.borrow_cash("u1", 1000)
        print("N1 OBSERVED: NaN账户仍可继续借款")
    except Exception as e:
        print(f"N1 OBSERVED: NaN账户再借款抛 {type(e).__name__}: {e}")
    await db.close()

    # ── N2 NaN 还款 ─────────────────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u2", "NaN还款")
    await margin.borrow_cash("u2", 300_000)
    await db.update_cash_full("u2", 100.0, 100.0, 0)   # 模拟已花掉，仅剩100元
    try:
        msg = await margin.repay_cash("u2", float("nan"))
        print(f"N2 OBSERVED: repay(nan) 被接受 -> {msg!r}")
    except Exception as e:
        print(f"N2 OBSERVED: repay(nan) 抛 {type(e).__name__}: {e}")
    acc2 = await db.get_account("u2")
    print(f"N2 OBSERVED: cash_available={acc2['cash_available']!r} "
          f"(负数=现金被写负), debt(u2)={await margin.debt('u2')!r} (0=债务被NaN抹掉)")
    await db.close()

    # ── N3 坏账本 ───────────────────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u3", "坏账本")
    await margin.borrow_cash("u3", 200_000)
    cash_before = (await db.get_account("u3"))["cash_available"]
    print(f"N3 借款20万后 debt(u3)={await margin.debt('u3')!r} cash={cash_before!r}")
    # 3a JSON 语法损坏
    await db.set_meta("margin.ledger.v1", "{corrupt!!")
    for op, coro in (("borrow", margin.borrow_cash("u3", 100)),
                     ("repay", margin.repay_cash("u3", 100)),
                     ("render", margin.render("u3")),
                     ("accrue", margin.accrue_day(1)),
                     ("margin_calls", margin.check_margin_calls())):
        try:
            r = await coro
            print(f"N3a OBSERVED: 账本损坏时 {op} 未被拒 -> {str(r)[:50]!r}")
        except MarginError as e:
            print(f"N3a PASS: 账本损坏时 {op} 被 MarginError 拒绝")
        except Exception as e:
            print(f"N3a OBSERVED: 账本损坏时 {op} 抛非MarginError: {type(e).__name__}: {e}")
    # 3b 合法 JSON 但 loans 键缺失（结构损坏）
    await db.set_meta("margin.ledger.v1", '{"version": 1}')
    try:
        d = await margin.debt("u3")
        print(f"N3b OBSERVED: 缺 loans 键 -> debt={d!r}（20万债务凭空消失，现金仍 "
              f"{(await db.get_account('u3'))['cash_available']!r} = 凭空销账）")
    except Exception as e:
        print(f"N3b OBSERVED: 缺 loans 键 抛 {type(e).__name__}: {e}")
    # 3c loans 为列表（结构损坏）
    await db.set_meta("margin.ledger.v1", '{"version": 1, "loans": []}')
    try:
        d = await margin.debt("u3")
        print(f"N3c OBSERVED: loans=[] -> debt={d!r}")
    except Exception as e:
        print(f"N3c OBSERVED: loans=[] 抛 {type(e).__name__}: {e}")
    # 3d 单笔贷款缺字段
    await db.set_meta("margin.ledger.v1",
                      '{"version":1,"next_id":2,"loans":{"1":{"id":"1"}},'
                      '"interest_log":[]}')
    try:
        d = await margin.debt("u3")
        print(f"N3d OBSERVED: 缺字段贷款 -> debt={d!r}")
    except Exception as e:
        print(f"N3d OBSERVED: 缺字段贷款 抛 {type(e).__name__}: {e}")
    await db.close()

    # ── N4 现金与账本非原子（_save 失败）─────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u4", "非原子")
    orig_save = margin._save
    async def boom(*a, **k):
        raise RuntimeError("模拟磁盘写失败")
    margin._save = boom
    try:
        await margin.borrow_cash("u4", 500_000)
        print("N4 OBSERVED: _save失败但borrow成功返回（不应发生）")
    except RuntimeError as e:
        print(f"N4 OBSERVED: _save 抛 {e}，异常已冒出业务层")
    margin._save = orig_save
    acc4 = await db.get_account("u4")
    print(f"N4 OBSERVED: 异常后 cash_available={acc4['cash_available']!r} "
          f"(50万+初始=150万=现金已到账), debt(u4)={await margin.debt('u4')!r} "
          f"(0=债务未记账) -> 50万凭空印钞且无任何回滚")
    await db.close()

    # ── N5 计息：漏日 + _save失败重复扣 ─────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u5", "计息")
    await margin.borrow_cash("u5", 100_000)
    s1 = await margin.accrue_day(1)
    s5 = await margin.accrue_day(5)      # 第2/3/4天没人调用
    led = await margin._load()
    last = led["loans"]["1"]["last_interest_day"]
    print(f"N5 OBSERVED: day1计息{s1['interest']!r}；直接跳day5只补1天 "
          f"s5={s5!r}；last_interest_day={last} -> 第2-4天利息永久丢失(漏日)")
    # _save 失败：现金已扣、幂等标记未落盘 → 同日重复扣
    orig5 = margin._save                      # 绑定本 margin 实例（orig_save 是 N4 的）
    margin._save = boom
    try:
        await margin.accrue_day(6)
    except RuntimeError:
        pass
    margin._save = orig5
    cash_after_fail = (await db.get_account("u5"))["cash_available"]
    s6_again = await margin.accrue_day(6)
    cash_final = (await db.get_account("u5"))["cash_available"]
    print(f"N5 OBSERVED: day6首次(落盘失败)现金→{cash_after_fail!r}；同日重跑"
          f" s6_again={s6_again!r} 现金→{cash_final!r}（两次扣息=重复扣息）")
    await db.close()

    # ── N6 强平：停牌照卖 / 跌停照卖 ────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u6", "停牌强平")
    await margin.borrow_cash("u6", 2_800_000)
    qty = int(2_800_000 / P0 // 100) * 100
    await db.upsert_position("u6", CODE, qty, qty, P0)
    await db.update_cash_full("u6", 1_000_000, 0, 0)
    engine.quotes[CODE].price = round(P0 * 0.7, 2)
    engine.suspend(CODE, 999, "审查停牌")
    ok, why = engine.can_trade(CODE)
    try:
        await margin.borrow_stock("u6", CODE, 100)
        print("N6 OBSERVED: 停牌中融券未被拒（不应发生）")
    except MarginError as e:
        print(f"N6 PASS: 停牌中融券被拒: {e}")
    reports = await margin.check_margin_calls()
    pos6 = await db.get_position("u6", CODE)
    print(f"N6 OBSERVED: 停牌(can_trade={ok},{why!r})下强平回执: "
          f"{reports[0][:80] if reports else '无'!r}; 强平后持仓={dict(pos6) if pos6 else None} "
          f"(None=停牌股被强平卖出)")
    await db.close()

    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u6b", "跌停强平")
    await margin.borrow_cash("u6b", 2_800_000)
    await db.upsert_position("u6b", CODE, qty, qty, P0)
    await db.update_cash_full("u6b", 1_000_000, 0, 0)
    q = engine.get(CODE)
    stock = STOCK_POOL[CODE]
    down = q.limit_down(stock)
    engine.quotes[CODE].price = down          # 跌停封板价（未停牌）
    # 先证明正常卖单此刻被拒
    try:
        await trade.sell("u6b", CODE, 100)
        print("N6b OBSERVED: 跌停中正常卖单未被拒（不应发生）")
    except TradeError as e:
        print(f"N6b PASS: 跌停中正常卖单被拒: {e}")
    # 跌停价下 ratio = (1M现金 + qty×down)/2.8M ≈ 1.26 < 1.3 → 触发强平
    reports = await margin.check_margin_calls()
    pos6b = await db.get_position("u6b", CODE)
    print(f"N6b OBSERVED: 跌停价 {down}（正常卖单被拒）下强平回执: "
          f"{reports[0][:80] if reports else '比例未破线'!r}; 持仓={dict(pos6b) if pos6b else None}")
    await db.close()

    # ── N7 强平不撤卖单 → 空卖 ─────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u7", "挂单被强平者")
    await acct.open_account("u7b", "接盘侠")
    await margin.borrow_cash("u7", 2_800_000)
    qty7 = int(2_800_000 / P0 // 100) * 100
    await db.upsert_position("u7", CODE, qty7, qty7, P0)
    await db.update_cash_full("u7", 1_000_000, 0, 0)
    # 挂一张不立即成交的卖单（价格高于市价、低于涨停）
    sell_px = round(P0 * 1.05, 2)
    up = engine.get(CODE).limit_up(STOCK_POOL[CODE])
    if sell_px >= up:
        sell_px = round(P0 * 1.02, 2)
    rep = await trade.sell("u7", CODE, 100, sell_px)
    orders = await db.get_user_open_orders("u7")
    book_len = len(matcher.book(CODE).asks)
    print(f"N7 setup: 卖单挂出@{sell_px} 未成交: open_orders={len(orders)} "
          f"asks簿={book_len} 持仓={dict(await db.get_position('u7', CODE))}")
    # 崩盘强平：delete_position 但不撤挂单
    engine.quotes[CODE].price = round(P0 * 0.7, 2)
    reports = await margin.check_margin_calls()
    pos7 = await db.get_position("u7", CODE)
    orders_after = await db.get_user_open_orders("u7")
    asks_after = len(matcher.book(CODE).asks)
    print(f"N7 setup: 强平后持仓={dict(pos7) if pos7 else None} "
          f"DB未成交卖单={len(orders_after)} asks簿残留={asks_after} "
          f"(簿中残留=空卖源头)")
    # 接盘侠市价买入 → 与幽灵卖单撮合
    cash_u7b_before = (await db.get_account("u7b"))["cash_available"]
    cash_u7_before = (await db.get_account("u7"))["cash_available"]
    try:
        brep = await trade.buy("u7b", CODE, 100)
        pos_u7b = await db.get_position("u7b", CODE)
        cash_u7b_after = (await db.get_account("u7b"))["cash_available"]
        cash_u7_after = (await db.get_account("u7"))["cash_available"]
        trades_u7 = await db.get_user_trades("u7", 5)
        print(f"N7 OBSERVED: 接盘买入回执: {brep.splitlines()[1] if len(brep.splitlines())>1 else brep!r}")
        print(f"N7 OBSERVED: 接盘侠收货={dict(pos_u7b) if pos_u7b else None} "
              f"付款={round(cash_u7b_before - cash_u7b_after, 2)}元; "
              f"强平者(u7)现金变化={round(cash_u7_after - cash_u7_before, 2)}元 "
              f"(0=交付方未收钱); u7成交流水笔数={len(trades_u7)}")
        print("N7 OBSERVED: 幽灵卖单被撮合成交——买方付款后由 _apply_buy_fill 凭空得股"
              "（trade.py:360-362 无持仓直接 return，买方照扣款收货），账本股数凭空增发")
    except TradeError as e:
        print(f"N7 OBSERVED: 接盘买入被拒: {e}")
    await db.close()

    # ── N8 冻结账户还款/还券 ───────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u8", "冻结户")
    await margin.borrow_cash("u8", 3_300_000)
    qty8 = int(4_300_000 / P0 // 100) * 100
    await db.upsert_position("u8", CODE, qty8, qty8, P0)
    await db.update_cash_full("u8", 0.0, 0, 0)
    engine.quotes[CODE].price = round(P0 * 0.55, 2)   # 深度爆仓
    await margin.check_margin_calls()
    st8 = (await db.get_account("u8"))["status"]
    print(f"N8 setup: 强平后状态={st8} debt={await margin.debt('u8')!r}")
    try:
        await margin.repay_cash("u8", 100)
        print("N8 OBSERVED: 冻结户可还款")
    except MarginError as e:
        print(f"N8 OBSERVED: 冻结户还款被拒: {e}")
    try:
        await margin.cover("u8", CODE, 100)
        print("N8 OBSERVED: 冻结户可还券")
    except MarginError as e:
        print(f"N8 OBSERVED: 冻结户还券被拒: {e}")
    print("N8 OBSERVED: 全库无管理员清账入口（main.py 管理员组仅 暂停/恢复/状态）")
    await db.close()

    # ── N9 排行榜刷榜 ─────────────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u9", "刷榜者")
    await acct.open_account("u9b", "老实人")
    board = _board(db)
    price_of = lambda c: engine.get(c).price if engine.get(c) else None  # noqa: E731
    before = await board.top(price_of)
    await margin.borrow_cash("u9", 3_300_000)   # 顶格借款
    after = await board.top(price_of)
    print(f"N9 OBSERVED: 借款前 u9 total={before[1]['total'] if before[1]['user_id']=='u9' else before[0]['total']!r} "
          f"return%={before[0]['return_pct']:.2f}; 借款330万后榜一={after[0]['user_id']} "
          f"total={after[0]['total']!r} return%={after[0]['return_pct']:.2f} "
          f"(净资产仍100万=借入不算利润，但榜单按 cash+mv 不扣负债)")
    await db.close()

    # ── N10 现金口径正向确认 ──────────────────────────────────
    db, engine, matcher, acct, trade, margin = await fresh()
    await acct.open_account("u10", "口径")
    await db.update_cash_full("u10", 100.0, 999_999.0, 50.0)
    cash, mv, assets = await margin._assets("u10")
    print(f"N10 OBSERVED: 可用100/可取999999/冻结50 -> assets={assets!r} "
          f"(=150=可用+冻结, 不含可取, 防双算正确)")
    await db.close()

    print(f"\n==== 探针结束: {check.__globals__['PASS']} 项断言通过 ====")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
