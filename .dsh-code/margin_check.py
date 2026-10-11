"""P3 融资融券离线测试（复用 sim_test 的 aiosqlite 伪造层与 check() 计数器）。

覆盖队长要求的、112 条继承断言未触及的行为：
1 净资产基线（无负债） / 2 融资借款到账、借入不算利润、借款上限
3 每日计息幂等（同日重复 + 重启后同日） / 4 还款先息后本、超额钳制
5 融资满仓崩盘 → 干净强平（卖持仓还款，保留活跃）
6 部分强平（只清爆仓股，保留未爆仓持仓）
7 深度亏损 → 强平后净资产<0 → L4 冻结 + 冻结封锁
8 融券卖出（净资产仅费用损耗）、下跌盈利、按借入市值计息、买券还券
9 融券大涨 → 强平买券还券
10 边界（零/负金额、不存在代码、非整手、停牌、无负债还款/还券）
11 render + 最终重启（冻结状态与未清负债持久化）

价格注入说明：直接改写 engine.quotes[code].price 模拟行情涨跌，
其余账目全部经 Database 真实落盘（sqlite3 伪造驱动），重启用同一 DB 文件。
"""
import asyncio
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_test as _st  # noqa: E402  注入伪造 aiosqlite + 插件包导入

PKG = _st.PKG
Database, PriceEngine, CFG, check = (_st.Database, _st.PriceEngine, _st.CFG,
                                     _st.check)
AccountService = _st.AccountService
_t = importlib.import_module(f"{PKG}.game.trade")
fees_sell = _t.fees_sell
_m = importlib.import_module(f"{PKG}.game.margin")
MarginService, MarginError = _m.MarginService, _m.MarginError

# 计息确定性：日息 0.1%
_m._cash_rate_daily = lambda: 0.001
_m._stock_rate_daily = lambda: 0.001

# 测试库放在 .dsh-code 下（code_verify 通道写插件根目录会 unable to open database file）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f".margin_check_{int(time.time() * 1000)}.db")


