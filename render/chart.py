"""K 线图渲染（Pillow）。

为什么不用 matplotlib：本机实测 **Pillow 12.3.0 可用而 matplotlib 不可用**；
AstrBot 自身要处理图片，Pillow 几乎必然存在。所以只依赖 Pillow —— 零新增重依赖，
而且能离线生成 + 验证。缺 Pillow 时 `available()` 返回 False，调用方回退文本 sparkline，
保证聊天里永远有输出。

中文字体按 Windows/常见发行版的 CJK 字体路径逐个尝试；都找不到时退回内置位图字体
（图表仍可见，只是中文标题会变成方块）。

2026-10-12 视觉重构（用户诉求「更精致更现代化一点」）：白底 → 深色金融终端主题。
- 深蓝黑背景分层：画布底色 / 成交量区微差 / 标题带分隔线 / 绘图区上下边框线；
- 现代红涨绿跌色值（中国市场惯例不变，只做饱和度与明度调优）；
- 分时图价格折线下方加**多段渐变面积填充**（顶部线条色 → 中部 55% 拐点 →
  底部沉入背景），比单一线性渐变更有层次；
- 蜡烛实体用圆角矩形；影线独立细线；日 K 最后一根高亮最新价胶囊；
- 标题右侧挂「最新价 / 涨跌幅」胶囊徽标（带 1px 同色系描边，徽标更立体）；
- 成交量柱「半透明实色 + 顶部亮条」双色观感，深底下有体积感；
- 图例色块 = 12px 小圆点 + 16px 直线组合，比单一直线更精致；
- 冷灰蓝刻度文字，网格线低对比。

渲染质量约束（承接 2026-10-11 逐条验收，深色主题下等价改写）：
- 右侧价格刻度右对齐到绘图区右缘，且右缘之外不得出现非背景像素（PAD_R 已放大）；
- 成交量标题单独占 16px 标题带，不与量柱重叠；
- 图例一律左对齐到绘图区左上角，MA 图例按文本实测宽度顺延、溢出换行；
- 分时图以**昨收为中枢**：昨收水平虚线画在图幅中部，上下按百分比对称取值域；
- 价格刻度小数位统一；蜡烛/量柱坐标全部取整，绘制走 2× 超采样再降采样；
- **渲染层不出现任何业务常量**：涨跌幅比例一律由调用方经 `limit_pct` 传入
  （见 mistakes.md「render/chart.py 硬编码 10% 涨跌停参考线」）。
"""

from __future__ import annotations

from pathlib import Path

W, H = 900, 520
SS = 2                              # 超采样倍数（模块级：helper 也要用）
PAD_L, PAD_R, PAD_B = 22, 108, 30   # PAD_R: 给右侧刻度留出独立栏位
PAD_T = 74                          # 标题带 + 图例带之下的绘图区起点

# ── 深色主题色板（2026-10-21 提亮：原色板在群里缩图后偏暗、层次糊）──────
BG          = (28, 36, 56)      # 画布深蓝（比旧色板亮一档）
PANEL       = (38, 48, 70)      # 区块微差底色
GRID        = (56, 70, 98)      # 网格线（能看见但不抢戏）
AXIS        = (104, 122, 152)   # 坐标轴 / 成交量基线
TEXT        = (238, 243, 252)   # 主文字（近白）
MUTED       = (170, 184, 208)   # 刻度 / 次级文字（冷灰蓝，已提亮）
HILITE      = (255, 255, 255)   # 最新价高亮描边
UP          = (248, 78, 78)     # 红涨（提亮，缩图仍可辨）
DOWN        = (24, 200, 140)    # 绿跌（提亮）
PREV_CLOSE  = (176, 190, 214)   # 昨收虚线
LIMIT_UP    = UP                # 涨停参考线：红
LIMIT_DOWN  = DOWN              # 跌停参考线：绿
AVG_LINE    = (250, 176, 40)    # 分时均价线：琥珀
# 强势微差：以昨收/前收盘为基准判定「强」，实体色在 UP/DOWN 与更亮的
# UP_BRIGHT/DOWN_BRIGHT 之间切换。只动颜色强度，不引入任何价格/涨跌幅常量
# （涨跌幅比例仍由 limit_pct 参数传入，见 mistakes.md「硬编码 10% 涨跌停」）。
UP_BRIGHT   = (255, 130, 130)   # 强阳：在 UP 上提亮
DOWN_BRIGHT = (74, 226, 168)    # 强阴：在 DOWN 上提亮
MA_COLORS = {5: (250, 176, 40), 10: (86, 170, 255), 20: (186, 120, 255),
             30: (176, 190, 214), 60: (139, 152, 176)}

