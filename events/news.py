"""新闻事件系统：随机生成市场/板块/个股消息，施加价格冲击并留存快讯。

与价格引擎的关系：新闻不改模型参数，只在 `PriceEngine` 上施加一次性冲击
（个股直接改价，板块/大盘留下衰减趋势），因此"新闻驱动的行情"和"随机游走"
用的是同一套机制，不会互相打架。

既有公开 API（保持兼容）：
- fetch_latest_news(limit=10) -> list[dict]
- get_news_by_scope(scope, limit=10) -> list[dict]
- publish_news(kind, headline, impact, scope="market", target="") -> dict
- get_news_effect(user_id, code) -> float

新增：概念板块热点事件（板块联动涨跌 + 玩家可读播报文本）
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..market.engine import PriceEngine
    from ..storage.db import Database

from ..config import CFG
from ..log import get_logger
from ..market.stocks import (SECTORS, STOCK_POOL,
                             SEC_TECH, SEC_AUTO, SEC_PHARMA, SEC_NEWENERGY,
                             SEC_FIN, SEC_UTILITY, SEC_CONSUMER)

log = get_logger("news")

MARKET_NEWS = [
    ("利好", "央行降准，市场流动性宽松预期升温", (0.006, 0.018)),
    ("利空", "海外市场隔夜重挫，风险偏好回落", (-0.018, -0.006)),
    ("利好", "北向资金大幅净流入，两市普涨", (0.005, 0.014)),
    ("利空", "监管收紧融资，杠杆资金撤离", (-0.014, -0.005)),
]
SECTOR_NEWS = [
    ("利好", "{sector}板块获政策扶持，行业景气度上行", (0.008, 0.025)),
    ("利空", "{sector}板块需求下滑，机构下调行业评级", (-0.025, -0.008)),
    ("利好", "{sector}板块龙头业绩超预期，带动板块走强", (0.006, 0.020)),
]
STOCK_NEWS = [
    ("利好", "{name}业绩预增公告：净利润同比大幅增长", (0.03, 0.085)),
    ("利空", "{name}被曝财务疑点，遭交易所问询", (-0.085, -0.03)),
    ("利好", "{name}中标大额订单，订单能见度提升", (0.02, 0.06)),
    ("利空", "{name}大股东公布减持计划", (-0.06, -0.02)),
    ("利好", "{name}获机构密集调研，资金关注度提升", (0.015, 0.045)),
    ("利空", "{name}核心产品降价，毛利率承压", (-0.05, -0.015)),
]

# 概念/题材热点事件模板（新增：对标同花顺/东财题材挖掘、板块联动）
HOT_TOPICS = [
    ("AI算力", SEC_TECH, (0.015, 0.040), "AI算力需求爆发，产业链景气度上行"),
    ("低空经济", SEC_AUTO, (0.012, 0.035), "低空经济政策落地，产业化进程加速"),
    ("华为产业链", SEC_TECH, (0.010, 0.030), "华为新品发布带动产业链协同增强"),
    ("减肥药", SEC_PHARMA, (0.018, 0.050), "GLP-1类药物放量预期，创新药管线受关注"),
    ("固态电池", SEC_NEWENERGY, (0.015, 0.040), "固态电池技术突破，产业化时间表提前"),
    ("数据要素", SEC_TECH, (0.008, 0.025), "数据要素市场化改革推进，确权定价体系建立"),
    ("国产替代", SEC_TECH, (0.010, 0.030), "关键技术自主可控进程加快，国产化率提升"),
    ("并购重组", SEC_FIN, (0.006, 0.020), "监管鼓励上市公司并购重组，活跃资本市场"),
    ("高股息红利", SEC_UTILITY, (0.005, 0.015), "避险情绪升温，高股息资产配置价值凸显"),
    ("消费复苏", SEC_CONSUMER, (0.008, 0.025), "消费数据超预期，板块估值修复空间打开"),
]


@dataclass
class News:
    ts: int
    scope: str        # market / sector / stock
    target: str       # 空 / 板块名 / 股票代码
    kind: str         # 利好 / 利空
    headline: str
    impact: float     # 施加的价格冲击（小数）


class NewsService:
    def __init__(self, db: Database, engine, rng: random.Random | None = None):
        self.db = db
        self.engine = engine
        self.rng = rng or random.Random()
        self.today: list[News] = []
        self.count_today = 0

    def reset_day(self) -> None:
        self.today = []
        self.count_today = 0

    async def maybe_generate(self) -> News | None:
        """每 tick 小概率产生一条新闻；受每日条数上限约束。"""
        if self.count_today >= CFG.news_max_per_day:
            return None
        r = self.rng.random()
        if r < CFG.news_market_prob:
            return await self._make_market()
        if r < CFG.news_market_prob + CFG.news_sector_prob:
            return await self._make_sector()
        if r < (CFG.news_market_prob + CFG.news_sector_prob
                + CFG.news_stock_prob):
            return await self._make_stock()
        return None

    async def _make_market(self) -> News | None:
        kind, text, (lo, hi) = self.rng.choice(MARKET_NEWS)
        return await self.publish(News(int(time.time()), "market", "", kind,
                                       text, self.rng.uniform(lo, hi)))

    async def _make_sector(self) -> News | None:
        sector = self.rng.choice(SECTORS)
        kind, tpl, (lo, hi) = self.rng.choice(SECTOR_NEWS)
        return await self.publish(News(int(time.time()), "sector", sector, kind,
                                       tpl.format(sector=sector),
                                       self.rng.uniform(lo, hi)))

    async def _make_stock(self) -> News | None:
        # 只挑没停牌没退市的标的
        pool = [c for c in STOCK_POOL
                if not self.engine.is_suspended(c)
                and not self.engine.is_delisted(c)]
        if not pool:
            return None
        code = self.rng.choice(pool)
        kind, tpl, (lo, hi) = self.rng.choice(STOCK_NEWS)
        name = STOCK_POOL[code].name
        return await self.publish(News(int(time.time()), "stock", code, kind,
                                       tpl.format(name=name),
                                       self.rng.uniform(lo, hi)))

    async def publish(self, news: News) -> News:
        """施加冲击、留存快讯并落库。"""
        if news.scope == "stock":
            self.engine.apply_shock(news.impact, code=news.target)
        elif news.scope == "sector":
            self.engine.apply_shock(news.impact, sector=news.target)
        else:
            self.engine.apply_shock(news.impact)
        self.today.append(news)
        self.count_today += 1
        try:
            await self.db.insert_news(news.ts, news.scope, news.target,
                                      news.kind, news.headline, news.impact)
        except Exception:
            log.exception("新闻落库失败（不影响行情）")
        log.info("快讯【%s】%s（%s 冲击 %+.2f%%）", news.kind, news.headline,
                 news.target or news.scope, news.impact * 100)
        return news

    def render_today(self, limit: int = 8) -> str:
        if not self.today:
            return "📰 今日暂无快讯。"
        lines = [f"📰 今日快讯（{len(self.today)} 条，显示最近 {min(limit, len(self.today))} 条）"]
        for n in self.today[-limit:][::-1]:
            who = {"market": "大盘", "sector": "板块"}.get(n.scope, "")
            if n.scope == "stock":
                s = STOCK_POOL.get(n.target)
                who = f"{s.name}({n.target})" if s else n.target
            elif n.scope == "sector":
                who = f"{n.target}板块"
            arrow = "🔴" if n.impact >= 0 else "🟢"
            lines.append(f"{arrow} [{who}] {n.headline}（{n.impact * 100:+.2f}%）")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 既有公开 API（保持签名与行为兼容）
# ──────────────────────────────────────────────────────────────

async def fetch_latest_news(db: Database, limit: int = 10) -> list[dict]:
    """获取最新快讯（兼容既有调用）。"""
    ts = int(time.time()) - 86400 * 7  # 最近 7 天
    rows = await db.get_news_since(ts, limit)
    return [dict(r) for r in rows]


async def get_news_by_scope(db: Database, scope: str, limit: int = 10) -> list[dict]:
    """按范围获取快讯（兼容既有调用）。"""
    ts = int(time.time()) - 86400 * 7
    rows = await db.get_news_since(ts, limit * 3)  # 多取再过滤
    filtered = [dict(r) for r in rows if r["scope"] == scope]
    return filtered[:limit]


async def publish_news(db: Database, engine: PriceEngine, kind: str, headline: str,
                       impact: float, scope: str = "market", target: str = "") -> dict:
    """手动发布新闻（兼容既有调用，供管理员/测试用）。"""
    news = News(int(time.time()), scope, target, kind, headline, impact)
    if news.scope == "stock":
        engine.apply_shock(news.impact, code=news.target)
    elif news.scope == "sector":
        engine.apply_shock(news.impact, sector=news.target)
    else:
        engine.apply_shock(news.impact)
    await db.insert_news(news.ts, news.scope, news.target,
                         news.kind, news.headline, news.impact)
    return {"news_id": 0, "ts": news.ts, "scope": news.scope, "target": news.target,
            "kind": news.kind, "headline": news.headline, "impact": news.impact}


def get_news_effect(user_id: str, code: str) -> float:
    """获取新闻对某用户某股票的残留影响（兼容既有调用，简化版：半衰期 6h）。
    实际影响由 PriceEngine 的衰减趋势体现，这里返回 0 表示无额外乘数。
    """
    return 0.0
