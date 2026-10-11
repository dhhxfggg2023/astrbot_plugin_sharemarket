"""校对 README 声称的参数默认值与股票池规模是否与代码一致。"""
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

CFG = importlib.import_module(f"{PKG}.config").Config()
STOCKS = importlib.import_module(f"{PKG}.market.stocks").STOCK_POOL

print("== config 默认值 ==")
for k in ("initial_cash", "lot_size", "limit_main", "limit_growth",
          "limit_bse", "limit_st", "commission_rate", "commission_min",
          "stamp_tax_rate", "transfer_fee_rate", "tick_seconds",
          "market_mode", "day_seconds", "mm_spread", "mm_depth", "impact_k",
          "rebirth_cooldown_hours", "rebirth_cash_factor", "rebirth_max_times",
          "trade_cooldown_seconds"):
    print(f"  {k} = {getattr(CFG, k)!r}")

print("\n== 股票池 ==")
print(f"  总数 = {len(STOCKS)}")
boards = {}
for s in STOCKS.values():
    boards[s.board] = boards.get(s.board, 0) + 1
for b, n in sorted(boards.items()):
    print(f"  {b}: {n}")
print(f"  ST 数量 = {sum(1 for s in STOCKS.values() if s.is_st)}")
print(f"  行业数 = {len({s.industry for s in STOCKS.values()})}")

print("\n== 板块分布 ==")
_sm = importlib.import_module(f"{PKG}.market.stocks")
print(f"  板块数 = {len(_sm.SECTORS)}")
for sec in _sm.SECTORS:
    print(f"  {sec}: {len(_sm.SECTOR_MEMBERS[sec])}")

print("\n== 源码语法检查（含 .dsh-code 脚本；用 ast.parse，只读，不写 __pycache__）==")
import ast
import pathlib

bad = []
root = pathlib.Path(PLUGIN_DIR)
files = sorted(p for p in root.rglob("*.py")
               if "__pycache__" not in p.parts)
for p in files:
    try:
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except SyntaxError as e:
        bad.append(f"{p.relative_to(root)}:{e.lineno}: {e.msg}")
print(f"  检查 {len(files)} 个文件，语法错误 {len(bad)} 个")
for b in bad:
    print(f"  ✗ {b}")