# 字号整体放大：群里图片会被缩放显示，原 10~13px 正文缩完看不清。
FONT_SCALE = 1.28

# 标题带 / 图例带 / 绘图区的纵向分界（1x 像素）
TITLE_BASE  = 32               # 标题基线
SEP_Y       = 44               # 标题带分隔线
LEGEND_Y    = 60               # 图例文字与示例线的垂直中心
PLOT_TOP    = PAD_T            # 价格区顶（=74，图例带之下）
VOL_TITLE_H = 20               # 成交量标题带高度

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
    size = max(8, int(round(size * FONT_SCALE)))
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


def _decimals(span: float) -> int:
    """按价格跨度选统一小数位：跨度小就给更多位，保持刻度可读。"""
    if span <= 0.5:
        return 4
    if span <= 5:
        return 3
    return 2


def _nice_scale(lo: float, hi: float, ticks: int = 5) -> tuple[float, float, float]:
    """好看的刻度：范围向外扩张并取整。返回 (lo, hi, step)。

    先按 1/2/5×10^n 取整步长，再把 lo/hi 对齐到步长整数倍，使 5 条刻度线落在
    同小数位的整数刻度上（避免出现 3.9876 这种逼死强迫症的刻度值）。
    """
    if hi - lo < 1e-9:
        hi, lo = hi * 1.001, lo * 0.999
    raw = (hi - lo) / (ticks - 1)
    exp = 0
    while raw >= 10 ** (exp + 1):
        exp += 1
    while raw < 10 ** exp:
        exp -= 1
    step = min([10 ** exp * m for m in (1, 2, 5)],
               key=lambda s: abs(s - raw))
    lo = (lo / step).__floor__() * step
    hi = (hi / step).__ceil__() * step
    return lo, hi, step


def _ticks(lo: float, hi: float, step: float, count: int = 5) -> list[float]:
    """取 count 条含首尾的刻度值，末位吸附到 hi 避免浮点残差。"""
    vals = [lo + (hi - lo) * k / (count - 1) for k in range(count)]
    vals[-1] = hi
    return vals


def _hybrid(a: tuple, b: tuple, t: float) -> tuple:
    """两色线性插值；t=0 → a，t=1 → b。用于渐变面积与胶囊描边。"""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _pill(d, box, fill) -> None:
    """圆角矩形胶囊；Pillow <8.2 无 rounded_rectangle 时退化为直角矩形。"""
    try:
        d.rounded_rectangle(box, radius=int((box[3] - box[1]) / 2), fill=fill)
    except AttributeError:      # pragma: no cover - 老版本 Pillow
        d.rectangle(box, fill=fill)


