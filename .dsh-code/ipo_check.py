"""P2 新股申购（打新）离线测试（复用 sim_test 的 aiosqlite 伪造层与 check()）。

覆盖 t1 契约行为：
0 环境 / 1 publish 校验（代码/板块/发行价/发行量/重复发行）
2 发行文案与打新面板 / 3 申购冻结（资金、上限、非整签、无账户、状态异常、
  申购单不进 orders 表）/ 4 申购不足全员中签（退款 0）
5 超购摇号（确定性抽签、退款、划股 sellable=0、上市登记、首日涨跌幅覆盖）
6 崩溃恢复：freezing 半截冻结重放 + 放弃退款 / 7 崩溃恢复：inflight 摇号重放
（不双退款、不重复配股）/ 8 restore() 重启重登记 + 首日免涨跌幅到期解除
9 边界（无申购摇号、重复摇号、摇号后申购、未知代码）

行情注入：直接改写 engine.quotes 不适用（新股上市由 ipo 自己登记）；
账目全部经 Database 真实落盘（sqlite3 伪造驱动），重启用同一 DB 文件。
"""
import asyncio
import importlib
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_test as _st  # noqa: E402  注入伪造 aiosqlite + 插件包导入

PKG = _st.PKG
Database, PriceEngine, check = _st.Database, _st.PriceEngine, _st.check
AccountService = _st.AccountService
_i = importlib.import_module(f"{PKG}.game.ipo")
IPOService, IpoError = _i.IPOService, _i.IpoError

# 测试库放在 .dsh-code 下（code_verify 通道写插件根目录会 unable to open database file）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f".ipo_check_{int(time.time() * 1000)}.db")

SEED_A = 777


