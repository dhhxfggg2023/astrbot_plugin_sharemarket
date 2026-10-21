"""龙虎榜 / 主力资金流 / 异动榜 —— 纯计算模块，无 IO、无 DB、可单测。

输入：行情快照（来自 market.engine.PriceEngine）+ 当日成交汇总
输出：三榜结构化数据，字段名固定，便于渲染层直接使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..market.engine import PriceEngine, Quote
    from ..market.stocks import Stock


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Seat:
    """龙虎榜单个席位"""
    rank: int
    name: str              # 席位名称（如 "机构专用"、"沪股通"、"营业部名称"）
    buy_amount: float      # 买入额（元）
    sell_amount: float     # 卖出额（元）
    net_amount: float      # 净买入（元，正=净买）


@dataclass(frozen=True)
class FundFlowItem:
    """主力资金净流入榜单条"""
    rank: int
    code: str
    name: str
    sector: str
    net_inflow: float      # 净流入（元，正=净流入）
    price: float           # 现价
    change_pct: float      # 涨跌幅（%）


@dataclass(frozen=True)
class AnomalyItem:
    """异动榜单条"""
    rank: int
    code: str
    name: str
    sector: str
    anomaly_type: str      # "涨幅异动" / "跌幅异动" / "换手异动" / "振幅异动"
    value: float           # 对应指标数值（涨跌幅%、换手率%、振幅%）
    threshold: float       # 触发阈值
    price: float
    change_pct: float


# ──────────────────────────────────────────────────────────────
# 龙虎榜：模拟买卖前五席位（对标同花顺/东方财富）
# ──────────────────────────────────────────────────────────────

# 预设席位名称池（模拟真实券商营业部/机构专用/沪深股通）
SEAT_NAMES = [
    "机构专用",
    "沪股通专用",
    "深股通专用",
    "华泰证券上海武定路",
    "中信证券深圳滨海大道",
    "东方财富拉萨团结路第二",
    "光大证券北京金融街",
    "国泰君安上海虹桥路",
    "申万宏源深圳红岭路",
    "海通证券上海东方路",
    "银河证券北京金融大街",
    "广发证券广州天河路",
    "招商证券深圳益田路",
    "中金公司北京金融街",
    "浙商证券杭州庆春路",
]


def _simulate_seats_for_stock(
    code: str,
    quote: Quote,
    stock: Stock,
    rng_seed: int,
) -> list[Seat]:
    """为单只股票生成模拟龙虎榜席位数据（确定性伪随机，同一快照结果固定）。

    种子必须跨进程稳定：不能用内置 hash()（PYTHONHASHSEED 随机化，
    重启后同一快照会换一批席位，与 docstring 承诺相反）。用 crc32。
    """
    import random
    import zlib
    stable = zlib.crc32(str(code).encode("utf-8"))
    r = random.Random(rng_seed ^ stable)

    # 基础成交额 = 成交量 * 现价
    base_volume = quote.volume
    base_amount = base_volume * quote.price

    # 买卖前五：各席位买入/卖出额在总成交额的 5%~25% 之间
    seats: list[Seat] = []
    used_names = r.sample(SEAT_NAMES, k=min(5, len(SEAT_NAMES)))

    for i, name in enumerate(used_names):
        rank = i + 1
        # 买入额：总成交额的 8%~22%
        buy_pct = r.uniform(0.08, 0.22)
        buy_amount = base_amount * buy_pct
        # 卖出额：总成交额的 6%~20%
        sell_pct = r.uniform(0.06, 0.20)
        sell_amount = base_amount * sell_pct
        net_amount = buy_amount - sell_amount
        seats.append(Seat(rank, name, round(buy_amount, 2), round(sell_amount, 2), round(net_amount, 2)))

    return seats


def get_dragon_tiger(
    engine: PriceEngine,
    stock_pool: dict[str, Stock],
    limit: int = 5,
    rng_seed: int = 0xC0FFEE,
) -> dict[str, list[Seat]]:
    """龙虎榜：返回 {code: [Seat, ...]}，仅包含当日成交量 > 0 的股票。

    - 席位按净买入额由大到小排序
    - 并列时按买入额降序
    - 不足 5 个席位时返回实际数量
    - 零成交股票不出现在结果中
    """
    result: dict[str, list[Seat]] = {}

    for code, quote in engine.quotes.items():
        if quote.volume <= 0:
            continue
        stock = stock_pool.get(code)
        if not stock:
            continue

        seats = _simulate_seats_for_stock(code, quote, stock, rng_seed)
        # 按净买入降序、买入额降序
        seats_sorted = sorted(seats, key=lambda s: (-s.net_amount, -s.buy_amount))
        # 重新编号
        for i, s in enumerate(seats_sorted[:limit]):
            result.setdefault(code, []).append(Seat(i + 1, s.name, s.buy_amount, s.sell_amount, s.net_amount))

    return result


# ──────────────────────────────────────────────────────────────
# 主力资金净流入榜
# ──────────────────────────────────────────────────────────────

def get_fund_flow_ranking(
    engine: PriceEngine,
    stock_pool: dict[str, Stock],
    limit: int = 20,
    min_inflow: float = 1.0,   # 最小记录金额（元），content_spec.md: 资金流最小记录1金
) -> list[FundFlowItem]:
    """主力资金净流入榜：按净流入降序，过滤掉 |net_inflow| < min_inflow。

    返回结构稳定：rank/code/name/sector/net_inflow/price/change_pct
    """
    items: list[FundFlowItem] = []

    for code, quote in engine.quotes.items():
        stock = stock_pool.get(code)
        if not stock:
            continue

        net_inflow = engine.fund_flow(code)
        if abs(net_inflow) < min_inflow:
            continue

        items.append(FundFlowItem(
            rank=0,  # 稍后填入
            code=code,
            name=stock.name,
            sector=stock.sector,
            net_inflow=round(net_inflow, 2),
            price=quote.price,
            change_pct=round(quote.change_pct, 2),
        ))

    # 按净流入降序排序
    items.sort(key=lambda x: -x.net_inflow)
    # 截取并重新编号
    top = items[:limit]
    for i, item in enumerate(top):
        # 创建新的 FundFlowItem 因为是 frozen=True
        top[i] = FundFlowItem(i + 1, item.code, item.name, item.sector,
                               item.net_inflow, item.price, item.change_pct)

    return top


# ──────────────────────────────────────────────────────────────
# 异动榜：涨跌幅 / 换手率 / 振幅 超阈值
# ──────────────────────────────────────────────────────────────

# 阈值参数（对标同花顺/东方财富异动榜常用标准；换手率按游戏成交量尺度标定）
ANOMALY_THRESHOLDS = {
    "涨幅异动": 7.0,      # 涨幅 ≥ 7%
    "跌幅异动": -7.0,     # 跌幅 ≤ -7%
    # 游戏口径换手率 = 成交股数 / 1e8 股流通盘 × 100。一个虚拟交易日的典型
    # 成交量约 60~90 万股（0.6%~0.9%），1.5% 已是明显放量；真实 A 股的
    # 20% 阈值在这个成交量尺度下永远不可达（旧实现因此从不触发）。
    "换手异动": 1.5,
    "振幅异动": 15.0,     # 振幅 ≥ 15%
}


def get_anomaly_ranking(
    engine: PriceEngine,
    stock_pool: dict[str, Stock],
    limit: int = 20,
) -> list[AnomalyItem]:
    """异动榜：检测涨跌幅/换手/振幅超阈值的股票。

    返回结构稳定：rank/code/name/sector/anomaly_type/value/threshold/price/change_pct
    同一股票可能触发多种异动，每种单独一条记录。
    """
    anomalies: list[AnomalyItem] = []

    for code, quote in engine.quotes.items():
        stock = stock_pool.get(code)
        if not stock:
            continue

        # 涨跌幅异动
        if quote.change_pct >= ANOMALY_THRESHOLDS["涨幅异动"]:
            anomalies.append(AnomalyItem(
                rank=0, code=code, name=stock.name, sector=stock.sector,
                anomaly_type="涨幅异动", value=round(quote.change_pct, 2),
                threshold=ANOMALY_THRESHOLDS["涨幅异动"],
                price=quote.price, change_pct=round(quote.change_pct, 2),
            ))
        elif quote.change_pct <= ANOMALY_THRESHOLDS["跌幅异动"]:
            anomalies.append(AnomalyItem(
                rank=0, code=code, name=stock.name, sector=stock.sector,
                anomaly_type="跌幅异动", value=round(quote.change_pct, 2),
                threshold=ANOMALY_THRESHOLDS["跌幅异动"],
                price=quote.price, change_pct=round(quote.change_pct, 2),
            ))

        # 振幅 = (high - low) / prev_close * 100
        if quote.prev_close > 0:
            amplitude = (quote.high - quote.low) / quote.prev_close * 100
            if amplitude >= ANOMALY_THRESHOLDS["振幅异动"]:
                anomalies.append(AnomalyItem(
                    rank=0, code=code, name=stock.name, sector=stock.sector,
                    anomaly_type="振幅异动", value=round(amplitude, 2),
                    threshold=ANOMALY_THRESHOLDS["振幅异动"],
                    price=quote.price, change_pct=round(quote.change_pct, 2),
                ))

        # 换手率（游戏口径）：成交股数 / 1e8 股流通盘 × 100。
        # 引擎的 quote.volume 是当日累计成交股数（每 tick 约 2~3 千股，
        # 一个虚拟交易日约 60~90 万股），所以不能再乘价格——旧实现
        # ``volume * price / (prev_close * 1e8)`` 恒等于 volume/1e8 的
        # 万分之几，20% 阈值永远达不到。
        turnover = quote.volume / 1e8 * 100
        if turnover >= ANOMALY_THRESHOLDS["换手异动"]:
            anomalies.append(AnomalyItem(
                rank=0, code=code, name=stock.name, sector=stock.sector,
                anomaly_type="换手异动", value=round(turnover, 2),
                threshold=ANOMALY_THRESHOLDS["换手异动"],
                price=quote.price, change_pct=round(quote.change_pct, 2),
            ))

    # 排序：按 value 绝对值降序（异动程度）
    anomalies.sort(key=lambda x: -abs(x.value))
    top = anomalies[:limit]
    for i, item in enumerate(top):
        top[i] = AnomalyItem(i + 1, item.code, item.name, item.sector,
                              item.anomaly_type, item.value, item.threshold,
                              item.price, item.change_pct)

    return top


# ──────────────────────────────────────────────────────────────
# 统一导出：一次性获取三榜（供指令层调用）
# ──────────────────────────────────────────────────────────────

def get_all_rankings(
    engine: PriceEngine,
    stock_pool: dict[str, Stock],
    dt_limit: int = 5,
    ff_limit: int = 20,
    an_limit: int = 20,
) -> tuple[dict[str, list[Seat]], list[FundFlowItem], list[AnomalyItem]]:
    """一次性获取龙虎榜、资金流榜、异动榜。"""
    dt = get_dragon_tiger(engine, stock_pool, limit=dt_limit)
    ff = get_fund_flow_ranking(engine, stock_pool, limit=ff_limit)
    an = get_anomaly_ranking(engine, stock_pool, limit=an_limit)
    return dt, ff, an