def _badge(d, x: float, y: float, label: str, fg: tuple, fnt) -> float:
    """标题右侧胶囊徽标，右端点在 x。返回胶囊左端点以便布局。

    y 为胶囊垂直中心。胶囊底色 = 同色系低透明度观感（用深底提亮近似），
    文字用 fg。1px 描边取深底与徽标色 45% 混合，让胶囊边缘从背景里浮起来。
    宽度按实测文本 + 左右内边距 12px。
    """
    tw = d.textlength(label, font=fnt)
    bw = tw + 24
    bh = 22
    box = [x - bw, y - bh / 2, x, y + bh / 2]
    _pill(d, box, _hybrid(BG, fg, 0.16))
    # 1px 描边（更粗的 rounded_rectangle 描边在 Pillow <8.2 上不保证线宽为 1）
    try:
        d.rounded_rectangle(box, radius=int((box[3] - box[1]) / 2),
                            outline=_hybrid(BG, fg, 0.45))
    except AttributeError:      # pragma: no cover - 老版本 Pillow
        d.rectangle(box, outline=_hybrid(BG, fg, 0.45))
    d.text((x - bw / 2 - tw / 2, y - 8), label, font=fnt, fill=fg,
           anchor="la")
    return x - bw


def _right_label(d, x_right: float, y_c: float, text: str, font,
                 fill: tuple) -> None:
    """绘图区右侧的刻度/参考线标签（2× 坐标）。

    统一走这里的原因：早期实现把「最终像素」的 x 与「2× 画布」的 y 混在一起
    （``d.text((x1 - tw / SS, yy / SS - 6), ...)``），结果刻度被整体压到上半
    幅、贴住蜡烛右缘还与标题带打架。现在一律左对齐贴绘图区右缘 + 8px、
    垂直居中于目标 y，既不出画布也不压图。
    """
    d.text((x_right + 8 * SS, y_c), text, font=font, fill=fill, anchor="lm")


def _label_column(d, x_right: float, labels: list, font, min_gap: float = 0) -> None:
    """把一组右侧标签按 y 排好并防止互相压字（2× 坐标）。

    labels = [(y, text, fill)]。价格域很宽或参考线簇在一起时（例如分时图上
    涨停/昨收/跌停与某条刻度贴得很近），固定间距会让标签叠成一坨；这里按
    顺序把后面的标签往下推至少 min_gap，保证每行都能读。
    """
    last = None
    for y, text, fill in sorted(labels, key=lambda it: it[0]):
        yy = y if last is None else max(y, last + min_gap)
        last = yy
        d.text((x_right + 8 * SS, yy), text, font=font, fill=fill, anchor="lm")


