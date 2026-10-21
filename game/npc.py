"""模拟股民警（P4 NPC）：虚拟账户 + 简单策略 + 由 tick 循环注入订单。

设计要点（t11 契约 + hang_analyst 只读核实结论）：

1. **只准备订单，下单由外层 tick 调 trade.buy/sell。**
   NpcService.tick() 自身**绝不持锁**——若它自己 `async with
   trade_lock()` 再去调 `trade.buy()`（t2 之后 trade.buy 入口也持锁），
   就是第 4 次同形状死锁（locks.py:15-19 是单例非重入锁）。
   正确形状：tick() 返回想下的单列表，`await self.trade.buy(npc_id,...)`
   由调用方在不持锁状态逐个执行。

2. **NPC 账户必须隔离出 accounts 表的统计口径。**
   user_id 统一下 `npc:` 前缀，且在三处过滤，否则 NPC 会污染真实玩家
   的统计与榜单（hang_analyst 发现的三个真实隐患）：
   - `storage/db.py stats()`：accounts/active/bankrupt 按状态统计，
     NPC 会虚增开户数；
   - `game/dayclose.py` 的 `_post_trade_bankruptcy_check` 会把现金耗尽的
     NPC 标 bankrupt，而 NPC 没有 `/炒股 重生` 入口，一旦进去就永久滞留
     破产名单，还会出现在破产榜叙事里；
   - `game/leaderboard.py top()` → `db.list_accounts()` 全表扫，NPC 的
     收益率会混进「股神排行榜」（DESIGN.md:313 定「收益率为主」）。
   实现方式：给 db 加 `list_accounts(is_npc=False)` 的过滤参数，
   leaderboard.py 的 top()/snapshot_day()/render_bankrupt() 全部传
   is_npc=False；stats() 的 SQL 加 `user_id NOT LIKE 'npc:%'`。
   破产 NPC 由本服务自动重建（rebuild），不进重生流程。

3. **NPC 无 /炒股 指令、无人类用户，绝不响应任何玩家消息。**

4. **模拟股民警「常开」（t1）**：默认就有 AI 股民在炒股，用户不需要
   选择开不开。`self.enabled` 恒为 True，`tick()` 不再做开关判断；
   `start()`/`stop()` 仅为签名兼容保留，`stop()` 不再关闭任何东西。
   `CFG.npc_active` 退化为预留开关（默认 True，不再提供指令切换）。
   NPC 仍不进入「玩家榜」/snapshot_day()/render_bankrupt()——
   它们只出现在 `leaderboard.top(include_npc=True)` 的全体榜里。
"""

from __future__ import annotations

import random
import time

from ..config import CFG
from ..log import get_logger
from ..market.stocks import STOCK_POOL
from ..storage.db import Database

log = get_logger("npc")

NPC_PREFIX = "npc:"          # NPC user_id 前缀（统计过滤的唯一依据）
_NPC_NAMES = ("张三", "李四", "王五", "赵六", "孙七", "周八",
              "吴九", "郑十", "钱十一", "冯十二")


def is_npc(user_id: str) -> bool:
    """判断一个 user_id 是否属于模拟股民警（统计过滤用）。"""
    return str(user_id).startswith(NPC_PREFIX)


def npc_id(index: int) -> str:
    """第 index 个 NPC 的稳定 user_id（0-based）。"""
    return f"{NPC_PREFIX}{index}"