async def main():
    db = Database(DB_PATH)
    await db.open()
    engine = PriceEngine(seed=42)
    acct = AccountService(db)
    margin = MarginService(db, engine)
    P0 = engine.get("600001").price
    P2 = engine.get("600002").price
    print(f"== 0. 基准价 600001={P0:.2f} 600002={P2:.2f} ==")

    print("== 1. 净资产基线 ==")
    ok, _ = await acct.open_account("u1", "玩家一")
    check("u1 开户", ok)
    check("无负债净资产=初始资金", await margin.net_worth("u1") == 1_000_000,
          f"net={await margin.net_worth('u1')}")
    check("无负债无维持担保比例", await margin.margin_ratio("u1") is None)
    check("无负债时 has_debt=False", not await margin.has_debt("u1"))

    print("== 2. 融资借款 ==")
    await margin.borrow_cash("u1", 300_000)
    acc = await db.get_account("u1")
    check("借款到账可用资金", abs(acc["cash_available"] - 1_300_000) < 0.01,
          f"avail={acc['cash_available']}")
    check("借入现金不计入净资产利润", await margin.net_worth("u1") == 1_000_000,
          f"net={await margin.net_worth('u1')}")
    check("负债=借款额", await margin.debt("u1") == 300_000)
    check("维持担保比例=总资产/负债",
          abs(await margin.margin_ratio("u1") - 1_300_000 / 300_000) < 1e-6)
    check("融资借款有日志", any("融资借款" in r for r in _st.LOG_RECORDS))
    try:
        await margin.borrow_cash("u1", 10_000_000)
        check("超限借款被拒", False, "未抛错")
    except MarginError:
        check("超限借款被拒", True)
    # 顶格：cap=(130万-1.3×30万)/0.3=303.33万，借 300 万应通过
    await margin.borrow_cash("u1", 3_000_000)
    acc = await db.get_account("u1")
    check("顶格借款成功", abs(acc["cash_available"] - 4_300_000) < 0.01,
          f"avail={acc['cash_available']}")
    check("顶格后比例恰在平仓线上",
          abs(await margin.margin_ratio("u1") - 4_300_000 / 3_300_000) < 1e-6,
          f"ratio={await margin.margin_ratio('u1')}")

    print("== 3. 每日计息幂等 + 重启持久化 ==")
    stats = await margin.accrue_day(1)
    acc = await db.get_account("u1")
    check("首日计息=本金×0.001", stats["interest"] == 3_300.0, f"stats={stats}")
    check("现金扣除利息",
          abs(acc["cash_available"] - (4_300_000 - 3_300)) < 0.01,
          f"avail={acc['cash_available']}")
    check("当日净资产=100万-息",
          abs(await margin.net_worth("u1") - 996_700) < 0.01)
    stats2 = await margin.accrue_day(1)
    acc = await db.get_account("u1")
    check("同日重复计息跳过",
          stats2["charged"] == 0
          and abs(acc["cash_available"] - (4_300_000 - 3_300)) < 0.01,
          f"stats2={stats2}")
    # ── 重启：关库重开，新引擎新服务 ──
    await db.close()
    db = Database(DB_PATH)
    await db.open()
    engine = PriceEngine(seed=42)
    acct = AccountService(db)          # 旧 acct 绑的是已关闭的 db！
    margin = MarginService(db, engine)
    check("重启后负债仍=3300000", await margin.debt("u1") == 3_300_000,
          f"debt={await margin.debt('u1')}")
    check("重启后现金不丢",
          abs((await db.get_account("u1"))["cash_available"]
              - (4_300_000 - 3_300)) < 0.01)
    stats3 = await margin.accrue_day(1)
    check("重启后同一日不再计息", stats3["charged"] == 0, f"stats3={stats3}")
    stats4 = await margin.accrue_day(2)
    check("次日计息", stats4["interest"] == 3_300.0, f"stats4={stats4}")
    check("净资产=100万-两日息",
          abs(await margin.net_worth("u1") - 993_400) < 0.01)
    # 利息流水上限
    for d in range(3, 203):
        await margin.accrue_day(d)
    ledger = await margin._load()
    check("利息流水上限 200 条", len(ledger["interest_log"]) == 200,
          f"n={len(ledger['interest_log'])}")

    print("== 4. 还款（先息后本 + 超额钳制）==")
    await margin.repay_cash("u1", 1_000_000)
    check("还款后本金减少", await margin.debt("u1") == 2_300_000,
          f"debt={await margin.debt('u1')}")
    try:
        await margin.repay_cash("u1", 9_999_999)
        check("超额还款被资金检查拦截", False, "未抛错")
    except MarginError:
        check("超额还款被资金检查拦截", True)
    await margin.repay_cash("u1", 2_300_000)
    check("全额还款清零", await margin.debt("u1") == 0,
          f"debt={await margin.debt('u1')}")
    check("还款后账户活跃", (await db.get_account("u1"))["status"] == "active")

    print("== 5. 融资满仓 + 崩盘 → 干净强平 ==")
    ok, _ = await acct.open_account("u2", "玩家二")
    check("u2 开户", ok)
    await margin.borrow_cash("u2", 2_800_000)
    qty1 = int(2_800_000 / P0 // 100) * 100
    await db.upsert_position("u2", "600001", qty1, 0, P0)
    await db.update_cash_full("u2", 1_000_000, 0, 0)   # 借来280万全换股票
    check("满仓后比例≈135.7%",
          abs(await margin.margin_ratio("u2") - 3_800_000 / 2_800_000) < 0.01,
          f"ratio={await margin.margin_ratio('u2')}")
    engine.quotes["600001"].price = round(P0 * 0.7, 2)   # 崩盘 30%
    ratio = await margin.margin_ratio("u2")
    check("崩盘后跌破 130% 平仓线", ratio is not None and ratio < 1.30,
          f"ratio={ratio}")
    reports = await margin.check_margin_calls()
    check("强平报告非空", len(reports) >= 1, f"reports={reports}")
    print("   强平回执:", reports[0].replace("\n", " | ") if reports else "-")
    check("强平后负债清零", await margin.debt("u2") == 0,
          f"debt={await margin.debt('u2')}")
    pos = await db.get_position("u2", "600001")
    check("强平后持仓清空", pos is None or pos["quantity"] == 0,
          f"pos={dict(pos) if pos else None}")
    check("强平后净资产为正（未触发 L4）", await margin.net_worth("u2") > 0,
          f"net={await margin.net_worth('u2')}")
    check("强平后账户仍活跃", (await db.get_account("u2"))["status"] == "active")

    print("== 6. 部分强平：保留未爆仓持仓 ==")
    ok, _ = await acct.open_account("u3", "玩家三")
    check("u3 开户", ok)
    await margin.borrow_cash("u3", 2_800_000)
    qtyA = int(2_600_000 / P0 // 100) * 100
    qtyB = int(200_000 / P2 // 100) * 100
    await db.upsert_position("u3", "600001", qtyA, 0, P0)
    await db.upsert_position("u3", "600002", qtyB, 0, P2)
    await db.update_cash_full("u3", 1_000_000, 0, 0)
    engine.quotes["600001"].price = round(P0 * 0.7, 2)   # 600001 再崩
    ratio = await margin.margin_ratio("u3")
    check("u3 跌破平仓线", ratio < 1.30, f"ratio={ratio}")
    await margin.check_margin_calls()
    check("强平后负债清零", await margin.debt("u3") == 0,
          f"debt={await margin.debt('u3')}")
    posA = await db.get_position("u3", "600001")
    posB = await db.get_position("u3", "600002")
    check("爆仓股被清空", posA is None or posA["quantity"] == 0,
          f"posA={dict(posA) if posA else None}")
    check("未爆仓股保留", posB is not None and posB["quantity"] == qtyB,
          f"posB={dict(posB) if posB else None}")
    check("u3 净资产为正", await margin.net_worth("u3") > 0)
    check("u3 账户活跃", (await db.get_account("u3"))["status"] == "active")

    print("== 7. 深度亏损 → L4 冻结 ==")
    ok, _ = await acct.open_account("u4", "玩家四")
    check("u4 开户", ok)
    await margin.borrow_cash("u4", 3_300_000)
    qty4 = int(4_300_000 / P0 // 100) * 100
    await db.upsert_position("u4", "600001", qty4, 0, P0)
    await db.update_cash_full("u4", 0.0, 0, 0)
    engine.quotes["600001"].price = round(P0 * 0.6, 2)   # 崩盘 40%
    ratio = await margin.margin_ratio("u4")
    check("u4 跌破平仓线", ratio < 1.30, f"ratio={ratio}")
    await margin.check_margin_calls()
    acc4 = await db.get_account("u4")
    check("净资产为负 → 账户冻结（L4）", acc4["status"] == "frozen",
          f"status={acc4['status']}")
    check("冻结后仍有未清负债", await margin.debt("u4") > 0,
          f"debt={await margin.debt('u4')}")
    check("冻结后净资产为负", await margin.net_worth("u4") < 0,
          f"net={await margin.net_worth('u4')}")
    try:
        await margin.borrow_cash("u4", 1000)
        check("冻结账户禁止再融资", False, "未抛错")
    except MarginError as e:
        check("冻结账户禁止再融资", "冻结" in str(e), str(e))

    print("== 8. 融券卖出 ==")
    ok, _ = await acct.open_account("u5", "玩家五")
    check("u5 开户", ok)
    qty5 = int(300_000 / P2 // 100) * 100
    await margin.borrow_stock("u5", "600002", qty5)
    gross = round(P2 * qty5, 2)
    fee5 = fees_sell(gross)
    acc = await db.get_account("u5")
    check("融券卖出所得到账",
          abs(acc["cash_available"] - (1_000_000 + gross - fee5)) < 0.01,
          f"avail={acc['cash_available']}")
    check("融券不影响净资产（仅费用损耗）",
          abs(await margin.net_worth("u5") - (1_000_000 - fee5)) < 0.01,
          f"net={await margin.net_worth('u5')}")
    check("负债=借入数量×市价", abs(await margin.debt("u5") - gross) < 0.01,
          f"debt={await margin.debt('u5')}")
    engine.quotes["600002"].price = round(P2 * 0.9, 2)   # 下跌 10%
    check("股价下跌融券盈利",
          abs(await margin.net_worth("u5")
              - (1_000_000 - fee5 + gross * 0.1)) < 1.0,
          f"net={await margin.net_worth('u5')}")
    net_before = await margin.net_worth("u5")
    exp_interest = round(gross * 0.001, 2)
    await margin.accrue_day(5)
    check("融券利息按借入时市值计提",
          abs(await margin.net_worth("u5") - (net_before - exp_interest)) < 0.01,
          f"net={await margin.net_worth('u5')} exp={net_before - exp_interest}")
    await margin.cover("u5", "600002", qty5)
    check("还券后负债清零", await margin.debt("u5") == 0,
          f"debt={await margin.debt('u5')}")
    check("融券/还券均有流水",
          any(r["code"] == "600002" for r in await db.get_user_trades("u5", 20)))
    try:
        await margin.cover("u5", "600002", 100)
        check("无负债还券被拒", False, "未抛错")
    except MarginError:
        check("无负债还券被拒", True)

    print("== 9. 融券大涨 → 强平买券还券 ==")
    ok, _ = await acct.open_account("u6", "玩家六")
    check("u6 开户", ok)
    P1 = engine.get("600001").price
    qty6 = int(3_000_000 / P1 // 100) * 100
    await margin.borrow_stock("u6", "600001", qty6)
    engine.quotes["600001"].price = round(P1 * 1.05, 2)   # 大涨 5%
    ratio = await margin.margin_ratio("u6")
    check("融券大涨后跌破平仓线", ratio < 1.30, f"ratio={ratio}")
    reports = await margin.check_margin_calls()
    check("u6 被强平还券", any(r for r in reports if "强平" in r))
    print("   强平回执:", (reports[-1] if reports else "-").replace("\n", " | "))
    check("强平后融券负债清零", await margin.debt("u6") == 0,
          f"debt={await margin.debt('u6')}")
    check("强平后净资产为正", await margin.net_worth("u6") > 0,
          f"net={await margin.net_worth('u6')}")
    check("u6 账户活跃", (await db.get_account("u6"))["status"] == "active")

    print("== 10. 边界 ==")
    ok, _ = await acct.open_account("u7", "玩家七")
    check("u7 开户", ok)
    try:
        await margin.borrow_cash("u7", 0)
        check("零金额借款被拒", False, "未抛错")
    except MarginError:
        check("零金额借款被拒", True)
    try:
        await margin.borrow_cash("u7", -100)
        check("负数借款被拒", False, "未抛错")
    except MarginError:
        check("负数借款被拒", True)
    try:
        await margin.borrow_stock("u7", "999999", 100)
        check("不存在代码融券被拒", False, "未抛错")
    except MarginError:
        check("不存在代码融券被拒", True)
    try:
        await margin.borrow_stock("u7", "600001", 150)
        check("非整手融券被拒", False, "未抛错")
    except MarginError:
        check("非整手融券被拒", True)
    engine.suspend("600001", 5, "测试停牌")
    try:
        await margin.borrow_stock("u7", "600001", 100)
        check("停牌股票融券被拒", False, "未抛错")
    except MarginError:
        check("停牌股票融券被拒", True)
    engine.resume("600001")
    try:
        await margin.repay_cash("u7", 100)
        check("无负债还款被拒", False, "未抛错")
    except MarginError:
        check("无负债还款被拒", True)

    print("== 11. render + 最终重启 ==")
    text = await margin.render("u4")
    check("render 输出信用账户", "信用账户" in text)
    print(text)
    await db.close()
    db = Database(DB_PATH)
    await db.open()
    engine = PriceEngine(seed=42)
    margin = MarginService(db, engine)
    check("重启后 u4 仍冻结", (await db.get_account("u4"))["status"] == "frozen")
    check("重启后 u4 负债仍在", await margin.debt("u4") > 0,
          f"debt={await margin.debt('u4')}")
    check("重启后 u2 负债为 0", await margin.debt("u2") == 0)

    print(f"\n==== 结果: {_st.PASS} 通过, {_st.FAIL} 失败 ====")
    await db.close()
    try:
        os.remove(DB_PATH)
    except OSError:
        print(f"（测试库保留：{os.path.basename(DB_PATH)}）")
    return _st.FAIL


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
