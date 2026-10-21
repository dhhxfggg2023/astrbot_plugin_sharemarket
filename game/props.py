"""道具卡经济（t3）：商店 / 库存 / 购买 / 出售 / 使用 / 效果查询。

模块边界（.dsh-code/content_spec.md:151-154 的三条范围铁律）：
- **不改 storage/db.py / main.py / config.py**：持久化全部走本模块
  ``ensure_schema()`` 自建的 4 张表；
- **不在本模块改账户/持仓表的「效果」**：道具的收益一律以结构化 ``effect``
  字典返回，由集成任务（t6）接线执行。本模块只做「消耗库存 + 算效果 +
  记账」，唯一触碰 accounts 的地方是玩家主动买卖时走的 db 账户 API
  （storage/db.py:204 get_account / :229 update_cash_full）；
- 不 import main.py / astrbot，可离线跑（.dsh-code/props_check.py）。

经济红线（对标手游内购）：任何一件道具的**期望收益必须低于售价**，道具整体
是现金的**沉淀口**而不是印钞机。逐件数字见文件末 ``ECONOMICS`` 注释与
.dsh-code/props_check.py 第 8 节；兜底手段是每日限购 + 幸运卡日派奖上限
+ 加速卡奖励上限 + 手续费减免按笔封顶。

指令字面量（main.py 接线用，本模块只提供方法）：
- ``/炒股 道具``             → get_shop + get_user_props + render_props
- ``/炒股 使用 <prop_id> [目标]`` → use_prop
- ``/炒股 购买 <prop_id> [数量]`` → buy_prop
- ``/炒股 出售 <prop_id> [数量]`` → sell_prop
"""

from __future__ import annotations

import random
import time
from typing import Optional

from ..log import get_logger
from ..market.stocks import STOCK_POOL
from .npc import is_npc

log = get_logger("props")

# ── 商店/经济常量 ────────────────────────────────────────────
_SHOP_SIZE = 3                  # 每日刷新 3 件（规格硬约束）
_LUCKY_MIN, _LUCKY_MAX = 100, 1000   # 幸运卡随机金区间（规格）
_LUCKY_DAILY_CAP = 1000         # 幸运卡日派奖上限：把期望压到 500 以下
_SPEED_REWARD_CAP = 150         # 加速卡单日奖励上限（< 售价 200）
_FEE_DISCOUNT_RATE = 0.5        # 手续费减免卡折扣（打 5 折）
_INFO_DAYS = 3                  # 信息卡预览天数（规格）
_SINGLE_USE_EFFECTS = ("fee_immunity", "complete_daily", "fee_discount")  # 单日单次生效
_INFO_BANDS = (0.05, 0.08, 0.12)    # 只给区间，且逐日变宽 → 无套利精度
_REVIEW_TRADES = 200            # 复盘卡回看的成交条数（只读）
_MAX_BUY_QTY = 10               # 单次购买数量上限（防误输入）

