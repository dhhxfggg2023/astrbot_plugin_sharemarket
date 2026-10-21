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

from astrbot.api import AstrBotConfig
from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .config import CFG
from .events.news import NewsService
from .game.account import AccountService
from .game.achieve import AchievementService
from .game.auction import AuctionService
from .game.corporate import CorporateService
from .game.dayclose import DayCloseService
from .game.ipo import IPOService
from .game.leaderboard import LeaderboardService
from .game.margin import MarginError, MarginService
from .game.flow import (get_all_rankings, get_anomaly_ranking,
                        get_dragon_tiger, get_fund_flow_ranking)
from .game.npc import NpcService
from .game.props import PROP_BY_ID, PropService
from .game.quest import QuestService
from .game.season import SeasonService
from .game.short import DEFAULT_LEVERAGE, LeverageError, ShortService
from .game.trade import TradeError, TradeService
from .game.watchlist import WatchlistService
from .log import bind_astrbot_logger, get_logger, set_level, setup_file_logging
from .locks import trade_lock
from .market import calendar
from .market.engine import PriceEngine
from .market.indicators import (kdj, ma, macd, rsi, sparkline,
                                turnover_rate, volume_ratio)
from .market.orderbook import Matcher
from .market.stocks import STOCK_POOL, Stock
from .storage.db import Database

log = get_logger("main")

# ── 翻页机制（P5）──────────────────────────────────────────
# 单页上限：QQ 官方适配器单条消息上限约 4000 字符（_post_send_one 侧会被
# 平台截断），所以按 3800 留出页脚余量。页数为 1 时完全不加页脚——
# 短回复不应为了「可能翻页」而变长。
_PAGE_LIMIT = 3800
_PAGE_TTL = 600.0        # 10 分钟过期
_PAGE_STATE: dict[str, dict] = {}   # user_id -> {kind, page, pages, ts}
_PAGE_FOOTER = "（第 {page}/{pages} 页，发送「炒股 下一页」翻页）"
_PAGE_HINT = "（第 {page}/{pages} 页，发送「炒股 上一页」返回）"

# 流通股本代理（股）：股票池没有流通股本字段，换手率统一按 1 亿股估算——
# 与 game/flow.py 异动榜的换手口径一致（那里用 成交股数/1e8×100，阈值 1.5%）。
_FLOAT_SHARES = 1e8


