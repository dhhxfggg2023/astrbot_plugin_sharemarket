"""五类信息卡片渲染（Pillow）：排行榜 / 龙虎榜 / 资金流 / 异动榜 / 道具商店。

与 chart.py 共用同一深色金融终端主题：色板、字体、胶囊样式直接从
``render.chart`` 导入（单一事实源，避免两处漂移）。缺 Pillow 时所有
``render_*`` 返回 ``None``，由调用方回退文本——聊天里永远有输出
（与 chart.py 同一契约）。

渲染层不含业务常量：
- 榜单阈值 / 排序 / 条数全部来自调用方传入的结构化数据
  （game/flow.py、game/leaderboard.py、game/props.py 的输出），本模块只画；
- 红涨绿跌沿用中国市场惯例；异动类型按类型关键字上色，是呈现语义不是行情逻辑；
- 展示条数上限（max_stocks/max_rows）是**排版**上限，分页由调用方负责。

卡片布局（1x 像素约定，绘制时 ×SS 超采样，落盘前 LANCZOS 降采样）：
- W=900 定宽，PAD=16 左右内边距，SS=2 超采样；
- 标题带 46px：左标题（行首 emoji 用彩色 emoji 字体画）+ 右胶囊徽标 + GRID 分隔线；
- 表头 26px（PANEL 底 + MUTED 字），数据行 30~36px，斑马纹低对比。

与 card-eng-1（t2，行情/个股/盘口/持仓/资金）共用本文件：helpers 名称
``_canvas/_title_band/_head_row/_fit_text/_fmt_amt/_val/_chip/_save``，
追加函数时请复用、勿重复定义、勿整文件覆盖。
"""

from __future__ import annotations

from pathlib import Path

from .chart import (BG, PANEL, GRID, AXIS, TEXT, MUTED, UP, DOWN,
                    _font, _pill, _hybrid, available)

# ── 布局常量（1x 像素）────────────────────────────────────────
SS = 2            # 超采样倍数（同 chart.py）
W = 900           # 卡片定宽
PAD = 16          # 左右内边距
TITLE_H = 46      # 标题带高度
HEAD_H = 26       # 表头行高

# 次级色板（chart.py 主色之外的卡片点缀色）
GOLD = (255, 197, 61)      # 金牌名次
SILVER = (203, 213, 225)   # 银牌名次
BRONZE = (217, 119, 6)     # 铜牌名次
AMBER = (245, 158, 11)     # 价格 / NPC 标记
BLUE = (56, 152, 255)      # 振幅异动 / 持有数量
ZEBRA = _hybrid(BG, PANEL, 0.6)   # 斑马纹（BG → PANEL 之间）

# 彩色 emoji 字体（🤖 🏆 等非 BMP 字形；缺字体时标题里丢弃该字符、
# NPC 行退化为纯 NPC 胶囊，绝不画豆腐块）
EMOJI_FONTS = [
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]


# ── 通用 helpers（与 t2 共用）──────────────────────────────────

