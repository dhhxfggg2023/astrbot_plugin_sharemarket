"""虚拟股市插件入口（P0）。所有指令以 /炒股 开头。

日志策略：
- 业务模块用 `log.get_logger(name)`（标准库 logging，不依赖 astrbot）
- 启动时把 AstrBot 的 logger 桥接进来（群里/AstrBot 控制台可见）
- 同时在插件数据目录落一份轮转文件日志 sharemarket.log（事后对账）
- 成交、拒单、日终清算、tick 异常、心跳全部埋点
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .config import CFG
from .events.news import NewsService
from .game.account import AccountService
from .game.auction import AuctionService
from .game.corporate import CorporateService
from .game.dayclose import DayCloseService
from .game.ipo import IPOService
from .game.leaderboard import LeaderboardService
from .game.margin import MarginError, MarginService
from .game.trade import TradeError, TradeService
from .log import bind_astrbot_logger, get_logger, set_level, setup_file_logging
from .market import calendar
from .market.engine import PriceEngine
from .market.indicators import ma, macd, rsi, sparkline
from .market.orderbook import Matcher
from .market.stocks import STOCK_POOL
from .storage.db import Database

log = get_logger("main")

HELP = """📈 虚拟股市 · 指令一览
/炒股 开户            注册账户，领 100 万初始资金
/炒股 行情            大盘概览 + 涨跌幅榜
/炒股 个股 <代码>     个股详情
/炒股 盘口 <代码>     五档盘口
/炒股 搜 <关键词>     按名称/行业找股票
/炒股 k线 <代码> [分时|日]   K 线与技术指标
/炒股 买入 <代码> <数量> [价格]
/炒股 卖出 <代码> <数量> [价格]
/炒股 撤单 <单号>     撤销未成交委托
/炒股 持仓            我的持仓与盈亏
/炒股 委托            我的未成交委托
/炒股 成交            我最近的成交记录
/炒股 资金            可用 / 冻结 / 总资产
/炒股 排行            股神排行榜
/炒股 重生            破产后东山再起
/炒股 快讯            今日快讯（新闻/公司行为/停牌/退市）
/炒股 打新 [代码] [签数]  新股申购（无参数查面板）
/炒股 融资 <金额>        融资借款（无参数查信用账户）
/炒股 还钱 <金额>        归还融资（先息后本）
/炒股 融券 <代码> <数量> 融券卖出
/炒股 还券 <代码> <数量> 买券还券
/炒股 帮助            本说明
数量单位为股，100 股 = 1 手；T+1，当日买入次日可卖；
佣金万 2.5（最低 5 元）、印花税 0.05%（卖出）、过户费 0.001%。"""


class ShareMarket(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = self._resolve_data_dir()
        self.db = Database(str(self.data_dir / "sharemarket.db"))
        self.engine = PriceEngine()
        self.matcher = Matcher()
        self.accounts = AccountService(self.db)
        self.trade = TradeService(self.db, self.engine, self.matcher)
        self.auction = AuctionService(self.db, self.engine, self.trade)
        self.trade.auction = self.auction      # 互相引用
        self.news = NewsService(self.db, self.engine)
        self.corporate = CorporateService(self.db, self.engine, self.news)
        self.board = LeaderboardService(self.db)
        self.ipo = IPOService(self.db, self.engine)
        self.margin = MarginService(self.db, self.engine, self.trade)
        self.dayclose = DayCloseService(self.db, self.engine, self.matcher,
                                        self.auction, self.corporate, self.news,
                                        margin=self.margin, ipo=self.ipo)
        self._tick_task: asyncio.Task | None = None
        self._rate: dict[str, float] = {}      # D10 限流
        self._started = False
        self._last_day_close = time.time()
        self._ticks = 0                        # 已推进的 tick 数
        self._last_tick_ts = 0.0
        self._boot_ts = time.time()
        self._phase: str | None = None         # 交易阶段（用于阶段切换清算）

    # ── 生命周期 ──────────────────────────────────────────
    def _resolve_data_dir(self) -> Path:
        """数据目录：优先 AstrBot 官方 plugin_data（重装不丢），否则退回插件目录。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            return Path(get_astrbot_data_path()) / "plugin_data" / self.name
        except Exception:
            return Path(__file__).parent / "data"

    @filter.on_astrbot_loaded()
    async def _on_loaded(self):
        if self._started:
            return
        self._started = True
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            bind_astrbot_logger(astrbot_logger)
            set_level(CFG.log_level)
            log_file = setup_file_logging(self.data_dir, CFG.log_level)
            log.info("=" * 52)
            log.info("虚拟股市插件启动：数据目录=%s", self.data_dir)
            log.info("日志文件=%s（级别 %s）", log_file or "（未启用）",
                     CFG.log_level)
            log.info("市场模式=%s tick=%.1fs 虚拟交易日=%ds 股票池=%d 只",
                     CFG.market_mode, CFG.tick_seconds, CFG.day_seconds,
                     len(STOCK_POOL))

            await self.db.open()
            rows = await self.db.get_all_prices()
            if rows:
                self.engine.load_rows(rows)
                log.info("恢复行情：%d 只股票价格已载入", len(rows))
            else:
                log.info("首次运行：行情按初始价建立（%d 只）", len(STOCK_POOL))
            await self._restore_orders()
            listed = await self.ipo.restore()
            log.info("打新恢复：%d 只已上市新股已重新登记", listed)
            calendar.SESSION.reset()
            log.info("交易阶段机初始化：当前阶段 %s（竞价长度 %ds / 收盘竞价 %ds）",
                     calendar.status_text(), CFG.auction_seconds,
                     CFG.closing_auction_seconds)
            stats = await self.db.stats()
            log.info("账户 %d（活跃 %d / 破产 %d）持仓 %d 条 未成交委托 %d 笔 "
                     "历史成交 %d 笔 冻结资金合计 %.2f",
                     stats["accounts"], stats["active"], stats["bankrupt"],
                     stats["positions"], stats["open_orders"], stats["trades"],
                     stats["frozen_total"])
            self._tick_task = asyncio.create_task(self._tick_loop())
            log.info("行情引擎已启动")
        except Exception:
            log.exception("插件启动失败")
            raise

    async def _restore_orders(self):
        """重启后把数据库里 open/partial 委托重新挂回内存订单簿（不重新撮合）。"""
        n = 0
        for code in STOCK_POOL:
            for side in ("buy", "sell"):
                for row in await self.db.get_book_orders(code, side):
                    remaining = row["quantity"] - row["filled"]
                    if remaining <= 0:
                        continue
                    self.matcher.restore(row["order_id"], row["user_id"], code,
                                         side, row["price"], remaining)
                    n += 1
        if n:
            log.info("恢复挂单：%d 笔未成交委托已挂回订单簿", n)

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(CFG.tick_seconds)
            try:
                await self._maybe_day_close()
                if self.engine.paused:
                    continue
                if not calendar.is_open():
                    continue
                pressures = {c: self.matcher.take_pressure(c) for c in STOCK_POOL}
                self.engine.tick(pressures)
                for row in self.engine.snapshot_rows():
                    await self.db.upsert_price(*row)
                await self.db.commit_prices()
                self._ticks += 1
                self._last_tick_ts = time.time()
                # 新闻生成（每 tick 小概率）
                news = await self.news.maybe_generate()
                if news:
                    log.info("生成新闻：%s", news.headline)
                if CFG.heartbeat_ticks > 0 and self._ticks % CFG.heartbeat_ticks == 0:
                    await self._heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # 行情循环绝不能死
                log.exception("tick 异常（已跳过本 tick，循环继续）")

    async def _heartbeat(self):
        stats = await self.db.stats()
        quotes = self.engine.quotes.values()
        avg = sum(q.change_pct for q in quotes) / max(len(quotes), 1)
        log.info("心跳：tick=%d 均涨跌=%.2f%% 账户=%d 活跃=%d 破产=%d "
                 "未成交委托=%d 成交累计=%d 冻结=%.2f",
                 self._ticks, avg, stats["accounts"], stats["active"],
                 stats["bankrupt"], stats["open_orders"], stats["trades"],
                 stats["frozen_total"])

    async def _maybe_day_close(self):
        """到点时执行日终清算（委托过期解冻 + T+1 解锁 + 昨收更新）。"""
        now = time.time()
        due = False
        if CFG.market_mode == "247":
            due = now - self._last_day_close >= CFG.day_seconds
        else:
            # realtime：闭市瞬间清算一次（用 meta 记录当日是否已清算）
            today = datetime.now(calendar.TZ).strftime("%Y-%m-%d")
            closed = not calendar.is_open()
            done = await self.db.get_meta("last_close_date")
            due = closed and done != today
            if due:
                await self.db.set_meta("last_close_date", today)
        if due:
            await self.dayclose.run()
            self._last_day_close = now
            # 新虚拟交易日：阶段机归零（重新从开盘集合竞价开始）
            calendar.SESSION.reset(now)
            self._phase = None

    async def _maybe_auction_clear(self):
        """交易阶段切换时清算集合竞价（开盘竞价结束时必须结算一次）。"""
        phase = calendar.SESSION.phase()
        if phase == self._phase:
            return
        prev, self._phase = self._phase, phase
        log.info("交易阶段切换：%s → %s（%s）", prev or "启动", phase,
                 calendar.status_text())
        if prev == "auction_open":
            fills, shares, amount = await self.auction.run("开盘集合竞价")
            if fills:
                limit_up, limit_down = self.engine.limit_counts()
                log.info("开盘竞价成交 %d 笔 / %d 股 / %.2f 元；"
                         "当前涨停 %d 跌停 %d", fills, shares, amount,
                         limit_up, limit_down)

    async def terminate(self):
        log.info("插件卸载/停用：正在落盘并关闭")
        if self._tick_task:
            self._tick_task.cancel()
        try:
            for row in self.engine.snapshot_rows():
                await self.db.upsert_price(*row)
            await self.db.commit_prices()
            log.info("行情已落盘（%d 只），tick 累计 %d",
                     len(STOCK_POOL), self._ticks)
        except Exception:
            log.exception("终止时落盘失败")
        await self.db.close()
        self._started = False

    # ── 工具 ──────────────────────────────────────────────
    def _uid(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _price_of(self, code: str):
        q = self.engine.get(code)
        return q.price if q else None

    @staticmethod
    def _money(x: float) -> str:
        """金额格式化：亿 / 万 / 元。"""
        if abs(x) >= 1e8:
            return f"{x / 1e8:+.2f} 亿"
        if abs(x) >= 1e4:
            return f"{x / 1e4:+.2f} 万"
        return f"{x:+,.0f} 元"

    @staticmethod
    def _fmt(x, digits: int = 2) -> str:
        return "—" if x is None else f"{x:.{digits}f}"

    def _rate_limited(self, uid: str) -> bool:
        now = time.monotonic()
        last = self._rate.get(uid, 0)
        if now - last < CFG.trade_cooldown_seconds:
            return True
        self._rate[uid] = now
        return False

    async def _safe(self, event: AstrMessageEvent, cmd: str, fn):
        """统一异常兜底：业务拒绝记 WARNING，未预期异常打完整堆栈并记 ERROR。"""
        if not self._started:
            return "⚠️ 插件尚未就绪（正在初始化），请稍后再试。"
        uid = self._uid(event)
        try:
            result = await fn()
            log.debug("指令<%s> u=%s 完成", cmd, uid)
            return result
        except TradeError as e:
            log.warning("指令<%s> u=%s 被拒：%s", cmd, uid, e)
            return f"⚠️ {e}"
        except Exception:
            log.exception("指令<%s> u=%s 异常", cmd, uid)
            return "⚠️ 系统开小差了，已记录日志，请稍后重试。"

    # ── 指令组 ────────────────────────────────────────────
    @filter.command_group("炒股", alias={"stock", "cg"})
    def stock(self):
        pass

    @stock.command("帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        """查看全部指令"""
        yield event.plain_result(HELP)

    @stock.command("开户")
    async def register(self, event: AstrMessageEvent):
        """注册账户，领取初始资金"""
        uid = self._uid(event)
        name = event.get_sender_name()
        if not self._started:
            yield event.plain_result("⚠️ 插件尚未就绪，请稍后再试。")
            return
        try:
            ok, msg = await self.accounts.open_account(uid, name)
        except Exception:
            log.exception("指令<开户> u=%s 异常", uid)
            yield event.plain_result("⚠️ 系统开小差了，已记录日志，请稍后重试。")
            return
        if not ok:
            log.warning("指令<开户> u=%s 被拒：%s", uid, msg)
        yield event.plain_result(("✅ " if ok else "⚠️ ") + msg)

    @stock.command("行情")
    async def market(self, event: AstrMessageEvent):
        """大盘概览、板块榜与涨跌幅榜"""
        quotes = sorted(self.engine.quotes.values(),
                        key=lambda q: q.change_pct, reverse=True)
        up, flat, down = self.engine.advancers()
        avg = self.engine.market_change()
        sectors = self.engine.sector_stats()
        limit_up, limit_down = self.engine.limit_counts()
        senti = self.engine.sentiment
        mood = "🔥 过热" if senti >= 75 else ("🧊 冰点" if senti <= 25 else "😐 中性")
        lines = [f"📊 大盘行情  {calendar.status_text()}",
                 f"平均涨跌幅 {avg:+.2f}%　涨 {up} / 平 {flat} / 跌 {down}"
                 f"　涨停 {limit_up} / 跌停 {limit_down}",
                 f"情绪指数 {senti:.0f}/100 {mood}"]
        if sectors:
            top = " · ".join(f"{s}{v:+.2f}%" for s, v in sectors[:3])
            bottom = " · ".join(f"{s}{v:+.2f}%" for s, v in sectors[-3:])
            lines.append(f"🧩 强势板块：{top}")
            lines.append(f"🧊 弱势板块：{bottom}")
        lines.append("")
        lines.append("🔥 涨幅榜：")
        for q in quotes[:5]:
            s = STOCK_POOL[q.code]
            lines.append(f"  {s.name}({q.code}) {q.price:.2f} {q.change_pct:+.2f}%")
        lines.append("💧 跌幅榜：")
        for q in quotes[-5:]:
            s = STOCK_POOL[q.code]
            lines.append(f"  {s.name}({q.code}) {q.price:.2f} {q.change_pct:+.2f}%")
        yield event.plain_result("\n".join(lines))

    @stock.command("个股")
    async def quote_cmd(self, event: AstrMessageEvent, code: str):
        """个股详情，如 /炒股 个股 600001"""
        s = STOCK_POOL.get(code)
        q = self.engine.get(code)
        if not s or not q:
            yield event.plain_result(f"⚠️ 股票 {code} 不存在，用 /炒股 搜 查代码。")
            return
        lines = [
            f"{'📈' if q.change_pct >= 0 else '📉'} {s.name}（{code}）"
            f"{'[ST]' if s.is_st else ''}",
            f"现价 {q.price:.2f}　{q.change_pct:+.2f}%　昨收 {q.prev_close:.2f}",
            f"今开 {q.open:.2f}　最高 {q.high:.2f}　最低 {q.low:.2f}",
            f"涨停 {q.limit_up(s):.2f} / 跌停 {q.limit_down(s):.2f}"
            f"（±{s.limit_pct:.0%}）",
            f"板块：{s.sector}（{self.engine.sector_change(s.sector):+.2f}%）"
            f" · {s.industry}",
            f"成交量 {q.volume:,} 股　主力净流入 "
            f"{self._money(self.engine.fund_flow(code))}",
        ]
        yield event.plain_result("\n".join(lines))

    @stock.command("盘口")
    async def depth_cmd(self, event: AstrMessageEvent, code: str):
        """五档盘口"""
        q = self.engine.get(code)
        if not q or code not in STOCK_POOL:
            yield event.plain_result(f"⚠️ 股票 {code} 不存在。")
            return
        bids, asks = self.matcher.five_levels(code, q.price)
        lines = [f"📖 {STOCK_POOL[code].name}（{code}）五档盘口 "
                 f"现价 {q.price:.2f}"]
        for i, (p, v) in enumerate(asks[:5], 1):
            lines.append(f"卖{i}  {p:.2f}  {v:,}")
        lines.append("─" * 20)
        for i, (p, v) in enumerate(bids[:5], 1):
            lines.append(f"买{i}  {p:.2f}  {v:,}")
        yield event.plain_result("\n".join(lines))

    @stock.command("搜")
    async def search(self, event: AstrMessageEvent, keyword: str):
        """按名称/行业/代码搜股票"""
        hits = [s for s in STOCK_POOL.values()
                if keyword in s.name or keyword in s.industry or keyword in s.code]
        if not hits:
            yield event.plain_result(f"没有找到与「{keyword}」相关的股票。")
            return
        lines = [f"🔍 找到 {len(hits)} 只相关股票："]
        for s in hits[:10]:
            q = self.engine.get(s.code)
            lines.append(f"  {s.name}({s.code}) {s.board}·{s.industry} "
                         f"{q.price:.2f} {q.change_pct:+.2f}%")
        yield event.plain_result("\n".join(lines))

    @stock.command("k线")
    async def kline(self, event: AstrMessageEvent, code: str,
                    period: str = "分时"):
        """K 线图：/炒股 k线 600001 [分时|日]（有 Pillow 时发图片，否则文本）"""
        s = STOCK_POOL.get(code)
        if not s:
            yield event.plain_result(f"⚠️ 股票 {code} 不存在，用 /炒股 搜 查代码。")
            return
        q = self.engine.get(code)
        want_day = period.startswith("日") or period.lower() in ("d", "day", "日k")

        if not want_day:
            bars = self.engine.intraday_bars(code, 120)
            if not bars:
                yield event.plain_result(
                    f"本交易日还没有分时数据；日 K 请发 /炒股 k线 {code} 日")
                return
            closes = [b.close for b in bars]
            title = (f"{s.name}（{code}）分时　{q.price:.2f} "
                     f"{q.change_pct:+.2f}%")
            summary = [
                f"🕯️ {title}",
                f"本日区间 {min(b.low for b in bars):.2f} ~ "
                f"{max(b.high for b in bars):.2f}　{len(bars)} 根"
                f"（每根 {self.engine.ticks_per_bar} tick）",
                f"MA5 {self._fmt(ma(closes, 5))}　"
                f"MA10 {self._fmt(ma(closes, 10))}　"
                f"均量 {sum(b.volume for b in bars) // max(len(bars), 1):,} 股",
            ]
            if await self._send_chart(event, "intra", code, title, bars):
                yield event.plain_result("\n".join(summary))
                return
            yield event.plain_result("\n".join(
                summary[:1] + [sparkline(closes)] + summary[1:]))
            return

        rows = await self.db.get_candles(code, "1d", 60)
        if not rows:
            yield event.plain_result(
                f"{s.name} 还没有日 K 数据（每个交易日收盘后生成一根）。")
            return
        closes = [r["close"] for r in rows]
        title = f"{s.name}（{code}）日K　最近 {len(rows)} 个交易日"
        tail = rows[-10:]
        summary = [f"🕯️ {title}"]
        for r in tail:
            d = datetime.fromtimestamp(r["ts"]).strftime("%m-%d")
            chg = (r["close"] / r["open"] - 1) * 100 if r["open"] else 0.0
            summary.append(f"{d} 开{r['open']:.2f} 高{r['high']:.2f} "
                           f"低{r['low']:.2f} 收{r['close']:.2f} {chg:+.2f}% "
                           f"量{r['volume']:,}")
        dif, dea, hist = macd(closes)
        summary.append(f"MA5 {self._fmt(ma(closes, 5))}　"
                       f"MA10 {self._fmt(ma(closes, 10))}　"
                       f"RSI14 {self._fmt(rsi(closes, 14), 1)}")
        summary.append(
            f"MACD 样本不足（需 {26 + 9} 天，现有 {len(closes)} 天）"
            if dif is None else
            f"MACD DIF {dif:+.3f}　DEA {dea:+.3f}　柱 {hist:+.3f}")

        if await self._send_chart(event, "day", code, title, rows,
                                  allow_shrink=len(rows) > 30):
            yield event.plain_result("\n".join(summary[-3:]))
            return
        yield event.plain_result("\n".join(
            summary[:1] + [sparkline(closes)] + summary[1:]))

    async def _send_chart(self, event: AstrMessageEvent, kind: str, code: str,
                          title: str, bars: list,
                          allow_shrink: bool = False) -> bool:
        """尝试发 K 线图片；成功返回 True（失败返回 False，由调用方回退文本）。"""
        try:
            from .render import chart as chart_render
            if not chart_render.available():
                return False
            if allow_shrink and len(bars) > 30:
                bars = bars[-30:]
            out = self.data_dir / "charts" / f"{code}_{kind}.png"
            path = (chart_render.render_kline(out, title, bars)
                    if kind == "day"
                    else chart_render.render_intraday(out, title, bars))
            if not path:
                return False
            # AstrBot 官方写法：非 yield 处发消息用 event.send(...)
            if not hasattr(event, "image_result") or not hasattr(event, "send"):
                log.warning("事件对象缺少 image_result/send，回退文本")
                return False
            await event.send(event.image_result(str(path)))
            log.info("K 线图片已发送：%s（%s）", path.name, kind)
            return True
        except Exception:
            log.exception("发送 K 线图片失败，回退文本")
            return False

    @stock.command("买入")
    async def buy(self, event: AstrMessageEvent, code: str, quantity: int,
                  price: float = None):
        """限价买入，如 /炒股 买入 600001 1000 4.5（价格可省，按涨停价抢筹）"""
        uid = self._uid(event)
        if self._rate_limited(uid):
            log.warning("限流：u=%s 买入 过于频繁", uid)
            yield event.plain_result(
                f"⚠️ 操作太频繁，每 {CFG.trade_cooldown_seconds:.0f} 秒只能下一单。")
            return
        msg = await self._safe(event, "买入",
                               lambda: self.trade.buy(uid, code, quantity, price))
        yield event.plain_result(msg)

    @stock.command("卖出")
    async def sell(self, event: AstrMessageEvent, code: str, quantity: int,
                   price: float = None):
        """限价卖出，如 /炒股 卖出 600001 1000 4.5（价格可省，按跌停价甩卖）"""
        uid = self._uid(event)
        if self._rate_limited(uid):
            log.warning("限流：u=%s 卖出 过于频繁", uid)
            yield event.plain_result(
                f"⚠️ 操作太频繁，每 {CFG.trade_cooldown_seconds:.0f} 秒只能下一单。")
            return
        msg = await self._safe(event, "卖出",
                               lambda: self.trade.sell(uid, code, quantity, price))
        yield event.plain_result(msg)

    @stock.command("撤单")
    async def cancel(self, event: AstrMessageEvent, order_id: int):
        """撤销未成交委托"""
        uid = self._uid(event)
        msg = await self._safe(event, "撤单",
                               lambda: self.trade.cancel(uid, order_id))
        yield event.plain_result(msg)

    @stock.command("持仓")
    async def positions(self, event: AstrMessageEvent):
        """查看我的持仓（仅自己可见）"""
        uid = self._uid(event)
        acc = await self.db.get_account(uid)
        if not acc:
            yield event.plain_result("⚠️ 你还没开户，发送 /炒股 开户 开始。")
            return
        rows = await self.db.get_positions(uid)
        if not rows:
            yield event.plain_result("你当前没有持仓。用 /炒股 行情 找机会吧。")
            return
        lines = ["💼 我的持仓"]
        total_mv = 0.0
        total_pnl = 0.0
        for pos in rows:
            s = STOCK_POOL.get(pos["code"])
            q = self.engine.get(pos["code"])
            if not s or not q:
                continue
            mv = q.price * pos["quantity"]
            pnl = (q.price - pos["cost_price"]) * pos["quantity"]
            pct = (q.price - pos["cost_price"]) / pos["cost_price"] * 100 \
                if pos["cost_price"] else 0
            total_mv += mv
            total_pnl += pnl
            mark = "🔒" if pos["sellable"] < pos["quantity"] else ""
            lines.append(
                f"{s.name}({pos['code']}) {pos['quantity']}股{mark} "
                f"成本{pos['cost_price']:.2f} 现价{q.price:.2f} "
                f"{'+' if pnl >= 0 else ''}{pnl:,.0f}({pct:+.1f}%)")
        lines.append(f"持仓市值 {total_mv:,.0f}　浮动盈亏 "
                     f"{'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}")
        lines.append("（🔒 含今日买入，T+1 次日可卖）")
        yield event.plain_result("\n".join(lines))

    @stock.command("委托")
    async def orders(self, event: AstrMessageEvent):
        """查看我的未成交委托"""
        rows = await self.db.get_user_open_orders(self._uid(event))
        if not rows:
            yield event.plain_result("没有未成交的委托。")
            return
        lines = ["📝 未成交委托"]
        for r in rows:
            s = STOCK_POOL.get(r["code"])
            left = r["quantity"] - r["filled"]
            frozen = f"（冻结 {r['frozen']:,.0f} 元）" if r["side"] == "buy" \
                and (r["frozen"] or 0) > 0 else ""
            lines.append(f"#{r['order_id']} {'买' if r['side'] == 'buy' else '卖'} "
                         f"{s.name if s else r['code']} {left}股 @ "
                         f"{r['price']:.2f}{frozen}")
        lines.append("撤单：/炒股 撤单 <单号>")
        yield event.plain_result("\n".join(lines))

    @stock.command("成交")
    async def my_trades(self, event: AstrMessageEvent):
        """查看我最近的成交记录"""
        rows = await self.db.get_user_trades(self._uid(event), 10)
        if not rows:
            yield event.plain_result("还没有成交记录。")
            return
        lines = ["🧾 最近成交（最多 10 笔）"]
        for r in rows:
            s = STOCK_POOL.get(r["code"])
            t = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
            side = "买入" if r["side"] == "buy" else "卖出"
            lines.append(f"{t} {side} {s.name if s else r['code']} "
                         f"{r['quantity']}股 @ {r['price']:.2f} "
                         f"费 {r['fee']:.2f}")
        yield event.plain_result("\n".join(lines))

    @stock.command("资金")
    async def funds(self, event: AstrMessageEvent):
        """查看可用资金与总资产"""
        uid = self._uid(event)
        acc = await self.db.get_account(uid)
        if not acc:
            yield event.plain_result("⚠️ 你还没开户，发送 /炒股 开户 开始。")
            return
        total = await self.accounts.total_assets(uid, self._price_of)
        pnl = total - acc["initial_cash"]
        frozen = acc["cash_frozen"] or 0.0
        lines = [
            "💰 我的资金",
            f"可用资金 {acc['cash_available']:,.2f}",
        ]
        if frozen > 0:
            lines.append(f"冻结资金 {frozen:,.2f}（挂单占用，撤单释放）")
        lines += [
            f"可取资金 {acc['cash_withdrawable']:,.2f}（日终结算后可取）",
            f"总资产   {total:,.2f}",
            f"累计盈亏 {'+' if pnl >= 0 else ''}{pnl:,.2f}"
            f"（{pnl / acc['initial_cash'] * 100:+.2f}%）",
        ]
        if acc["status"] == "bankrupt":
            lines.append("💀 状态：已破产，可 /炒股 重生")
        yield event.plain_result("\n".join(lines))

    @stock.command("排行")
    async def rank(self, event: AstrMessageEvent):
        """股神排行榜（收益率）"""
        entries = await self.board.top(self._price_of, 10)
        yield event.plain_result(self.board.render(entries))

    @stock.command("重生")
    async def rebirth(self, event: AstrMessageEvent):
        """破产后东山再起（冷却24h、资金打折、终身3次）"""
        uid = self._uid(event)
        try:
            ok, msg = await self.accounts.rebirth(uid)
        except Exception:
            log.exception("指令<重生> u=%s 异常", uid)
            yield event.plain_result("⚠️ 系统开小差了，已记录日志，请稍后重试。")
            return
        if not ok:
            log.warning("指令<重生> u=%s 被拒：%s", uid, msg)
        yield event.plain_result(("✅ " if ok else "⚠️ ") + msg)

    @stock.command("快讯")
    async def news_cmd(self, event: AstrMessageEvent):
        """查看今日快讯（新闻、公司行为、停牌、退市）"""
        yield event.plain_result(self.news.render_today())

    @stock.command("打新")
    async def ipo_cmd(self, event: AstrMessageEvent, code: str = None,
                      lots: int = None):
        """新股申购：/炒股 打新（发行面板）· /炒股 打新 <代码> <签数>"""
        uid = self._uid(event)
        if code is None:
            msg = await self._safe(event, "打新",
                                   lambda: self.ipo.status_text(uid))
            yield event.plain_result(msg)
            return
        if lots is None:
            panel = await self.ipo.status_text(uid)
            hint = (f"{panel}\n申购：/炒股 打新 {code} <签数>"
                    if code in panel else
                    f"⚠️ 没有发行中的新股 {code}，"
                    f"用 /炒股 打新 查看发行列表。")
            yield event.plain_result(hint)
            return
        msg = await self._safe(event, "打新",
                               lambda: self.ipo.subscribe(uid, code, lots))
        yield event.plain_result(msg)

    @stock.command("融资")
    async def margin_buy_cmd(self, event: AstrMessageEvent,
                             amount: float = None):
        """信用账户面板（无参数）/ 融资借款：/炒股 融资 <金额>"""
        uid = self._uid(event)
        if amount is None:
            msg = await self._safe(event, "融资",
                                   lambda: self.margin.render(uid))
            yield event.plain_result(msg)
            return
        msg = await self._safe(event, "融资",
                               lambda: self.margin.borrow_cash(uid, amount))
        yield event.plain_result(msg)

    @stock.command("还钱")
    async def margin_repay_cmd(self, event: AstrMessageEvent,
                               amount: float = None):
        """归还融资（先息后本），不填金额全额还清"""
        uid = self._uid(event)
        if amount is None:
            msg = await self._safe(event, "还钱",
                                   lambda: self._repay_all(uid))
        else:
            msg = await self._safe(event, "还钱",
                                   lambda: self.margin.repay_cash(uid, amount))
        yield event.plain_result(msg)

    async def _repay_all(self, uid: str) -> str:
        """按当前负债全额归还融资（先息后本）。"""
        debt = await self.margin.debt(uid)
        if debt <= 0:
            raise MarginError("你没有融资融券负债。")
        return await self.margin.repay_cash(uid, debt)

    @stock.command("融券")
    async def borrow_stock_cmd(self, event: AstrMessageEvent, code: str,
                               quantity: int):
        """融券卖出：/炒股 融券 <代码> <数量>"""
        uid = self._uid(event)
        msg = await self._safe(event, "融券",
                               lambda: self.margin.borrow_stock(uid, code,
                                                                quantity))
        yield event.plain_result(msg)

    @stock.command("还券")
    async def repay_margin_cmd(self, event: AstrMessageEvent, code: str,
                               quantity: int):
        """买券还券：/炒股 还券 <代码> <数量>"""
        uid = self._uid(event)
        msg = await self._safe(event, "还券",
                               lambda: self.margin.cover(uid, code, quantity))
        yield event.plain_result(msg)

    # ── 管理员指令组（需平台管理员权限）────────────────────
    @stock.group("管理员")
    def admin(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @admin.command("暂停市场")
    async def admin_pause(self, event: AstrMessageEvent):
        """暂停行情推进（管理员）"""
        self.engine.paused = True
        log.warning("管理员 %s 暂停市场", self._uid(event))
        yield event.plain_result("⏸️ 行情已暂停（挂单不受影响，日终清算仍会执行）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @admin.command("恢复市场")
    async def admin_resume(self, event: AstrMessageEvent):
        """恢复行情推进（管理员）"""
        self.engine.paused = False
        log.warning("管理员 %s 恢复市场", self._uid(event))
        yield event.plain_result("▶️ 行情已恢复。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @admin.command("状态")
    async def admin_status(self, event: AstrMessageEvent):
        """查看运行状态（管理员）"""
        stats = await self.db.stats()
        quotes = self.engine.quotes.values()
        avg = sum(q.change_pct for q in quotes) / max(len(quotes), 1)
        uptime = time.time() - self._boot_ts
        last = (f"{time.time() - self._last_tick_ts:.1f}s 前"
                if self._last_tick_ts else "尚未 tick")
        lines = [
            "⚙️ 虚拟股市运行状态",
            f"模式 {CFG.market_mode}（{calendar.status_text()}）"
            f"{'　⏸️ 已暂停' if self.engine.paused else ''}",
            f"运行 {uptime / 3600:.1f} 小时，tick 累计 {self._ticks}，上次 {last}",
            f"距下次日终清算 {max(CFG.day_seconds - (time.time() - self._last_day_close), 0) / 60:.1f} 分钟"
            if CFG.market_mode == "247" else "日终清算按真实收盘触发",
            f"均涨跌幅 {avg:+.2f}%",
            f"账户 {stats['accounts']}（活跃 {stats['active']} / 破产 {stats['bankrupt']}）",
            f"持仓 {stats['positions']} 条，未成交委托 {stats['open_orders']} 笔",
            f"历史成交 {stats['trades']} 笔，冻结资金 {stats['frozen_total']:,.2f}",
            f"日志级别 {CFG.log_level}，日志文件 {self.data_dir / 'sharemarket.log'}",
        ]
        yield event.plain_result("\n".join(lines))
