# game/season.py
# 赛季段位与赛季结算模块
# 实现 .dsh-code/content_spec.md 中的规格

import json
import time
from typing import Dict, List, Optional, Tuple

# 导入现有的异步数据库接口
# 注意：我们不能修改现有文件，但可以导入使用它们
# 我们假设存储层提供一个全局的 db 实例，或者我们可以创建一个实例
# 然而，现有的 storage/db.py 是一个类，需要实例化
# 我们将在服务中通过依赖注入接收 db 实例
# 为了与现有代码保持兼容，我们采用依赖注入的方式
# 但为了使模块能够独立工作，我们提供一个可以被注入的类

class SeasonService:
    def __init__(self, db, reward_callback=None):
        """
        :param db: 存储层实例，需提供异步方法 get_account, get_position 等
        :param reward_callback: 可选的奖励发放回调函数，签名为 callback(user_id: str, reward: dict) -> None
        """
        self.db = db
        self.reward_callback = reward_callback

    async def ensure_schema(self):
        """确保所需的数据表存在。应在服务启动时调用。"""
        # 创建 user_season 表
        await self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_season (
                user_id TEXT NOT NULL,
                season_id TEXT NOT NULL,
                rank INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL DEFAULT 0,
                last_update_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, season_id)
            )
        """)
        # 创建 season_defs 表（如果不存在）
        await self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS season_defs (
                season_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                rank_thresholds TEXT NOT NULL,
                rewards TEXT NOT NULL
            )
        """)
        # 创建 user_season_daily 表以跟踪每日积分（用于每日上限）
        await self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_season_daily (
                user_id TEXT NOT NULL,
                season_id TEXT NOT NULL,
                day_ts INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, season_id, day_ts)
            )
        """)
        # 创建 user_season_reward_claims 表以跟踪奖励领取状态（幂等性）
        await self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_season_reward_claims (
                user_id TEXT NOT NULL,
                season_id TEXT NOT NULL,
                claimed_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, season_id)
            )
        """)
        await self.db._commit()

    async def get_season_info(self, user_id: str) -> Dict:
        """获取用户当前赛季的信息"""
        now = int(time.time())
        # 查找当前有效的赛季（现在时间在 start_ts 和 end_ts 之间）
        cursor = await self.db.conn.execute(
            """
            SELECT season_id, name, start_ts, end_ts, rank_thresholds, rewards
            FROM season_defs
            WHERE start_ts <= ? AND end_ts > ?
            ORDER BY start_ts DESC LIMIT 1
            """,
            (now, now)
        )
        row = await cursor.fetchone()
        if not row:
            # 如果没有当前赛季，返回默认值
            return {
                "season_id": None,
                "season_name": None,
                "rank": 0,
                "points": 0,
                "next_threshold": None,
                "season_start": None,
                "season_end": None
            }
        season_id = row["season_id"]
        name = row["name"]
        start_ts = row["start_ts"]
        end_ts = row["end_ts"]
        rank_thresholds = json.loads(row["rank_thresholds"])
        rewards = json.loads(row["rewards"])

        # 获取用户在此赛季的记录
        cursor = await self.db.conn.execute(
            """
            SELECT rank, points, last_update_ts
            FROM user_season
            WHERE user_id = ? AND season_id = ?
            """,
            (user_id, season_id)
        )
        user_row = await cursor.fetchone()
        if not user_row:
            # 如果用户没有记录，创建默认记录
            await self.db.conn.execute(
                """
                INSERT INTO user_season (user_id, season_id, rank, points, last_update_ts)
                VALUES (?, ?, 0, 0, 0)
                """,
                (user_id, season_id)
            )
            await self.db._commit()
            rank = 0
            points = 0
            last_update_ts = 0
        else:
            rank = user_row["rank"]
            points = user_row["points"]
            last_update_ts = user_row["last_update_ts"]

        # 根据积分计算当前应该的段位（以防有漏更新）
        calculated_rank = 0
        for threshold in sorted(rank_thresholds, reverse=True):
            if points >= threshold:
                calculated_rank = threshold
                break
        # 如果数据库中的 rank 与计算的不一致，我们返回计算的 rank
        rank = calculated_rank

        # 确定下一个段位的门槛
        next_threshold = None
        for threshold in sorted(rank_thresholds):
            if points < threshold:
                next_threshold = threshold
                break

        return {
            "season_id": season_id,
            "season_name": name,
            "rank": rank,
            "points": points,
            "next_threshold": next_threshold,
            "season_start": start_ts,
            "season_end": end_ts,
            "rank_thresholds": rank_thresholds,
            "rewards": rewards
        }

    async def update_season_points(self, user_id: str, delta: int, reason: str) -> Dict:
        """更新用户的赛季积分，考虑每日上限"""
        now = int(time.time())
        # 获取当前赛季
        season_info = await self.get_season_info(user_id)
        if not season_info["season_id"]:
            # 没有当前赛季，返回错误
            return {"success": False, "error": "No active season"}

        season_id = season_info["season_id"]
        # 计算今天的日期戳（零点）
        today_midnight = now - (now % 86400)  # 假设一天86400秒，UTC零点

        # 获取用户今天已经获得的积分
        cursor = await self.db.conn.execute(
            """
            SELECT points FROM user_season_daily
            WHERE user_id = ? AND season_id = ? AND day_ts = ?
            """,
            (user_id, season_id, today_midnight)
        )
        row = await cursor.fetchone()
        today_points = row["points"] if row else 0

        # 应用每日上限（50）
        max_daily = 50
        if delta >= 0:
            # 只有正向积分才受每日上限限制
            if today_points >= max_daily:
                # 已经达到或超过上限，不能获得更多积分
                actual_delta = 0
            else:
                # 计算可以获得的积分
                available = max_daily - today_points
                actual_delta = min(delta, available)
        else:
            # 负向积分（亏损）不受每日上限限制
            actual_delta = delta

        # 更新用户赛季总积分
        new_points = season_info["points"] + actual_delta
        # 根据新积分计算段位
        new_rank = 0
        for threshold in sorted(season_info["rank_thresholds"], reverse=True):
            if new_points >= threshold:
                new_rank = threshold
                break
        await self.db.conn.execute(
            """
            UPDATE user_season
            SET points = ?, rank = ?, last_update_ts = ?
            WHERE user_id = ? AND season_id = ?
            """,
            (new_points, new_rank, now, user_id, season_id)
        )

        # 更新用户每日积分
        await self.db.conn.execute(
            """
            INSERT INTO user_season_daily (user_id, season_id, day_ts, points)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, season_id, day_ts) DO UPDATE SET points = points + ?
            """,
            (user_id, season_id, today_midnight, today_points + actual_delta, actual_delta)
        )

        await self.db._commit()

        # 重新获取更新后的信息以返回
        updated_info = await self.get_season_info(user_id)
        return {
            "success": True,
            "points": updated_info["points"],
            "rank": updated_info["rank"],
            "delta_actual": actual_delta,
            "reason": reason
        }

    async def get_season_reward(self, user_id: str) -> Dict:
        """获取用户当前赛季的奖励（如果尚未领取）"""
        season_info = await self.get_season_info(user_id)
        if not season_info["season_id"]:
            return {"success": False, "error": "No active season"}

        if await self._is_reward_claimed(user_id, season_info["season_id"]):
            return {"success": False, "error": "Reward already claimed for this season"}

        season_id = season_info["season_id"]
        # 获取赛季定义中的奖励
        cursor = await self.db.conn.execute(
            """
            SELECT rewards FROM season_defs WHERE season_id = ?
            """,
            (season_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"success": False, "error": "Season definition not found"}
        rewards_json = row["rewards"]
        rewards = json.loads(rewards_json)

        # 根据用户的段位获取奖励
        rank = season_info["rank"]
        # 假设 rewards 是一个 JSON 对象，键是段位数值（字符串），值是奖励描述
        reward = rewards.get(str(rank))
        if reward is None:
            # 尝试整数键
            reward = rewards.get(rank)
        if not reward:
            return {"success": False, "error": "No reward defined for this rank"}

        # 如果有回调，则通过回调发放奖励
        if self.reward_callback:
            try:
                self.reward_callback(user_id, reward)
            except Exception as e:
                return {"success": False, "error": f"Reward callback failed: {e}"}

        # 标记奖励已领取
        await self.db.conn.execute(
            """
            INSERT INTO user_season_reward_claims (user_id, season_id, claimed_at)
            VALUES (?, ?, ?)
            """,
            (user_id, season_id, int(time.time()))
        )
        await self.db._commit()

        return {
            "success": True,
            "reward": reward,
            "rank": rank
        }

    async def _is_reward_claimed(self, user_id: str, season_id: str) -> bool:
        """检查用户在指定赛季的奖励是否已经领取"""
        cursor = await self.db.conn.execute(
            """
            SELECT 1 FROM user_season_reward_claims
            WHERE user_id = ? AND season_id = ?
            """,
            (user_id, season_id)
        )
        row = await cursor.fetchone()
        return row is not None

    async def reset_season(self):
        """重置赛季：结算未领取的奖励，并准备进入新赛季"""
        now = int(time.time())
        # 找到最近的已结束赛季（end_ts <= now）
        cursor = await self.db.conn.execute(
            """
            SELECT season_id, name, start_ts, end_ts, rank_thresholds, rewards
            FROM season_defs
            WHERE end_ts <= ?
            ORDER BY end_ts DESC LIMIT 1
            """,
            (now,)
        )
        row = await cursor.fetchone()
        if not row:
            # 没有已结束的赛季
            return
        ended_season_id = row["season_id"]
        rank_thresholds = json.loads(row["rank_thresholds"])
        rewards = json.loads(row["rewards"])

        # 获取所有在此已结束赛季中的用户
        cursor = await self.db.conn.execute(
            """
            SELECT user_id FROM user_season
            WHERE season_id = ?
            """,
            (ended_season_id,)
        )
        rows = await cursor.fetchall()
        for (user_id,) in rows:
            # 检查用户在已结束赛季中的奖励是否已经领取
            if not await self._is_reward_claimed(user_id, ended_season_id):
                # 获取用户在此赛季的记录以得到积分和段位
                cursor = await self.db.conn.execute(
                    """
                    SELECT points FROM user_season
                    WHERE user_id = ? AND season_id = ?
                    """,
                    (user_id, ended_season_id)
                )
                user_row = await cursor.fetchone()
                if user_row:
                    points = user_row["points"]
                    # 根据积分计算段位
                    calculated_rank = 0
                    for threshold in sorted(rank_thresholds, reverse=True):
                        if points >= threshold:
                            calculated_rank = threshold
                            break
                    # 根据段位获取奖励
                    reward = rewards.get(str(calculated_rank))
                    if reward is None:
                        reward = rewards.get(calculated_rank)
                    if reward:
                        # 如果有回调，则通过回调发放奖励
                        if self.reward_callback:
                            try:
                                self.reward_callback(user_id, reward)
                            except Exception as e:
                                # 记录错误但继续
                                pass
                        # 标记奖励已领取（在已结束赛季中）
                        await self.db.conn.execute(
                            """
                            INSERT INTO user_season_reward_claims (user_id, season_id, claimed_at)
                            VALUES (?, ?, ?)
                            """,
                            (user_id, ended_season_id, int(time.time()))
                        )
        await self.db._commit()

        # 注意：我们不清除历史数据，因为上一赛季成绩可查
        # 新赛季将通过 season_defs 表中的新记录来定义
        # 这里我们不自动创建新赛季，假设新赛季的定义会由外部（如管理员）插入
        # 我们仅确保在需要时能够获取到当前赛季
        # 什么都不做，因为 get_season_info 会根据时间自动选择当前赛季
        pass

    async def _get_current_season_id(self) -> Optional[str]:
        """获取当前赛季ID（如果有）"""
        now = int(time.time())
        cursor = await self.db.conn.execute(
            """
            SELECT season_id FROM season_defs
            WHERE start_ts <= ? AND end_ts > ?
            ORDER BY start_ts DESC LIMIT 1
            """,
            (now, now)
        )
        row = await cursor.fetchone()
        return row["season_id"] if row else None