def _canvas():
    """新建 2× 超采样画布并打通调色盘。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W * 2, H * 2), BG)
    return img, ImageDraw.Draw(img)


def _gradient_band(d, pts, color: tuple, top: float, bot: float) -> None:
    """价格折线下方的垂直渐变面积带：顶部 color → 中部 55% → 底部透明（BG）。

    pts 为折线采样点（已取整）。自上而下画若干 2px 高的横条，逐条把 color
    淡向 BG，得到"到底部消失"的渐变观感。比 RGBA 图层合成省一次全图扫描。

    多段渐变：两条曲线在 55% 高处交叉——上半段（t<0.55）比单一线性渐变
    **更慢**地淡出（t^1.6），保住线条附近的色浓度；下半段更快沉入背景。
    这让面积带出现"顶部实、中部过渡、底部化开"的层次，而不是一条直愣的斜坡。
    """
    if len(pts) < 2:
        return
    if bot <= top:
        return
    # 求出每个 x 列的折线 y（用相邻点线性插值，避免采样点之间出现空白）
    x_lo, x_hi = pts[0][0], pts[-1][0]
    segs = []                      # (x0, x1, y0, y1) 每个采样段
    for i in range(len(pts) - 1):
        (xa, ya), (xb, yb) = pts[i], pts[i + 1]
        segs.append((xa, xb, ya, yb))

    def line_y(x: float) -> float:
        for xa, xb, ya, yb in segs:
            if xa <= x <= xb and xb > xa:
                return ya + (yb - ya) * (x - xa) / (xb - xa)
        return pts[-1][1]

    steps = max(1, int((bot - top) / 2))
    for k in range(steps):
        y0 = top + k * 2
        t = k / steps              # 0 顶部 → 1 底部
        # 多段渐变：t<0.55 用 t^1.6（慢淡出），t>=0.55 线性插值到 BG
        a = (t / 0.55) ** 1.6 if t < 0.55 else 1.0
        b = (t - 0.55) / 0.45 if t >= 0.55 else 0.0
        mix = min(1.0, a + b)
        col = _hybrid(color, BG, mix)
        xs = list(range(x_lo, x_hi + 1, 2))
        for x in xs:
            ly = line_y(x)
            if ly < y0:
                d.line([(x, y0), (x + 2, y0)], fill=col)
    # 面积带底部用最低折线点封口，保证与横轴之间无条纹
    d.line(pts, fill=color, width=2 * 2)


def render_kline(out_path: Path, title: str, bars: list, ma_windows=(5, 10, 20),
                 volume: bool = True) -> Path | None:
    """日 K：蜡烛 + 均线 + 成交量。bars 需含 open/high/low/close/volume。"""
    if not bars:
        return None
    try:
        from PIL import Image, ImageDraw
        from PIL.Image import Resampling
    except Exception:
        return None

    opens, highs, lows, closes, vols = _series(bars)
    n = len(bars)

    SS = 2
    img, d = _canvas()
    f_title, f_lab, f_mid = _font(15 * SS), _font(11 * SS), _font(12 * SS)

    vol_h = int(H * SS * 0.20) if volume else 0
    price_top = PLOT_TOP * SS
    price_bot = H * SS - PAD_B * SS - vol_h - 10 * SS
    lo, hi = min(lows), max(highs)
    lo, hi, step = _nice_scale(lo, hi, ticks=5)
    dec = _decimals(hi - lo)

    def yv(p):
        return price_bot - (p - lo) / (hi - lo) * (price_bot - price_top)

    x0, x1 = (PAD_L + 10) * SS, (W - PAD_R) * SS
    x1 = max(x1, x0 + 1)
    step_x = (x1 - x0) / max(n, 1)
    body_w = max(1.5 * SS, step_x * 0.62)

    def xv(i):
        return x0 + step_x * (i + 0.5)

    # 成交量区微差底色（先画，网格/蜡烛覆盖其上）
    if vol_h:
        vtop, vbot = H * SS - PAD_B * SS - vol_h, H * SS - PAD_B * SS
        d.rectangle([0, vtop, W * SS, vbot], fill=PANEL)

    # 网格 + 右侧价格刻度（统一标签列，见 _label_column）
    labels = []
    for k, val in enumerate(_ticks(lo, hi, step, 5)):
        yy = round(price_top + (price_bot - price_top) * k / 4)
        d.line([(x0, yy), (x1, yy)], fill=GRID)
        labels.append((yy, f"{val:.{dec}f}", MUTED))
    d.line([(x0, price_top), (x1, price_top)], fill=_hybrid(GRID, TEXT, 0.12),
           width=SS)
    d.line([(x0, price_bot), (x1, price_bot)], fill=_hybrid(GRID, TEXT, 0.12),
           width=SS)

    # 蜡烛：影线 + 圆角实体（坐标先聚合到最终像素再画，避免半像素发虚）
    for i in range(n):
        base = closes[i - 1] if i else opens[i]
        strong = closes[i] >= opens[i] and closes[i] > base
        color = (UP_BRIGHT if strong else UP) if closes[i] >= opens[i] \
            else (DOWN_BRIGHT if closes[i] < base else DOWN)
        x = round(xv(i))
        d.line([(x, round(yv(highs[i]))), (x, round(yv(lows[i])))],
               fill=color, width=SS)
        o, c = yv(opens[i]), yv(closes[i])
        top, bot = min(o, c), max(o, c)
        if bot - top < 1.0 * SS:
            bot = top + 1.0 * SS
        d.rounded_rectangle(
            [round(x - body_w / 2), round(top),
             round(x + body_w / 2), round(bot)],
            radius=max(1, round(min(body_w, bot - top) / 2)),
            fill=color)

    # 均线 + 图例（左对齐绘图区，按实测文本宽度顺延，溢出换行）
    legend_x = x0 + 6 * SS
    for w in ma_windows:
        if n < w:
            continue
        col = MA_COLORS.get(w, (150, 150, 150))
        pts = [(round(xv(i)), round(yv(sum(closes[i - w + 1:i + 1]) / w)))
               for i in range(w - 1, n)]
        d.line(pts, fill=col, width=SS)
        text = f"MA{w}"
        tw = d.textlength(text, font=f_lab)
        # 图例色块 = 12px 小圆点 + 16px 直线，比单一直线更精致
        dot_r = 3 * SS
        need = (12 + 16) * SS + tw + 14 * SS
        if legend_x + need > x1:
            legend_x = x0 + 6 * SS
        cy = LEGEND_Y * SS
        d.ellipse([legend_x - dot_r, cy - dot_r,
                   legend_x + dot_r, cy + dot_r], fill=col)
        d.line([(legend_x + 6 * SS, cy),
                (legend_x + (12 + 16) * SS, cy)], fill=col, width=2 * SS)
        d.text((legend_x + (12 + 16 + 8) * SS, cy), text,
               font=f_lab, fill=col, anchor="lm")
        legend_x += need

    # 成交量
    if vol_h:
        vmax = max(vols) or 1
        d.line([(x0, vbot), (x1, vbot)], fill=AXIS, width=SS)
        for i in range(n):
            h = vols[i] / vmax * (vbot - vtop - 16 * SS)
            # 量柱双色观感：暗色实底 + 顶部 2px 亮条（深底下有体积感）
            color = UP_BRIGHT if closes[i] >= opens[i] else DOWN_BRIGHT
            dim = _hybrid(color, BG, 0.55)
            x = round(xv(i))
            bw = max(1, round(body_w))
            yhi, ylo = round(vbot - h), round(vbot)
            d.rectangle([x - bw // 2, yhi, x + bw // 2, ylo], fill=dim)
            if ylo - yhi >= 4 * SS:
                d.rectangle([x - bw // 2, yhi, x + bw // 2,
                             yhi + 2 * SS], fill=color)
        # 成交量标题放在成交量区内（vtop 下方 2px），而非价格区
        d.text((x0 + 2 * SS, vtop + 2 * SS),
               f"成交量（峰值 {vmax:,}）", font=f_lab, fill=MUTED, anchor="la")

    # 标题 + 最新价胶囊徽标（日 K 无涨跌幅概念，只挂「最新」价签）
    last = closes[-1]
    prev = closes[-2] if n > 1 else closes[-1]
    up = last >= prev
    badge_col = UP if up else DOWN
    pct = (last - prev) / prev * 100 if prev else 0.0
    badge = f"{last:.2f}  {pct:+.2f}%"
    fnt = f_mid
    tw = d.textlength(badge, font=fnt)
    # 右端缩进 12*SS：让胶囊右缘远离图幅右缘，右缘 5 列保持纯背景
    bx = (W - 12) * SS
    by = 24 * SS
    _badge(d, bx, by, badge, badge_col, fnt)
    # 标题带底部分隔线
    d.line([(PAD_L * SS, SEP_Y * SS), (W * SS - PAD_R * SS, SEP_Y * SS)],
           fill=GRID, width=SS)
    d.text((PAD_L * SS, TITLE_BASE * SS), title, font=f_title, fill=TEXT,
           anchor="lb")
    # 右侧刻度统一在最后画，保证压在网格与蜡烛之上、且彼此不叠字
    _label_column(d, x1, labels, f_lab, min_gap=18 * FONT_SCALE * SS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = img.resize((W, H), Resampling.LANCZOS)
    img.save(out_path, format="PNG")
    return out_path


def render_intraday(out_path: Path, title: str, bars: list,
                    volume: bool = True,
                    prev_close: float | None = None,
                    limit_pct: float | None = None) -> Path | None:
    """分时：价格折线 + 均价线 + 成交量。bars 需含 close/volume。

    prev_close 为真实昨收价；传 None 时退回 closes[0]（兼容旧调用），
    并在 docstring 记录该降级行为。昨收非 None 时额外绘制昨收虚线、
    涨跌停参考线与最新价高亮点，价格域以昨收为中枢按百分比对称展开。

    limit_pct 为该股票**真实**涨跌幅比例（如主板 0.10 / 创业板 0.20 /
    北交所 0.30 / ST 0.05，以及新股首日 IPO 覆盖值）；由调用方取自
    ``Quote.limit_pct_of(stock)``。传 None 时退回主板口径 ±10%，
    保证既有调用方（如 chart_check.py）行为不变。
    """
    if not bars:
        return None
    try:
        from PIL import Image, ImageDraw
        from PIL.Image import Resampling
    except Exception:
        return None

    opens, highs, lows, closes, vols = _series(bars)
    n = len(bars)

    SS = 2
    img, d = _canvas()
    f_title, f_lab, f_mid = _font(15 * SS), _font(11 * SS), _font(12 * SS)

    vol_h = int(H * SS * 0.20) if volume else 0
    top = PLOT_TOP * SS
    bot = H * SS - PAD_B * SS - vol_h - 10 * SS

    # 昨收：None 时退回 closes[0]
    pc = closes[0] if prev_close is None else float(prev_close)

    # 取值域：以昨收为中枢，上下按 (最大偏移/昨收) 的百分比对称展开
    offsets = [abs(highs[i] - pc) / max(pc, 1e-9) for i in range(n)]
    offsets += [abs(lows[i] - pc) / max(pc, 1e-9) for i in range(n)]
    pct = max(offsets) * 1.12 if offsets else 0.01
    pct = max(pct, 0.001)
    lo, hi = pc * (1 - pct), pc * (1 + pct)

    def yv(p):
        return bot - (p - lo) / (hi - lo) * (bot - top)

    x0, x1 = (PAD_L + 10) * SS, (W - PAD_R) * SS
    x1 = max(x1, x0 + 1)
    step_x = (x1 - x0) / max(n, 1)

    def xv(i):
        return x0 + step_x * (i + 0.5)

    # 成交量区微差底色
    if vol_h:
        vtop, vbot = H * SS - PAD_B * SS - vol_h, H * SS - PAD_B * SS
        d.rectangle([0, vtop, W * SS, vbot], fill=PANEL)

    # 网格 + 右侧价格刻度（统一走标签列，最后绘制）
    labels = []
    for k in range(5):
        yy = round(top + (bot - top) * k / 4)
        val = hi - (hi - lo) * k / 4
        d.line([(x0, yy), (x1, yy)], fill=GRID)
        dec = _decimals(hi - lo)
        labels.append((yy, f"{val:.{dec}f}", MUTED))

    def _dashed_line(y_s: float, color):
        """水平虚线：从 x0 到 x1，样式 4px 实 / 4px 虚。"""
        y = round(y_s)
        dash, gap = 4 * SS, 4 * SS
        x = x0
        while x < x1:
            xe = min(x + dash, x1)
            d.line([(x, y), (xe, y)], fill=color, width=SS)
            x = xe + gap

    # 涨跌停参考线（用传入的 limit_pct；None 时退回主板口径 ±10%）
    lp = 0.10 if limit_pct is None else float(limit_pct)
    lu = round(pc * (1 + lp), 2)
    ld = round(pc * (1 - lp), 2)
    for val, col, name in ((lu, LIMIT_UP, "涨停"), (ld, LIMIT_DOWN, "跌停")):
        if lo <= val <= hi:
            _dashed_line(yv(val), col)
            labels.append((yv(val), f"{name} {val:.2f}", col))

    # 昨收水平虚线（画在图幅中部：域以昨收为中枢，昨收即中线）
    _dashed_line(yv(pc), PREV_CLOSE)
    labels.append((yv(pc), f"昨收 {pc:.2f}", PREV_CLOSE))

    # 价格折线（相对昨收着色：红涨绿跌）+ 渐变面积填充
    color = UP if closes[-1] >= pc else DOWN
    pts = [(round(xv(i)), round(yv(closes[i]))) for i in range(n)]
    _gradient_band(d, pts, color, top=min(p[1] for p in pts), bot=bot)
    # 最新价：高亮点 + 左上角价格签（画在图内，避免贴右缘与刻度打架）
    lx, ly = pts[-1]
    r = 3 * SS
    d.ellipse([lx - r, ly - r, lx + r, ly + r], fill=color,
              outline=HILITE, width=SS)
    d.text((lx - 9 * SS, ly - 12 * SS), f"{closes[-1]:.2f}",
           font=f_lab, fill=color, anchor="rm")

    # 均价线
    avg = [sum(closes[:i + 1]) / (i + 1) for i in range(n)]
    d.line([(round(xv(i)), round(yv(avg[i]))) for i in range(n)],
           fill=AVG_LINE, width=SS)

    # 成交量
    if vol_h:
        vmax = max(vols) or 1
        d.line([(x0, vbot), (x1, vbot)], fill=AXIS, width=SS)
        bw = max(1.0 * SS, step_x * 0.8)
        for i in range(n):
            h = vols[i] / vmax * (vbot - vtop - 16 * SS)
            c = UP if closes[i] >= (closes[i - 1] if i else closes[0]) else DOWN
            x = round(xv(i))
            bwr = max(1, round(bw))
            d.rectangle([x - bwr // 2, round(vbot - h),
                         x + bwr // 2, round(vbot)], fill=c)
        # 成交量标题放在成交量区内顶部，而非价格区
        d.text((x0 + 2 * SS, vtop + 2 * SS),
               f"成交量（峰值 {vmax:,}）", font=f_lab, fill=MUTED, anchor="la")

    # 标题 + 最新价/涨跌幅胶囊徽标
    d.line([(PAD_L * SS, SEP_Y * SS), (W * SS - PAD_R * SS, SEP_Y * SS)],
           fill=GRID, width=SS)
    d.text((PAD_L * SS, TITLE_BASE * SS), title, font=f_title, fill=TEXT,
           anchor="lb")
    change_pct = (closes[-1] - pc) / pc * 100 if pc else 0.0
    badge = f"{closes[-1]:.2f}  {change_pct:+.2f}%"
    tw = d.textlength(badge, font=f_mid)
    # 右端缩进：胶囊右缘 + 1px 描边 + 抗锯齿余量，右缘 5 列保持纯背景。
    bx = (W - 12) * SS
    by = 24 * SS
    bcol = UP if change_pct >= 0 else DOWN
    _badge(d, bx, by, badge, bcol, f_mid)

    # 图例：左对齐绘图区左上角，顺延排列（宽度不足则换行）
    legend_x = x0 + 6 * SS
    for col, text in ((color, "价格"), (AVG_LINE, "均价"),
                      (PREV_CLOSE, "昨收")):
        tw = d.textlength(text, font=f_lab)
        need = 18 * SS + tw + 16 * SS
        if legend_x + need > x1:
            legend_x = x0 + 6 * SS
        cy = LEGEND_Y * SS
        d.line([(legend_x, cy), (legend_x + 18 * SS, cy)], fill=col,
               width=2 * SS)
        d.text((legend_x + 22 * SS, cy), text, font=f_lab,
               fill=TEXT, anchor="lm")
        legend_x += need

    # 右侧标签最后画（刻度 + 涨跌停 + 昨收），按 y 排序并防叠字
    _label_column(d, x1, labels, f_lab, min_gap=18 * FONT_SCALE * SS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = img.resize((W, H), Resampling.LANCZOS)
    img.save(out_path, format="PNG")
    return out_path