async def main():
    db = Database(DB_PATH)
    await db.open()
    engine = PriceEngine(seed=42)
    acct = AccountService(db)
    ipo = IPOService(db, engine)

    print("== 0. 环境 ==")
    check("静态股票池存在", "600001" in __import__(
        f"{PKG}.market.stocks", fromlist=["STOCK_POOL"]).STOCK_POOL)

    print("== 1. publish 校验 ==")
    async def expect_err(name, coro):
        try:
            await coro
            check(name, False, "未抛错")
        except IpoError:
            check(name, True)

    await expect_err("代码非数字被拒",
                     ipo.publish("abc", "坏公司", "主板", "周期", "测试", 10, 10000))
    await expect_err("板块无效被拒",
                     ipo.publish("600990", "坏公司", "月球", "周期", "测试", 10, 10000))
    await expect_err("板块联动单位无效被拒",
                     ipo.publish("600990", "坏公司", "主板", "电竞", "测试", 10, 10000))
    await expect_err("名称为空被拒",
                     ipo.publish("600990", "", "主板", "周期", "测试", 10, 10000))
    await expect_err("发行价非正被拒",
                     ipo.publish("600990", "坏公司", "主板", "周期", "测试", 0, 10000))
    await expect_err("发行价 NaN 被拒（穿过 <= 0 比较）",
                     ipo.publish("600990", "坏公司", "主板", "周期", "测试",
                                 float("nan"), 10000))
    await expect_err("发行价 inf 被拒",
                     ipo.publish("600990", "坏公司", "主板", "周期", "测试",
                                 float("inf"), 10000))
    await expect_err("发行量不足一签被拒",
                     ipo.publish("600990", "坏公司", "主板", "周期", "测试", 10, 500))
    text = await ipo.publish("600999", "云峰新材", "主板", "周期", "测试材料",
                             10.00, 20_000, seed=SEED_A)
    check("发行文案含名称与签数", "云峰新材" in text and "每签 1000 股" in text)
    check("发行登记有日志", any("发行登记" in r for r in _st.LOG_RECORDS))
    await expect_err("重复发行同代码被拒",
                     ipo.publish("600999", "云峰新材", "主板", "周期", "测试材料",
                                 10.00, 20_000, seed=SEED_A))
    await expect_err("股票池已有代码被拒",
                     ipo.publish("600001", "钢铁联合", "主板", "周期", "钢铁",
                                 4.20, 20_000, seed=1))
    ledger = await ipo._load()
    ipo_a = ipo._ipo_of(ledger, "600999")
    check("发行登记状态=open", ipo_a and ipo_a["status"] == "open")
    check("每签股数=1000（沪市）", ipo_a and ipo_a["lot_size"] == 1000)
    check("总签数=20", ipo_a and ipo_a["total_lots"] == 20)
    check("每账户上限=1 签（发行量1%向下取整、至少1签）",
          ipo_a and IPOService._cap_lots(ipo_a["total_shares"], ipo_a["lot_size"]) == 1)

    print("== 2. 面板与发行列表 ==")
    panel = await ipo.status_text(None)
    check("面板列出发行中新股", "云峰新材" in panel and "600999" in panel)

    print("== 3. 申购冻结 ==")
    ok, _ = await acct.open_account("u01", "玩家一")
    check("u01 开户", ok)
    await ipo.subscribe("u01", "600999", 1)
    acc = await db.get_account("u01")
    check("申购扣可用 10000", abs(acc["cash_available"] - 990_000) < 0.01,
          f"avail={acc['cash_available']}")
    check("申购进冻结 10000", abs(acc["cash_frozen"] - 10_000) < 0.01,
          f"frozen={acc['cash_frozen']}")
    check("可取资金不动", acc["cash_withdrawable"] == 1_000_000)
    check("申购单不进 orders 表",
          len(await db.get_user_open_orders("u01")) == 0)
    await expect_err("超出申购上限被拒", ipo.subscribe("u01", "600999", 1))
    for bad in (0, -1, 1.5, True):
        await expect_err(f"签数 {bad!r} 被拒", ipo.subscribe("u01", "600999", bad))
    await expect_err("无账户申购被拒", ipo.subscribe("nobody", "600999", 1))
    await expect_err("未知代码申购被拒", ipo.subscribe("u01", "600001", 1))
    # 状态异常（frozen）禁止申购
    await db.set_status("u01", "frozen")
    await expect_err("账户状态异常申购被拒", ipo.subscribe("u01", "600999", 1))
    await db.set_status("u01", "active")
    # 发行量小 + 价高 → 资金不足（IPO-B 资金分支在后面 u 系列上测）
    ok, _ = await acct.open_account("u02", "玩家二")
    check("u02 开户", ok)

    print("== 4. 申购不足全员中签 ==")
    text_b = await ipo.publish("601999", "长江存储", "主板", "科技", "芯片",
                               8.00, 10_000, seed=888)
    check("IPO-B 每签 1000 股", "每签 1000 股" in text_b)
    await ipo.subscribe("u02", "601999", 1)
    ok, _ = await acct.open_account("u03", "玩家三")
    check("u03 开户", ok)
    await ipo.subscribe("u03", "601999", 1)
    acc = await db.get_account("u02")
    check("IPO-B 冻结 8000", abs(acc["cash_frozen"] - 8_000) < 0.01)
    info_b = await ipo.draw("601999")
    check("全员中签：中签率=1", info_b["rate"] == 1.0, f"rate={info_b['rate']}")
    check("全员中签：中签签数=总签数", info_b["winning_lots"] == 2)
    check("全员中签：退款 0（冻结即缴款）", info_b["refunded"] == 0.0)
    acc = await db.get_account("u02")
    check("中签缴款：可用回到 992000",
          abs(acc["cash_available"] - 992_000) < 0.01,
          f"avail={acc['cash_available']}")
    check("中签缴款：冻结清零", acc["cash_frozen"] == 0)
    pos = await db.get_position("u02", "601999")
    check("中签划股 1000 股", pos and pos["quantity"] == 1000)
    check("中签股 T+1 不可卖（sellable=0）", pos and pos["sellable"] == 0)
    check("中签股成本=发行价", pos and abs(pos["cost_price"] - 8.0) < 1e-9)
    q = engine.quotes.get("601999")
    check("上市登记行情", q is not None and abs(q.price - 8.0) < 1e-9)
    check("主板首日涨跌幅覆盖 ±44%",
          q is not None and q.pct_override == 0.44)
    check("已上市股票在股票池", "601999" in __import__(
        f"{PKG}.market.stocks", fromlist=["STOCK_POOL"]).STOCK_POOL)

    print("== 5. 超购摇号（确定性抽签）==")
    # u01 已在第 3 节申购 1 签（cap 测试）；这里补 u02..u25 共 24 人
    users = [f"u{i:02d}" for i in range(2, 26)]
    for u in users:
        if not await db.get_account(u):
            await acct.open_account(u, f"玩家{u}")
        await ipo.subscribe(u, "600999", 1)
    ledger = await ipo._load()
    subs_a = ipo._subs_of(ledger, ipo_a["id"])
    check("申购总量=25 签", len(subs_a) == 25
          and sum(s["lots"] for s in subs_a) == 25)
    # 期望中签：与实现同算法（seed 完全确定，可复现）
    rng = random.Random(SEED_A)
    winners = set(rng.sample(range(25), 20))
    expect_win = {}
    offset = 0
    for i, s in enumerate(subs_a):
        expect_win[s["user_id"]] = sum(1 for j in winners
                                       if offset <= j < offset + s["lots"])
        offset += s["lots"]
    info_a = await ipo.draw("600999")
    check("中签签数=min(发行,申购)=20", info_a["winning_lots"] == 20,
          f"winning={info_a['winning_lots']}")
    check("中签率=20/25=0.8", abs(info_a["rate"] - 0.8) < 1e-9,
          f"rate={info_a['rate']}")
    check("中签人数=20", info_a["winners"] == 20)
    check("配售股数=20000", info_a["allotted_shares"] == 20_000)
    check("退款=5×10000=50000", info_a["refunded"] == 50_000.0,
          f"refunded={info_a['refunded']}")
    ledger = await ipo._load()
    for s in ipo._subs_of(ledger, ipo_a["id"]):
        u = s["user_id"]
        exp = expect_win[u]
        check(f"{u} 中签数符合确定性抽签（{exp}）",
              (s.get("win_lots") or 0) == exp,
              f"win_lots={s.get('win_lots')}")
        acc = await db.get_account(u)
        base = 1_000_000 - (8_000 if u in ("u02", "u03") else 0)   # 二人另有 IPO-B 缴款
        if exp > 0:
            check(f"{u} 中签缴款后可用={base - 10_000:.0f}",
                  abs(acc["cash_available"] - (base - 10_000)) < 0.01,
                  f"avail={acc['cash_available']}")
            p = await db.get_position(u, "600999")
            check(f"{u} 中签持仓 1000 股 sellable=0",
                  p and p["quantity"] == 1000 and p["sellable"] == 0)
        else:
            check(f"{u} 未中签全额退款",
                  abs(acc["cash_available"] - base) < 0.01,
                  f"avail={acc['cash_available']}")
            check(f"{u} 未中签无持仓",
                  not await db.get_position(u, "600999"))
        check(f"{u} 结算后冻结清零", acc["cash_frozen"] == 0)
    q = engine.quotes.get("600999")
    check("IPO-A 上市价=发行价", q and abs(q.price - 10.0) < 1e-9)
    check("IPO-A 首日 ±44% 覆盖", q and q.pct_override == 0.44)
    check("limit_counts 不 KeyError", isinstance(engine.limit_counts(), tuple))
    await expect_err("重复摇号被拒", ipo.draw("600999"))
    await expect_err("摇号后申购被拒", ipo.subscribe("u01", "600999", 1))

    print("== 6. 崩溃恢复：freezing 半截冻结重放 ==")
    await ipo.publish("603999", "华新电子", "主板", "科技", "电子",
                      5.00, 50_000, seed=999)
    ok, _ = await acct.open_account("u26", "玩家二六")
    check("u26 开户", ok)
    await ipo.subscribe("u26", "603999", 1)
    # 模拟崩溃：把该订阅打回 freezing 半截状态（pending 增量 + pre-image）
    # ——pre-image 是"该次增量冻结之前"的状态：首次申购即 [1M, 1M, 0]
    ledger = await ipo._load()
    sub_c = ipo._subs_of(ledger, 3)[0]           # 第三个发行 = IPO-C
    sub_c.update(state="freezing", quantity=0, lots=0, amount=0.0,
                 pending={"quantity": 1000, "lots": 1, "amount": 5000.0},
                 pre={"cash": [1_000_000.0, 1_000_000.0, 0.0], "pos": None})
    await ipo._save(ledger)
    # 模拟真实崩溃窗口：意图已落盘、现金划转尚未发生
    await db.update_cash_full("u26", 1_000_000, 1_000_000, 0.0)
    info_c = await ipo.draw("603999")            # 内部先恢复重放再结算
    check("半截冻结恢复后正常中签", info_c["winning_lots"] == 1)
    acc = await db.get_account("u26")
    check("恢复后可用=995000（不双扣不双退）",
          abs(acc["cash_available"] - 995_000) < 0.01,
          f"avail={acc['cash_available']}")
    check("恢复后冻结清零", acc["cash_frozen"] == 0)
    pos = await db.get_position("u26", "603999")
    check("恢复后划股 1000 股", pos and pos["quantity"] == 1000)

    print("== 6b. 崩溃恢复：窗口内现金被抽干 → 放弃申购 ==")
    await ipo.publish("609999", "放弃股份", "主板", "周期", "测试",
                      5.00, 50_000, seed=444)
    ok, _ = await acct.open_account("u30", "玩家三十")
    check("u30 开户", ok)
    await ipo.subscribe("u30", "609999", 1)
    ledger = await ipo._load()
    h_id = ipo._ipo_of(ledger, "609999")["id"]
    sub_h = ipo._subs_of(ledger, h_id)[0]
    sub_h.update(state="freezing", quantity=0, lots=0, amount=0.0,
                 pending={"quantity": 1000, "lots": 1, "amount": 5000.0},
                 pre={"cash": [1_000.0, 1_000_000.0, 0.0], "pos": None})
    await ipo._save(ledger)
    # 崩溃窗口内现金被其它操作抽干（可用仅 1000，pending 需 5000）
    await db.update_cash_full("u30", 1_000.0, 1_000_000.0, 0.0)
    info_h = await ipo.draw("609999")
    check("放弃后无配股", info_h["winning_lots"] == 0)
    acc = await db.get_account("u30")
    check("放弃后现金不受损（恢复 pre）",
          abs(acc["cash_available"] - 1_000.0) < 0.01,
          f"avail={acc['cash_available']}")
    check("放弃后冻结清零", acc["cash_frozen"] == 0)
    check("放弃后无持仓", not await db.get_position("u30", "609999"))
    ledger = await ipo._load()
    sub_h2 = ipo._subs_of(ledger, h_id)[0]
    check("放弃的申购置 settled 且归零",
          sub_h2["state"] == "settled" and sub_h2["quantity"] == 0
          and (sub_h2.get("win_lots") or 0) == 0)
    check("放弃的 IPO 仍照常上市", engine.quotes.get("609999") is not None)

    print("== 7. 崩溃恢复：inflight 摇号中断重放 ==")
    await ipo.publish("605999", "中信地产", "主板", "基建地产", "房地产",
                      4.00, 30_000, seed=111)
    await acct.open_account("u27", "玩家二七")
    await acct.open_account("u28", "玩家二八")
    await ipo.subscribe("u27", "605999", 1)
    await ipo.subscribe("u28", "605999", 1)
    # 模拟崩溃：status=drawing，两笔订阅打到 inflight（中签结果已定）
    ledger = await ipo._load()
    d_id = ipo._ipo_of(ledger, "605999")["id"]
    for s in ipo._subs_of(ledger, d_id):
        s.update(state="inflight", win_lots=1 if s["user_id"] == "u27" else 0,
                 pre={"cash": [996_000.0, 1_000_000.0, 4_000.0], "pos": None})
    for k, v in ledger["ipos"].items():
        if v["code"] == "605999":
            v["status"] = "drawing"
    await ipo._save(ledger)
    info_d = await ipo.draw("605999")
    check("inflight 重放：中签 1 签", info_d["winning_lots"] == 1)
    check("inflight 重放：退款 4000", info_d["refunded"] == 4_000.0)
    acc27 = await db.get_account("u27")
    acc28 = await db.get_account("u28")
    check("u27 中签：可用 996000（不双退款）",
          abs(acc27["cash_available"] - 996_000) < 0.01,
          f"avail={acc27['cash_available']}")
    check("u28 未中签：可用 1000000",
          abs(acc28["cash_available"] - 1_000_000) < 0.01,
          f"avail={acc28['cash_available']}")
    check("u27 冻结清零", acc27["cash_frozen"] == 0)
    pos27 = await db.get_position("u27", "605999")
    check("u27 划股 1000 股（不重复配股）",
          pos27 and pos27["quantity"] == 1000 and pos27["sellable"] == 0)
    check("u28 无持仓", not await db.get_position("u28", "605999"))

    print("== 8. restore 重启重登记 + 首日免涨跌幅到期 ==")
    await ipo.publish("300999", "星光智造", "创业板", "科技", "软件",
                      20.00, 40_000, seed=222)
    await acct.open_account("u29", "玩家二九")
    await ipo.subscribe("u29", "300999", 1)      # 500 股/签 × 20 = 10000
    check("创业板每签 500 股",
          ipo._ipo_of(await ipo._load(), "300999")["lot_size"] == 500)
    await ipo.draw("300999")
    await ipo.publish("607999", "空仓新股", "主板", "周期", "测试",
                      6.00, 20_000, seed=333)
    info_f = await ipo.draw("607999")            # 无人申购也照常上市
    check("无申购摇号：中签率 0", info_f["rate"] == 0.0)
    check("无申购摇号：仍上市登记", engine.quotes.get("607999") is not None)
    # ── 重启：全新引擎（行情全空），restore 重登记 ──
    engine2 = PriceEngine(seed=7)
    ipo2 = IPOService(db, engine2)
    n = await ipo2.restore()
    check("restore 重登记 7 只已上市新股", n == 7, f"n={n}")
    check("restore 后行情存在", engine2.quotes.get("600999") is not None)
    check("restore 价格回发行价（无 prices 快照）",
          abs(engine2.quotes["600999"].price - 10.0) < 1e-9)
    check("restore 恢复主板首日 ±44%",
          engine2.quotes["600999"].pct_override == 0.44)
    check("restore 恢复创业板免涨跌幅覆盖 ±1000%",
          engine2.quotes["300999"].pct_override == 10.0)
    check("restore 后 limit_counts 不 KeyError",
          isinstance(engine2.limit_counts(), tuple))
    n2 = await ipo2.restore()
    check("restore 幂等（再调一次仍 7 只、行情不重置）",
          n2 == 7 and abs(engine2.quotes["600999"].price - 10.0) < 1e-9)
    # 日切推进：主板第 1 日到期解除（A/B/C/D/F/H 六只）；创业板第 5 日到期
    cleared = await ipo2.on_day_start(1)
    check("第 1 日：6 只主板新股解除覆盖", cleared == 6, f"cleared={cleared}")
    check("主板新股覆盖已解除", engine2.quotes["600999"].pct_override is None)
    check("创业板新股覆盖保留", engine2.quotes["300999"].pct_override == 10.0)
    for day in (2, 3, 4):
        c = await ipo2.on_day_start(day)
        check(f"第 {day} 日创业板覆盖保留", c == 0)
    cleared = await ipo2.on_day_start(5)
    check("第 5 日：创业板解除覆盖", cleared == 1, f"cleared={cleared}")
    check("创业板覆盖已解除", engine2.quotes["300999"].pct_override is None)
    c = await ipo2.on_day_start(5)
    check("同日重复调用幂等", c == 0)
    ledger = await ipo2._load()
    check("日切状态持久化", ledger["last_limit_day"] == 5)

    print("== 9. 面板（含用户结果）==")
    panel = await ipo2.status_text("u02")
    check("面板含已摇号记录", "长江存储" in panel and "中签" in panel)

    print("== 10. 坏账本防护（C2/C3 同类加固）==")
    good = await db.get_meta("ipo.ledger.v1")
    await db.set_meta("ipo.ledger.v1", '{"version":1,"next_id":9}')
    await expect_err("缺 ipos/subs 结构账本被拒（不凭空销账）",
                     ipo2.publish("608001", "幽灵股", "主板", "周期", "测试",
                                  6.00, 20_000))
    await db.set_meta("ipo.ledger.v1", "not-json{")
    await expect_err("JSON 解析失败账本被拒", ipo2.publish(
        "608002", "幽灵股", "主板", "周期", "测试", 6.00, 20_000))
    await db.set_meta("ipo.ledger.v1", good)
    text = await ipo2.publish("608003", "恢复发行", "主板", "周期", "测试",
                              6.00, 20_000, seed=555)
    check("账本修复后恢复发行", "恢复发行" in text)

    await db.close()
    try:
        os.remove(DB_PATH)
    except OSError:
        pass
    return _st.FAIL


if __name__ == "__main__":
    rc = asyncio.run(main())
    print(f"==== 结果: {_st.PASS} 通过, {_st.FAIL} 失败 ====")
    sys.exit(1 if rc else 0)
