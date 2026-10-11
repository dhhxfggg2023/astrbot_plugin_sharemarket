"""K 线图片渲染验证：生成分时图与日 K 图，检查文件是合法 PNG 并报告尺寸。

产出的 PNG 放在插件根目录（工作区根可写），跑完保留供人工查看。
"""
import importlib
import os
import struct
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PLUGIN_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PLUGIN_DIR.parent))
PKG = PLUGIN_DIR.name

chart = importlib.import_module(f"{PKG}.render.chart")
PriceEngine = importlib.import_module(f"{PKG}.market.engine").PriceEngine
STOCK_POOL = importlib.import_module(f"{PKG}.market.stocks").STOCK_POOL

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def png_size(path: Path):
    """读 PNG 头拿宽高（顺便证明它是合法 PNG，而不是空文件/文本）。"""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def main():
    check("Pillow 可用（K 线图片渲染）", chart.available())
    if not chart.available():
        print("  （未安装 Pillow，生产会回退文本 sparkline）")
        return FAIL

    code = "600001"
    s = STOCK_POOL[code]
    eng = PriceEngine(seed=2024)
    for _ in range(200):
        eng.tick({})
    intraday = eng.intraday_bars(code, 120)

    p1 = chart.render_intraday(PLUGIN_DIR / "_demo_intraday.png",
                              f"{s.name}（{code}）分时", intraday)
    check("分时图生成", p1 is not None and p1.exists(), str(p1))
    if p1 and p1.exists():
        size = png_size(p1)
        check("分时图是合法 PNG", size is not None, str(size))
        check("分时图尺寸合理", size and size[0] > 300 and size[1] > 200,
              str(size))
        check("分时图非空", p1.stat().st_size > 5000, f"{p1.stat().st_size} B")
        print(f"  分时图：{p1.name} {size} {p1.stat().st_size:,} B")

    # 日 K：用 30 根由分时收盘价合成的 bar
    day_bars = []
    price = s.initial_price
    import random
    rng = random.Random(7)
    for i in range(30):
        o = price
        c = round(o * (1 + rng.gauss(0, 0.02)), 2)
        day_bars.append({"open": o, "high": round(max(o, c) * 1.006, 2),
                         "low": round(min(o, c) * 0.994, 2), "close": c,
                         "volume": rng.randint(500_000, 3_000_000)})
        price = c
    p2 = chart.render_kline(PLUGIN_DIR / "_demo_kline.png",
                           f"{s.name}（{code}）日K", day_bars)
    check("日 K 图生成", p2 is not None and p2.exists(), str(p2))
    if p2 and p2.exists():
        size = png_size(p2)
        check("日 K 图是合法 PNG", size is not None, str(size))
        check("日 K 图尺寸合理", size and size[0] > 300 and size[1] > 200,
              str(size))
        print(f"  日 K 图：{p2.name} {size} {p2.stat().st_size:,} B")

    check("空数据返回 None（不回退成空图）",
          chart.render_kline(PLUGIN_DIR / "_x.png", "t", []) is None)

    print(f"\n==== 结果: {PASS} 通过, {FAIL} 失败 ====")
    return FAIL


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
