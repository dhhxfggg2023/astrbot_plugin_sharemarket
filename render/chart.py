"""K 线图渲染（Pillow）。

为什么不用 matplotlib：本机实测 **Pillow 12.3.0 可用而 matplotlib 不可用**；
AstrBot 自身要处理图片，Pillow 几乎必然存在。所以只依赖 Pillow —— 零新增重依赖，
而且能离线生成 + 验证。缺 Pillow 时 `available()` 返回 False，调用方回退文本 sparkline，
保证聊天里永远有输出。

中文字体按 Windows/常见发行版的 CJK 字体路径逐个尝试；都找不到时退回内置位图字体
（图表仍可见，只是中文标题会变成方块）。
"""

from __future__ import annotations

from pathlib import Path

W, H = 900, 520
PAD_L, PAD_R, PAD_T, PAD_B = 14, 74, 40, 26
UP = (211, 58, 44)        # 红涨
DOWN = (26, 156, 91)      # 绿跌
GRID = (228, 228, 228)
AXIS = (120, 120, 120)
TEXT = (34, 34, 34)
MA_COLORS = {5: (224, 139, 43), 10: (56, 120, 192), 20: (138, 85, 200),
             30: (120, 120, 120), 60: (90, 90, 90)}

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def available() -> bool:
    """Pillow 是否可用（不可用则由调用方回退文本）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return True
    except Exception:
        return False


def _font(size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _get(bar, key):
    return bar[key] if isinstance(bar, dict) else getattr(bar, key)


def _series(bars):
    return ([_get(b, "open") for b in bars], [_get(b, "high") for b in bars],
            [_get(b, "low") for b in bars], [_get(b, "close") for b in bars],
            [_get(b, "volume") for b in bars])


def render_kline(out_path: Path, title: str, bars: list, ma_windows=(5, 10, 20),
                 volume: bool = True) -> Path | None:
    """日 K：蜡烛 + 均线 + 成交量。bars 需含 open/high/low/close/volume。"""
    if not bars:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    opens, highs, lows, closes, vols = _series(bars)
    n = len(bars)
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title, f_lab = _font(15), _font(11)

    vol_h = int(H * 0.20) if volume else 0
    price_top = PAD_T + 14
    price_bot = H - PAD_B - vol_h - 10
    lo, hi = min(lows), max(highs)
    if hi - lo < 1e-9:
        hi, lo = hi * 1.001, lo * 0.999
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad

    def yv(p):
        return price_bot - (p - lo) / (hi - lo) * (price_bot - price_top)

    x0, x1 = PAD_L + 10, W - PAD_R
    x1 = max(x1, x0 + 1)
    step = (x1 - x0) / max(n, 1)
    body_w = max(1.5, step * 0.62)

    def xv(i):
        return x0 + step * (i + 0.5)

    # 网格 + 右侧价格刻度
    for k in range(5):
        yy = price_top + (price_bot - price_top) * k / 4
        d.line([(x0, yy), (x1, yy)], fill=GRID)
        d.text((x1 + 6, yy - 6), f"{hi - (hi - lo) * k / 4:.2f}",
               font=f_lab, fill=AXIS)

    # 蜡烛：影线 + 实体
    for i in range(n):
        color = UP if closes[i] >= opens[i] else DOWN
        d.line([(xv(i), yv(highs[i])), (xv(i), yv(lows[i]))], fill=color, width=1)
        o, c = yv(opens[i]), yv(closes[i])
        top, bot = min(o, c), max(o, c)
        if bot - top < 1.0:
            bot = top + 1.0
        d.rectangle([xv(i) - body_w / 2, top, xv(i) + body_w / 2, bot],
                    fill=color)

    # 均线
    for w in ma_windows:
        if n < w:
            continue
        pts = [(xv(i), yv(sum(closes[i - w + 1:i + 1]) / w))
               for i in range(w - 1, n)]
        d.line(pts, fill=MA_COLORS.get(w, (150, 150, 150)), width=1)
        # 图例
        lx = x0 + 6 + 62 * list(ma_windows).index(w)
        d.line([(lx, PAD_T + 2), (lx + 18, PAD_T + 2)],
               fill=MA_COLORS.get(w, (150, 150, 150)), width=2)
        d.text((lx + 22, PAD_T - 6), f"MA{w}", font=f_lab,
               fill=MA_COLORS.get(w, (150, 150, 150)))

    # 成交量
    if vol_h:
        vmax = max(vols) or 1
        vtop, vbot = H - PAD_B - vol_h, H - PAD_B
        d.line([(x0, vbot), (x1, vbot)], fill=AXIS)
        for i in range(n):
            h = vols[i] / vmax * (vbot - vtop - 16)
            color = UP if closes[i] >= opens[i] else DOWN
            d.rectangle([xv(i) - body_w / 2, vbot - h, xv(i) + body_w / 2, vbot],
                        fill=color)
        d.text((x0 + 2, vtop - 4), f"成交量（峰值 {vmax:,}）", font=f_lab,
               fill=AXIS)

    d.text((PAD_L, 10), title, font=f_title, fill=TEXT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def render_intraday(out_path: Path, title: str, bars: list,
                    volume: bool = True) -> Path | None:
    """分时：价格折线 + 均价线 + 成交量。bars 需含 close/volume（high/low 用于取值域）。"""
    if not bars:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    opens, highs, lows, closes, vols = _series(bars)
    n = len(bars)
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title, f_lab = _font(15), _font(11)

    vol_h = int(H * 0.20) if volume else 0
    top = PAD_T + 14
    bot = H - PAD_B - vol_h - 10
    lo, hi = min(lows), max(highs)
    if hi - lo < 1e-9:
        hi, lo = hi * 1.001, lo * 0.999
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad

    def yv(p):
        return bot - (p - lo) / (hi - lo) * (bot - top)

    x0, x1 = PAD_L + 10, W - PAD_R
    step = (x1 - x0) / max(n, 1)

    def xv(i):
        return x0 + step * (i + 0.5)

    prev_close = closes[0]
    for k in range(5):
        yy = top + (bot - top) * k / 4
        d.line([(x0, yy), (x1, yy)], fill=GRID)
        d.text((x1 + 6, yy - 6), f"{hi - (hi - lo) * k / 4:.2f}",
               font=f_lab, fill=AXIS)

    # 价格折线（相对昨收着色：红涨绿跌）
    color = UP if closes[-1] >= prev_close else DOWN
    d.line([(xv(i), yv(closes[i])) for i in range(n)], fill=color, width=2)
    avg = [sum(closes[:i + 1]) / (i + 1) for i in range(n)]
    d.line([(xv(i), yv(avg[i])) for i in range(n)],
           fill=(224, 139, 43), width=1)

    if vol_h:
        vmax = max(vols) or 1
        vtop, vbot = H - PAD_B - vol_h, H - PAD_B
        d.line([(x0, vbot), (x1, vbot)], fill=AXIS)
        bw = max(1.0, step * 0.8)
        for i in range(n):
            h = vols[i] / vmax * (vbot - vtop - 16)
            c = UP if closes[i] >= (closes[i - 1] if i else closes[0]) else DOWN
            d.rectangle([xv(i) - bw / 2, vbot - h, xv(i) + bw / 2, vbot], fill=c)
        d.text((x0 + 2, vtop - 4), f"成交量（峰值 {vmax:,}）", font=f_lab,
               fill=AXIS)

    d.text((PAD_L, 10), title, font=f_title, fill=TEXT)
    d.text((x0 + 6, PAD_T - 6), "价格", font=f_lab, fill=color)
    d.line([(x0 + 52, PAD_T + 2), (x0 + 70, PAD_T + 2)],
           fill=(224, 139, 43), width=2)
    d.text((x0 + 74, PAD_T - 6), "均价", font=f_lab, fill=(224, 139, 43))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path