class NpcService:
    """模拟股民警：极简策略的虚拟交易者，让市场不只有真实玩家的手在动。"""

    def __init__(self, db: Database, engine, trade):
        self.db = db
        self.engine = engine
        self.trade = trade
        self._rng = random.Random(0x9E37)
        # t1：模拟股民警改为「常开」语义——默认常开、无需开关，
        # enabled 恒为 True，tick() 不再受开关判断拦截。
        self.enabled = True
        self._decisions = 0                 # 累计决策次数（面板展示）

    # ── 散户大军（人群层）────────────────────────────────────
    # 现实里股民「数不清」。逐个建账户既不像真，也跑不动（每单都要写库），
    # 所以用聚合模型：一个 2.8~7.6 万人的群体，每 tick 只在少数股票上产生
    # 净买卖压力，喂给撮合与供需冲击；不占账户、不写库、不进排行榜。
    CROWD_MIN = 28000
    CROWD_MAX = 76000

    def _ensure_crowd(self) -> dict:
        crowd = getattr(self, "_crowd", None)
        if crowd is None:
            rng = random.Random(0xC0FFEE)
            self._crowd_rng = rng
            crowd = self._crowd = {
                "people": rng.randint(self.CROWD_MIN, self.CROWD_MAX),
                "net": 0.0,        # 今日净买入（元，正=净买入）
                "turnover": 0.0,   # 今日成交额（元）
                "ret": 0.0,        # 群体累计收益（小数，长期偏负）
                "mood": 50.0,      # 0..100 群体情绪
            }
        return crowd

    def crowd_tick(self) -> dict[str, int]:
        """推进人群层一个 tick，返回 {code: 净买股数}（正=买入）。

        行为：追涨杀跌（顺着上一个 tick 的涨跌幅下单）、人越多手越重、
        整体偏买方；收益长期被追高杀低与手续费吃掉，所以 ret 偏负。
        """
        crowd = self._ensure_crowd()
        rng = self._crowd_rng
        people = crowd["people"]
        drift = max(1, people // 500)
        crowd["people"] = max(self.CROWD_MIN, min(
            self.CROWD_MAX, people + rng.randint(-drift, drift) + 1))
        codes = [c for c, q in self.engine.quotes.items() if q is not None]
        if not codes:
            return {}
        pressure: dict[str, int] = {}
        scale = crowd["people"] / 50000.0
        picks = rng.sample(codes, max(6, min(len(codes), len(codes) // 5)))
        for code in picks:
            q = self.engine.get(code)
            if q is None:
                continue
            bias = max(-1.0, min(1.0, q.change_pct / 4.0))
            side = bias * 0.65 + rng.gauss(0.02, 0.42)
            qty = int(abs(rng.gauss(0.0, 1.0)) * 2200 * scale) + rng.randint(100, 900)
            if side < -0.12:
                qty = -qty
            elif side < 0.10:
                qty = max(1, int(qty * 0.2))
            pressure[code] = pressure.get(code, 0) + qty
            crowd["net"] += qty * q.price
            crowd["turnover"] += abs(qty) * q.price
        pcts = [q.change_pct for q in self.engine.quotes.values() if q is not None]
        avg = sum(pcts) / max(len(pcts), 1)
        crowd["mood"] = max(0.0, min(
            100.0, crowd["mood"] * 0.985 + (50.0 + avg * 6.0) * 0.015))
        crowd["ret"] = crowd["ret"] * 0.999 + avg / 100.0 * 0.9 - 0.00002
        return pressure

    def crowd_stats(self) -> dict:
        """人群层快照（行情卡/全体榜展示用）。"""
        return dict(self._ensure_crowd())

    # ── 账户 ────────────────────────────────────────────────
    async def ensure_accounts(self) -> int:
        """确保 cfg.npc_count 个 NPC 账户存在（幂等；一次性启动调用）。"""
        n = 0
        now = int(time.time())
        for i in range(max(0, int(CFG.npc_count))):
            uid = npc_id(i)
            if await self.db.get_account(uid) is None:
                name = _NPC_NAMES[i % len(_NPC_NAMES)]
                # create_account 是本项目唯一的开户 dao（storage/db.py:209）。
                await self.db.create_account(
                    uid, f"[股民]{name}", CFG.npc_initial_cash, now)
                n += 1
        if n:
            log.info("模拟股民警：新建 %d 个虚拟账户（共 %d 个）",
                     n, int(CFG.npc_count))
        return n

    async def _live_npcs(self) -> list[str]:
        """取当前可用的 NPC 账户（只取存在的，不负责补齐）。"""
        rows = await self.db.list_accounts(is_npc=True)
        return [r["user_id"] for r in rows]

    # ── 策略 ────────────────────────────────────────────────
    def _pick(self, uid: str) -> tuple[str, str, int, float] | None:
        """选一只股票、一个方向、一个手数。

        跟势（CFG.npc_momentum）：挑当前涨幅最猛或最惨的股票做反向/顺势，
        其余情况随机。返回 (code, side, lots, price)。
        """
        codes = list(STOCK_POOL)
        if not codes:
            return None
        quotes = [(c, self.engine.get(c)) for c in codes]
        quotes = [(c, q) for c, q in quotes if q is not None]
        if not quotes:
            return None
        if self._rng.random() < CFG.npc_momentum:
            quotes.sort(key=lambda kv: kv[1].change_pct)
            code, q = quotes[-1] if self._rng.random() < 0.5 else quotes[0]
        else:
            code, q = self._rng.choice(quotes)
        side = "buy" if self._rng.random() < 0.55 else "sell"
        lots = self._rng.randint(1, max(1, int(CFG.npc_max_lots)))
        # limit_up/limit_down 的签名是 (stock: Stock)——Quote 自己不持有所属
        # 股票引用，必须从 STOCK_POOL 取（见 market/engine.py:96-99）。
        stock = STOCK_POOL[code]
        price = q.limit_up(stock) if side == "buy" else q.limit_down(stock)
        return code, side, lots, round(price, 2)

    async def _decide(self, uid: str) -> dict | None:
        """为一个 NPC 产出一笔决策（可能是 buy 也可能是 sell）。"""
        choice = self._pick(uid)
        if choice is None:
            return None
        code, side, lots, price = choice
        if side == "sell":
            pos = await self.db.get_position(uid, code)
            if not pos or pos["sellable"] <= 0:
                # 没票可卖 → 改买一手（保证这个 NPC 有动作）
                side = "buy"
                lots = 1
        return {"user_id": uid, "code": code, "side": side,
                "lots": lots, "price": price}

    # ── 每 tick 驱动 ────────────────────────────────────────
    async def tick(self) -> list[dict]:
        """产出本 tick 的全部 NPC 决策（只读，不持锁、不下单）。

        真正下单由调用方（main.py 的 _tick_loop）按用户习惯逐个调
        trade.buy/sell——那两个方法自带 trade_lock，所以这里绝不能再持锁。

        t1：模拟股民警改为「常开」语义——不再受 enabled / CFG.npc_active
        开关判断拦截，每 tick 都产出决策（账户尚未初始化时返回空）。

        下面 `npcs = ...` / `if not npcs:` 是**账户存在性**守卫，不是开关。
        """
        npcs = await self._live_npcs()
        if not npcs:
            return []
        # 每 tick 只让一部分 NPC 动手（约半数），避免满屏噪声
        acted = self._rng.sample(npcs, max(1, len(npcs) // 2))
        orders = []
        for uid in acted:
            d = await self._decide(uid)
            if d is not None:
                orders.append(d)
        self._decisions += len(orders)
        return orders

    async def run_orders(self, orders: list[dict]) -> tuple[int, int]:
        """执行一批评单（调用方在不持锁状态调）。

        返回 (成功笔数, 失败笔数)。失败只记账不抛：NPC 下单失败是常态
        （资金不足/无票可卖/撞涨跌停），绝不能把 tick 循环带崩。
        """
        ok = bad = 0
        for o in orders:
            try:
                if o["side"] == "buy":
                    # buy 的 quantity 是「股」，而 _check_common 要求
                    # CFG.lot_size 的整数倍（game/trade.py:521-525）。
                    await self.trade.buy(o["user_id"], o["code"],
                                        o["lots"] * CFG.lot_size,
                                        o["price"])
                else:
                    # sell 的 quantity 同样是「股」。
                    await self.trade.sell(o["user_id"], o["code"],
                                          o["lots"] * CFG.lot_size,
                                          o["price"])
                ok += 1
            except Exception as e:                      # noqa: BLE001
                bad += 1
                log.debug("NPC 下单失败 %s %s: %s", o["user_id"], o["side"], e)
        return ok, bad

    # ── 破产重建 ────────────────────────────────────────────
    async def rebuild_bankrupt(self) -> int:
        """把破产的 NPC 重置回可交易状态（NPC 无重生入口，自愈）。

        复用 do_rebirth 会累加 rebirth_count，那会让 NPC 出现在破产榜
        叙事里（DESIGN 里那是玩家的故事）。所以这里直接 set_status
        active + 补一笔初始资金，不碰 rebirth_count。
        """
        rows = await self.db.list_accounts(is_npc=True)
        fixed = 0
        for r in rows:
            if r["status"] != "bankrupt":
                continue
            await self.db.set_status(r["user_id"], "active", int(time.time()))
            acc = await self.db.get_account(r["user_id"])
            await self.db.update_cash_full(
                r["user_id"], CFG.npc_initial_cash,
                CFG.npc_initial_cash, 0.0)
            log.info("NPC %s 破产自愈：现金重置 %.2f",
                     r["user_id"], acc["cash_available"] if acc else 0.0)
            fixed += 1
        return fixed

    # ── 指令面板 ────────────────────────────────────────────
    async def render(self) -> str:
        """`/炒股 npc` 面板：常开状态 + 每人数额与收益率。

        t1：常开语义下不再有「开启/暂停」可展示，改为一行「常开」说明；
        每人附带相对初始资金的收益率，与排行榜口径一致。
        """
        rows = await self.db.list_accounts(is_npc=True)
        if not rows:
            return ("👥 模拟股民警：尚未初始化\n"
                    "系统会在启动时自动生成虚拟账户（常开，无需开关）。")
        lines = ["👥 模拟股民警（常开 · 默认运行，无需开关）",
                 f"虚拟账户 {len(rows)} 个"]
        total = 0.0
        for r in sorted(rows, key=lambda x: x["user_id"]):
            cash = r["cash_available"]
            total += cash
            initial = r["initial_cash"] or 1.0
            pct = (cash - initial) / initial * 100.0
            tag = "💀" if r["status"] == "bankrupt" else "  "
            sign = "+" if pct >= 0 else ""
            lines.append(f"{tag}{r['user_name']} 可用 {cash:,.0f}"
                         f"（{sign}{pct:.2f}%）")
        lines.append(f"合计可用 {total:,.0f}")
        lines.append(f"累计决策 {self._decisions} 次")
        lines.append("模拟股民警计入「全体排行榜」，不影响玩家榜。")
        return "\n".join(lines)

    async def start(self) -> str:
        """`/炒股 npc`：常开语义，仅确保账户已建（不再切换开关）。

        保留方法签名兼容 wiring_check 第 9 节；返回值语义从「已开启」
        改为「常开」，调用方不应据此判断开关状态。
        """
        await self.ensure_accounts()
        self.enabled = True
        CFG.npc_active = True
        n = len(await self._live_npcs())
        return f"✅ 模拟股民警常开（{n} 个虚拟账户，无需开关）。"

    async def stop(self) -> str:
        """常开语义下的兼容占位：暂停请求不再生效。

        t1 产品要求「默认有 AI 股民炒股，不需要自己选择开不开」，
        所以这里保留签名与账户数据，但不再关闭任何东西。
        """
        self.enabled = True
        CFG.npc_active = True
        n = len(await self._live_npcs())
        return f"👥 模拟股民警为常开模式（{n} 个虚拟账户），无需暂停。"
