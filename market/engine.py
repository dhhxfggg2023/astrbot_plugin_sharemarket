"""行情引擎（P1）：三层因子 + 肥尾冲击 + 黑天鹅跳空 + 虚拟主力 + 情绪指数。

设计取向（用户明确要求）：**像真实股市一样凶险，且大体不可预测**。
- 个股冲击用 t 分布（肥尾）：极端行情比正态更常见
- 大盘/个股都有罕见的跳空（暴跌、暴雷、暴涨），冲击带衰减
- 均值回归被压到很弱（`CFG.mean_revert=0.00005`）→ 价格更接近纯随机游走，
  没有"跌多了就会回来"的隐性保护
- 虚拟主力在暗处吸筹/拉升/派发/砸盘，制造放量长阳与阴跌（状态不外露，
  只暴露"主力净流入"这类真实市场也有的数据）

每 tick 收益率：
    ret = beta·dM + sector_beta·dSec + idio(肥尾) + 主力效应 + 跳空 + 弱均值回归 + 供需冲击
所有"日口径"波动率按 k = 1/√(每虚拟日 tick 数) 折算；持续性因子的累积因子
必须用 n/(1-φ)² − 2φ(1-φⁿ)/(1-φ)³（见 `_factor_sigmas`）。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from ..config import CFG
from ..log import get_logger
from .stocks import (BOARD_BSE, BOARD_GEM, BOARD_STAR, SECTORS, STOCK_POOL,
                     Stock)

log = get_logger("engine")

BASE_MARKET_DAILY_VOL = 0.012    # 大盘基础日波动（再乘 CFG.vol_scale）
BASE_SECTOR_DAILY_VOL = 0.020    # 板块基础日波动（再乘 CFG.vol_scale）
MARKET_PHI = 0.9                 # 大盘因子 AR(1) 持续系数
SECTOR_PHI = 0.9                 # 板块因子 AR(1) 持续系数
FACTOR_NOISE_SHARE = 0.35        # 因子中"当日独立噪声"占比
ANCHOR_ALPHA = 0.0001            # 锚（慢速 EMA）更新速度
AMBIENT_VOLUME = 0.4             # 每 tick 基础成交量 = CFG.mm_depth × 该系数

# 虚拟主力：状态 → (每 tick 收益效应, 成交量倍数)
# 效应刻意小于板块因子：主力制造的是"个股行情"，不该盖过板块联动
MF_EFFECT = {"观望": 0.0, "吸筹": 0.0002, "拉升": 0.0004,
             "派发": -0.0003, "砸盘": -0.0012}
MF_VOLUME = {"观望": 0.6, "吸筹": 1.8, "拉升": 3.0, "派发": 1.9, "砸盘": 3.2}
MF_TRANS = {                     # 当前状态 → [(下一状态, 概率)]，其余概率回落到观望
    "观望": [("吸筹", 0.020), ("派发", 0.018)],
    "吸筹": [("拉升", 0.050)],
    "拉升": [("派发", 0.090)],
    "派发": [("砸盘", 0.050)],
    "砸盘": [("观望", 0.200)],
}
MF_DURATION = {"观望": (30, 200), "吸筹": (120, 420), "拉升": (30, 120),
               "派发": (120, 420), "砸盘": (20, 90)}
MF_INFLOW_DIR = {"吸筹": 1, "拉升": 1, "派发": -1, "砸盘": -1, "观望": 0}


@dataclass
class MainForce:
    """虚拟主力（庄家）：状态不外露，只对外给"净流入"。"""
    state: str = "观望"
    ticks_left: int = 0
    net_inflow: float = 0.0      # 当日净流入（元，正=净买）


@dataclass
class Bar:
    """一根 K 线（分时用）。"""
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass
class Quote:
    code: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: int = 0
    pct_override: float | None = None   # ST 戴帽/摘帽等动态涨跌幅覆盖

    @property
    def change_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100

    def limit_pct_of(self, stock: Stock) -> float:
        return self.pct_override if self.pct_override is not None \
            else stock.limit_pct

    def limit_up(self, stock: Stock) -> float:
        return round(self.prev_close * (1 + self.limit_pct_of(stock)), 2)

    def limit_down(self, stock: Stock) -> float:
        return round(self.prev_close * (1 - self.limit_pct_of(stock)), 2)


class PriceEngine:
    def __init__(self, seed: int | None = None,
                 tick_seconds: float | None = None,
                 day_seconds: float | None = None):
        self.rng = random.Random(seed)
        self.quotes: dict[str, Quote] = {}
        self.market_ret = 0.0          # 本 tick 大盘收益率（含跳空）
        self.paused = False            # 管理员暂停行情推进
        self.anchor: dict[str, float] = {}
        self._raw: dict[str, float] = {}    # 全精度内部价（展示价取整到 0.01）
        self._mkt_mom = 0.0
        self._sector_mom: dict[str, float] = {}
        self._jump = 0.0               # 大盘跳空冲击（衰减中）
        self._senti = 0.0              # 情绪原始值（-1..1）
        self.main_force: dict[str, MainForce] = {}
        self.suspended: dict[str, int] = {}        # code -> 剩余停牌 tick 数
        self.resume_gaps: dict[str, float] = {}    # code -> 复牌跳空幅度（一次性）
        self.st_flag: dict[str, bool] = {c: s.is_st for c, s in STOCK_POOL.items()}
        self.delisted: set[str] = set()
        # 逐项收益分解累计（标定用：查"长期漂移是谁带来的"，不参与业务逻辑）
        self.decomp: dict[str, float] = {
            "mkt": 0.0, "sector": 0.0, "idio": 0.0, "mf": 0.0,
            "drift": 0.0, "jump": 0.0, "shock": 0.0, "impact": 0.0}
        self._tick_seconds = tick_seconds or CFG.tick_seconds
        self._day_seconds = day_seconds or CFG.day_seconds
        # 分时 K 线：每 CFG.bars_per_day 根一个虚拟交易日
        self.ticks_per_bar = max(1, int(round(
            self.ticks_per_day / max(CFG.bars_per_day, 1))))
        self.bars: dict[str, list[Bar]] = {c: [] for c in STOCK_POOL}
        self._bar_seq = 0
        self._bar_tick = 0
        self._bar_roll = False
        for s in STOCK_POOL.values():
            p = s.initial_price
            self.quotes[s.code] = Quote(s.code, p, p, p, p, p)
            self.anchor[s.code] = p
            self._raw[s.code] = p
            self.main_force[s.code] = MainForce(
                ticks_left=self.rng.randint(*MF_DURATION["观望"]))

    # ── 尺度 ─────────────────────────────────────────────
    @property
    def ticks_per_day(self) -> float:
        return max(self._day_seconds / max(self._tick_seconds, 1e-6), 1.0)

    @property
    def k(self) -> float:
        """日口径 → 每 tick 的折算系数（仅用于独立同分布噪声）。"""
        return 1.0 / math.sqrt(self.ticks_per_day)

    def _factor_sigmas(self, target_daily: float, phi: float,
                       noise_share: float) -> tuple[float, float]:
        """把"日波动目标"折算成 AR(1) 因子的两份每 tick 标准差。

        持续性成分的 n 期累积方差是 σ_u²·[n/(1-φ)² − 2φ(1-φⁿ)/(1-φ)³]，
        在 φ=0.9 / n=400 时约为 38200·σ_u²（≈195²），**不是** n·σ_u²（=400）；
        若按 √n 折算，日内波动会被放大近 10 倍 —— 实测踩过。
        """
        n = self.ticks_per_day
        phi_n = phi ** n
        var_sum = n / (1 - phi) ** 2 \
            - 2 * phi * (1 - phi_n) / (1 - phi) ** 3
        accum = math.sqrt(max(var_sum, 1.0))
        sig_ar = target_daily * math.sqrt(max(1.0 - noise_share ** 2, 0.0)) / accum
        sig_noise = target_daily * noise_share / math.sqrt(n)
        return sig_ar, sig_noise

    def _fat_z(self) -> float:
        """单位方差的 t 分布样本：比正态更容易出极端值（肥尾）。"""
        dof = max(int(CFG.fat_tail_dof), 3)
        g = self.rng.gauss(0, 1)
        chi = sum(self.rng.gauss(0, 1) ** 2 for _ in range(dof))
        t = g / math.sqrt(max(chi / dof, 1e-9))
        return t / math.sqrt(dof / (dof - 2))

    # ── 查询 ─────────────────────────────────────────────
    def get(self, code: str) -> Quote | None:
        return self.quotes.get(code)

    def mid(self, code: str) -> float:
        return self.quotes[code].price

    def market_change(self) -> float:
        """大盘涨跌幅（等权，展示用）。"""
        qs = self.quotes.values()
        return sum(q.change_pct for q in qs) / max(len(qs), 1)

    @property
    def sentiment(self) -> float:
        """市场情绪指数 0–100（50 中性；>75 过热，<25 冰点）。"""
        return 50.0 + 50.0 * self._senti

    def sector_members(self, sector: str) -> list[str]:
        return [c for c, s in STOCK_POOL.items() if s.sector == sector]

    def sector_change(self, sector: str) -> float:
        """板块涨跌幅（等权，展示用）。"""
        members = self.sector_members(sector)
        if not members:
            return 0.0
        return sum(self.quotes[c].change_pct for c in members) / len(members)

    def sector_stats(self) -> list[tuple[str, float]]:
        return sorted(((sec, self.sector_change(sec)) for sec in SECTORS),
                      key=lambda x: -x[1])

    def advancers(self) -> tuple[int, int, int]:
        """(上涨家数, 平盘, 下跌家数)。"""
        up = sum(1 for q in self.quotes.values() if q.change_pct > 0)
        down = sum(1 for q in self.quotes.values() if q.change_pct < 0)
        return up, len(self.quotes) - up - down, down

    def limit_counts(self) -> tuple[int, int]:
        """(涨停家数, 跌停家数)。"""
        up = down = 0
        for c, q in self.quotes.items():
            s = STOCK_POOL[c]
            if q.price >= q.limit_up(s):
                up += 1
            elif q.price <= q.limit_down(s):
                down += 1
        return up, down

    def fund_flow(self, code: str) -> float:
        """主力当日净流入（元，正=净买入）。"""
        mf = self.main_force.get(code)
        return mf.net_inflow if mf else 0.0

    def main_force_state(self, code: str) -> str:
        """主力状态（仅供日志/后台诊断，不面向玩家展示）。"""
        mf = self.main_force.get(code)
        return mf.state if mf else "观望"

    # ── 主力状态机 ───────────────────────────────────────
    def _mf_advance(self, mf: MainForce) -> None:
        transitions = MF_TRANS.get(mf.state, [])
        nxt = "观望"
        r = self.rng.random()
        acc = 0.0
        for state, p in transitions:
            acc += p
            if r < acc:
                nxt = state
                break
        if nxt != mf.state:
            log.debug("主力状态 %s → %s", mf.state, nxt)
        mf.state = nxt
        mf.ticks_left = self.rng.randint(*MF_DURATION.get(nxt, (30, 200)))

    # ── 推进 ─────────────────────────────────────────────
    def tick(self, pressures: dict[str, int] | None = None) -> None:
        """推进一个 tick。pressures: code -> 净买入股数（供需冲击）。"""
        pressures = pressures or {}
        if self.paused:
            return
        k = self.k

        # 0) 情绪 → 大盘反作用（过热压制、冰点托底；幅度刻意很小）
        d_m_extra = -CFG.sentiment_feedback * self._senti

        # 1) 大盘因子：AR(1) 动量 + 噪声 + 黑天鹅跳空
        sig_m_ar, sig_m_noise = self._factor_sigmas(
            BASE_MARKET_DAILY_VOL * CFG.vol_scale, MARKET_PHI,
            FACTOR_NOISE_SHARE)
        self._mkt_mom = MARKET_PHI * self._mkt_mom + self.rng.gauss(0, sig_m_ar)
        d_m = self._mkt_mom + self.rng.gauss(0, sig_m_noise) + d_m_extra

        if self.rng.random() < CFG.crash_prob:
            size = self.rng.uniform(*CFG.crash_size)
            self._jump -= size
            log.warning("黑天鹅：大盘暴跌冲击 -%.2f%%（当前累积跳空 %.2f%%）",
                        size * 100, self._jump * 100)
        if self.rng.random() < CFG.melt_prob:
            size = self.rng.uniform(*CFG.melt_size)
            self._jump += size
            log.info("狂欢：大盘跳空 +%.2f%%", size * 100)
        d_m += self._jump
        self._jump *= CFG.jump_decay
        self.market_ret = d_m

        # 2) 板块因子：每个板块一条独立 AR(1)
        sig_s_ar, sig_s_noise = self._factor_sigmas(
            BASE_SECTOR_DAILY_VOL * CFG.vol_scale, SECTOR_PHI,
            FACTOR_NOISE_SHARE)
        d_sec: dict[str, float] = {}
        for sec in SECTORS:
            prev = SECTOR_PHI * self._sector_mom.get(sec, 0.0) \
                + self.rng.gauss(0, sig_s_ar)
            self._sector_mom[sec] = prev
            d_sec[sec] = prev + self.rng.gauss(0, sig_s_noise)

        # 3) 个股：三层因子 + 主力 + 暴雷/暴涨 + 弱均值回归 + 供需冲击
        #    先判定 K 线是否收新的一根
        self._bar_tick += 1
        self._bar_roll = self._bar_tick >= self.ticks_per_bar
        if self._bar_roll:
            self._bar_tick = 0
            self._bar_seq += 1
        for code, q in self.quotes.items():
            if self.is_suspended(code) or code in self.delisted:
                continue          # 停牌/退市：不产生价格与成交量
            s = STOCK_POOL[code]
            idio = s.volatility * CFG.idio_scale * k * self._fat_z()
            drift = -CFG.mean_revert * math.log(self._raw[code] / self.anchor[code])
            impact = CFG.impact_k * (pressures.get(code, 0) / max(CFG.mm_depth, 1))
            impact = max(-0.02, min(0.02, impact))    # 单 tick 冲击封顶 ±2%

            mf = self.main_force[code]
            if CFG.main_force_enabled:
                if mf.ticks_left <= 0:
                    self._mf_advance(mf)
                mf.ticks_left -= 1
                mf_effect = MF_EFFECT.get(mf.state, 0.0)
            else:
                mf_effect = 0.0

            shock = 0.0
            if self.rng.random() < CFG.stock_jump_prob:
                if self.rng.random() < 0.65:      # 偏负：暴雷比暴涨更常见
                    shock = -self.rng.uniform(*CFG.stock_jump_neg)
                    log.info("个股暴雷：%s %s 冲击 -%.2f%%", s.name, code,
                             shock * 100)
                else:
                    shock = self.rng.uniform(*CFG.stock_jump_pos)

            ret = (s.beta * d_m + s.sector_beta * d_sec[s.sector] + idio
                   + mf_effect + shock + drift + impact)
            d = self.decomp
            d["mkt"] += s.beta * d_m
            d["sector"] += s.sector_beta * d_sec[s.sector]
            d["idio"] += idio
            d["mf"] += mf_effect
            d["shock"] += shock
            d["drift"] += drift
            d["impact"] += impact

            # 内部按全精度累积，展示价才取整到 0.01——否则低价股（1.8 元）
            # 每 tick 的理论波动不足一个最小变动单位，会被取整悄悄抹平
            raw = self._raw[code] * (1 + ret)
            raw = min(max(raw, q.limit_down(s)), q.limit_up(s))
            self._raw[code] = raw
            new_price = max(round(raw, 2), CFG.price_tick)

            if new_price != q.price:
                q.high = max(q.high, new_price)
                q.low = min(q.low, new_price)
                q.price = new_price

            # 锚（很慢的 EMA）跟随价格
            self.anchor[code] += ANCHOR_ALPHA * (q.price - self.anchor[code])

            # 主力净流入 + 环境成交量
            flow_dir = MF_INFLOW_DIR.get(mf.state, 0) if CFG.main_force_enabled else 0
            base_vol = CFG.mm_depth * AMBIENT_VOLUME
            tick_volume = int(base_vol * MF_VOLUME.get(mf.state, 1.0)
                              * self.rng.uniform(0.7, 1.3))
            q.volume += tick_volume
            if flow_dir:
                # 主力主动成交约占环境量的一成
                mf.net_inflow += flow_dir * q.price * tick_volume * 0.1

            self._append_bar(code, q, tick_volume)

        # 4) 情绪：由涨跌家数 + 大盘收益合成（EMA 平滑）
        up, _flat, down = self.advancers()
        breadth = (up - down) / max(len(self.quotes), 1)
        raw_senti = math.tanh(1.6 * breadth + self.market_ret * 30)
        self._senti = 0.97 * self._senti + 0.03 * raw_senti

        # 5) 停牌倒计时
        for code in list(self.suspended):
            self.suspended[code] -= 1
            if self.suspended[code] <= 0:
                del self.suspended[code]
                gap = self.resume_gaps.pop(code, 0.0)
                if gap:
                    self.apply_shock(gap, code=code)
                    log.info("复牌：%s %s（跳空 %+.2f%%）",
                             STOCK_POOL[code].name, code, gap * 100)
                else:
                    log.info("复牌：%s %s", STOCK_POOL[code].name, code)

    def on_fill(self, code: str, quantity: int) -> None:
        self.quotes[code].volume += quantity

    # ── 事件：停牌 / ST / 退市 / 新闻冲击 ─────────────────
    @staticmethod
    def _board_limit(stock: Stock) -> float:
        if stock.board in (BOARD_GEM, BOARD_STAR):
            return CFG.limit_growth
        if stock.board == BOARD_BSE:
            return CFG.limit_bse
        return CFG.limit_main

    def is_suspended(self, code: str) -> bool:
        return self.suspended.get(code, 0) > 0

    def is_delisted(self, code: str) -> bool:
        return code in self.delisted

    def is_st(self, code: str) -> bool:
        return bool(self.st_flag.get(code))

    def can_trade(self, code: str) -> tuple[bool, str]:
        """能否交易（供下单校验用）。"""
        if self.is_delisted(code):
            return False, "已退市，无法交易"
        if self.is_suspended(code):
            return False, "停牌中，无法交易"
        return True, ""

    def suspend(self, code: str, ticks: int, reason: str = "重大事项",
                resume_gap: float = 0.0) -> bool:
        """停牌 ticks 个 tick；resume_gap 为复牌时的跳空幅度（小数，如 0.05=+5%）。

        resume_gap 在复牌瞬间通过 apply_shock 生效（自动倒计时到期与
        手动 resume() 都会应用），并保证只生效一次。
        """
        if self.is_delisted(code) or self.is_suspended(code):
            return False
        self.suspended[code] = max(int(ticks), 1)
        self.resume_gaps[code] = resume_gap
        log.info("停牌：%s %s（%d tick，%s，复牌跳空 %+.2f%%）",
                 STOCK_POOL[code].name, code, self.suspended[code], reason,
                 resume_gap * 100)
        return True

    def resume(self, code: str, gap_pct: float = 0.0) -> None:
        if not self.is_suspended(code):
            return
        self.suspended.pop(code, None)
        gap = self.resume_gaps.pop(code, 0.0) or gap_pct
        if gap:
            self.apply_shock(gap, code=code)
        log.info("复牌：%s %s（跳空 %+.2f%%）", STOCK_POOL[code].name, code,
                 gap * 100)

    def set_st(self, code: str, flag: bool) -> None:
        """ST 戴帽/摘帽：涨跌幅限制随之切换。"""
        if self.st_flag.get(code) == flag:
            return
        self.st_flag[code] = flag
        q = self.quotes.get(code)
        s = STOCK_POOL.get(code)
        if q and s:
            q.pct_override = CFG.limit_st if flag else self._board_limit(s)
        log.warning("%s：%s %s（涨跌幅限制 → ±%.0f%%）",
                    "戴帽" if flag else "摘帽", STOCK_POOL[code].name, code,
                    (CFG.limit_st if flag else self._board_limit(s)) * 100)

    def delist(self, code: str) -> None:
        """退市整理：停止交易并从行情中移除该标的。"""
        self.delisted.add(code)
        self.suspended.pop(code, None)
        log.warning("退市：%s %s 终止上市", STOCK_POOL[code].name, code)

    def apply_shock(self, pct: float, code: str | None = None,
                    sector: str | None = None) -> None:
        """新闻冲击：个股直接改价；板块/大盘留下衰减趋势。"""
        if code:
            q = self.quotes.get(code)
            if not q or code in self.delisted:
                return
            s = STOCK_POOL[code]
            raw = self._raw[code] * (1 + pct)
            raw = min(max(raw, q.limit_down(s)), q.limit_up(s))
            self._raw[code] = raw
            new_price = max(round(raw, 2), CFG.price_tick)
            if new_price != q.price:
                q.high = max(q.high, new_price)
                q.low = min(q.low, new_price)
                q.price = new_price
        elif sector:
            self._sector_mom[sector] = self._sector_mom.get(sector, 0.0) + pct
        else:
            self._jump += pct

    def adjust_price(self, code: str, delta: float | None = None,
                     factor: float | None = None) -> bool:
        """除权除息基准价调整（game/corporate.py 分红/送转专用）。

        delta 为绝对额（现金分红除息，正数表示上调），factor 为比例
        （送转除权，如 1/1.5）。与 apply_shock 的区别：这是对"基准价"
        本身的修正，同样受当日涨跌停钳制且不得低于最小变动单位；
        内部全精度价同步修正，避免均值回归把除权效果立刻拉回。
        返回 False 表示无有效行情（未上市/已退市）或参数缺失。
        """
        q = self.quotes.get(code)
        if not q or code in self.delisted:
            return False
        if delta is None and factor is None:
            return False
        s = STOCK_POOL[code]
        new = q.price + delta if delta is not None else q.price * factor
        new = min(max(new, q.limit_down(s)), q.limit_up(s))
        new = max(round(new, 2), CFG.price_tick)
        q.price = new
        self._raw[code] = new
        log.info("除权除息：%s %s → %.2f（%s）", s.name, code, new,
                 f"绝对额 {delta:+.2f}" if delta is not None
                 else f"比例 ×{factor:.4f}")
        return True

    # ── 分时 K 线 ────────────────────────────────────────
    def _append_bar(self, code: str, q: Quote, vol: int) -> None:
        """把本 tick 的价格/成交量并入当前 K 线，必要时收新的一根。"""
        bars = self.bars[code]
        if self._bar_roll or not bars:
            bars.append(Bar(self._bar_seq, q.price, q.price, q.price, q.price, vol))
            if len(bars) > CFG.bars_keep:
                del bars[0:len(bars) - CFG.bars_keep]
        else:
            b = bars[-1]
            if q.price > b.high:
                b.high = q.price
            if q.price < b.low:
                b.low = q.price
            b.close = q.price
            b.volume += vol

    def intraday_bars(self, code: str, limit: int = 0) -> list[Bar]:
        """当前虚拟交易日的分时 K 线（最近 limit 根；limit=0 表示全部）。"""
        bars = self.bars.get(code, [])
        return bars[-limit:] if limit else list(bars)

    def day_close(self) -> list[tuple]:
        """日终：返回当日日 K 行供落库，并重置日内状态。"""
        rows: list[tuple] = []
        ts = int(time.time())
        for code, q in self.quotes.items():
            rows.append((code, "1d", ts, q.open, q.high, q.low, q.price, q.volume))
            self.bars[code].clear()
            q.prev_close = q.price
            q.open = q.price
            q.high = q.price
            q.low = q.price
            q.volume = 0
            mf = self.main_force.get(code)
            if mf:
                mf.net_inflow = 0.0
        self._bar_tick = 0
        self._bar_roll = False
        self._bar_seq += 1
        return rows

    def snapshot_rows(self) -> list[tuple]:
        return [
            (c, q.price, q.prev_close, q.open, q.high, q.low, q.volume)
            for c, q in self.quotes.items()
        ]

    def load_rows(self, rows) -> None:
        for r in rows:
            self.quotes[r["code"]] = Quote(
                r["code"], r["price"], r["prev_close"], r["open"],
                r["high"], r["low"], r["volume"])
            self.anchor[r["code"]] = r["price"]
            self._raw[r["code"]] = r["price"]

    # ── 新股上市登记（IPO 模块公开入口，P2）─────────────────
    def register(self, stock: Stock, price: float | None = None) -> bool:
        """把已发行股票接入行情状态（game/ipo.py 上市与重启恢复的唯一入口）。

        幂等：已登记的 code 只更新股票池属性，不重建行情状态（重复调用
        不会把价格打回发行价）。返回 True 表示本次新建了行情状态。
        """
        STOCK_POOL[stock.code] = stock
        self.st_flag[stock.code] = stock.is_st
        self.bars.setdefault(stock.code, [])
        if stock.code not in self.main_force:
            self.main_force[stock.code] = MainForce(
                ticks_left=self.rng.randint(*MF_DURATION["观望"]))
        if stock.code in self.quotes:
            return False
        p = price if price is not None else stock.initial_price
        self.quotes[stock.code] = Quote(stock.code, p, p, p, p, p)
        self.anchor[stock.code] = p
        self._raw[stock.code] = p
        return True
