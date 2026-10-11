"""按 /炒股 k线 的输出格式渲染一份样例（用真实引擎与指标函数，便于肉眼检查观感）。"""
import importlib
import os
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
STOCK_POOL = importlib.import_module(f"{PKG}.market.stocks").STOCK_POOL
ind = importlib.import_module(f"{PKG}.market.indicators")

CODE = "600001"
eng = PriceEngine(seed=1234)
for _ in range(120):
    eng.tick({})

s = STOCK_POOL[CODE]
q = eng.get(CODE)
bars = eng.intraday_bars(CODE, 80)

print(f"🕯️ {s.name}（{CODE}）分时　现价 {q.price:.2f} {q.change_pct:+.2f}%")
print(ind.sparkline([b.close for b in bars]))
print(f"本日区间 {min(b.low for b in bars):.2f} ~ {max(b.high for b in bars):.2f}"
      f"　{len(bars)} 根（每根 {eng.ticks_per_bar} tick）")
closes = [b.close for b in bars]
m5, m10 = ind.ma(closes, 5), ind.ma(closes, 10)
print(f"MA5 {'—' if m5 is None else f'{m5:.2f}'}　"
      f"MA10 {'—' if m10 is None else f'{m10:.2f}'}　"
      f"均量 {sum(b.volume for b in bars) // max(len(bars), 1):,} 股")

print("\n（日 K 样例：用分时收盘价合成的 12 天序列演示渲染格式）")
seq = []
price = s.initial_price
for d in range(12):
    o = price
    for _ in range(20):
        eng.tick({})
    c = eng.get(CODE).price
    seq.append((o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1234567))
    price = c
print(f"🕯️ {s.name}（{CODE}）日K　最近 {len(seq)} 个交易日")
print(ind.sparkline([r[3] for r in seq]))
for i, (o, h, low, c, v) in enumerate(seq[-10:], 1):
    chg = (c / o - 1) * 100
    print(f"第{i:02d}天 开{o:.2f} 高{h:.2f} 低{low:.2f} 收{c:.2f} "
          f"{chg:+.2f}% 量{v:,}")
dc = [r[3] for r in seq]
dif, dea, hist = ind.macd(dc)
m5d, m10d, r14 = ind.ma(dc, 5), ind.ma(dc, 10), ind.rsi(dc, 14)


def _fmt(x, digits=2):
    """与 main.py 的 _fmt 一致：None 安全（样本不足时不能拿去格式化）。"""
    return "—" if x is None else f"{x:.{digits}f}"


print(f"MA5 {_fmt(m5d)}　MA10 {_fmt(m10d)}　RSI14 {_fmt(r14, 1)}")
print("MACD 样本不足（需 35 天，现有 12 天）" if dif is None else
      f"MACD DIF {dif:+.3f}　DEA {dea:+.3f}　柱 {hist:+.3f}")
