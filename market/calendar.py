"""交易时段与阶段机。

阶段（`SessionClock.phase`）：
- `auction_open`  开盘集合竞价（虚拟交易日开头 / 真实 9:15–9:25）
- `continuous`    连续竞价
- `auction_close` 收盘集合竞价（日终前 / 真实 14:57–15:00）
- `closed`        休市

`market_mode="247"`（演示）用虚拟交易日长度切分阶段；`"realtime"` 用真实时钟窗口。
`SESSION` 是模块级单例，主循环在每个虚拟交易日开始时 `reset()`。
`is_trading_day()` = 工作日且非法定节假日（内置 2024–2026 节假日表，
模块级 `HOLIDAYS` 可整体替换实现自定义休市）。
"""

from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta

from ..config import CFG

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 缺 tzdata 时退回固定时区
    from datetime import timedelta, timezone
    TZ = timezone(timedelta(hours=8))

_CONTINUOUS = [(dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(14, 57))]
_AUCTION_OPEN = (dtime(9, 15), dtime(9, 25))
_AUCTION_CLOSE = (dtime(14, 57), dtime(15, 0))

# ── 法定节假日表（DESIGN §3：内置节假日表，可配置）─────────────
# 数据源：国务院办公厅年度通知，区间含首尾、含段内周末：
#   2024 国办发明电〔2023〕7号 / 2025 国办发明电〔2024〕12号 /
#   2026 国办发明电〔2025〕7号
# 只记"放假"区间：调休上班的周末不录——A 股周末恒休，调休日不开市。
# 扩展：向此表追加 (起, 止, 节日) 后重建 HOLIDAYS（见 _build_holidays）。
_HOLIDAY_SPANS: tuple[tuple[str, str, str], ...] = (
    ("2024-01-01", "2024-01-01", "元旦"),
    ("2024-02-10", "2024-02-17", "春节"),
    ("2024-04-04", "2024-04-06", "清明节"),
    ("2024-05-01", "2024-05-05", "劳动节"),
    ("2024-06-10", "2024-06-10", "端午节"),
    ("2024-09-15", "2024-09-17", "中秋节"),
    ("2024-10-01", "2024-10-07", "国庆节"),
    ("2025-01-01", "2025-01-01", "元旦"),
    ("2025-01-28", "2025-02-04", "春节"),
    ("2025-04-04", "2025-04-06", "清明节"),
    ("2025-05-01", "2025-05-05", "劳动节"),
    ("2025-05-31", "2025-06-02", "端午节"),
    ("2025-10-01", "2025-10-08", "国庆节、中秋节"),
    ("2026-01-01", "2026-01-03", "元旦"),
    ("2026-02-15", "2026-02-23", "春节"),
    ("2026-04-04", "2026-04-06", "清明节"),
    ("2026-05-01", "2026-05-05", "劳动节"),
    ("2026-06-19", "2026-06-21", "端午节"),
    ("2026-09-25", "2026-09-27", "中秋节"),
    ("2026-10-01", "2026-10-07", "国庆节"),
)


def _build_holidays() -> frozenset[date]:
    out: set[date] = set()
    for a, b, _name in _HOLIDAY_SPANS:
        d = date.fromisoformat(a)
        end = date.fromisoformat(b)
        while d <= end:
            out.add(d)
            d += timedelta(days=1)
    return frozenset(out)


# 公开可配置：整体替换该集合即可覆盖/追加节假日（如自定义休市安排）
HOLIDAYS: frozenset[date] = _build_holidays()


def is_holiday(now: datetime | None = None) -> bool:
    """是否落在法定节假日区间（含段内周末；与周末判断正交）。"""
    now = now or datetime.now(TZ)
    return now.date() in HOLIDAYS


def is_trading_day(now: datetime | None = None) -> bool:
    """交易日 = 工作日（周一至周五）且非法定节假日。"""
    now = now or datetime.now(TZ)
    if now.weekday() >= 5:
        return False
    return now.date() not in HOLIDAYS


