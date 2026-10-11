"""技术指标（纯函数，输入收盘价序列；不依赖任何状态与第三方库）。"""

from __future__ import annotations

BLOCKS = "▁▂▃▄▅▆▇█"


def ma(values: list[float], n: int) -> float | None:
    """简单移动平均（最近 n 个）。"""
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values: list[float], n: int) -> list[float]:
    """指数移动平均序列。"""
    if not values or n <= 0:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[float | None, float | None, float | None]:
    """返回 (DIF, DEA, MACD柱)。样本不足时返回 (None, None, None)。"""
    if len(closes) < slow + signal:
        return None, None, None
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, signal)
    return dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2


def rsi(closes: list[float], n: int = 14) -> float | None:
    """RSI（简单平均口径）。样本不足返回 None。"""
    if n <= 0 or len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses <= 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100.0 - 100.0 / (1.0 + rs)


def boll(closes: list[float], n: int = 20,
         k: float = 2.0) -> tuple[float | None, float | None, float | None]:
    """布林带 (中轨, 上轨, 下轨)。"""
    mid = ma(closes, n)
    if mid is None:
        return None, None, None
    var = sum((c - mid) ** 2 for c in closes[-n:]) / n
    sd = var ** 0.5
    return mid, mid + k * sd, mid - k * sd


def sparkline(values: list[float]) -> str:
    """把序列画成一行方块走势（QQ 里不需要图片）。"""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return BLOCKS[3] * len(values)
    return "".join(
        BLOCKS[min(int((v - lo) / (hi - lo) * (len(BLOCKS) - 0.001)),
                   len(BLOCKS) - 1)]
        for v in values)