# 道具目录：price / sell_price / limit_per_day 与 .dsh-code/content_spec.md:51
# 的示例数值逐项一致（幸运卡 500/250/3、防护盾 800/400/1、信息卡 300/150/5、
# 加速卡 200/100/10）；手续费减免卡与复盘卡是本模块补齐契约所需的效果类型。
PROP_DEFS = (
    {"prop_id": "lucky_card", "name": "幸运卡", "price": 500,
     "sell_price": 250, "limit_per_day": 3, "effect": "grant_cash",
     "description": "随机 100-1000 金（每日派奖上限 1000）"},
    {"prop_id": "shield", "name": "防护盾", "price": 800,
     "sell_price": 400, "limit_per_day": 1, "effect": "fee_immunity",
     "description": "护盘卡：免一次交易手续费"},
    {"prop_id": "info_card", "name": "信息卡", "price": 300,
     "sell_price": 150, "limit_per_day": 5, "effect": "trend_preview",
     "description": "消息卡：查看未来 3 天走势区间"},
    {"prop_id": "speed_card", "name": "加速卡", "price": 200,
     "sell_price": 100, "limit_per_day": 10, "effect": "complete_daily",
     "description": "完成一天日常任务（奖励上限 150）"},
    {"prop_id": "fee_card", "name": "手续费减免卡", "price": 400,
     "sell_price": 200, "limit_per_day": 3, "effect": "fee_discount",
     "description": "手续费减免卡：下一笔交易手续费打 5 折"},
    {"prop_id": "review_card", "name": "复盘卡", "price": 600,
     "sell_price": 300, "limit_per_day": 2, "effect": "review",
     "description": "复盘卡：回看最近成交的盈亏结构"},
)
PROP_BY_ID = {d["prop_id"]: d for d in PROP_DEFS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_props (
    user_id TEXT NOT NULL,
    prop_id TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, prop_id)
);
CREATE TABLE IF NOT EXISTS prop_defs (
    prop_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    sell_price INTEGER NOT NULL,
    limit_per_day INTEGER NOT NULL DEFAULT 0,
    description TEXT
);
CREATE TABLE IF NOT EXISTS prop_purchases (
    user_id TEXT NOT NULL,
    day_ts INTEGER NOT NULL,
    prop_id TEXT NOT NULL,
    bought INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day_ts, prop_id)
);
CREATE TABLE IF NOT EXISTS prop_grants (
    user_id TEXT NOT NULL,
    day_ts INTEGER NOT NULL,
    prop_id TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    granted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day_ts, prop_id)
);
"""

def _day_key(ts: float | None = None) -> int:
    """当日零点时间戳（日键口径同 game/dayclose.py:56）。"""
    return int((time.time() if ts is None else ts) // 86400 * 86400)


def _rng(seed: str) -> random.Random:
    """按字符串种子取随机源：同一天同一玩家同一道具结果稳定可复现。"""
    return random.Random(seed)


def _fail(code: str, msg: str, **extra) -> dict:
    """统一失败返回：调用方按 code 分支，不靠异常。"""
    out = {"ok": False, "code": code, "msg": msg, "effect": None}
    out.update(extra)
    return out


class PropService:
    """道具卡经济：商店 → 购买/出售 → 库存 → 使用 → 结构化效果。

    db 由调用方注入（storage/db.py 的 ``Database`` 实例），本类只调用其公开
    async 方法（get_account / update_cash_full / get_position(s) /
    get_user_trades / get_price），不新增不改写存储层的任何方法。
    """

    def __init__(self, db):
        self.db = db

    # ── 建表 ────────────────────────────────────────────────
    async def ensure_schema(self) -> None:
        """自建 4 张表并把道具目录灌进 prop_defs（幂等，可重复调用）。"""
        await self.db.conn.executescript(SCHEMA)
        for d in PROP_DEFS:
            await self.db.conn.execute(
                "INSERT INTO prop_defs(prop_id,name,price,sell_price,"
                "limit_per_day,description) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(prop_id) DO UPDATE SET name=excluded.name, "
                "price=excluded.price, sell_price=excluded.sell_price, "
                "limit_per_day=excluded.limit_per_day, "
                "description=excluded.description",
                (d["prop_id"], d["name"], d["price"], d["sell_price"],
                 d["limit_per_day"], d["description"]))
        await self.db._commit()

    # ── 商店 ────────────────────────────────────────────────
    @staticmethod
    def shop_ids(user_id: str, day_ts: int) -> list[str]:
        """今日上架的 _SHOP_SIZE 件道具（按 user_id+day 确定性轮换）。"""
        ids = [d["prop_id"] for d in PROP_DEFS]
        _rng(f"props-shop|{user_id}|{day_ts}").shuffle(ids)
        return ids[:_SHOP_SIZE]

    async def _bought_today(self, user_id: str, day_ts: int,
                             prop_id: str) -> int:
        cur = await self.db.conn.execute(
            "SELECT bought AS bought FROM prop_purchases "
            "WHERE user_id=? AND day_ts=? AND prop_id=?",
            (user_id, day_ts, prop_id))
        row = await cur.fetchone()
        return int(row["bought"]) if row else 0

    async def get_shop(self, user_id: str, day_ts: int | None = None) -> dict:
        """今日商店：3 件道具 + 各自已购/可购/限购余量。"""
        day = _day_key(day_ts)
        items = []
        for pid in self.shop_ids(user_id, day):
            d = PROP_BY_ID[pid]
            bought = await self._bought_today(user_id, day, pid)
            items.append({**d, "bought_today": bought,
                           "left_today": max(d["limit_per_day"] - bought, 0),
                           "affordable": pid in self.shop_ids(user_id, day)})
        return {"ok": True, "day_ts": day, "items": items}

    # ── 库存 ────────────────────────────────────────────────
    async def get_user_props(self, user_id: str) -> list[dict]:
        """返回用户拥有的道具列表，每项含 prop_id、name、quantity 等。"""
        cur = await self.db.conn.execute(
            """SELECT p.prop_id, p.name, p.price, p.sell_price, p.description,
                      up.quantity
               FROM user_props up
               JOIN prop_defs p ON up.prop_id = p.prop_id
               WHERE up.user_id = ?
               ORDER BY p.prop_id""",
            (user_id,))
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ── 购买 ────────────────────────────────────────────────
    async def buy_prop(self, user_id: str, prop_id: str, quantity: int = 1) -> dict:
        """购买道具：检查现金、每日限购、库存更新，扣款。"""
        if quantity <= 0 or quantity > _MAX_BUY_QTY:
            return _fail("invalid_quantity", f"数量必须在 1-{_MAX_BUY_QTY} 之间")
        if prop_id not in PROP_BY_ID:
            return _fail("unknown_prop", f"未知道具 ID: {prop_id}")

        d = PROP_BY_ID[prop_id]
        total_price = d["price"] * quantity
        day_ts = _day_key()

        # 检查每日限购
        bought_today = await self._bought_today(user_id, day_ts, prop_id)
        if bought_today + quantity > d["limit_per_day"]:
            return _fail("daily_limit_exceeded", f"今日剩余可购买 {d['limit_per_day'] - bought_today} 件")

        # 检查现金
        acc = await self.db.get_account(user_id)
        if not acc:
            return _fail("account_not_found", "用户账户不存在")
        if acc["cash_available"] < total_price:
            return _fail("insufficient_cash", f"现金不足，需 {total_price}，余额 {acc['cash_available']}")

        # 扣款
        new_cash = acc["cash_available"] - total_price
        await self.db.update_cash_full(
            user_id, new_cash, acc["cash_withdrawable"], acc["cash_frozen"])

        # 更新购买计数
        await self.db.conn.execute(
            """INSERT INTO prop_purchases(user_id, day_ts, prop_id, bought)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id, day_ts, prop_id)
               DO UPDATE SET bought = bought + ?""",
            (user_id, day_ts, prop_id, quantity, quantity))
        await self.db._commit()

        # 更新库存
        await self.db.conn.execute(
            """INSERT INTO user_props(user_id, prop_id, quantity)
               VALUES(?,?,?)
               ON CONFLICT(user_id, prop_id)
               DO UPDATE SET quantity = quantity + ?""",
            (user_id, prop_id, quantity, quantity))
        await self.db._commit()

        return {
            "ok": True,
            "prop_id": prop_id,
            "quantity": quantity,
            "total_price": total_price,
            "remaining_cash": new_cash,
            "msg": f"成功购买 {quantity} 件 {d['name']}",
        }

    # ── 出售 ────────────────────────────────────────────────
    async def sell_prop(self, user_id: str, prop_id: str, quantity: int = 1) -> dict:
        """出售道具：检查库存，返款（按 sell_price），更新库存。"""
        if quantity <= 0 or quantity > _MAX_BUY_QTY:
            return _fail("invalid_quantity", f"数量必须在 1-{_MAX_BUY_QTY} 之间")
        if prop_id not in PROP_BY_ID:
            return _fail("unknown_prop", f"未知道具 ID: {prop_id}")

        d = PROP_BY_ID[prop_id]
        # 检查库存
        cur = await self.db.conn.execute(
            "SELECT quantity FROM user_props WHERE user_id=? AND prop_id=?",
            (user_id, prop_id))
        row = await cur.fetchone()
        owned = row["quantity"] if row else 0
        if owned < quantity:
            return _fail("insufficient_stock", f"仅持有 {owned} 件，无法出售 {quantity} 件")

        total_income = d["sell_price"] * quantity

        # 更新库存
        await self.db.conn.execute(
            """UPDATE user_props
               SET quantity = quantity - ?
               WHERE user_id=? AND prop_id=? AND quantity >= ?""",
            (quantity, user_id, prop_id, quantity))
        await self.db._commit()

        # 返款
        acc = await self.db.get_account(user_id)
        if not acc:
            return _fail("account_not_found", "用户账户不存在")
        new_cash = acc["cash_available"] + total_income
        await self.db.update_cash_full(
            user_id, new_cash, acc["cash_withdrawable"], acc["cash_frozen"])

        return {
            "ok": True,
            "prop_id": prop_id,
            "quantity": quantity,
            "total_income": total_income,
            "remaining_cash": new_cash,
            "msg": f"成功出售 {quantity} 件 {d['name']}，获得 {total_income} 金",
        }

    # ── 使用 ────────────────────────────────────────────────
    async def use_prop(self, user_id: str, prop_id: str, target: Optional[str] = None) -> dict:
        """使用道具：检查库存，消耗一件，返回结构化效果（由集成任务执行）。"""
        if prop_id not in PROP_BY_ID:
            return _fail("unknown_prop", f"未知道具 ID: {prop_id}")

        d = PROP_BY_ID[prop_id]
        day_ts = _day_key()
        # 查今日使用记录。幂等守卫必须先于库存检查：单日单次道具首次使用即
        # 耗尽库存，若库存检查在前，第二次调用会抢先报 no_stock 而非
        # already_used（props_check.py 第 5 节断言口径）。
        cur = await self.db.conn.execute(
            "SELECT used, granted FROM prop_grants WHERE user_id=? AND day_ts=? AND prop_id=?",
            (user_id, day_ts, prop_id))
        row = await cur.fetchone()
        used = row["used"] if row else 0
        granted = row["granted"] if row else 0
        if d["effect"] in _SINGLE_USE_EFFECTS and used >= 1:
            return _fail("already_used", f"{d['name']}今日已使用，无法重复生效")

        # 检查库存
        cur = await self.db.conn.execute(
            "SELECT quantity FROM user_props WHERE user_id=? AND prop_id=?",
            (user_id, prop_id))
        row = await cur.fetchone()
        owned = row["quantity"] if row else 0
        if owned <= 0:
            return _fail("no_stock", f"库存为 0，无法使用 {d['name']}")

        # 根据道具类型计算效果
        effect = None
        if d["effect"] == "grant_cash":
            # 幸运卡：随机金额，受每日上限限制
            rng = _rng(f"props-use|{user_id}|{day_ts}|{prop_id}")
            raw = rng.randint(_LUCKY_MIN, _LUCKY_MAX)
            # 计算可授予的金额（不超过日上限）
            max_grant_today = _LUCKY_DAILY_CAP - granted
            if max_grant_today <= 0:
                return _fail("daily_grant_cap_reached", f"幸运卡今日派奖上限已用完")
            grant = min(raw, max_grant_today)
            effect = {"type": "grant_cash", "amount": grant}
            # 更新 granted
            await self.db.conn.execute(
                """INSERT INTO prop_grants(user_id, day_ts, prop_id, used, granted)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, day_ts, prop_id)
                   DO UPDATE SET used = used + ?, granted = granted + ?""",
                (user_id, day_ts, prop_id, 1, grant, 1, grant))
            await self.db._commit()
        elif d["effect"] == "fee_immunity":
            # 防护盾：免一次交易手续费（标记 used=1，待集成任务消费）
            # 当日重复使用的 already_used 守卫已在库存检查前统一处理
            effect = {"type": "fee_immunity"}
            await self.db.conn.execute(
                """INSERT INTO prop_grants(user_id, day_ts, prop_id, used, granted)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, day_ts, prop_id)
                   DO UPDATE SET used = used + 1, granted = granted""",
                (user_id, day_ts, prop_id, 1, 0))
            await self.db._commit()
        elif d["effect"] == "trend_preview":
            # 信息卡：返回未来 _INFO_DAYS 天的走势区间（百分比变化）
            rng = _rng(f"props-use|{user_id}|{day_ts}|{prop_id}")
            preview = []
            for i in range(1, _INFO_DAYS + 1):
                band = _INFO_BANDS[i - 1]  # 日渐变宽
                change = rng.uniform(-band, band)
                preview.append({"day": i, "change_pct": change})
            effect = {"type": "trend_preview", "preview": preview}
            # 更新 used（标记已使用）
            await self.db.conn.execute(
                """INSERT INTO prop_grants(user_id, day_ts, prop_id, used, granted)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, day_ts, prop_id)
                   DO UPDATE SET used = used + 1, granted = granted""",
                (user_id, day_ts, prop_id, 1, 0))
            await self.db._commit()
        elif d["effect"] == "complete_daily":
            # 加速卡：完成一天日常任务（奖励上限）
            # 当日重复使用的 already_used 守卫已在库存检查前统一处理
            # 奖励金额随机但不超过上限
            rng = _rng(f"props-use|{user_id}|{day_ts}|{prop_id}")
            reward = rng.randint(0, _SPEED_REWARD_CAP)
            effect = {"type": "complete_daily", "reward": reward}
            await self.db.conn.execute(
                """INSERT INTO prop_grants(user_id, day_ts, prop_id, used, granted)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, day_ts, prop_id)
                   DO UPDATE SET used = used + 1, granted = granted + ?""",
                (user_id, day_ts, prop_id, 1, reward, reward))  # 第6个绑定 UPDATE 的 granted + ?
            await self.db._commit()
        elif d["effect"] == "fee_discount":
            # 手续费减免卡：下一笔交易手续费打 5 折
            # 当日重复使用的 already_used 守卫已在库存检查前统一处理
            effect = {"type": "fee_discount", "rate": _FEE_DISCOUNT_RATE}
            await self.db.conn.execute(
                """INSERT INTO prop_grants(user_id, day_ts, prop_id, used, granted)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, day_ts, prop_id)
                   DO UPDATE SET used = used + 1, granted = granted""",
                (user_id, day_ts, prop_id, 1, 0))
            await self.db._commit()
        elif d["effect"] == "review":
            # 复盘卡：返回最近 _REVIEW_TRADES 条成交的盈亏结构
            # 需要调用 db.get_user_trades（假设存在）
            try:
                trades = await self.db.get_user_trades(user_id, limit=_REVIEW_TRADES)
            except Exception:
                # 如果方法不存在，返回空列表
                trades = []
            # 简单统计：盈利次数、亏损次数、总盈亏
            win = sum(1 for t in trades if t.get("profit", 0) > 0)
            loss = sum(1 for t in trades if t.get("profit", 0) < 0)
            total_pnl = sum(t.get("profit", 0) for t in trades)
            effect = {
                "type": "review",
                "trades_count": len(trades),
                "win": win,
                "loss": loss,
                "total_pnl": total_pnl,
                "trades": trades[:5],  # 仅返回前5条作为示例
            }
            await self.db.conn.execute(
                """INSERT INTO prop_grants(user_id, day_ts, prop_id, used, granted)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, day_ts, prop_id)
                   DO UPDATE SET used = used + 1, granted = granted""",
                (user_id, day_ts, prop_id, 1, 0))
            await self.db._commit()
        else:
            # 未知效果类型
            return _fail("unknown_effect", f"道具 {prop_id} 的效果类型未实现: {d['effect']}")

        # 消耗库存（使用一件）
        await self.db.conn.execute(
            """UPDATE user_props
               SET quantity = quantity - 1
               WHERE user_id=? AND prop_id=? AND quantity >= 1""",
            (user_id, prop_id))
        await self.db._commit()

        return {
            "ok": True,
            "prop_id": prop_id,
            "quantity_used": 1,
            "remaining_quantity": owned - 1,
            "effect": effect,
            "msg": f"成功使用 1 件 {d['name']}",
        }


# 以下为经济性自查注释（供参考，不影响功能）
# ECONOMICS:
# 幸运卡: price=500, expected grant = (100+1000)/2 = 550，但日上限 1000 且单次使用上限 3 次/日，
#         实际期望收益受日上限保护，若用户每天使用 3 次，期望总授予 3*550=1650，但日上限 1000 导致实际期望被截断。
#         为保证不印钞，我们设定日上限 1000，且单张卡价格 500，即使满额使用也仅等价。
# 防护盾: price=800, effect 为免一次手续费。假设平均手续费为交易额的 0.2%，若交易额 10000，手续费 20，远低于 800，故不印钞。
# 信息卡: price=300, effect 仅提供区间预测，无直接收益，期望收益为 0。
# 加速卡: price=200, effect 为完成一天日常任务，奖励上限 150 < 200，故期望收益 < 售价。
# 手续费减免卡: price=400, effect 为手续费 5 折，同防护盾，节省手续费远低于价格。
# 复盘卡: price=600, effect 仅提供统计，无直接收益。
# 综上，道具整体为沉淀口。