def is_open(now: datetime | None = None) -> bool:
    """当前是否可下单（含竞价阶段）。"""
    return SESSION.phase() != "closed"


def status_text(now: datetime | None = None) -> str:
    p = SESSION.phase()
    if CFG.market_mode == "247":
        return {"auction_open": "🟡 开盘集合竞价（7×24 演示模式）",
                "continuous": "🟢 开市（7×24 演示模式）",
                "auction_close": "🟠 收盘集合竞价（7×24 演示模式）",
                "closed": "🔴 已闭市"}[p]
    return {"auction_open": "🟡 开盘集合竞价中",
            "continuous": "🟢 连续竞价中",
            "auction_close": "🟠 收盘集合竞价中",
            "closed": "🔴 已闭市"}[p]


def phase(now: datetime | None = None) -> str:
    """兼容旧接口：直接转调阶段机。"""
    return SESSION.phase()


class SessionClock:
    """交易阶段机。"""

    def __init__(self, mode: str | None = None):
        self.mode = mode or CFG.market_mode
        self.started = time.time()

    def reset(self, now: float | None = None) -> None:
        """虚拟交易日开始（247 模式）时调用。"""
        self.started = now if now is not None else time.time()

    # ── 阶段 ─────────────────────────────────────────────
    def phase(self, now: float | None = None) -> str:
        if not CFG.auction_enabled:
            return "continuous" if self._legacy_open(now) else "closed"
        if self.mode == "247":
            elapsed = (now if now is not None else time.time()) - self.started
            if elapsed < CFG.auction_seconds:
                return "auction_open"
            if elapsed >= CFG.day_seconds - CFG.closing_auction_seconds:
                return "auction_close"
            return "continuous"
        now_dt = datetime.fromtimestamp(now, TZ) if now is not None \
            else datetime.now(TZ)
        if not is_trading_day(now_dt):
            return "closed"
        t = now_dt.time()
        if _AUCTION_OPEN[0] <= t < _AUCTION_OPEN[1]:
            return "auction_open"
        if _AUCTION_CLOSE[0] <= t <= _AUCTION_CLOSE[1]:
            return "auction_close"
        for a, b in _CONTINUOUS:
            if a <= t <= b:
                return "continuous"
        return "closed"

    def _legacy_open(self, now: float | None = None) -> bool:
        """关闭竞价特性时的简单开市判断。"""
        if self.mode == "247":
            return True
        now_dt = datetime.fromtimestamp(now, TZ) if now is not None \
            else datetime.now(TZ)
        if not is_trading_day(now_dt):
            return False
        t = now_dt.time()
        if _AUCTION_OPEN[0] <= t < _AUCTION_OPEN[1]:
            return True
        if _AUCTION_CLOSE[0] <= t <= _AUCTION_CLOSE[1]:
            return True
        return any(a <= t <= b for a, b in _CONTINUOUS)

    def is_auction(self, now: float | None = None) -> bool:
        return self.phase(now) in ("auction_open", "auction_close")

    def auction_label(self, now: float | None = None) -> str:
        return "开盘集合竞价" if self.phase(now) == "auction_open" \
            else "收盘集合竞价"

    def cancellable(self, now: float | None = None) -> bool:
        """竞价撤单窗口：开盘竞价前段可撤（真实 9:15–9:20），收盘竞价一律不可撤。"""
        p = self.phase(now)
        if p != "auction_open":
            return False
        if self.mode == "247":
            elapsed = (now if now is not None else time.time()) - self.started
            return elapsed < CFG.auction_seconds * CFG.auction_cancel_ratio
        return datetime.now(TZ).time() < dtime(9, 20)

    def seconds_to_day_close(self, now: float | None = None) -> float:
        if self.mode != "247":
            return 0.0
        elapsed = (now if now is not None else time.time()) - self.started
        return max(CFG.day_seconds - elapsed, 0.0)


SESSION = SessionClock()