def _emoji_font(size: int):
    """彩色 emoji 字体；不可用返回 None（调用方走降级路径）。"""
    try:
        from PIL import ImageFont
    except Exception:
        return None
    for path in EMOJI_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _canvas(w: int, h: int):
    """新建 2× 超采样画布（同 chart._canvas，尺寸参数化）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w * SS, h * SS), BG)
    return img, ImageDraw.Draw(img)


def _val(obj, key, default=None):
    """字段读取：dict 与 dataclass/对象统一，缺字段不抛（比 chart._get 宽容）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fmt_amt(v) -> str:
    """金额展示：亿 / 万 / 千分位（负号随数值保留）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:,.0f}"


def _signed_amt(v) -> str:
    """带显式正号的金额（净流入 / 净买入列用）。"""
    s = _fmt_amt(v)
    return s if s.startswith("-") else "+" + s


def _pct(v) -> str:
    """带符号百分比（同文本榜口径 {v:+.2f}%）。"""
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def _fit_text(d, text, font, max_w: float) -> str:
    """按实测宽度截断并加省略号（宽度内原样返回）。"""
    s = "" if text is None else str(text)
    if d.textlength(s, font=font) <= max_w:
        return s
    while s and d.textlength(s + "…", font=font) > max_w:
        s = s[:-1]
    return s + "…" if s else ""


def _chip(d, y_c, label, fg, fnt, right=None, left=None, h=32) -> float:
    """胶囊徽标（2× 坐标）。right / left 二选一定锚，返回**另一侧**边缘 x。

    底色 = 同色系深底提亮近似（hybrid(BG, fg, 0.16)），文字用 fg，
    1px 描边取 45% 混合——同 chart._badge 观感，但支持左右锚点。
    """
    tw = d.textlength(label, font=fnt)
    bw = tw + 24
    if right is None:
        right = (left or 0) + bw
        edge = right              # 左锚 → 返回**右**缘（供横向排版续画）
    else:
        edge = right - bw         # 右锚 → 返回**左**缘（标题带定标题宽度）
    box = [right - bw, y_c - h / 2, right, y_c + h / 2]
    _pill(d, box, _hybrid(BG, fg, 0.16))
    try:
        d.rounded_rectangle(box, radius=int(h / 2),
                            outline=_hybrid(BG, fg, 0.45))
    except AttributeError:      # pragma: no cover - 老版本 Pillow
        d.rectangle(box, outline=_hybrid(BG, fg, 0.45))
    d.text((right - bw / 2, y_c), label, font=fnt, fill=fg, anchor="mm")
    return edge


def _emoji_cp(cp: int) -> bool:
    """行首「emoji 运行段」判定：非 BMP emoji + 常见 BMP 符号区
    （⚠️ U+26A0 在 BMP，漏掉会被 msyh 画成豆腐块）+ 变体选择符 U+FE0F。"""
    return (cp > 0xFFFF or cp == 0xFE0F
            or 0x2600 <= cp <= 0x26FF or 0x2700 <= cp <= 0x27BF)


def _title_text(d, x, y_c, title, fnt, max_w) -> None:
    """标题文字：行首 emoji 运行段改用彩色 emoji 字体绘制（⚠️/🏆/🐉…）；
    emoji 字体不可用时**丢弃**该字符（不画豆腐块），其余文字照常截断绘制。

    x / y_c 为 2× 坐标；fnt.size 亦为 2× 像素（_font(size*SS) 传入）。
    U+FE0F 只跟随前一字符，不单独绘制。
    """
    s = "" if title is None else str(title)
    size = int(getattr(fnt, "size", 16) or 16)
    while s and _emoji_cp(ord(s[0])):
        ch, s = s[0], s[1:]
        if ord(ch) == 0xFE0F:
            continue
        ef = _emoji_font(size)
        if ef is not None:
            d.text((x + size * 0.65, y_c), ch, font=ef, anchor="mm")
            x += size * 1.35
    if s:
        d.text((x, y_c), _fit_text(d, s, fnt, max_w), font=fnt, fill=TEXT,
               anchor="lm")


def _title_band(d, title, badge=None, badge_col=None) -> None:
    """标题带（46px @1x）：左标题 + 右胶囊徽标（可选）+ 底部 GRID 分隔线。"""
    badge_left = (W - PAD) * SS
    if badge:
        badge_left = _chip(d, 21 * SS, badge, badge_col or MUTED,
                           _font(11 * SS), right=(W - PAD) * SS, h=36)
    _title_text(d, PAD * SS, 21 * SS, title, _font(16 * SS),
                badge_left - PAD * SS - 14 * SS)
    d.line([(PAD * SS, (TITLE_H - 2) * SS),
            ((W - PAD) * SS, (TITLE_H - 2) * SS)], fill=GRID, width=SS)


def _head_row(d, y0, cols) -> None:
    """表头行：整幅 PANEL 带 + MUTED 小字。cols = [(label, x, kind)]，
    kind: "l" 左对齐 / "r" 右对齐 / "c" 居中（x 均为 1x 像素）。"""
    d.rectangle([0, y0 * SS, W * SS, (y0 + HEAD_H) * SS], fill=PANEL)
    cy = (y0 + HEAD_H / 2) * SS
    f = _font(11 * SS)
    anchor = {"l": "lm", "r": "rm", "c": "mm"}
    for label, x, kind in cols:
        d.text((x * SS, cy), label, font=f, fill=MUTED, anchor=anchor[kind])


def _row_bg(d, y0, h, stripe: bool) -> None:
    """数据行底：斑马纹 + 行底 GRID 分隔线（y0/h 为 1x 像素）。"""
    if stripe:
        d.rectangle([0, y0 * SS, W * SS, (y0 + h) * SS], fill=ZEBRA)
    d.line([(0, (y0 + h) * SS), (W * SS, (y0 + h) * SS)], fill=GRID, width=SS)


def _save(img, out_path) -> Path:
    """降采样 + 落盘（父目录自动创建），返回最终路径。"""
    from PIL.Image import Resampling
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, img.height // SS), Resampling.LANCZOS).save(
        out_path, format="PNG")
    return out_path


def _npc_mark(d, x, y_c) -> float:
    """🤖 NPC 标记：有彩色 emoji 字体先画 🤖，随后**恒画** NPC 胶囊——
    emoji 缺字体/缺字形时标记仍然可读。返回右缘 x（2× 坐标）。"""
    ef = _emoji_font(13 * SS)
    if ef is not None:
        d.text((x + 10 * SS, y_c), "🤖", font=ef, anchor="mm")
        x += 20 * SS + d.textlength("🤖", font=ef)
    return _chip(d, y_c, "NPC", AMBER, _font(10 * SS), left=x, h=26)


def _save_note(d, y, text) -> None:
    """底部备注（截断提示等），MUTED 小字。"""
    d.text((PAD * SS, y * SS), text, font=_font(10 * SS), fill=MUTED,
           anchor="lm")


HINT_H = 34       # 底部操作提示条高度（1x）


def _hint(d, y, text) -> None:
    """底部操作提示条：PANEL 底 + 顶部 GRID 分隔 + MUTED 小字。

    每张卡片都以「下一步能发什么指令」收尾——玩家看完图不用回想帮助。
    """
    d.rectangle([0, y * SS, W * SS, (y + HINT_H) * SS], fill=PANEL)
    d.line([(0, y * SS), (W * SS, y * SS)], fill=GRID, width=SS)
    d.text((PAD * SS, (y + HINT_H / 2) * SS),
           _fit_text(d, text, _font(10 * SS), (W - 2 * PAD) * SS),
           font=_font(10 * SS), fill=MUTED, anchor="lm")


def _sep(d, y, gap: int = 8) -> int:
    """区块分隔线，返回下一区块的起始 y（1x）。"""
    d.line([(PAD * SS, y * SS), ((W - PAD) * SS, y * SS)],
           fill=GRID, width=SS)
    return y + gap


# ── ① 排行榜 ──────────────────────────────────────────────────

def render_leaderboard(out_path, entries, title) -> Path | None:
    """排行榜卡片：名次 / 名称（NPC 带 🤖+NPC 标记）/ 收益率 / 总资产。

    entries 与 ``LeaderboardService.top()/period_top()`` 的 dict 同形：
    name / total / return_pct / status / rebirth / is_npc（缺字段按 None 处理）。
    前三名金/银/铜名次胶囊；破产行整体降灰、收益率列显示「已破产」。
    空榜返回 None（调用方回退「还没有玩家开户」文本）。
    """
    if not entries:
        return None
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None

    ROW_H = 36
    n = len(entries)
    H = TITLE_H + HEAD_H + ROW_H * n + 6
    img, d = _canvas(W, H)
    f_row, f_num, f_sub = _font(13 * SS), _font(12 * SS), _font(10 * SS)

    npc_n = sum(1 for e in entries if _val(e, "is_npc"))
    _title_band(d, title, f"{n} 人" + (f" · NPC×{npc_n}" if npc_n else ""))
    y = TITLE_H
    _head_row(d, y, [("名次", 42, "c"), ("名称", 78, "l"),
                     ("收益率", 640, "r"), ("总资产", W - PAD, "r")])
    y += HEAD_H

    medals = (GOLD, SILVER, BRONZE)
    for i, e in enumerate(entries):
        _row_bg(d, y, ROW_H, i % 2 == 1)
        cy = (y + ROW_H / 2) * SS
        bankrupt = _val(e, "status") == "bankrupt"

        # 名次：前三名彩色胶囊，其余默数
        if i < 3:
            _chip(d, cy, str(i + 1), medals[i], _font(12 * SS),
                  left=22 * SS, h=30)
        else:
            d.text((42 * SS, cy), str(i + 1), font=f_num, fill=MUTED,
                   anchor="mm")

        # 名称 + 🤖 NPC 标记 + 重生次数
        x = 78 * SS
        name = _fit_text(d, _val(e, "name", ""), f_row, 300 * SS)
        d.text((x, cy), name, font=f_row,
               fill=MUTED if bankrupt else TEXT, anchor="lm")
        x += d.textlength(name, font=f_row) + 8 * SS
        if _val(e, "is_npc"):
            x = _npc_mark(d, x, cy)
        rb = int(_val(e, "rebirth", 0) or 0)
        if rb:
            d.text((x, cy), f"重生{rb}次", font=f_sub, fill=MUTED,
                   anchor="lm")

        # 收益率 / 总资产（破产行降灰）
        if bankrupt:
            d.text((640 * SS, cy), "已破产", font=f_row, fill=MUTED,
                   anchor="rm")
            d.text(((W - PAD) * SS, cy), "—", font=f_row, fill=MUTED,
                   anchor="rm")
        else:
            ret = float(_val(e, "return_pct", 0) or 0)
            d.text((640 * SS, cy), _pct(ret), font=f_row,
                   fill=UP if ret >= 0 else DOWN, anchor="rm")
            d.text(((W - PAD) * SS, cy),
                   f"{float(_val(e, 'total', 0) or 0):,.0f}",
                   font=f_row, fill=TEXT, anchor="rm")
        y += ROW_H
    return _save(img, out_path)


# ── ② 龙虎榜 ──────────────────────────────────────────────────

def _dt_rows(data) -> list[dict]:
    """龙虎榜入参归一化 → [{"code","name","seats"}]。

    支持 ``{code: [Seat,...]}``（game.flow 原样输出）、
    ``[{"code","name","seats"}, ...]``、 ``{"stocks": [...]}`` 三种形状。
    """
    rows: list[dict] = []
    if isinstance(data, dict):
        rich = data.get("stocks") or data.get("items")
        if isinstance(rich, list):
            data = rich          # 落到下面的 list 分支
        else:
            for code, seats in data.items():
                rows.append({"code": code, "name": None, "seats": seats or []})
    if isinstance(data, list):
        for it in data:
            rows.append({"code": _val(it, "code"), "name": _val(it, "name"),
                         "seats": _val(it, "seats") or []})
    return [r for r in rows if r["seats"]]


def render_dragon_tiger(out_path, data, max_stocks: int = 10) -> Path | None:
    """龙虎榜卡片：每只股票一组，席位买卖**横向条形对比**（买红卖绿、净额右列）。

    data 支持 ``{code: [Seat,...]}``（只显示代码）与带股票名的富化形状
    （见 ``_dt_rows``）。组内按 max(买, 卖) 独立缩放，买条在上卖条在下。
    展示上限 max_stocks 只，截断时底部提示分页；空数据返回 None
    （调用方回退「今日暂无龙虎榜数据」文本）。
    """
    rows_all = _dt_rows(data)
    if not rows_all:
        return None
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None

    rows = rows_all[:max(1, int(max_stocks))]
    truncated = len(rows_all) > len(rows)
    SEAT_H, HEAD_BH, GAP_B = 30, 32, 10
    NOTE_H = 20 if truncated else 8
    H = TITLE_H + sum(HEAD_BH + SEAT_H * len(r["seats"]) + GAP_B
                      for r in rows) + NOTE_H
    img, d = _canvas(W, H)
    f_name, f_seat, f_lab = _font(13 * SS), _font(12 * SS), _font(11 * SS)
    _title_band(d, "🐉 龙虎榜（买卖前五席位）", f"{len(rows_all)} 只",
                badge_col=UP)
    y = TITLE_H

    BAR_X0, BAR_X1 = 300, 560     # 席位条形区（1x）
    for r in rows:
        code, seats = str(r["code"]), r["seats"]
        label = f"{r['name']}（{code}）" if r["name"] else code
        tb = sum(float(_val(s, "buy_amount", 0) or 0) for s in seats)
        ts = sum(float(_val(s, "sell_amount", 0) or 0) for s in seats)
        tn = tb - ts

        # 组头：股票标签左、本组合计净买入右
        cy_h = (y + 16) * SS
        d.text((PAD * SS, cy_h), _fit_text(d, label, f_name, 440 * SS),
               font=f_name, fill=TEXT, anchor="lm")
        d.text(((W - PAD) * SS, cy_h), f"合计净买入 {_signed_amt(tn)}",
               font=f_lab, fill=UP if tn >= 0 else DOWN, anchor="rm")
        d.line([(PAD * SS, (y + 31) * SS), ((W - PAD) * SS, (y + 31) * SS)],
               fill=GRID, width=SS)
        y += HEAD_BH

        # 组内缩放：max(买, 卖) → 满条宽（每只股票独立对比口径）
        scale = max(max(float(_val(s, "buy_amount", 0) or 0),
                        float(_val(s, "sell_amount", 0) or 0))
                    for s in seats) or 1
        track = _hybrid(BG, TEXT, 0.06)
        span = (BAR_X1 - BAR_X0) * SS
        for s in seats:
            cy1 = y + SEAT_H / 2
            cy = cy1 * SS
            seat_name = f"{_val(s, 'rank', '')}. {_val(s, 'name', '')}"
            d.text((40 * SS, cy), _fit_text(d, seat_name, f_seat, 250 * SS),
                   font=f_seat, fill=TEXT, anchor="lm")
            buy = float(_val(s, "buy_amount", 0) or 0)
            sell = float(_val(s, "sell_amount", 0) or 0)
            net = float(_val(s, "net_amount", buy - sell) or 0)
            # 轨道（淡）+ 双条：上买红 / 下卖绿
            d.rectangle([BAR_X0 * SS, (cy1 - 8) * SS,
                         BAR_X1 * SS, (cy1 - 3) * SS], fill=track)
            d.rectangle([BAR_X0 * SS, (cy1 + 3) * SS,
                         BAR_X1 * SS, (cy1 + 8) * SS], fill=track)
            d.rectangle([BAR_X0 * SS, (cy1 - 8) * SS,
                         BAR_X0 * SS + span * buy / scale,
                         (cy1 - 3) * SS], fill=UP)
            d.rectangle([BAR_X0 * SS, (cy1 + 3) * SS,
                         BAR_X0 * SS + span * sell / scale,
                         (cy1 + 8) * SS], fill=DOWN)
            # 数值列：买 / 卖 / 净额
            d.text((660 * SS, cy), _fmt_amt(buy), font=f_lab, fill=UP,
                   anchor="rm")
            d.text((760 * SS, cy), _fmt_amt(sell), font=f_lab, fill=DOWN,
                   anchor="rm")
            d.text(((W - PAD) * SS, cy), _signed_amt(net), font=f_lab,
                   fill=UP if net >= 0 else DOWN, anchor="rm")
            d.line([(40 * SS, (y + SEAT_H) * SS),
                    ((W - PAD) * SS, (y + SEAT_H) * SS)], fill=GRID,
                   width=SS)
            y += SEAT_H
        y += GAP_B

    if truncated:
        _save_note(d, y + 10,
                   f"仅展示前 {len(rows)} 只，其余 {len(rows_all) - len(rows)} "
                   f"只请分页查看")
    return _save(img, out_path)


# ── ③ 资金流 ──────────────────────────────────────────────────

def render_fund_flow(out_path, items, max_rows: int = 30) -> Path | None:
    """资金流排行卡片：横向**双向**柱状图，红=净流入、绿=净流出（零轴居中）。

    items 与 ``game.flow.get_fund_flow_ranking()`` 的 FundFlowItem 同形：
    rank / code / name / sector / net_inflow / price / change_pct。
    展示上限 max_rows 条（分页由调用方负责）；空数据返回 None
    （调用方回退「暂无资金流数据」文本）。
    """
    if not items:
        return None
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None

    items = list(items)[:max(1, int(max_rows))]
    ROW_H = 34
    H = TITLE_H + HEAD_H + ROW_H * len(items) + 6
    img, d = _canvas(W, H)
    f_row, f_sub, f_lab = _font(13 * SS), _font(10 * SS), _font(11 * SS)
    ZERO_X = 480                       # 零轴（1x）
    HALF = 190                         # 单侧最大条长（1x）

    _title_band(d, "💰 主力资金净流入榜", f"{len(items)} 只")
    y = TITLE_H
    _head_row(d, y, [("名次", 42, "c"), ("名称", 78, "l"),
                     ("净流出 ←", ZERO_X - 10, "r"),
                     ("→ 净流入", ZERO_X + 10, "l"),
                     ("净额", 790, "r"), ("涨跌幅", W - PAD, "r")])
    y += HEAD_H
    y_top = y

    scale = max(abs(float(_val(it, "net_inflow", 0) or 0)) for it in items) or 1
    for i, it in enumerate(items):
        _row_bg(d, y, ROW_H, i % 2 == 1)
        cy = y + ROW_H / 2
        d.text((42 * SS, cy * SS), str(_val(it, "rank", i + 1) or i + 1),
               font=f_lab, fill=MUTED, anchor="mm")
        d.text((78 * SS, (y + 11) * SS),
               _fit_text(d, _val(it, "name", ""), f_row, 180 * SS),
               font=f_row, fill=TEXT, anchor="lm")
        sub = f"{_val(it, 'code', '')} {_val(it, 'sector', '')}"
        d.text((78 * SS, (y + 26) * SS), _fit_text(d, sub, f_sub, 180 * SS),
               font=f_sub, fill=MUTED, anchor="lm")

        net = float(_val(it, "net_inflow", 0) or 0)
        w = HALF * abs(net) / scale * SS
        x0 = ZERO_X * SS
        if net > 0:
            d.rectangle([x0, (cy - 8) * SS, x0 + w, (cy + 8) * SS], fill=UP)
        elif net < 0:
            d.rectangle([x0 - w, (cy - 8) * SS, x0, (cy + 8) * SS], fill=DOWN)
        else:
            d.rectangle([x0 - SS, (cy - 8) * SS, x0 + SS, (cy + 8) * SS],
                        fill=MUTED)
        d.text((790 * SS, cy * SS), _signed_amt(net), font=f_lab,
               fill=UP if net >= 0 else DOWN, anchor="rm")
        pct = float(_val(it, "change_pct", 0) or 0)
        d.text(((W - PAD) * SS, cy * SS), _pct(pct), font=f_lab,
               fill=UP if pct >= 0 else DOWN, anchor="rm")
        y += ROW_H

    # 零轴最后画（压在斑马纹与柱条之上，标出红绿分界）
    d.line([(ZERO_X * SS, y_top * SS), (ZERO_X * SS, (y - 6) * SS)],
           fill=AXIS, width=SS)
    return _save(img, out_path)


# ── ④ 异动榜 ──────────────────────────────────────────────────

def _type_color(t) -> tuple:
    """异动类型 → 展示色（纯呈现映射：涨红 / 跌绿 / 换手琥珀 / 振幅蓝）。"""
    s = "" if t is None else str(t)
    if "涨" in s:
        return UP
    if "跌" in s:
        return DOWN
    if "换" in s:
        return AMBER
    if "振" in s:
        return BLUE
    return AMBER


def render_anomaly(out_path, items, max_rows: int = 30) -> Path | None:
    """异动榜卡片：名次 / 名称 / 异动类型胶囊 / 数值 / 阈值 / 现价 / 涨跌幅。

    items 与 ``game.flow.get_anomaly_ranking()`` 的 AnomalyItem 同形：
    rank / code / name / sector / anomaly_type / value / threshold /
    price / change_pct。同股多条异动各占一行（上游已按异动程度排序）。
    展示上限 max_rows 条；空数据返回 None（回退「今日暂无异动股票」）。
    """
    if not items:
        return None
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None

    items = list(items)[:max(1, int(max_rows))]
    ROW_H = 36
    H = TITLE_H + HEAD_H + ROW_H * len(items) + 6
    img, d = _canvas(W, H)
    f_row, f_sub, f_lab = _font(13 * SS), _font(10 * SS), _font(11 * SS)

    _title_band(d, "⚠️ 异动榜", f"{len(items)} 条", badge_col=AMBER)
    y = TITLE_H
    _head_row(d, y, [("名次", 42, "c"), ("名称", 78, "l"),
                     ("异动类型", 300, "l"), ("数值", 560, "r"),
                     ("阈值", 668, "r"), ("现价", 776, "r"),
                     ("涨跌幅", W - PAD, "r")])
    y += HEAD_H

    for i, it in enumerate(items):
        _row_bg(d, y, ROW_H, i % 2 == 1)
        cy = y + ROW_H / 2
        d.text((42 * SS, cy * SS), str(_val(it, "rank", i + 1) or i + 1),
               font=f_lab, fill=MUTED, anchor="mm")
        d.text((78 * SS, (y + 12) * SS),
               _fit_text(d, _val(it, "name", ""), f_row, 180 * SS),
               font=f_row, fill=TEXT, anchor="lm")
        sub = f"{_val(it, 'code', '')} {_val(it, 'sector', '')}"
        d.text((78 * SS, (y + 28) * SS), _fit_text(d, sub, f_sub, 180 * SS),
               font=f_sub, fill=MUTED, anchor="lm")

        atype = _val(it, "anomaly_type", "")
        col = _type_color(atype)
        _chip(d, cy * SS, atype, col, f_lab, left=300 * SS, h=30)

        value = float(_val(it, "value", 0) or 0)
        d.text((560 * SS, cy * SS), _pct(value), font=f_lab, fill=col,
               anchor="rm")
        thr = _val(it, "threshold", None)
        if isinstance(thr, (int, float)) and not isinstance(thr, bool):
            thr_s = f"阈值 {float(thr):g}"
        else:
            thr_s = f"阈值 {thr}" if thr is not None else ""
        d.text((668 * SS, cy * SS), thr_s, font=f_lab, fill=MUTED,
               anchor="rm")
        d.text((776 * SS, cy * SS),
               f"{float(_val(it, 'price', 0) or 0):.2f}",
               font=f_lab, fill=TEXT, anchor="rm")
        pct = float(_val(it, "change_pct", 0) or 0)
        d.text(((W - PAD) * SS, cy * SS), _pct(pct), font=f_lab,
               fill=UP if pct >= 0 else DOWN, anchor="rm")
        y += ROW_H
    return _save(img, out_path)


# ── ⑤ 道具商店 ────────────────────────────────────────────────

def _owned_map(owned) -> dict:
    """持有量归一化 → {prop_id: quantity}。接受 list[dict] / dict{pid: qty}。"""
    if not owned:
        return {}
    if isinstance(owned, dict):
        out = {}
        for pid, v in owned.items():
            if isinstance(v, dict):
                v = _val(v, "quantity", 0)
            try:
                out[pid] = int(v or 0)
            except (TypeError, ValueError):
                continue
        return out
    out = {}
    for o in owned:
        pid = _val(o, "prop_id")
        if not pid:
            continue
        try:
            out[pid] = out.get(pid, 0) + int(_val(o, "quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
    return out


def render_props_shop(out_path, shop, owned) -> Path | None:
    """道具商店卡片：今日上架道具网格（名称/价格/描述/限购余量/持有数）+ 我的背包。

    shop 与 ``PropService.get_shop()`` 同形 ``{"ok","day_ts","items":[...]}``
    （或直接传 items 列表）；owned 与 ``get_user_props()`` 的 list 同形
    （或 {prop_id: qty}）。商店为空/None 返回 None（回退文本商店）。
    """
    items = _val(shop, "items") if shop is not None else None
    if items is None and isinstance(shop, list):
        items = shop
    if not items:
        return None
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None

    omap = _owned_map(owned)
    COLS, GAP_C, CARD_H = 2, 16, 104
    card_w = (W - 2 * PAD - GAP_C * (COLS - 1)) // COLS
    grid_rows = (len(items) + COLS - 1) // COLS
    grid_y = TITLE_H + 10
    body_end = grid_y + grid_rows * (CARD_H + GAP_C) - GAP_C

    # 背包区：list 形式的 owned 带显示名；{pid: qty} 形式退回 pid。
    # 固定每行 3 枚胶囊 → 预算高度与绘制布局完全一致。
    names = {}
    if isinstance(owned, list):
        names = {_val(o, "prop_id"): (_val(o, "name") or _val(o, "prop_id"))
                 for o in owned if _val(o, "prop_id")}
    owned_list = [(names.get(pid, pid), q)
                  for pid, q in omap.items() if q > 0]
    per_line = 3
    inv_lines = [owned_list[i:i + per_line]
                 for i in range(0, len(owned_list), per_line)]
    H = body_end + 14
    if inv_lines:
        H += 30 + len(inv_lines) * 38 + 4

    img, d = _canvas(W, H)
    f_name, f_price = _font(13 * SS), _font(11 * SS)
    f_sub, f_lab, f_inv = _font(10 * SS), _font(11 * SS), _font(11 * SS)
    _title_band(d, "🏪 道具商店", f"今日上架 {len(items)} 件")

    # 道具网格
    for idx, it in enumerate(items):
        r, c = divmod(idx, COLS)
        x = PAD + c * (card_w + GAP_C)
        y = grid_y + r * (CARD_H + GAP_C)
        box = [x * SS, y * SS, (x + card_w) * SS, (y + CARD_H) * SS]
        try:
            d.rounded_rectangle(box, radius=8 * SS, fill=PANEL,
                                outline=GRID, width=SS)
        except AttributeError:      # pragma: no cover - 老版本 Pillow
            d.rectangle(box, fill=PANEL, outline=GRID, width=SS)
        qty = omap.get(_val(it, "prop_id"), 0)

        # 第 1 行：名称 + 价格胶囊
        cy1 = (y + 18) * SS
        d.text(((x + 14) * SS, cy1),
               _fit_text(d, _val(it, "name", ""), f_name,
                         (card_w - 160) * SS),
               font=f_name, fill=TEXT, anchor="lm")
        price = _val(it, "price", None)
        if price is not None:
            _chip(d, cy1, f"{price} 金", AMBER, f_price,
                  right=(x + card_w - 12) * SS, h=30)
        # 第 2 行：描述
        d.text(((x + 14) * SS, (y + 44) * SS),
               _fit_text(d, _val(it, "description", ""), f_sub,
                         (card_w - 28) * SS),
               font=f_sub, fill=MUTED, anchor="lm")
        # 第 3 行：今日限购余量 + 持有胶囊
        left = _val(it, "left_today", None)
        if left is not None:
            try:
                left_i = int(left)
            except (TypeError, ValueError):
                left_i = None
            if left_i is not None:
                d.text(((x + 14) * SS, (y + 68) * SS),
                       "今日已满" if left_i <= 0 else f"今日余 {left_i}",
                       font=f_lab, fill=UP if left_i <= 0 else MUTED,
                       anchor="lm")
        if qty > 0:
            _chip(d, (y + 68) * SS, f"持有 ×{qty}", BLUE, f_lab,
                  right=(x + card_w - 12) * SS, h=28)
        # 第 4 行：回收价
        sell = _val(it, "sell_price", None)
        if sell is not None:
            d.text(((x + 14) * SS, (y + 90) * SS), f"回收 {sell} 金",
                   font=f_sub, fill=MUTED, anchor="lm")

    # 我的背包
    if inv_lines:
        y0 = body_end + 14
        d.text((PAD * SS, y0 * SS), "我的背包", font=f_lab, fill=MUTED,
               anchor="lm")
        y0 += 24
        # 每行 3 枚，按总宽均分上限截断，保证不越右缘
        chip_max = ((W - 2 * PAD) * SS - 16 * SS) // per_line
        for line in inv_lines:
            x2 = PAD * SS
            for name, q in line:
                label = _fit_text(d, f"{name} ×{q}", f_inv,
                                  chip_max - 24)
                x2 = _chip(d, (y0 + 14) * SS, label, BLUE, f_inv,
                           left=x2, h=30) + 8 * SS
            y0 += 38
    return _save(img, out_path)


# ── ⑥ 行情总览 ────────────────────────────────────────────────

def render_market_overview(out_path, data) -> Path | None:
    """行情总览卡片：指数（可选）+ 情绪 + 涨跌家数 + 板块热力 + 涨跌榜。

    data 字段（全部可选，缺省不画对应区）：
    - indices: [{name, value, change_pct}]  大盘指数列表（指令层当前不传）
    - sentiment: float 0~100  市场情绪
    - breadth: {up, down, flat, limit_up, limit_down}  涨跌家数
    - sectors: [{name, change_pct}]  板块热力条
    - gainers / losers: [{code, name, change_pct, price}]  涨跌榜

    每一区的高度都按「是否真的传了数据」累加，缺数据不留空白；
    涨跌榜左右两组用同一套列偏移，表头与数据行逐列对齐。
    """
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    data = data or {}
    indices = _val(data, "indices") or []
    sentiment = _val(data, "sentiment")
    breadth = _val(data, "breadth") or {}
    sectors = _val(data, "sectors") or []
    gainers = _val(data, "gainers") or []
    losers = _val(data, "losers") or []
    crowd = _val(data, "crowd") or {}

    up = int(float(_val(breadth, "up", 0) or 0))
    down = int(float(_val(breadth, "down", 0) or 0))
    flat = int(float(_val(breadth, "flat", 0) or 0))
    limit_up = int(float(_val(breadth, "limit_up", 0) or 0))
    limit_down = int(float(_val(breadth, "limit_down", 0) or 0))
    total = up + down + flat

    f_row, f_sub, f_lab = _font(13 * SS), _font(10 * SS), _font(11 * SS)
    IDX_H = 56

    H = TITLE_H + 8
    if indices:
        H += IDX_H + 8
    if sentiment is not None:
        H += 46
    if crowd:
        H += 40
    if total > 0:
        H += 58
    if sectors:
        H += 60
    list_n = max(len(gainers), len(losers))
    if list_n:
        H += HEAD_H + 30 * list_n + 8
    H += HINT_H

    img, d = _canvas(W, H)
    _title_band(d, "📊 行情总览", None)
    y = TITLE_H + 8

    # ── 大盘指数（可选区）──
    if indices:
        col_w = (W - 2 * PAD) / max(len(indices), 1)
        for i, idx in enumerate(indices):
            cx = (PAD + col_w * (i + 0.5)) * SS
            d.text((cx, (y + 16) * SS), _val(idx, "name", ""), font=f_sub,
                   fill=MUTED, anchor="mm")
            val = float(_val(idx, "value", 0) or 0)
            d.text((cx, (y + 34) * SS), f"{val:,.2f}", font=f_row, fill=TEXT,
                   anchor="mm")
            pct = float(_val(idx, "change_pct", 0) or 0)
            d.text((cx, (y + 48) * SS), _pct(pct), font=f_lab,
                   fill=UP if pct >= 0 else DOWN, anchor="mm")
        y += IDX_H + 8

    # ── 市场情绪条 ──
    if sentiment is not None:
        s = max(0.0, min(100.0, float(sentiment)))
        cy = (y + 14) * SS
        d.text((PAD * SS, cy), "情绪", font=f_sub, fill=MUTED, anchor="lm")
        bar_x0, bar_x1 = (PAD + 52) * SS, (W - PAD - 56) * SS
        d.rectangle([bar_x0, (y + 7) * SS, bar_x1, (y + 21) * SS],
                    fill=PANEL, outline=GRID, width=SS)
        fill_x = bar_x0 + (bar_x1 - bar_x0) * s / 100
        if fill_x > bar_x0:
            t = s / 100
            col = _hybrid(DOWN, UP, t) if t > 0.5 \
                else _hybrid(MUTED, DOWN, t / 0.5)
            d.rectangle([bar_x0, (y + 7) * SS, fill_x, (y + 21) * SS],
                        fill=col)
        d.text(((W - PAD) * SS, cy), f"{s:.0f}/100", font=f_row, fill=TEXT,
               anchor="rm")
        y += 46

    # ── 散户大军（人群层，可选区）──
    if crowd:
        people = float(_val(crowd, "people", 0) or 0)
        net = float(_val(crowd, "net", 0) or 0)
        ret = float(_val(crowd, "ret", 0) or 0)
        cy = (y + 14) * SS
        d.text((PAD * SS, cy), f"👥 散户 {people / 10000:.1f} 万人在场",
               font=f_sub, fill=TEXT, anchor="lm")
        net_col = UP if net >= 0 else DOWN
        d.text(((W - PAD) * SS, cy),
               f"净买 {net / 1e8:+.2f} 亿 · 群体收益 {ret * 100:+.1f}%",
               font=f_sub, fill=net_col, anchor="rm")
        y += 40
    if total > 0:
        cy = (y + 16) * SS
        d.text((PAD * SS, cy), "涨跌", font=f_sub, fill=MUTED, anchor="lm")
        bx0, bx1 = (PAD + 44) * SS, (W - PAD - 132) * SS
        d.rectangle([bx0, (y + 9) * SS, bx1, (y + 23) * SS], fill=PANEL)
        x = bx0
        for n, col in ((up, UP), (flat, MUTED), (down, DOWN)):
            if n <= 0:
                continue
            w = (bx1 - bx0) * n / float(total)
            if w > 0:
                d.rectangle([x, (y + 9) * SS, x + w, (y + 23) * SS], fill=col)
                x += w
        d.text(((W - PAD) * SS, cy), f"涨停 {limit_up} · 跌停 {limit_down}",
               font=f_sub, fill=MUTED, anchor="rm")
        ly = (y + 42) * SS
        xx = PAD * SS
        for label, col in ((f"涨 {up}", UP), (f"平 {flat}", MUTED),
                           (f"跌 {down}", DOWN)):
            d.text((xx, ly), label, font=f_lab, fill=col, anchor="lm")
            xx += d.textlength(label, font=f_lab) + 14 * SS
        d.text(((W - PAD) * SS, ly), f"全市场 {total} 只", font=f_sub,
               fill=MUTED, anchor="rm")
        y += 58

    # ── 板块热力条 ──
    if sectors:
        d.text((PAD * SS, (y + 14) * SS), "板块", font=f_sub, fill=MUTED,
               anchor="lm")
        ax0 = PAD + 44
        slot = (W - PAD - ax0) / max(len(sectors), 1)
        for i, sec in enumerate(sectors):
            name = str(_val(sec, "name", ""))
            pct = float(_val(sec, "change_pct", 0) or 0)
            col = UP if pct >= 0 else DOWN
            sx = (ax0 + slot * i) * SS
            sw = slot * 0.8 * SS
            d.text((sx + sw / 2, (y + 13) * SS),
                   _fit_text(d, name, f_sub, sw), font=f_sub, fill=TEXT,
                   anchor="mm")
            d.rectangle([sx, (y + 22) * SS, sx + sw, (y + 32) * SS],
                        fill=_hybrid(BG, col, min(1.0, 0.2 + abs(pct) / 5)))
            d.text((sx + sw / 2, (y + 44) * SS), _pct(pct),
                   font=_font(9 * SS), fill=col, anchor="mm")
        y += 60

    # ── 涨跌榜（左右两组同列偏移）──
    if list_n:
        groups = (("涨幅榜", gainers, PAD), ("跌幅榜", losers, W // 2 + 8))
        heads = []
        for title, _lst, gx in groups:
            heads += [(title, gx, "l"), ("代码", gx + 140, "c"),
                      ("现价", gx + 326, "r"), ("涨跌幅", gx + 420, "r")]
        _head_row(d, y, heads)
        y += HEAD_H
        for i in range(list_n):
            _row_bg(d, y, 30, i % 2 == 1)
            cy = (y + 15) * SS
            for _title, lst, gx in groups:
                if i >= len(lst):
                    continue
                it = lst[i]
                pct = float(_val(it, "change_pct", 0) or 0)
                col = UP if pct >= 0 else DOWN
                d.text((gx * SS, cy),
                       _fit_text(d, _val(it, "name", ""), f_row, 124 * SS),
                       font=f_row, fill=TEXT, anchor="lm")
                d.text(((gx + 140) * SS, cy), str(_val(it, "code", "")),
                       font=f_sub, fill=MUTED, anchor="mm")
                d.text(((gx + 326) * SS, cy),
                       f"{float(_val(it, 'price', 0) or 0):.2f}",
                       font=f_lab, fill=col, anchor="rm")
                d.text(((gx + 420) * SS, cy), _pct(pct), font=f_lab,
                       fill=col, anchor="rm")
            y += 30
    _hint(d, y + 4,
          "「炒股 个股 <代码>」看详情　·　「炒股 盘口 <代码>」看五档　·　"
          "「炒股 帮助」看全部指令")
    return _save(img, out_path)


# ── ⑦ 个股详情 ────────────────────────────────────────────────

def render_stock_detail(out_path, data) -> Path | None:
    """个股详情卡片：价格块 + 涨跌停位置条 + 关键字段 + 操作提示。

    data 字段：name, code, price, change_pct, open, high, low,
    prev_close, limit_up, limit_down, sector, industry, volume,
    net_inflow（全部可选，缺省显示 0.00 / —）。
    """
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    data = data or {}
    f_big, f_title, f_row, f_sub, f_lab = (
        _font(26 * SS), _font(15 * SS), _font(13 * SS),
        _font(10 * SS), _font(11 * SS))

    price = float(_val(data, "price", 0) or 0)
    pct = float(_val(data, "change_pct", 0) or 0)
    lu = float(_val(data, "limit_up", 0) or 0)
    ld = float(_val(data, "limit_down", 0) or 0)
    prev = float(_val(data, "prev_close", 0) or 0)
    high = float(_val(data, "high", 0) or 0)
    low = float(_val(data, "low", 0) or 0)
    col = UP if pct >= 0 else DOWN

    H = TITLE_H + 76 + 54 + 3 * 30 + 10 + HINT_H
    img, d = _canvas(W, H)
    _title_band(d, f"📈 {_val(data, 'name', '个股详情')}", _pct(pct), col)
    y = TITLE_H + 6

    # ── 价格块 ──
    price_s = f"{price:.2f}"
    d.text((PAD * SS, (y + 26) * SS), price_s, font=f_big, fill=col,
           anchor="lm")
    pw = d.textlength(price_s, font=f_big)
    d.text((PAD * SS + pw + 12 * SS, (y + 26) * SS), _pct(pct),
           font=f_title, fill=col, anchor="lm")
    d.text(((W - PAD) * SS, (y + 14) * SS), f"涨停 {lu:.2f}", font=f_lab,
           fill=UP, anchor="rm")
    d.text(((W - PAD) * SS, (y + 36) * SS), f"跌停 {ld:.2f}", font=f_lab,
           fill=DOWN, anchor="rm")
    sub = str(_val(data, "code", "") or "")
    for key in ("sector", "industry"):
        v = _val(data, key)
        if v:
            sub += f" · {v}"
    d.text((PAD * SS, (y + 58) * SS),
           _fit_text(d, sub, f_sub, (W - 2 * PAD - 180) * SS),
           font=f_sub, fill=MUTED, anchor="lm")
    y += 76

    # ── 涨跌停位置条（现价在 [跌停, 涨停] 区间中的位置）──
    if lu > ld > 0:
        bx0, bx1 = (PAD + 6) * SS, (W - PAD - 6) * SS
        d.rectangle([bx0, (y + 10) * SS, bx1, (y + 22) * SS], fill=PANEL,
                    outline=GRID, width=SS)
        r = max(0.0, min(1.0, (price - ld) / (lu - ld)))
        mx = bx0 + (bx1 - bx0) * r
        if ld < prev < lu:
            rx = bx0 + (bx1 - bx0) * ((prev - ld) / (lu - ld))
            d.line([(rx, (y + 6) * SS), (rx, (y + 26) * SS)], fill=MUTED,
                   width=SS)
        if r > 0:
            d.rectangle([bx0, (y + 10) * SS, mx, (y + 22) * SS],
                        fill=_hybrid(BG, col, 0.55))
        d.ellipse([mx - 6 * SS, (y + 10) * SS, mx + 6 * SS, (y + 22) * SS],
                  fill=col)
        d.text((PAD * SS, (y + 38) * SS), f"跌停 {ld:.2f}", font=f_sub,
               fill=DOWN, anchor="lm")
        d.text(((W - PAD) * SS, (y + 38) * SS), f"涨停 {lu:.2f}", font=f_sub,
               fill=UP, anchor="rm")
        d.text((((PAD + (W - PAD) / 2)) * SS, (y + 38) * SS),
               f"昨收 {prev:.2f}", font=f_sub, fill=MUTED, anchor="mm")
        y += 54
    else:
        y += 8

    # ── 关键字段（两列三行，值按列宽截断）──
    half = (W - 2 * PAD) // 2

    def kv(label, val, x, y_c, val_col=TEXT):
        d.text((x * SS, y_c * SS), label, font=f_sub, fill=MUTED,
               anchor="lm")
        d.text(((x + 76) * SS, y_c * SS),
               _fit_text(d, val, f_row, (half - 96) * SS),
               font=f_row, fill=val_col, anchor="lm")

    net = float(_val(data, "net_inflow", 0) or 0)
    amp = ((high - low) / prev * 100) if prev > 0 else 0.0
    rows = [
        ("今开", f"{float(_val(data, 'open', 0) or 0):.2f}"),
        ("最高", f"{high:.2f}"),
        ("最低", f"{low:.2f}"),
        ("成交量", _fmt_amt(_val(data, "volume", 0))),
        ("主力净流入", _signed_amt(net), UP if net >= 0 else DOWN),
        ("振幅", f"{amp:.2f}%"),
    ]
    for i, r in enumerate(rows):
        rcol = r[2] if len(r) > 2 else TEXT
        cx = PAD if i % 2 == 0 else W // 2 + 8
        kv(r[0], r[1], cx, y + 14 + (i // 2) * 30, rcol)
    y += 3 * 30 + 8
    _hint(d, y,
          "「炒股 k线 <代码> 分时」看分时图　·　"
          "「炒股 买入 <代码> <数量>」下单　·　「炒股 盘口 <代码>」看五档")
    return _save(img, out_path)


# ── ⑧ 五档盘口 ────────────────────────────────────────────────

def render_orderbook(out_path, data) -> Path | None:
    """五档盘口：卖五→卖一 / 现价中枢 / 买一→买五，量条按本盘口最大档缩放。

    data 字段：bids / asks 各 [{price, volume}]（5 档，买入价升序）；
    可选 name / price / change_pct：传了就在标题带显示涨跌胶囊、
    中枢条显示现价与买卖价差。缺档补「—」。
    """
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    data = data or {}
    raw_asks = list(_val(data, "asks") or [])[:5]
    raw_bids = list(_val(data, "bids") or [])[:5]
    price = _val(data, "price")
    pct = float(_val(data, "change_pct", 0) or 0)

    asks = raw_asks[::-1]          # 卖五在最上 → 卖一在中间上方
    while len(asks) < 5:
        asks.insert(0, {"price": None, "volume": 0})
    bids = list(raw_bids)
    while len(bids) < 5:
        bids.append({"price": None, "volume": 0})

    f_row, f_sub, f_lab = _font(13 * SS), _font(10 * SS), _font(12 * SS)
    ROW_H, MID_H = 32, 44
    H = TITLE_H + HEAD_H + 4 + ROW_H * 10 + MID_H + 8 + HINT_H
    img, d = _canvas(W, H)
    badge = _pct(pct) if price is not None else None
    _title_band(d, "📋 五档盘口", badge, UP if pct >= 0 else DOWN)

    y = TITLE_H
    _head_row(d, y, [("方向", PAD, "l"), ("价格", 190, "r"),
                     ("数量", W - PAD, "r")])
    y += HEAD_H + 4

    all_v = [float(_val(x, "volume", 0) or 0) for x in raw_bids + raw_asks]
    scale = (max(all_v) if all_v else 0) or 1
    BAR_X0, BAR_X1 = 250, 700
    track = _hybrid(BG, TEXT, 0.06)
    CN = ("一", "二", "三", "四", "五")

    def draw_level(level, side, y0, idx):
        p = _val(level, "price")
        vol = float(_val(level, "volume", 0) or 0)
        cy = (y0 + ROW_H / 2) * SS
        col = DOWN if side == "ask" else UP
        d.text((PAD * SS, cy), ("卖" if side == "ask" else "买") + CN[idx],
               font=f_lab, fill=col, anchor="lm")
        d.text((190 * SS, cy), f"{float(p):.2f}" if p is not None else "—",
               font=f_row, fill=col, anchor="rm")
        d.rectangle([BAR_X0 * SS, (y0 + 8) * SS,
                     BAR_X1 * SS, (y0 + ROW_H - 8) * SS], fill=track)
        # 条形长度按 1x 像素计算，画的时候统一 ×SS；不要把已含 SS 的宽度
        # 再乘一次 SS（那样最长档会从 0 越过到负坐标，盖住左侧方向/价格列）。
        w = (BAR_X1 - BAR_X0) * vol / scale
        if w > 0:
            d.rectangle([(BAR_X1 - w) * SS, (y0 + 8) * SS,
                         BAR_X1 * SS, (y0 + ROW_H - 8) * SS],
                        fill=_hybrid(BG, col, 0.8))
        d.text(((W - PAD) * SS, cy), _fmt_amt(vol), font=f_row, fill=TEXT,
               anchor="rm")

    for i, a in enumerate(asks):
        draw_level(a, "ask", y, 4 - i)      # i=0 → 卖五
        y += ROW_H

    # ── 现价中枢 ──
    d.rectangle([0, y * SS, W * SS, (y + MID_H) * SS], fill=PANEL)
    d.line([(0, y * SS), (W * SS, y * SS)], fill=GRID, width=SS)
    d.line([(0, (y + MID_H) * SS), (W * SS, (y + MID_H) * SS)], fill=GRID,
           width=SS)
    cy = (y + MID_H / 2) * SS
    if price is None:
        d.text(((W / 2) * SS, cy), "现价 —", font=f_row, fill=MUTED,
               anchor="mm")
    else:
        d.text((PAD * SS, cy), "现价", font=f_sub, fill=MUTED, anchor="lm")
        d.text(((PAD + 44) * SS, cy), f"{float(price):.2f}",
               font=_font(18 * SS), fill=UP if pct >= 0 else DOWN,
               anchor="lm")
        a1 = _val(raw_asks[0], "price") if raw_asks else None
        b1 = _val(raw_bids[0], "price") if raw_bids else None
        if a1 is not None and b1 is not None:
            d.text(((W - PAD) * SS, cy),
                   f"买卖价差 {float(a1) - float(b1):.2f}", font=f_sub,
                   fill=MUTED, anchor="rm")
    y += MID_H

    for i, b in enumerate(bids):
        draw_level(b, "bid", y, i)
        y += ROW_H
    _hint(d, y + 8,
          "「炒股 买入 <代码> <数量>」按涨停价吃单　·　"
          "「炒股 卖出 <代码> <数量>」按跌停价甩卖")
    return _save(img, out_path)


# ── ⑨ 持仓 ────────────────────────────────────────────────────

def render_holdings(out_path, data) -> Path | None:
    """持仓卡片：名称/代码（今日买入标可卖）/持仓/成本/现价/盈亏/盈亏% + 合计条。

    兼容两种入参：``holdings``（首选手写契约）或 ``positions``（main.py
    旧键名）——两者都按 {code, name, shares, sellable, cost, price,
    pnl, pnl_pct} 读取，缺 sellable 视为全部可卖。空持仓返回 None。
    可选合计字段：total_market_value / total_pnl / cash / total_assets。
    """
    data = data or {}
    holdings = _val(data, "holdings") or _val(data, "positions") or []
    if not holdings:
        return None
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None

    ROW_H, FOOT_H = 40, 58
    H = TITLE_H + HEAD_H + ROW_H * len(holdings) + FOOT_H + HINT_H
    img, d = _canvas(W, H)
    f_row, f_sub, f_lab = _font(13 * SS), _font(10 * SS), _font(11 * SS)
    _title_band(d, "💼 我的持仓", f"{len(holdings)} 只")
    y = TITLE_H
    _head_row(d, y, [("名称", PAD, "l"), ("持仓", 330, "r"),
                     ("成本", 420, "r"), ("现价", 510, "r"),
                     ("浮动盈亏", 700, "r"), ("盈亏%", W - PAD, "r")])
    y += HEAD_H

    for i, h in enumerate(holdings):
        _row_bg(d, y, ROW_H, i % 2 == 1)
        cy = (y + ROW_H / 2) * SS
        d.text((PAD * SS, (y + 13) * SS),
               _fit_text(d, _val(h, "name", ""), f_row, 190 * SS),
               font=f_row, fill=TEXT, anchor="lm")
        code = str(_val(h, "code", "") or "")
        d.text((PAD * SS, (y + 30) * SS), code, font=f_sub, fill=MUTED,
               anchor="lm")
        shares = int(float(_val(h, "shares", 0) or 0))
        sellable = int(float(_val(h, "sellable", shares) or 0))
        if sellable < shares:
            cx = PAD * SS + d.textlength(code, font=f_sub) + 8 * SS
            d.text((cx, (y + 30) * SS), f"可卖 {sellable}", font=f_sub,
                   fill=AMBER, anchor="lm")
        d.text((330 * SS, cy), f"{shares:,}", font=f_lab, fill=TEXT,
               anchor="rm")
        d.text((420 * SS, cy), f"{float(_val(h, 'cost', 0) or 0):.2f}",
               font=f_lab, fill=MUTED, anchor="rm")
        d.text((510 * SS, cy), f"{float(_val(h, 'price', 0) or 0):.2f}",
               font=f_lab, fill=TEXT, anchor="rm")
        pnl = float(_val(h, "pnl", 0) or 0)
        pnl_pct = float(_val(h, "pnl_pct", 0) or 0)
        col = UP if pnl >= 0 else DOWN
        d.text((700 * SS, cy), f"{pnl:+,.0f}", font=f_lab, fill=col,
               anchor="rm")
        d.text(((W - PAD) * SS, cy), _pct(pnl_pct), font=f_lab, fill=col,
               anchor="rm")
        y += ROW_H

    # ── 合计条 ──
    mv = float(_val(data, "total_market_value", 0) or 0)
    total_pnl = float(_val(data, "total_pnl", 0) or 0)
    cash = _val(data, "cash")
    assets = _val(data, "total_assets")
    d.rectangle([0, y * SS, W * SS, (y + FOOT_H) * SS], fill=PANEL)
    d.line([(0, y * SS), (W * SS, y * SS)], fill=GRID, width=SS)
    d.text((PAD * SS, (y + 18) * SS), f"持仓市值 {_fmt_amt(mv)}",
           font=f_lab, fill=TEXT, anchor="lm")
    d.text(((W - PAD) * SS, (y + 18) * SS),
           f"浮动盈亏 {_signed_amt(total_pnl)}", font=f_lab,
           fill=UP if total_pnl >= 0 else DOWN, anchor="rm")
    line2 = []
    if cash is not None:
        line2.append(f"现金 {_fmt_amt(cash)}")
    if assets is not None:
        line2.append(f"总资产 {_fmt_amt(assets)}")
    if line2:
        d.text((PAD * SS, (y + 40) * SS), "　·　".join(line2), font=f_sub,
               fill=MUTED, anchor="lm")
    y += FOOT_H
    _hint(d, y,
          "显示「可卖」的股份含今日买入，T+1 次日才可卖　·　"
          "「炒股 卖出 <代码> <数量>」平仓")
    return _save(img, out_path)


# ── ⑩ 资金仪表盘 ──────────────────────────────────────────────

def render_funds(out_path, data) -> Path | None:
    """资金仪表盘：累计盈亏摘要 + 可用/冻结/可取/总资产四块 + 操作提示。

    data 字段：available, frozen, withdrawable, total（缺省 0）；
    可选 pnl / pnl_pct / initial：传了就在顶部画累计盈亏摘要行。
    """
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    data = data or {}
    f_big, f_sub, f_lab = _font(24 * SS), _font(10 * SS), _font(15 * SS)

    items = [
        ("可用资金", float(_val(data, "available", 0) or 0), BLUE),
        ("冻结资金", float(_val(data, "frozen", 0) or 0), AMBER),
        ("可取资金", float(_val(data, "withdrawable", 0) or 0),
         _hybrid(DOWN, GOLD, 0.5)),
        ("总资产", float(_val(data, "total", 0) or 0), TEXT),
    ]
    COLS, GAP, card_h = 2, 16, 96
    card_w = (W - 2 * PAD - GAP) // COLS
    rows = (len(items) + COLS - 1) // COLS
    H = TITLE_H + 44 + rows * (card_h + GAP) - GAP + 12 + HINT_H
    img, d = _canvas(W, H)
    _title_band(d, "💰 资金仪表盘", None)

    y = TITLE_H + 6
    pnl = _val(data, "pnl")
    pnl_pct = _val(data, "pnl_pct")
    initial = _val(data, "initial")
    if pnl is not None:
        pv = float(pnl or 0)
        col = UP if pv >= 0 else DOWN
        d.text((PAD * SS, (y + 14) * SS), "累计盈亏", font=f_sub, fill=MUTED,
               anchor="lm")
        amt = _signed_amt(pv)
        d.text(((PAD + 62) * SS, (y + 14) * SS), amt, font=f_lab, fill=col,
               anchor="lm")
        if pnl_pct is not None:
            wx = PAD * SS + 62 * SS + d.textlength(amt, font=f_lab) + 8 * SS
            d.text((wx, (y + 14) * SS), f"({_pct(pnl_pct)})", font=f_sub,
                   fill=col, anchor="lm")
        if initial is not None:
            d.text(((W - PAD) * SS, (y + 14) * SS),
                   f"初始资金 {_fmt_amt(float(initial or 0))}", font=f_sub,
                   fill=MUTED, anchor="rm")
    y += 44

    for i, (label, val, col) in enumerate(items):
        r, c = divmod(i, COLS)
        x = PAD + c * (card_w + GAP)
        cy = y + r * (card_h + GAP)
        box = [x * SS, cy * SS, (x + card_w) * SS, (cy + card_h) * SS]
        try:
            d.rounded_rectangle(box, radius=10 * SS,
                                fill=_hybrid(BG, col, 0.10),
                                outline=_hybrid(BG, col, 0.35), width=SS)
        except AttributeError:      # pragma: no cover - 老版本 Pillow
            d.rectangle(box, fill=_hybrid(BG, col, 0.10),
                        outline=_hybrid(BG, col, 0.35))
        d.text(((x + 16) * SS, (cy + 26) * SS), label, font=f_sub, fill=MUTED,
               anchor="lm")
        d.text(((x + 16) * SS, (cy + 62) * SS), _fmt_amt(val), font=f_big,
               fill=col, anchor="lm")
    y += rows * (card_h + GAP) - GAP + 12
    _hint(d, y,
          "冻结资金为挂单占用，撤单即释放　·　可取资金为已完成 T+1 结算的部分")
    return _save(img, out_path)

# ── ⑪ 帮助手册 ────────────────────────────────────────────────

def _parse_help(text) -> list[dict]:
    """把 main.HELP 的纯文本手册拆成 [{title, entries:[(kind, cmd, desc)]}]。

    kind：`cmd`（可执行指令）/ `note`（该节的说明文字）。手册是单一事实源，
    卡片只负责排版——改指令只改 main.HELP，图会跟着变。
    """
    import re as _re

    sections: list[dict] = []
    cur = None
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        indented = raw[:1].isspace()
        s = raw.strip()
        if s.startswith("━━"):
            cur = {"title": s.strip("━ ").strip(), "entries": []}
            sections.append(cur)
            continue
        if cur is None:
            cur = {"title": "总览", "entries": []}
            sections.append(cur)
        if s.startswith("/炒股"):
            parts = _re.split(r"\s{2,}", s)
            cur["entries"].append(("cmd", parts[0], " ".join(parts[1:]).strip()))
        elif indented and cur["entries"] and cur["entries"][-1][0] == "cmd":
            kind, cmd, desc = cur["entries"][-1]
            cur["entries"][-1] = (kind, cmd, (desc + " " + s).strip())
        else:
            cur["entries"].append(("note", s, ""))
    return sections


def render_help(out_path, text, page: int = 0) -> Path | None:
    """指令手册卡片：按手册分组渲染，每组两列「指令 + 说明」。

    数据来自 main.HELP（唯一事实源），本函数只排版。手册较长，按分组对半
    切成两页（page=0/1），每页约 700px 高，手机上看字形不至于缩成一条线。
    缺 Pillow 返回 None，调用方回退原来的分页纯文本。
    """
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    sections = _parse_help(text)
    if not sections:
        return None
    pages = 2 if len(sections) > 2 else 1
    page = max(0, min(int(page), pages - 1))
    if pages == 2:
        cut = (len(sections) + 1) // 2
        sections = sections[:cut] if page == 0 else sections[cut:]
        if not sections:
            return None

    f_sec = _font(14 * SS)
    f_cmd = _font(13 * SS)
    f_desc = _font(11 * SS)
    ROW_H = 30
    SEC_H = 30
    SUB_H = 34
    COL_GAP = 16
    col_w = (W - 2 * PAD - COL_GAP) // 2

    # 先算高度：
    total = TITLE_H + SUB_H
    for sec in sections:
        rows = [e for e in sec["entries"]]
        cmd_rows = sum(1 for e in rows if e[0] == "cmd")
        note_rows = len(rows) - cmd_rows
        total += SEC_H + (max(1, (cmd_rows + 1) // 2) * ROW_H) + note_rows * 26
    H = total + 10 + HINT_H

    img, d = _canvas(W, H)
    n_cmd = sum(1 for s in sections for e in s["entries"] if e[0] == "cmd")
    badge = f"{n_cmd} 条" if pages == 1 else f"{page + 1}/{pages} 页 · {n_cmd} 条"
    _title_band(d, "📖 炒股 · 命令手册", badge, AMBER)

    d.text((PAD * SS, (TITLE_H + 18) * SS),
           "全部指令以 /炒股 开头，别名 /stock、/cg　·　"
           "长按图片可放大查看", font=f_desc, fill=MUTED, anchor="lm")

    y = TITLE_H + SUB_H
    for sec in sections:
        # 分组表头带
        d.rectangle([0, y * SS, W * SS, (y + SEC_H) * SS], fill=PANEL)
        d.text((PAD * SS, (y + SEC_H / 2) * SS), sec["title"], font=f_sec,
               fill=AMBER, anchor="lm")
        cmds = [e for e in sec["entries"] if e[0] == "cmd"]
        notes = [e for e in sec["entries"] if e[0] == "note"]
        if cmds:
            d.text(((W - PAD) * SS, (y + SEC_H / 2) * SS), f"{len(cmds)} 条",
                   font=f_desc, fill=MUTED, anchor="rm")
        y += SEC_H

        half = (len(cmds) + 1) // 2
        for i in range(half):
            for c in range(2):
                idx = i + c * half
                if idx >= len(cmds):
                    continue
                _kind, cmd, desc = cmds[idx]
                x = PAD + c * (col_w + COL_GAP)
                cy = (y + i * ROW_H + ROW_H / 2) * SS
                d.text((x * SS, cy), cmd, font=f_cmd, fill=TEXT, anchor="lm")
                cw = d.textlength(cmd, font=f_cmd)
                room = (col_w - cw - 14) * SS
                if desc and room > 30 * SS:
                    d.text(((x + col_w) * SS, cy),
                           _fit_text(d, desc, f_desc, room), font=f_desc,
                           fill=MUTED, anchor="rm")
        y += max(1, half) * ROW_H
        for _kind, note, _ in notes:
            d.text((PAD * SS, (y + 12) * SS),
                   _fit_text(d, note, f_desc, (W - 2 * PAD) * SS),
                   font=f_desc, fill=MUTED, anchor="lm")
            y += 26

    _hint(d, y + 8, "开始玩：/炒股 开户　·　看大盘：/炒股 行情　·　"
                    "排名：/炒股 排行")
    return _save(img, out_path)

