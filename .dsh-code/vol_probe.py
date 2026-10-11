"""价格模型标定探针：测量真实实现的日波动、板块相关性、涨跌停触发频率。

不是"看着差不多"，而是跑出数字再决定常数该不该改。
"""
import importlib
import math
import os
import statistics as st
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

CFG = importlib.import_module(f"{PKG}.config").CFG
PriceEngine = importlib.import_module(f"{PKG}.market.engine").PriceEngine
stocks = importlib.import_module(f"{PKG}.market.stocks")
STOCK_POOL, SECTORS = stocks.STOCK_POOL, stocks.SECTORS

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 7


def main():
    eng = PriceEngine(seed=SEED, tick_seconds=CFG.tick_seconds,
                      day_seconds=CFG.day_seconds)
    tpd = int(eng.ticks_per_day)
    print(f"股票池 {len(STOCK_POOL)} 只 / {len(SECTORS)} 个板块；"
          f"每虚拟日 {tpd} 个 tick（tick={CFG.tick_seconds}s day={CFG.day_seconds}s）")
    print(f"折算系数 k = {eng.k:.5f}")

    closes: dict[str, list[float]] = {c: [] for c in STOCK_POOL}
    limit_hits = {c: 0 for c in STOCK_POOL}
    limit_ticks = 0
    total_ticks = 0

    for _ in range(DAYS):
        prev_seen = {c: eng.quotes[c].price for c in STOCK_POOL}
        for _ in range(tpd):
            eng.tick({})
            total_ticks += 1
            for c, q in eng.quotes.items():
                s = STOCK_POOL[c]
                if q.price >= q.limit_up(s) or q.price <= q.limit_down(s):
                    limit_ticks += 1
        for c, q in eng.quotes.items():
            closes[c].append(q.price)
            if abs(q.price - prev_seen[c]) > 1e-9:
                pass
        eng.day_close()

    # 日收益（对数收益），跨日
    rets: dict[str, list[float]] = {}
    for c, seq in closes.items():
        r = [math.log(seq[i] / seq[i - 1]) for i in range(1, len(seq))
             if seq[i - 1] > 0]
        rets[c] = r

    def daily_vol(c):
        return st.pstdev(rets[c]) if len(rets[c]) > 1 else 0.0

    print("\n== 各板块日波动率（目标：主板≈1.7% 创业板≈2.5% 科创板≈2.8% 北交所≈3.5%）==")
    by_board: dict[str, list[float]] = {}
    for c, s in STOCK_POOL.items():
        by_board.setdefault(s.board, []).append(daily_vol(c))
    for b, v in sorted(by_board.items()):
        print(f"  {b:4s} n={len(v):3d} 平均日波动 {st.mean(v) * 100:5.2f}% "
              f"中位 {st.median(v) * 100:5.2f}% 最低 {min(v) * 100:5.2f}% "
              f"最高 {max(v) * 100:5.2f}%")

    print("\n== 便宜股 vs 高价股（检验 0.01 最小变动造成的量化失真）==")
    cheap = [c for c, s in STOCK_POOL.items() if s.initial_price <= 5]
    pricey = [c for c, s in STOCK_POOL.items() if s.initial_price >= 50]
    for label, group in (("≤5 元", cheap), ("≥50 元", pricey)):
        if group:
            v = [daily_vol(c) for c in group]
            print(f"  {label:6s} n={len(v):3d} 平均日波动 {st.mean(v) * 100:5.2f}%")

    print("\n== 板块内 / 跨板块 相关性（日收益）==")

    def corr(a, b):
        n = min(len(a), len(b))
        if n < 5:
            return 0.0
        a, b = a[:n], b[:n]
        ma, mb = st.mean(a), st.mean(b)
        sa = math.sqrt(sum((x - ma) ** 2 for x in a))
        sb = math.sqrt(sum((x - mb) ** 2 for x in b))
        if sa == 0 or sb == 0:
            return 0.0
        return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)

    same, cross = [], []
    codes = list(STOCK_POOL)
    for i, c1 in enumerate(codes):
        for c2 in codes[i + 1:]:
            r = corr(rets[c1], rets[c2])
            if STOCK_POOL[c1].sector == STOCK_POOL[c2].sector:
                same.append(r)
            else:
                cross.append(r)
    print(f"  同板块 {len(same)} 对：平均相关 {st.mean(same):+.3f} "
          f"（中位 {st.median(same):+.3f}）")
    print(f"  跨板块 {len(cross)} 对：平均相关 {st.mean(cross):+.3f} "
          f"（中位 {st.median(cross):+.3f}）")

    print("\n== 大盘与板块指数日波动 ==")
    # 等权大盘收益
    mkt = [st.mean([rets[c][i] for c in codes if i < len(rets[c])])
           for i in range(DAYS - 1)]
    print(f"  等权大盘日波动 {st.pstdev(mkt) * 100:.2f}%")
    for sec in SECTORS[:4]:
        members = [c for c, s in STOCK_POOL.items() if s.sector == sec]
        if not members:
            continue
        sr = [st.mean([rets[c][i] for c in members if i < len(rets[c])])
              for i in range(DAYS - 1)]
        print(f"  板块 {sec:5s}（{len(members)}只）日波动 {st.pstdev(sr) * 100:.2f}%")

    print("\n== 涨跌停触发频率 ==")
    print(f"  触及涨/跌停的 tick 占比 {limit_ticks / max(total_ticks, 1) * 100:.2f}%"
          f"（{limit_ticks}/{total_ticks}）")

    print("\n== 抽样个股（日波动 / 30日累计涨跌）==")
    for c in list(STOCK_POOL)[:6]:
        s = STOCK_POOL[c]
        drift = (closes[c][-1] / s.initial_price - 1) * 100
        print(f"  {s.name}({c}) 现价{closes[c][-1]:8.2f} 日波动"
              f"{daily_vol(c) * 100:5.2f}% 累计{drift:+7.1f}%")

    print("\n== 凶险度体检 ==")

    def kurt(xs):
        n = len(xs)
        if n < 4:
            return 0.0
        m = st.mean(xs)
        s2 = sum((x - m) ** 2 for x in xs) / n
        if s2 <= 0:
            return 0.0
        return sum((x - m) ** 4 for x in xs) / n / (s2 ** 2)

    all_daily = [r for c in codes for r in rets[c]]
    print(f"  个股日收益：峰度 {kurt(all_daily):.2f}（正态=3，越大尾越肥）"
          f" 最差单日 {min(all_daily) * 100:+.2f}%"
          f" 最好单日 {max(all_daily) * 100:+.2f}%")
    big = sum(1 for r in all_daily if abs(r) > 0.05) / max(len(all_daily), 1)
    print(f"  单日 |涨跌| >5% 的比例 {big * 100:.2f}%"
          f"（{sum(1 for r in all_daily if abs(r) > 0.05)}/{len(all_daily)}）")

    print("\n  各大盘板最差单日：")
    for c, s in list(STOCK_POOL.items())[:78]:
        if c in cheap[:1]:
            pass
    worst_by_board = {}
    for c, s in STOCK_POOL.items():
        w = min(rets[c]) if rets[c] else 0.0
        worst_by_board.setdefault(s.board, []).append(w)
    for b, v in sorted(worst_by_board.items()):
        print(f"    {b:4s} 平均最差单日 {st.mean(v) * 100:+.2f}%"
              f" 极端 {min(v) * 100:+.2f}%")

    # 最大回撤（按日收盘）
    dd = []
    for c in codes:
        peak = closes[c][0]
        mdd = 0.0
        for p in closes[c]:
            peak = max(peak, p)
            mdd = min(mdd, p / peak - 1)
        dd.append(mdd)
    print(f"\n  个股最大回撤（{DAYS} 日内）：平均 {st.mean(dd) * 100:.2f}%"
          f" 中位 {st.median(dd) * 100:.2f}% 最深 {min(dd) * 100:.2f}%")
    blown = sum(1 for d in dd if d <= -0.4)
    print(f"  回撤 >40% 的股票数：{blown}/{len(dd)}")

    print("\n== 长期漂移分解（每 tick 每只股票的平均贡献 ×1e6）==")
    nn = DAYS * tpd * len(codes)
    for k, v in eng.decomp.items():
        print(f"  {k:7s} {v / nn * 1e6:+8.3f}")
    tot = sum(eng.decomp.values())
    print(f"  {'合计':7s} {tot / nn * 1e6:+8.3f}"
          f"   → 折算 30 日累计约 {tot / nn * DAYS * tpd * 100:+.1f}%")
    drifts = [(closes[c][-1] / STOCK_POOL[c].initial_price - 1) * 100
              for c in codes]
    print(f"\n  30 日累计涨跌：平均 {st.mean(drifts):+.1f}%"
          f" 中位 {st.median(drifts):+.1f}%"
          f" 上涨 {sum(1 for d in drifts if d > 0)}/{len(drifts)} 只")
    occ = {}
    for mf in eng.main_force.values():
        occ[mf.state] = occ.get(mf.state, 0) + 1
    print(f"  主力状态快照分布：{occ}")


if __name__ == "__main__":
    main()