def _coerce_cfg(key: str, value):
    """把 AstrBotConfig 读回来的值转成 config.py 声明的类型。

    WebUI 的 _conf_schema.json 把「(0.010, 0.025)」这类 tuple 存成逗号字符串；
    原样 setattr 进 CFG 后，engine 里 ``*CFG.crash_size`` 会按**字符**解包
    （TypeError: uniform() takes 3 positional arguments but 10 were given）。
    转换失败时保留默认值——坏配置绝不能写进 CFG。
    """
    cur = getattr(CFG, key, None)
    try:
        if isinstance(cur, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(cur, (tuple, list)):
            if isinstance(value, str):
                parts = [p.strip() for p in value.replace("，", ",").split(",")
                         if p.strip()]
                conv = tuple(float(p) for p in parts)
            elif isinstance(value, (list, tuple)):
                conv = tuple(float(x) for x in value)
            else:
                return cur
            if not conv or len(conv) != len(cur):
                return cur
            return conv
        if isinstance(cur, int) and not isinstance(cur, bool):
            return int(float(value))
        if isinstance(cur, float):
            return float(value)
        if isinstance(cur, str):
            return str(value)
    except (TypeError, ValueError):
        return cur
    return value


def _as_msg(res) -> str:
    """把 service 返回值统一成可发送文本。

    watchlist.add/remove、ipo.issue_stock 返回 ``(bool, str)``：直接把它交给
    ``event.plain_result`` 会触发 pydantic ``ValidationError: str type expected``
    （AstrBot 4.28 实测），玩家看到的是「指令没反应」。这里统一拆包。
    """
    if isinstance(res, tuple) and res and isinstance(res[0], bool):
        ok, msg = res[0], res[1]
        return ("✅ " if ok else "⚠️ ") + str(msg)
    return "" if res is None else str(res)


def _hook_bare_group(groupable, plugin) -> bool:
    """接住「只发 /炒股」（不带子命令），改发图片手册。

    AstrBot 的 ``CommandGroupFilter.filter`` 在消息恰好等于组名时会直接
    ``raise ValueError("参数不足。…指令树…")``，waking_check 把它包装成一条
    长文本发出去——这就是玩家看到的「一大堆文字」。框架没有留默认处理器
    钩子，所以这里给**本插件自己的**指令组实例换掉 filter：同一情况下改为
    异步发手册图，然后吞掉异常并 stop_event，不影响其他组。

    返回 True 表示本次装上了钩子（重复调用返回 False，幂等）。
    """
    pg = getattr(groupable, "parent_group", None)
    if pg is None or getattr(pg, "_sharemarket_hooked", False):
        return False
    orig = pg.filter

    def _filter(event, cfg):
        try:
            return orig(event, cfg)
        except ValueError:
            if event.message_str.strip() not in pg.get_complete_command_names():
                raise                       # 不是裸组名，交回框架
            try:
                asyncio.get_running_loop().create_task(
                    plugin._send_help_cards(event))
            except RuntimeError:            # 同步上下文/单测里没有事件循环
                pass
            event.stop_event()
            return False

    pg.filter = _filter
    pg._sharemarket_hooked = True
    return True


HELP = """📖 炒股 · 命令手册
按区块扫读，长内容自动分页（发送「炒股 下一页」翻页）。

━━ ① 入门 ━━
/炒股 开户              注册账户，领 100 万初始资金
/炒股 菜单              本手册（= 帮助）
/炒股 行情              大盘概览：涨跌家数 / 情绪 / 散户 / 板块 / 榜单
/炒股 列表 [页码|板块]  全部 150 只股票按涨跌幅排序，可翻页或按板块看
/炒股 搜 <关键词>       按名称、行业或代码找股票

━━ ② 看盘 ━━
/炒股 个股 <代码>       个股详情（含涨跌停位置条）
/炒股 盘口 <代码>       五档盘口 + 现价中枢
/炒股 k线 <代码> [分时|日]   K 线图 + MA / MACD / RSI / KDJ
/炒股 龙虎榜            当日买卖前五席位
/炒股 资金流            主力资金净流入榜
/炒股 异动榜            涨跌幅 / 换手 / 振幅异动

━━ ③ 交易 ━━
/炒股 买入 <代码> <数量> [价格]   限价买入；省略价格=按涨停价抢筹
/炒股 卖出 <代码> <数量> [价格]   限价卖出；省略价格=按跌停价甩卖
/炒股 撤单 <单号>        撤销未成交委托（释放冻结）
/炒股 委托              我的未成交委托
/炒股 成交              我最近 10 笔成交

━━ ④ 资产 ━━
/炒股 持仓              持仓、成本与浮动盈亏
/炒股 资金              可用 / 冻结 / 可取 / 总资产
/炒股 排行 [日/周/总]    股神排行榜（真实玩家）
/炒股 全体榜            全体榜（玩家 + 模拟股民警）
/炒股 排行历史          状元收益曲线（近 14 天）
/炒股 破产榜            破产玩家名录
/炒股 自选 / 自选加 <代码> / 自选删 <代码>   自选股
/炒股 成就              我的成就进度

━━ ⑤ 玩法 ━━
/炒股 签到              每日签到领金币（连击递增）
/炒股 日常任务          今日任务进度与领取
/炒股 道具              道具商店 + 我的库存
/炒股 购买 <prop_id> [数量]     购买道具
/炒股 出售 <prop_id> [数量]     出售道具
/炒股 使用 <prop_id> [目标]     使用道具
/炒股 赛季 / 赛季奖励     段位进度与奖励
/炒股 快讯              今日快讯（新闻 / 停牌 / 退市）
/炒股 打新 [代码] [签数]  新股申购（无参数看发行面板）
/炒股 发行 <名称> <发行价> <发行股数>   自发行股票
/炒股 我的发行          我发行的股票状态
/炒股 NPC               股民警与散户大军状态（常开）
/炒股 重生              破产后东山再起

━━ ⑥ 进阶（融资融券）━━
/炒股 融资 <金额>        融资借款（无参数看信用账户）
/炒股 还钱 <金额>        归还融资（先息后本）
/炒股 融券 <代码> <数量> 融券卖出
/炒股 做空 <代码> <数量> 做空（= 融券卖出；无参数看面板）
/炒股 还券 <代码> <数量> 买券还券
/炒股 杠杆 [倍数]        杠杆查询 / 校验（1-3×）

━━ ⑦ 设置 ━━
运行参数全在 AstrBot「插件设置 → 虚拟股市」里改：交易时段模式、
是否暂停行情、置顶公告、涨跌停/费率/波动率/NPC 等，改完重载插件生效。
聊天里不再有管理员指令；需要停用或重载插件请在 WebUI 插件页操作。
/炒股 帮助              本手册

━━ 代码与规则 ━━
代码用 6 位原码（600001）或 3 位短码（JR01）：
JR 金融 · XF 消费 · YY 医药 · KJ 科技 · XNY 新能源
ZQ 周期 · JC 基建地产 · YS 运输公用 · QC 汽车军工 · NY 农业传媒
数量单位「股」，100 股 = 1 手；T+1，当日买入次日可卖。
佣金万 2.5（最低 5 元）、印花税 0.05%（仅卖出）、过户费 0.001%。
行情/个股/盘口/持仓/资金/排行/龙虎榜/资金流/异动榜/道具优先发图片卡片，
无 Pillow 或发送失败时自动降级为文本。"""


class ShareMarket(Star):
    def __init__(self, context: Context,
                 config: AstrBotConfig | None = None):
        super().__init__(context)
        # 从 AstrBotConfig 同步到 CFG（离线测试仍用 config.py 的 CFG 默认值）。
        # 只同步 schema 里声明的键，缺项保留 config.py 默认。
        if config is not None:
            try:
                items = config.items()
            except Exception:
                items = []
            for k, v in items:
                if hasattr(CFG, k):
                    setattr(CFG, k, _coerce_cfg(k, v))
        self.data_dir = self._resolve_data_dir()
        self.db = Database(str(self.data_dir / "sharemarket.db"))
        self.engine = PriceEngine()
        self.matcher = Matcher()
        self.ipo = IPOService(self.db, self.engine)
        self.trade = TradeService(self.db, self.engine, self.matcher)
        self.auction = AuctionService(self.db, self.engine, self.trade)
        self.trade.auction = self.auction      # 互相引用
        self.news = NewsService(self.db, self.engine)
        self.corporate = CorporateService(self.db, self.engine, self.news)
        self.board = LeaderboardService(self.db)
        self.watchlist = WatchlistService(self.db)
        self.achieve = AchievementService(self.db)
        self.margin = MarginService(self.db, self.engine, self.trade)
        # P5 做空/杠杆：杠杆层只读查询，下单仍走 margin.borrow_stock / borrow_cash
        self.short = ShortService(self.db, self.margin, self.trade)
        self.accounts = AccountService(self.db, margin=self.margin,
                                       ipo=self.ipo)
        self.dayclose = DayCloseService(self.db, self.engine, self.matcher,
                                        self.auction, self.corporate, self.news,
                                        margin=self.margin, ipo=self.ipo)
        # P4 模拟股民警：t11 落地 game/npc.py 后直接装配（NpcService 只产
        # 决策不下单，真正下单在 _tick_loop 的不持锁状态执行）。
        self.npc = NpcService(self.db, self.engine, self.trade)
        # S6 内容模块（集成任务接线）：道具/签到/赛季/资金流龙虎榜
        self.props = PropService(self.db)
        self.quest = QuestService(self.db)
        self.season = SeasonService(self.db)
        self._tick_task: asyncio.Task | None = None
        self._rate: dict[str, float] = {}      # D10 限流
        self._started = False
        self._last_day_close = time.time()
        self._backup_err_day: str | None = None   # 每日备份失败已记日志的自然日
        self._ticks = 0                        # 已推进的 tick 数
        self._last_tick_ts = 0.0
        self._boot_ts = time.time()
        self._phase: str | None = None         # 交易阶段（用于阶段切换清算）
        # 裸「/炒股」不再让框架回退成指令树文本，改为发图片手册
        _hook_bare_group(getattr(self, "stock", None), self)

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
        await self._start_up()

    async def initialize(self) -> None:
        """Star 生命周期入口：整机启动 / 单插件重载 / 面板启用都会调用。

        on_astrbot_loaded 只在 AstrBot 整机启动时由 core_lifecycle.start()
        触发（core_lifecycle.py:370-375）；单独重载或启用插件走的是
        star_manager.reload() -> load()，那里只调用 initialize()
        （star_manager.py:1420-1421），**不会**触发 on_astrbot_loaded。
        所以真正的初始化必须放这里，两个入口收敛到同一个 _start_up()。
        """
        if self._started:
            return
        await self._start_up()

    async def _start_up(self) -> None:
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
            # §16 启动即补每日备份：今天已备份则 no-op；失败不阻塞启动
            await self._maybe_daily_backup()
            rows = await self.db.get_all_prices()
            if rows:
                self.engine.load_rows(rows)
                log.info("恢复行情：%d 只股票价格已载入", len(rows))
            else:
                log.info("首次运行：行情按初始价建立（%d 只）", len(STOCK_POOL))
            await self._restore_orders()
            listed = await self.ipo.restore()
            log.info("打新恢复：%d 只已上市新股已重新登记", listed)
            await self._apply_settings()
            log.info("交易阶段机初始化：当前阶段 %s（竞价长度 %ds / 收盘竞价 %ds）",
                     calendar.status_text(), CFG.auction_seconds,
                     CFG.closing_auction_seconds)
            stats = await self.db.stats()
            log.info("账户 %d（活跃 %d / 破产 %d）持仓 %d 条 未成交委托 %d 笔 "
                     "历史成交 %d 笔 冻结资金合计 %.2f",
                     stats["accounts"], stats["active"], stats["bankrupt"],
                     stats["positions"], stats["open_orders"], stats["trades"],
                     stats["frozen_total"])
            # P5 模拟股民警：默认常开，无开关（game/npc.py always-on）
            n = await self.npc.ensure_accounts()
            rebuilt = await self.npc.rebuild_bankrupt()
            if n or rebuilt:
                log.info("模拟股民警：新建 %d 个账户，自愈 %d 个破产 NPC",
                         n, rebuilt)
            # S6 内容模块建表 + 赛季种子（幂等）
            await self.props.ensure_schema()
            await self.quest.ensure_schema()
            await self.season.ensure_schema()
            seeded = await self._seed_default_season()
            if seeded:
                log.info("赛季：已注入默认赛季定义（%s）", seeded)
            self._tick_task = asyncio.create_task(self._tick_loop())
            log.info("行情引擎已启动")
        except Exception:
            log.exception("插件启动失败")
            raise

    async def _apply_settings(self) -> None:
        """把插件设置里的运维开关落到运行时（唯一入口，启动时调一次）。

        交易时段模式只认 AstrBotConfig 的 `market_mode`；早期版本允许在聊天
        里由管理员切换并写 meta，该入口已移除 —— 旧 meta 值不再覆盖配置，
        避免「设置页写着 X、运行时跑 Y」。暂停与公告同理：改配置 → 重载插件
        → 这里生效。
        """
        mode = CFG.market_mode if CFG.market_mode in ("realtime", "247") \
            else "247"
        CFG.market_mode = mode
        calendar.SESSION.mode = mode
        self.engine.paused = bool(CFG.market_paused)
        notice = (CFG.notice or "").strip()[:200]
        await self.db.set_meta("notice", notice)
        log.info("插件设置：模式=%s，行情=%s，公告=%s", mode,
                 "已暂停" if self.engine.paused else "运行中",
                 notice or "（无）")
        calendar.SESSION.reset()

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
                await self._maybe_daily_backup()
                await self._maybe_day_close()
                # 阶段机切换必须每个 tick 都检查：竞价结束时清算开盘竞价簿。
                # 缺这个调用则 _maybe_auction_clear 是死代码，集合竞价永不清算。
                await self._maybe_auction_clear()
                if self.engine.paused:
                    continue
                if not calendar.is_open():
                    continue
                pressures = {c: self.matcher.take_pressure(c) for c in STOCK_POOL}
                # 散户大军：人群层（数万人聚合模型）的净买卖并入本 tick 冲击
                for code, qty in self.npc.crowd_tick().items():
                    pressures[code] = pressures.get(code, 0) + qty
                self.engine.tick(pressures)
                for row in self.engine.snapshot_rows():
                    await self.db.upsert_price(*row)
                await self.db.commit_prices()
                # 模拟股民警（t11）：NpcService 只产决策不下单，这里在不持锁
                # 状态执行——trade.buy/sell 入口自带 trade_lock，NpcService 若
                # 自己持锁就是第 4 次同形状死锁。
                # P5 模拟股民警：默认常开，无开关（game/npc.py always-on）
                try:
                    orders = await self.npc.tick()
                    if orders:
                        ok, bad = await self.npc.run_orders(orders)
                        if ok or bad:
                            log.debug("NPC 本 tick：成交 %d 笔 / 失败 %d 笔",
                                      ok, bad)
                except Exception:
                    log.exception("NPC tick 异常（已跳过，行情循环继续）")
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

    async def _maybe_daily_backup(self):
        """每日自动备份（§16）：每 tick 调用、同日幂等，失败不打断行情循环。

        每自然日只记一次失败堆栈（避免每 3s 一条刷屏），但仍每 tick 重试，
        瞬时失败（文件被占用等）可自愈；次日自动恢复记日志。
        """
        try:
            if await self.db.maybe_daily_backup():
                log.info("每日备份完成：%s", self.db.backup_path())
        except Exception:
            today = time.strftime("%Y-%m-%d")
            if self._backup_err_day != today:
                self._backup_err_day = today
                log.exception("每日备份失败（本日不再重复记日志，仍每 tick 重试）")

    _SEASON_RANK_THRESHOLDS = [0, 100, 300, 600, 1000, 1500]
    # content_spec.md:86 段位分档，奖励段位1=100金 … 段位6=2000金
    _SEASON_REWARDS = [100, 300, 600, 1000, 1500, 2000]

    async def _seed_default_season(self) -> str | None:
        """注入一条覆盖当前时间的默认赛季（幂等）。

        season_defs 无管理员插入入口，若不种种子则所有用户 get_season_info
        返回 season_id=None、update_season_points 报 No active season。
        一条 90 天滚动赛季（content_spec.md:86），起止按自然月对齐。
        返回赛季名（首次注入时）或 None（已存在）。
        """
        import json as _json
        now = int(time.time())
        cursor = await self.db.conn.execute(
            "SELECT season_id FROM season_defs WHERE start_ts <= ? AND end_ts > ? "
            "ORDER BY start_ts DESC LIMIT 1", (now, now))
        row = await cursor.fetchone()
        if row:
            return None
        # 用当前所在季度作赛季 id，确保跨重启幂等
        import datetime as _dt
        d = _dt.datetime.fromtimestamp(now)
        q = (d.month - 1) // 3 + 1
        season_id = f"{d.year}-Q{q}"
        start_ts = int(_dt.datetime(d.year, 3 * q - 2, 1).timestamp())
        end_ts = start_ts + 90 * 86400
        await self.db.conn.execute(
            "INSERT OR IGNORE INTO season_defs(season_id,name,start_ts,end_ts,"
            "rank_thresholds,rewards) VALUES(?,?,?,?,?,?)",
            (season_id, f"{d.year}年第{q}赛季", start_ts, end_ts,
             _json.dumps(self._SEASON_RANK_THRESHOLDS),
             _json.dumps(self._SEASON_REWARDS)))
        await self.db._commit()
        return season_id

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
            try:
                # 锁外落排行榜快照（P3 历史曲线），快照失败不影响日终主流程
                await self.dayclose.run(self.board, self._price_of)
            except Exception:
                # 失败也必须推进计时：否则每 3 秒一个 tick 重放同一批竞价单，
                # 3.3 小时写满 5 个日志文件，日终后续步骤（解冻/T+1/昨收/日K/
                # 快照）永远跑不到（2026-10-12 生产日志 3690 次现值）。
                self._last_day_close = now
                self._day_close_errors = getattr(
                    self, "_day_close_errors", 0) + 1
                log.exception("日终清算失败，已跳过本虚拟交易日"
                              "（累计 %d 次）；下一交易日重新计时",
                              self._day_close_errors)
                calendar.SESSION.reset(now)
                self._phase = None
                return
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
            self._tick_task = None
        # db.conn 为 None 说明本实例从未成功启动过（_start_up 抛异常或走的是
        # 另一条加载路径）。此时 upsert_price 会炸 AttributeError，把干净的
        # 终止变成 ERROR 堆栈。守卫后空转一次即可。
        if self.db.conn is None:
            log.info("数据库未打开（本实例未成功启动），跳过落盘")
            self._started = False
            return
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

    @staticmethod
    def _resolve_code(code: str) -> str | None:
        """把用户输入的股票代码解析为 6 位原码。

        支持 6 位原码（如 600001）与 3 位短码（如 JR01）。
        短码通过 ``Stock.lookup`` 查 ``STOCK_BY_SHORT`` 映射回原码，
        原码直接通过 ``STOCK_POOL`` 校验。找不到返回 None。
        """
        if not code:
            return None
        code = str(code).strip()
        s = Stock.lookup(code)
        return s.code if s is not None else None

    def _rate_limited(self, uid: str) -> bool:
        now = time.monotonic()
        last = self._rate.get(uid, 0)
        if now - last < CFG.trade_cooldown_seconds:
            return True
        self._rate[uid] = now
        return False

    # ── 翻页（P5）──────────────────────────────────────────
    @staticmethod
    def _split_pages(text: str, limit: int = _PAGE_LIMIT) -> list[str]:
        """按行拆分，单行绝不截断；每页尽量接近 limit 但不超。

        单行超 limit 的极端情况（只可能来自超长自发行股票名等异常输入）
        保留原样，下一步交给平台侧截断——造半行比让平台截断更难读。
        """
        if not text:
            return [""]
        pages: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for ln in text.split("\n"):
            cost = len(ln) + 1          # +1 是该行的换行符
            if cur and cur_len + cost > limit:
                pages.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(ln)
            cur_len += cost
        if cur or not pages:
            pages.append("\n".join(cur))
        return pages

    def _page_store(self, uid: str, kind: str, texts: list[str]) -> None:
        """覆盖式存状态：同一 uid 后发的新分页顶掉旧分页（无队列）。"""
        _PAGE_STATE[uid] = {"kind": kind, "page": 1,
                            "pages": len(texts), "texts": texts,
                            "ts": time.time()}

    @staticmethod
    def _page_prune(now: float | None = None) -> int:
        """清掉超过 _PAGE_TTL 的过期状态，返回清理条数。"""
        now = time.time() if now is None else now
        dead = [uid for uid, st in _PAGE_STATE.items()
                if now - st["ts"] > _PAGE_TTL]
        for uid in dead:
            _PAGE_STATE.pop(uid, None)
        return len(dead)

    async def _paged_send(self, event: AstrMessageEvent, text: str,
                          kind: str) -> None:
        """统一分页发送：拆页 → 存状态 → 发首页。

        页数为 1 时原样发送、**不加页脚、不落状态**（短回复零开销）。
        """
        if text is None:
            text = ""
        if not self._started:
            await event.send(event.plain_result(
                "⚠️ 插件尚未就绪（正在初始化），请稍后再试。"))
            return
        uid = self._uid(event)
        self._page_prune()
        pages = self._split_pages(text)
        if len(pages) <= 1:
            await event.send(event.plain_result(text))
            return
        self._page_store(uid, kind, pages)
        body = f"{pages[0]}\n{_PAGE_FOOTER.format(page=1, pages=len(pages))}"
        await event.send(event.plain_result(body))
        log.debug("分页发送<%s> u=%s 共 %d 页", kind, uid, len(pages))

    async def _page_turn(self, event: AstrMessageEvent, delta: int) -> str:
        """翻页：delta=+1 下一页 / -1 上一页。返回要发送的文本。"""
        uid = self._uid(event)
        self._page_prune()
        st = _PAGE_STATE.get(uid)
        if not st:
            return "⚠️ 没有可翻页的内容，先打开一个长面板（如 /炒股 帮助）。"
        target = st["page"] + delta
        if target < 1 or target > st["pages"]:
            edge = "已经第一页了" if target < 1 else "已经最后一页了"
            return (f"{edge}（第 {st['page']}/{st['pages']} 页）。\n"
                    f"重新打开面板：/炒股 帮助、/炒股 排行 等。")
        st["page"] = target
        st["ts"] = time.time()
        tpl = _PAGE_FOOTER if delta > 0 else _PAGE_HINT
        texts: list[str] = st.get("texts") or []
        if target - 1 >= len(texts):
            # 只有进程内 dict 被外部清空才可能走到这里
            return "⚠️ 分页内容已失效，请重新打开面板。"
        return (f"{texts[target - 1]}\n"
                f"{tpl.format(page=target, pages=st['pages'])}")

    async def _quest_progress(self, uid: str, task_id: str,
                              proof: dict | None = None):
        """把真实行为喂给日常任务；达标时把金币直接加到可用/可取。

        quest.submit_task 原本没有任何调用点，进度永远为 0（审计发现）。
        """
        try:
            res = await self.quest.submit_task(uid, task_id, proof or {})
        except Exception:
            log.debug("日常任务进度写入失败：%s", task_id, exc_info=True)
            return None
        reward = int(res.get("reward") or 0) if res.get("ok") else 0
        if reward:
            acc = await self.db.get_account(uid)
            if acc:
                await self.db.update_cash_full(
                    uid, acc["cash_available"] + reward,
                    acc["cash_withdrawable"] + reward, acc["cash_frozen"])
        return res

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

    @stock.command("帮助", alias={"菜单", "说明", "help", "?"})
    async def help_cmd(self, event: AstrMessageEvent):
        """查看全部指令"""
        await self._send_help_cards(event)

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
        crowd = self.npc.crowd_stats()
        lines.append(
            f"👥 散户 {crowd['people'] / 10000:.1f} 万人在场"
            f"　今日净买 {crowd['net'] / 1e8:+.2f} 亿"
            f"　群体收益 {crowd['ret'] * 100:+.1f}%")
        # 置顶公告（来自插件设置 notice；§19 的聊天管理入口已移除）
        notice = await self.db.get_meta("notice")
        if notice:
            lines.insert(1, f"📢 公告：{notice}")
            lines.insert(2, "")
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
        text = "\n".join(lines)
        # 优先发图片（render/cards.py），失败降级为原文本
        ok = await self._send_card(
            event, "render_market_overview", "market.png",
            {
                "sentiment": senti,
                "crowd": crowd,
                "breadth": {"up": up, "down": down, "flat": flat,
                            "limit_up": limit_up, "limit_down": limit_down,
                            "total": up + down + flat},
                "sectors": [{"name": n, "change_pct": v} for n, v in sectors],
                "gainers": [{"code": q.code,
                             "name": STOCK_POOL[q.code].name,
                             "change_pct": q.change_pct,
                             "price": q.price} for q in quotes[:5]],
                "losers": [{"code": q.code,
                            "name": STOCK_POOL[q.code].name,
                            "change_pct": q.change_pct,
                            "price": q.price} for q in quotes[-5:]],
            })
        if ok:
            return
        yield event.plain_result(text)

    @stock.command("列表", alias={"全部"})
    async def market_list(self, event: AstrMessageEvent, arg: str = None):
        """全部股票一览（分页）：/炒股 列表 [页码|板块]"""
        quotes = list(self.engine.quotes.values())
        quotes.sort(key=lambda q: q.change_pct, reverse=True)
        all_sectors = sorted({s.sector for s in STOCK_POOL.values()})
        page, sector = 1, None
        if arg:
            a = str(arg).strip()
            if a.isdigit():
                page = max(1, int(a))
            elif a in all_sectors:
                sector = a
                quotes = [q for q in quotes
                          if STOCK_POOL[q.code].sector == sector]
        per = 20
        pages = max(1, (len(quotes) + per - 1) // per)
        page = min(page, pages)
        chunk = quotes[(page - 1) * per: page * per]
        title = f"{sector}板块" if sector else "全部股票"
        lines = [f"📃 {title}（{len(quotes)} 只）第 {page}/{pages} 页",
                 "名称　　　　　代码　　　现价　　涨跌幅"]
        for q in chunk:
            s = STOCK_POOL[q.code]
            lines.append(f"{s.name}（{q.code}）{q.price:.2f} "
                         f"{q.change_pct:+.2f}%")
        lines.append("")
        lines.append("翻页：/炒股 列表 <页码>　·　按板块：/炒股 列表 科技")
        await self._paged_send(event, "\n".join(lines), "stock_list")

    @stock.command("个股")
    async def quote_cmd(self, event: AstrMessageEvent, code: str):
        """个股详情，如 /炒股 个股 600001（支持 3 位短码 JR01）"""
        code = self._resolve_code(code)
        if not code:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        s = STOCK_POOL.get(code)
        q = self.engine.get(code)
        if not s or not q:
            yield event.plain_result(f"⚠️ 股票 {code} 不存在，用 /炒股 搜 查代码。")
            return
        data = {
            "name": s.name, "code": code, "price": q.price,
            "change_pct": q.change_pct, "open": q.open, "high": q.high,
            "low": q.low, "prev_close": q.prev_close,
            "limit_up": q.limit_up(s), "limit_down": q.limit_down(s),
            "sector": s.sector, "industry": s.industry, "volume": q.volume,
            "net_inflow": self.engine.fund_flow(code),
        }
        if await self._send_card(event, "render_stock_detail",
                                 f"stock_{code}.png", data):
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
        """五档盘口（支持 3 位短码）"""
        code = self._resolve_code(code)
        if not code:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        q = self.engine.get(code)
        if not q or code not in STOCK_POOL:
            yield event.plain_result(f"⚠️ 股票 {code} 不存在。")
            return
        bids, asks = self.matcher.five_levels(code, q.price)
        data = {
            "name": STOCK_POOL[code].name,
            "code": code,
            "price": q.price,
            "change_pct": q.change_pct,
            "prev_close": q.prev_close,
            "bids": [{"price": p, "volume": v} for p, v in bids[:5]],
            "asks": [{"price": p, "volume": v} for p, v in asks[:5]],
        }
        if await self._send_card(event, "render_orderbook",
                                 f"orderbook_{code}.png", data):
            return
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
        """K 线图：/炒股 k线 600001 [分时|日]（支持 3 位短码；有 Pillow 时发图片，否则文本）"""
        code = self._resolve_code(code)
        if not code:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
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
            # KDJ / 量比 / 换手率（DESIGN §12.5）
            kdj_k, kdj_d, kdj_j = kdj([b.high for b in bars],
                                      [b.low for b in bars], closes)
            summary.append(
                f"KDJ9 K {self._fmt(kdj_k, 1)}　D {self._fmt(kdj_d, 1)}　"
                f"J {self._fmt(kdj_j, 1)}")
            # 量比：把今日进度折算成整日口径后复用纯函数 volume_ratio——
            # 今日每根均量 × 每日根数 ÷ 过去 5 日日均量 == 每根均量之比。
            day_rows = (await self.db.get_candles(code, "1d", 5)
                        if self.db.conn else [])
            day_vols = [float(r["volume"]) for r in day_rows]
            elapsed = max(len(self.engine.intraday_bars(code)), 1)
            bars_per_day = max(self.engine.ticks_per_day
                               / max(self.engine.ticks_per_bar, 1), 1.0)
            proj_today = q.volume / elapsed * bars_per_day
            vr = (volume_ratio(day_vols + [proj_today], n=len(day_vols))
                  if day_vols else None)
            tr = turnover_rate(q.volume, _FLOAT_SHARES)
            tr_txt = "—" if tr is None else f"{tr:.2f}%"
            summary.append(f"量比 {self._fmt(vr)}　换手率 {tr_txt}")
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
        kdj_k, kdj_d, kdj_j = kdj([r["high"] for r in rows],
                                  [r["low"] for r in rows], closes)
        summary.append(
            f"KDJ9 K {self._fmt(kdj_k, 1)}　D {self._fmt(kdj_d, 1)}　"
            f"J {self._fmt(kdj_j, 1)}")
        day_vols = [float(r["volume"]) for r in rows]
        vr = volume_ratio(day_vols)
        tr = turnover_rate(day_vols[-1], _FLOAT_SHARES)
        tr_txt = "—" if tr is None else f"{tr:.2f}%"
        summary.append(f"量比 {self._fmt(vr)}　换手率 {tr_txt}")

        if await self._send_chart(event, "day", code, title, rows,
                                  allow_shrink=len(rows) > 30):
            yield event.plain_result("\n".join(summary[-5:]))
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
            if kind == "day":
                path = chart_render.render_kline(out, title, bars)
            else:
                q = self.engine.get(code)
                prev_close = q.prev_close if q else None
                # 真实涨跌幅：Quote.limit_pct_of(stock) 覆盖 IPO 首日
                # pct_override（主板 0.44 / 创业科创北交所 10.0）与 ST 0.05，
                # 否则回落股票所属板块的 limit_pct（主板 0.10 / 创业板 0.20 /
                # 北交所 0.30）。不要在这层写死比例。
                s = STOCK_POOL.get(code)
                limit_pct = (q.limit_pct_of(s) if q and s else None)
                path = chart_render.render_intraday(out, title, bars,
                                                    prev_close=prev_close,
                                                    limit_pct=limit_pct)
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

    async def _send_help_cards(self, event: AstrMessageEvent) -> None:
        """发图片手册（两页）；无 Pillow / 发送失败时回退分页文本。"""
        p1 = await self._send_card(event, "render_help", "help_1.png", HELP,
                                   page=0)
        p2 = await self._send_card(event, "render_help", "help_2.png", HELP,
                                   page=1)
        if not (p1 or p2):
            await self._paged_send(event, HELP, "help")

    async def _send_card(self, event: AstrMessageEvent, fn_name: str,
                         out_name: str, *args, **kwargs) -> bool:
        """尝试调用 render.cards 的渲染函数并发送图片。

        成功返回 True；Pillow 不可用、渲染返回 None 或发送失败时返回 False
        （由调用方回退文本）。fn_name 是 render/cards.py 中的函数名，args/kwargs
        透传给该函数。
        """
        try:
            from .render import cards as cards_render
            if not cards_render.available():
                return False
            fn = getattr(cards_render, fn_name, None)
            if fn is None:
                return False
            out = self.data_dir / "cards" / out_name
            out.parent.mkdir(parents=True, exist_ok=True)
            path = fn(out, *args, **kwargs)
            if not path:
                return False
            if not hasattr(event, "image_result") or not hasattr(event, "send"):
                log.warning("事件对象缺少 image_result/send，回退文本")
                return False
            await event.send(event.image_result(str(path)))
            log.info("卡片图片已发送：%s", path.name)
            return True
        except Exception:
            log.exception("发送卡片图片失败，回退文本（%s）", fn_name)
            return False

    @stock.command("买入")
    async def buy(self, event: AstrMessageEvent, code: str, quantity: int,
                  price: float = None):
        """限价买入，如 /炒股 买入 600001 1000 4.5（支持 3 位短码；价格可省，按涨停价抢筹）"""
        uid = self._uid(event)
        if self._rate_limited(uid):
            log.warning("限流：u=%s 买入 过于频繁", uid)
            yield event.plain_result(
                f"⚠️ 操作太频繁，每 {CFG.trade_cooldown_seconds:.0f} 秒只能下一单。")
            return
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "买入",
                               lambda: self.trade.buy(uid, resolved, quantity, price))
        if msg.startswith("📋"):
            await self._quest_progress(uid, "trade_count", {"trade_count": 1})
        yield event.plain_result(msg)

    @stock.command("卖出")
    async def sell(self, event: AstrMessageEvent, code: str, quantity: int,
                   price: float = None):
        """限价卖出，如 /炒股 卖出 600001 1000 4.5（支持 3 位短码；价格可省，按跌停价甩卖）"""
        uid = self._uid(event)
        if self._rate_limited(uid):
            log.warning("限流：u=%s 卖出 过于频繁", uid)
            yield event.plain_result(
                f"⚠️ 操作太频繁，每 {CFG.trade_cooldown_seconds:.0f} 秒只能下一单。")
            return
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "卖出",
                               lambda: self.trade.sell(uid, resolved, quantity, price))
        if msg.startswith("📋"):
            await self._quest_progress(uid, "trade_count", {"trade_count": 1})
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
        await self._quest_progress(uid, "position_count",
                                   {"position_count": len(rows)})
        lines = ["💼 我的持仓"]
        total_mv = 0.0
        total_pnl = 0.0
        card_positions = []
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
            card_positions.append({
                "code": pos["code"], "name": s.name,
                "shares": pos["quantity"], "sellable": pos["sellable"],
                "cost": pos["cost_price"], "price": q.price,
                "pnl": pnl, "pnl_pct": pct,
            })
        lines.append(f"持仓市值 {total_mv:,.0f}　浮动盈亏 "
                     f"{'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}")
        lines.append("（🔒 含今日买入，T+1 次日可卖）")
        if await self._send_card(
                event, "render_holdings", f"holdings_{uid}.png",
                {
                    "holdings": card_positions,
                    "total_market_value": total_mv,
                    "total_pnl": total_pnl,
                    "cash": acc["cash_available"],
                    "total_assets": acc["cash_available"] + total_mv,
                }):
            return
        await self._paged_send(event, "\n".join(lines), "positions")

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
        await self._paged_send(event, "\n".join(lines), "orders")

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
        data = {
            "available": acc["cash_available"],
            "frozen": frozen,
            "withdrawable": acc["cash_withdrawable"],
            "total": total,
            "pnl": pnl,
            "pnl_pct": (pnl / acc["initial_cash"] * 100
                        if acc["initial_cash"] else 0.0),
            "initial": acc["initial_cash"],
        }
        if await self._send_card(event, "render_funds", f"funds_{uid}.png",
                                 data):
            return
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
    async def rank(self, event: AstrMessageEvent, period: str = None):
        """股神排行榜（仅真实玩家）：/炒股 排行 [日/周/总]，缺省=总榜"""
        await self._quest_progress(self._uid(event), "view_leaderboard")
        p = (period or "总").strip()
        if p in ("总", "全部", "总榜", "total", "all"):
            entries = await self.board.top(self._price_of,
                                           CFG.rank_all_limit)
            text = self.board.render(entries)
            if await self._send_card(event, "render_leaderboard",
                                     "rank.png", entries,
                                     "🏆 股神排行榜"):
                return
            await self._paged_send(event, text, "rank")
            return
        kind = ("day" if p in ("日", "今日", "day", "d")
                else "week" if p in ("周", "本周", "week", "w") else None)
        if kind is None:
            await event.send(event.plain_result(
                "⚠️ 周期参数：/炒股 排行 日 | 周 | 总"))
            return
        entries = await self.board.period_top(kind, self._price_of,
                                              CFG.rank_all_limit)
        if entries is None:      # 首个虚拟交易日收盘前还没有快照
            await event.send(event.plain_result(
                "还没有日终快照（首个虚拟交易日收盘后生成），"
                "先看总榜：/炒股 排行 总"))
            return
        title = ("🏆 日榜（今日收益率）" if kind == "day"
                 else "🏆 周榜（近 7 日收益率）")
        if await self._send_card(event, "render_leaderboard",
                                 f"rank_{kind}.png", entries, title):
            return
        await self._paged_send(event, self.board.render(entries, title),
                               f"rank_{kind}")

    @stock.command("全体榜", alias={"总排行"})
    async def rank_all(self, event: AstrMessageEvent):
        """全体排行榜（真实玩家 + 模拟股民警，t1）"""
        entries = await self.board.top(self._price_of,
                                       CFG.rank_all_limit,
                                       include_npc=True)
        await self._paged_send(
            event,
            self.board.render(entries,
                              "🏆 全体排行榜（玩家 + 模拟股民警）"),
            "rank_all")

    # ── P5：纯文本翻页（QQ 官方按钮不可达，见 decisions.md）──────
    @stock.command("下一页", alias={"下页", "p"})
    async def page_next_cmd(self, event: AstrMessageEvent):
        """翻到下一页：/炒股 下一页"""
        yield event.plain_result(await self._page_turn(event, +1))

    @stock.command("上一页", alias={"上页"})
    async def page_prev_cmd(self, event: AstrMessageEvent):
        """翻回上一页：/炒股 上一页"""
        yield event.plain_result(await self._page_turn(event, -1))

    # ── P3：自选股 / 历史曲线 / 破产榜 / 成就 ──────────────────
    @stock.command("自选")
    async def watchlist_cmd(self, event: AstrMessageEvent):
        """自选股行情：/炒股 自选"""
        uid = self._uid(event)
        msg = await self._safe(event, "自选",
                                lambda: self.watchlist.render(uid,
                                                             self._price_of))
        yield event.plain_result(msg)

    @stock.command("自选加")
    async def watchlist_add_cmd(self, event: AstrMessageEvent, code: str):
        """加入自选：/炒股 自选加 <代码>（支持 3 位短码）"""
        uid = self._uid(event)
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "自选加",
                               lambda: self.watchlist.add(uid, resolved))
        yield event.plain_result(_as_msg(msg))

    @stock.command("自选删")
    async def watchlist_remove_cmd(self, event: AstrMessageEvent,
                                   code: str):
        """移出自选：/炒股 自选删 <代码>（支持 3 位短码）"""
        uid = self._uid(event)
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "自选删",
                               lambda: self.watchlist.remove(uid, resolved))
        yield event.plain_result(_as_msg(msg))

    @stock.command("排行历史")
    async def rank_history_cmd(self, event: AstrMessageEvent):
        """状元收益曲线（近 14 天）：/炒股 排行历史"""
        # service 契约：无 Pillow → ASCII 文本；有 Pillow 时 render/chart.py
        # 的 render_history_chart 由 city_probe 侧接入，这里只做兜底，
        # 避免 None 直接抛给用户。
        msg = await self._safe(event, "排行历史",
                               lambda: self.board.render_history(14))
        if msg is None:
            msg = "历史曲线需要 Pillow 出图，当前环境不可用。"
        yield event.plain_result(msg)

    @stock.command("破产榜")
    async def bankrupt_cmd(self, event: AstrMessageEvent):
        """破产玩家名录：/炒股 破产榜"""
        msg = await self._safe(event, "破产榜",
                               lambda: self.board.render_bankrupt(20))
        await self._paged_send(event, msg, "bankrupt")

    @stock.command("成就")
    async def achieve_cmd(self, event: AstrMessageEvent):
        """我的成就：/炒股 成就"""
        uid = self._uid(event)
        msg = await self._safe(event, "成就",
                               lambda: self.achieve.render(uid,
                                                           self._price_of))
        # 达成的成就自动发放金币（每项 50），已领记录在 meta（逗号分隔）
        try:
            items = await self.achieve.list_achievements(uid, self._price_of)
            key = f"achieve.claimed.{uid}"
            raw = await self.db.get_meta(key) or ""
            claimed = {x for x in raw.split(",") if x}
            fresh = [a["id"] for a in items
                     if a.get("done") and a["id"] not in claimed]
            if fresh:
                coins = 50 * len(fresh)
                acc = await self.db.get_account(uid)
                if acc:
                    await self.db.update_cash_full(
                        uid, acc["cash_available"] + coins,
                        acc["cash_withdrawable"] + coins, acc["cash_frozen"])
                await self.db.set_meta(
                    key, ",".join(sorted(claimed | set(fresh))))
                msg = (f"🏅 达成 {len(fresh)} 项新成就，奖励 {coins} 金币！\n"
                       + (msg or ""))
        except Exception:
            log.exception("成就发放失败（不影响成就展示）")
        await self._paged_send(event, msg, "achieve")

    # ── S6 内容：签到 / 日常任务 ──────────────────────────
    @stock.command("签到")
    async def quest_checkin_cmd(self, event: AstrMessageEvent):
        """每日签到领金币（连击递增）：/炒股 签到"""
        uid = self._uid(event)

        async def op():
            res = await self.quest.handle_checkin(uid)
            if res.get("ok"):
                acc = await self.db.get_account(uid)
                if acc:
                    await self.db.update_cash_full(
                        uid, acc["cash_available"] + res["reward"],
                        acc["cash_withdrawable"] + res["reward"],
                        acc["cash_frozen"])
                return f"✅ {res['msg']}"
            return f"⚠️ {res['error']}"

        msg = await self._safe(event, "签到", op)
        yield event.plain_result(msg)

    @stock.command("日常任务", alias={"任务"})
    async def quest_tasks_cmd(self, event: AstrMessageEvent):
        """今日日常任务进度：/炒股 日常任务"""
        uid = self._uid(event)

        async def op():
            info = await self.quest.get_user_quest_info(uid)
            if not info.get("ok"):
                return "⚠️ 尚未开启任务记录，先 /炒股 签到 自动开启。"
            q = info["quest_info"]
            lines = [f"📝 日常任务（连击 {q['checkin_streak']} 天）"]
            for t in info["daily_tasks"]:
                done = t["progress"] >= t["target"]
                mark = "✅" if done else "⬜"
                lines.append(
                    f"{mark} {t['title']}：{t['progress']}/{t['target']}"
                    f"{t['unit']}（奖励 {t['reward']} 金）")
            return "\n".join(lines)

        msg = await self._safe(event, "日常任务", op)
        await self._paged_send(event, msg, "quest_tasks")

    # ── S6 内容：道具商店 ──────────────────────────────────
    @stock.command("道具")
    async def props_cmd(self, event: AstrMessageEvent):
        """道具商店 + 我的库存：/炒股 道具"""
        uid = self._uid(event)

        async def op():
            shop = await self.props.get_shop(uid)
            owned = await self.props.get_user_props(uid)
            if await self._send_card(event, "render_props_shop",
                                     f"props_{uid}.png", shop, owned):
                return None  # 已发图，op 返回 None 让 _safe 不报空
            lines = ["🛒 今日道具商店"]
            for it in shop["items"]:
                lines.append(
                    f"  {it['prop_id']} {it['name']}　{it['price']}金"
                    f"（余 {it['left_today']}/{it['limit_per_day']}）"
                    f" — {it['description']}")
            lines.append("🎒 我的库存")
            if not owned:
                lines.append("  （空）")
            else:
                for p in owned:
                    d = PROP_BY_ID.get(p["prop_id"], {})
                    lines.append(f"  {p['prop_id']} {d.get('name','?')}"
                                 f" ×{p['quantity']}")
            lines.append("用法：/炒股 购买 <prop_id> [数量] · "
                         "/炒股 使用 <prop_id> · /炒股 出售 <prop_id> [数量]")
            return "\n".join(lines)

        msg = await self._safe(event, "道具", op)
        if msg is None:
            return
        await self._paged_send(event, msg, "props")

    @stock.command("购买")
    async def props_buy_cmd(self, event: AstrMessageEvent, prop_id: str,
                            quantity: int = 1):
        """购买道具：/炒股 购买 <prop_id> [数量]"""
        uid = self._uid(event)
        msg = await self._safe(event, "购买",
                               lambda: self._props_buy(uid, prop_id, quantity))
        yield event.plain_result(msg)

    async def _props_buy(self, uid: str, prop_id: str, quantity: int) -> str:
        res = await self.props.buy_prop(uid, prop_id, quantity)
        if not res["ok"]:
            return f"⚠️ {res['msg']}"
        d = PROP_BY_ID.get(prop_id, {})
        return (f"✅ 购买成功：{d.get('name', prop_id)} ×{res['quantity']}，"
                f"花费 {res['total_price']:,.2f} 金（余 {res['remaining_cash']:,.2f} 金）。")

    @stock.command("出售")
    async def props_sell_cmd(self, event: AstrMessageEvent, prop_id: str,
                             quantity: int = 1):
        """出售道具：/炒股 出售 <prop_id> [数量]"""
        uid = self._uid(event)
        msg = await self._safe(event, "出售",
                               lambda: self._props_sell(uid, prop_id, quantity))
        yield event.plain_result(msg)

    async def _props_sell(self, uid: str, prop_id: str, quantity: int) -> str:
        res = await self.props.sell_prop(uid, prop_id, quantity)
        if not res["ok"]:
            return f"⚠️ {res['msg']}"
        d = PROP_BY_ID.get(prop_id, {})
        return (f"✅ 出售成功：{d.get('name', prop_id)} ×{res['quantity']}，"
                f"获得 {res['total_income']:,.2f} 金（余 {res['remaining_cash']:,.2f} 金）。")

    @stock.command("使用")
    async def props_use_cmd(self, event: AstrMessageEvent, prop_id: str,
                            target: str = None):
        """使用道具：/炒股 使用 <prop_id> [目标]"""
        uid = self._uid(event)
        msg = await self._safe(event, "使用",
                               lambda: self._props_use(uid, prop_id, target))
        if msg.startswith("✅"):
            await self._quest_progress(uid, "use_prop", {"use_count": 1})
        yield event.plain_result(msg)

    async def _props_use(self, uid: str, prop_id: str,
                         target: str | None) -> str:
        res = await self.props.use_prop(uid, prop_id, target)
        if not res["ok"]:
            return f"⚠️ {res['msg']}"
        effect = res.get("effect") or {}
        etype = effect.get("type")
        if etype == "grant_cash":
            amount = effect.get("amount", 0)
            acc = await self.db.get_account(uid)
            if acc:
                await self.db.update_cash_full(
                    uid, acc["cash_available"] + amount,
                    acc["cash_withdrawable"] + amount, acc["cash_frozen"])
            return f"✅ {res['msg']}，获得 {amount:,.2f} 金。"
        if etype == "trend_preview":
            days = effect.get("preview", [])
            if not days:
                return f"✅ {res['msg']}，但暂无可用走势数据。"
            lines = [f"✅ {res['msg']}，未来 {len(days)} 天走势区间："]
            for d in days:
                lines.append(
                    f"  第{d.get('day','?')}天：{d.get('change_pct',0)*100:+.2f}%")
            return "\n".join(lines)
        if etype == "complete_daily":
            # 奖励发放上限由模块控制
            reward = effect.get("reward", 0)
            if reward:
                acc = await self.db.get_account(uid)
                if acc:
                    await self.db.update_cash_full(
                        uid, acc["cash_available"] + reward,
                        acc["cash_withdrawable"] + reward, acc["cash_frozen"])
            return (f"✅ {res['msg']}，获得 {reward:,.2f} 金加速奖励。")
        if etype == "review":
            lines = [
                f"✅ {res['msg']}，近 {effect.get('trades_count',0)} 笔成交复盘：",
                f"  总盈亏 {effect.get('total_pnl',0):,.2f} 金",
                f"  盈利 {effect.get('win',0)} 笔 / 亏损 {effect.get('loss',0)} 笔"]
            return "\n".join(lines)
        # fee_immunity / fee_discount：状态型效果，下次交易自动消耗，
        # 这里只提示已激活（实际钩子在 buy/sell 包装层）。
        return f"✅ {res['msg']}，效果已激活（下次交易生效）。"

    # ── S6 内容：赛季 / 段位 ───────────────────────────────
    @stock.command("赛季", alias={"段位"})
    async def season_cmd(self, event: AstrMessageEvent):
        """我的赛季段位：/炒股 赛季"""
        uid = self._uid(event)

        async def op():
            info = await self.season.get_season_info(uid)
            if not info.get("season_id"):
                return "⚠️ 当前没有进行中的赛季。"
            rank_names = ["新手", "老手", "能手", "大师", "宗师", "传奇"]
            thr = info.get("rank_thresholds") or self._SEASON_RANK_THRESHOLDS
            rank_idx = 0
            for i, t in enumerate(thr):
                if info["points"] >= t:
                    rank_idx = i
            name = rank_names[min(rank_idx, len(rank_names) - 1)]
            nxt = info.get("next_threshold")
            nxt_txt = (f"，距下一段位 {nxt - info['points']} 分"
                       if nxt else "（已达最高段位）")
            return (f"🏆 {info['season_name']}\n"
                    f"段位：{name}（{info['points']} 分）{nxt_txt}")

        msg = await self._safe(event, "赛季", op)
        yield event.plain_result(msg)

    @stock.command("赛季奖励")
    async def season_reward_cmd(self, event: AstrMessageEvent):
        """领取赛季段位奖励：/炒股 赛季奖励"""
        uid = self._uid(event)

        async def op():
            res = await self.season.get_season_reward(uid)
            if not res.get("success"):
                return f"⚠️ {res.get('error', '领取失败')}"
            reward = res.get("reward", 0)
            acc = await self.db.get_account(uid)
            if acc and reward:
                await self.db.update_cash_full(
                    uid, acc["cash_available"] + reward,
                    acc["cash_withdrawable"] + reward, acc["cash_frozen"])
            return f"✅ 赛季奖励已领取：{reward:,.2f} 金。"

        msg = await self._safe(event, "赛季奖励", op)
        yield event.plain_result(msg)

    # ── S6 内容：龙虎榜 / 资金流 / 异动榜 ──────────────────
    @stock.command("龙虎榜")
    async def dragon_tiger_cmd(self, event: AstrMessageEvent):
        """龙虎榜（买卖前五席位）：/炒股 龙虎榜"""
        async def op():
            dt = get_dragon_tiger(self.engine, STOCK_POOL, limit=5)
            if not dt:
                return "今日暂无龙虎榜数据。"
            if await self._send_card(event, "render_dragon_tiger",
                                     "dragon_tiger.png", dt):
                return None
            lines = ["🐉 龙虎榜（买卖前五席位）"]
            for code, seats in dt.items():
                s = STOCK_POOL.get(code)
                name = s.name if s else code
                lines.append(f"【{name}（{code}）】")
                for seat in seats:
                    net = "净买" if seat.net_amount >= 0 else "净卖"
                    lines.append(
                        f"  {seat.rank}. {seat.name}　买 "
                        f"{seat.buy_amount:,.0f} 卖 {seat.sell_amount:,.0f} "
                        f"（{net} {abs(seat.net_amount):,.0f}）")
            return "\n".join(lines)

        msg = await self._safe(event, "龙虎榜", op)
        if msg is None:
            return
        await self._paged_send(event, msg, "dragon_tiger")

    @stock.command("资金流")
    async def fund_flow_cmd(self, event: AstrMessageEvent):
        """主力资金净流入榜：/炒股 资金流"""
        async def op():
            items = get_fund_flow_ranking(self.engine, STOCK_POOL, limit=20)
            if not items:
                return "暂无资金流数据。"
            if await self._send_card(event, "render_fund_flow",
                                     "fund_flow.png", items):
                return None
            lines = ["💰 主力资金净流入榜"]
            for it in items:
                arrow = "↑" if it.net_inflow >= 0 else "↓"
                lines.append(
                    f"  {it.rank}. {it.name}（{it.code}）{arrow} "
                    f"{it.net_inflow:,.0f}　价 {it.price:.2f} "
                    f"（{it.change_pct:+.2f}%）")
            return "\n".join(lines)

        msg = await self._safe(event, "资金流", op)
        if msg is None:
            return
        await self._paged_send(event, msg, "fund_flow")

    @stock.command("异动榜")
    async def anomaly_cmd(self, event: AstrMessageEvent):
        """异动榜（涨跌幅/换手/振幅超阈值）：/炒股 异动榜"""
        async def op():
            items = get_anomaly_ranking(self.engine, STOCK_POOL, limit=20)
            if not items:
                return "今日暂无异动股票。"
            if await self._send_card(event, "render_anomaly",
                                     "anomaly.png", items):
                return None
            lines = ["⚠️ 异动榜"]
            for it in items:
                lines.append(
                    f"  {it.rank}. {it.name}（{it.code}）{it.anomaly_type} "
                    f"{it.value:+.2f}（阈值 {it.threshold}）"
                    f"　价 {it.price:.2f}（{it.change_pct:+.2f}%）")
            return "\n".join(lines)

        msg = await self._safe(event, "异动榜", op)
        if msg is None:
            return
        await self._paged_send(event, msg, "anomaly")

    # ── P4：玩家自发行股票 / 我的发行 / 模拟股民警 ──────────────
    @stock.command("发行")
    async def issue_cmd(self, event: AstrMessageEvent, name: str,
                        price: float, total_shares: int):
        """玩家自发行股票：/炒股 发行 <名称> <发行价> <发行股数>"""
        uid = self._uid(event)
        msg = await self._safe(event, "发行",
                               lambda: self.ipo.issue_stock(
                                   uid, name, price, total_shares))
        yield event.plain_result(_as_msg(msg))

    @stock.command("我的发行")
    async def my_issues_cmd(self, event: AstrMessageEvent):
        """我发行的股票状态：/炒股 我的发行"""
        uid = self._uid(event)
        msg = await self._safe(event, "我的发行",
                               lambda: self.ipo.list_my_ipos(uid))
        await self._paged_send(event, msg, "my_ipo")

    @stock.command("npc")
    async def npc_cmd(self, event: AstrMessageEvent):
        """模拟股民警状态：/炒股 npc（常开模式）"""
        msg = await self._safe(event, "npc", self.npc.render)
        await self._paged_send(event, msg, "npc")

    @stock.command("npc开", alias={"npc启动", "开启npc"})
    async def npc_start_cmd(self, event: AstrMessageEvent):
        """模拟股民警已常开（历史指令兼容）"""
        msg = await self._safe(event, "npc开", self.npc.start)
        yield event.plain_result(msg)

    @stock.command("npc关", alias={"关npc", "npc停"})
    async def npc_stop_cmd(self, event: AstrMessageEvent):
        """模拟股民警为常开模式，暂停请求不再生效（历史指令兼容）"""
        msg = await self._safe(event, "npc关", self.npc.stop)
        yield event.plain_result(msg)

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
        await self._paged_send(event, self.news.render_today(), "news")

    @stock.command("打新")
    async def ipo_cmd(self, event: AstrMessageEvent, code: str = None,
                      lots: int = None):
        """新股申购：/炒股 打新（发行面板）· /炒股 打新 <代码> <签数>"""
        uid = self._uid(event)
        if code is None:
            msg = await self._safe(event, "打新",
                                   lambda: self.ipo.status_text(uid))
            await self._paged_send(event, msg, "ipo")
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
            await self._paged_send(event, msg, "margin")
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
        """融券卖出：/炒股 融券 <代码> <数量>（支持 3 位短码）"""
        uid = self._uid(event)
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "融券",
                               lambda: self.margin.borrow_stock(uid, resolved,
                                                                quantity))
        yield event.plain_result(msg)

    @stock.command("还券")
    async def repay_margin_cmd(self, event: AstrMessageEvent, code: str,
                               quantity: int):
        """买券还券：/炒股 还券 <代码> <数量>（支持 3 位短码）"""
        uid = self._uid(event)
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "还券",
                               lambda: self.margin.cover(uid, resolved, quantity))
        yield event.plain_result(msg)

    # ── 做空 / 杠杆（P5，杠杆层只读查询，下单仍走融券/融资）──────
    @stock.command("做空", alias={"空"})
    async def short_cmd(self, event: AstrMessageEvent, code: str = None,
                        quantity: int = None):
        """做空面板（无参）/ 融券卖出：/炒股 做空 <代码> <数量>（支持 3 位短码）"""
        uid = self._uid(event)
        if code is None:
            msg = await self._safe(event, "做空",
                                   lambda: self.short.panel(uid))
            await self._paged_send(event, msg, "short")
            return
        if quantity is None:
            raise MarginError("用法：/炒股 做空 <代码> <数量>，"
                              "或不带参数查看做空面板。")
        resolved = self._resolve_code(code)
        if not resolved:
            yield event.plain_result(f"⚠️ 股票代码无效，用 /炒股 搜 查代码。")
            return
        msg = await self._safe(event, "做空",
                               lambda: self.margin.borrow_stock(uid, resolved,
                                                                quantity))
        yield event.plain_result(msg)

    @stock.command("杠杆")
    async def leverage_cmd(self, event: AstrMessageEvent, leverage: int = None):
        """杠杆查询/设定：/炒股 杠杆 或 /炒股 杠杆 <1-3>"""
        uid = self._uid(event)
        if leverage is None:
            room = await self._safe(event, "杠杆",
                                    lambda: self.short.leverage_room(uid))
            cap = self.short.max_leverage()
            msg = (f"📈 杠杆能力\n"
                   f"当前上限 {cap}×（默认 {DEFAULT_LEVERAGE}×）\n"
                   f"可开杠杆买入额：{room:,.2f} 元\n"
                   f"设定倍数：/炒股 杠杆 <1-{cap}>")
            await self._paged_send(event, msg, "leverage")
            return
        try:
            self.short.check_leverage(leverage)
        except LeverageError as e:
            yield event.plain_result(f"⚠️ {e}")
            return
        room = await self._safe(event, "杠杆",
                                lambda: self.short.leverage_room(uid, leverage))
        msg = (f"✅ 杠杆倍数已校验通过：{leverage}×\n"
               f"该倍数下可买入额：{room:,.2f} 元\n"
               f"（做空下单仍走 /炒股 融券；融资下单走 /炒股 融资）")
        await self._paged_send(event, msg, "leverage")

