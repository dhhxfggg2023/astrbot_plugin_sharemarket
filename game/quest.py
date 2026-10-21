"""每日签到与日常任务模块。

实现 .dsh-code/content_spec.md 中的规格：
- 单日单次签到、连击递增奖励、断签重置、同日重复签到被拒
- 日常任务（完成 N 笔交易、持仓 N 只、使用道具、查看榜单等）
- 模块自建表 ensure_schema(db) + 供集成任务调用的公开 API（enable / record / status / claim / settle_day）
- 奖励额度小、可预期、每天可领一次

模块边界（.dsh-code/content_spec.md:151-154 的三条范围铁律）：
- **不改 storage/db.py / main.py / config.py**：持久化全部走本模块 ``ensure_schema()`` 自建表
- **不在本模块改账户/持仓表的「效果」**：签到奖励由集成任务通过注入回调执行，本模块只记录状态
- 不 import main.py / astrbot，可离线跑（.dsh-code/quest_check.py）。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

# 签到奖励基数（content_spec.md:17 规定基础奖励 100、第 n 天 100+(n-1)*50，
# 第 11 天起固定 800）。此前 handle_checkin 引用未定义的 CHECKIN_BASE_REWARD 会
# NameError；模块常量在此集中声明。
CHECKIN_BASE_REWARD = 100


class QuestService:
    """每日签到与日常任务服务。

    db 由调用方注入（storage/db.py 的 ``Database`` 实例），本类只调用其公开
    async 方法（get_account / get_position / upsert_position / _commit 等），
    不新增不改写存储层的任何方法。
    """

    def __init__(self, db):
        self.db = db

    # ── 建表 ──────────────────────────────────────────────
    async def ensure_schema(self) -> None:
        """确保所需的数据表存在。应在服务启动时调用。"""
        await self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_quests (
                user_id TEXT PRIMARY KEY,
                checkin_streak INTEGER NOT NULL DEFAULT 0,
                last_checkin_ts INTEGER NOT NULL DEFAULT 0,
                daily_task_progress TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await self.db._commit()

    # ── 公开 API ──────────────────────────────────────────

    async def enable(self, user_id: str) -> dict:
        """为用户启用 quest 记录（如果尚未记录）。"""
        cursor = await self.db.conn.execute(
            "SELECT * FROM user_quests WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            await self.db.conn.execute(
                """
                INSERT INTO user_quests (user_id, checkin_streak, last_checkin_ts, daily_task_progress)
                VALUES (?, 0, 0, '{}')
                """,
                (user_id,),
            )
            await self.db._commit()
        return {"ok": True, "msg": "quest enabled"}

    async def status(self, user_id: str) -> dict:
        """返回用户当前的签到与任务状态。"""
        cursor = await self.db.conn.execute(
            "SELECT * FROM user_quests WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"ok": False, "error": "quest not enabled, call enable first"}
        return {
            "ok": True,
            "checkin_streak": row["checkin_streak"],
            "last_checkin_ts": row["last_checkin_ts"],
            "daily_task_progress": row["daily_task_progress"],
        }

    async def handle_checkin(self, user_id: str) -> dict:
        """执行每日签到。

        规则：
        - 单日单次签到：如果今日已签到，返回“已签过过”
        - 连击递增奖励： streak 次连续签到，奖励 = CHECKIN_STREAK_REWARDS[min(streak-1, 14)]
        - 断签重置：如果间隔超过当日零点，连击重置为 0
        - 返回奖励金额
        """
        await self.enable(user_id)

        status = await self.status(user_id)
        streak = status["checkin_streak"]
        last_ts = status["last_checkin_ts"]

        now = int(time.time())
        today_midnight = now - (now % 86400)

        # 检查是否已在今日签到
        if last_ts >= today_midnight:
            reward = 0
            reason = "already_checkined_today"
            return {
                "ok": False,
                "error": f"今日已签到（连击{streak}），请明天再来",
                "reward": reward,
                "reason": reason,
            }

        # 计算连击奖励 - 断签重置逻辑
        # 断签判定：上次签到早于「昨日零点」即视为断签（中间至少空了一整天）。
        # 若只是「今天 vs 昨天」则连击递增，不重置。
        yesterday_midnight = today_midnight - 86400
        if last_ts > 0 and last_ts < yesterday_midnight:
            # 上次签到距今超过 1 天，断签重置
            streak = 0

        new_streak = streak + 1
        # 奖励：第1天100，第2天150，...，第11天后固定800
        if new_streak <= 10:
            reward = CHECKIN_BASE_REWARD + (new_streak - 1) * 50
        else:
            reward = 800  # 第11天后固定800

        # 更新数据库
        await self.db.conn.execute(
            """
            UPDATE user_quests 
            SET checkin_streak = ?, last_checkin_ts = ?
            WHERE user_id = ?
            """,
            (new_streak, now, user_id),
        )
        await self.db._commit()

        return {
            "ok": True,
            "reward": reward,
            "new_streak": new_streak,
            "reason": "success",
            "msg": f"签到成功，获得 {reward} 金币，当前连击{new_streak}",
        }

    async def get_daily_tasks(self, user_id: str) -> List[dict]:
        """返回可用的日常任务列表（至少4个）。"""
        tasks = [
            {
                "task_id": "trade_count",
                "title": "完成 N 笔交易",
                "description": "在一天内完成 N 笔买入或卖出成交",
                "target": 3,
                "unit": "笔",
                "progress": 0,
                "completed": False,
                "reward": 50,
            },
            {
                "task_id": "position_count",
                "title": "持仓 N 只",
                "description": "同时持有 N 只不同股票",
                "target": 2,
                "unit": "只",
                "progress": 0,
                "completed": False,
                "reward": 100,
            },
            {
                "task_id": "use_prop",
                "title": "使用道具",
                "description": "使用一个道具卡",
                "target": 1,
                "unit": "次",
                "progress": 0,
                "completed": False,
                "reward": 50,
            },
            {
                "task_id": "view_leaderboard",
                "title": "查看榜单",
                "description": "查看龙虎榜或资排行",
                "target": 1,
                "unit": "次",
                "progress": 0,
                "reward": 50,
            },
        ]
        return tasks

    async def submit_task(self, user_id: str, task_id: str, proof: dict) -> dict:
        """提交任务进度。

        规则：
        - 进度累加，达标后可领取奖励
        - 每个任务只能领取一次
        - 跨日后进度重置
        """
        await self.enable(user_id)

        status = await self.status(user_id)
        progress = status["daily_task_progress"]
        progress_data = json.loads(progress) if isinstance(progress, str) else progress

        tasks = await self.get_daily_tasks(user_id)
        task_map = {t["task_id"]: t for t in tasks}

        if task_id not in task_map:
            return {"ok": False, "error": f"未知任务: {task_id}"}

        task = task_map[task_id]

        # 检查是否已完成且已领取
        if task.get("completed") and task.get("claimed"):
            return {"ok": False, "error": f"任务已完成并领取 over: {task_id}"}

        # 累加进度
        current_progress = progress_data.get(task_id, 0)

        # 根据任务类型统计进度
        if task_id == "trade_count":
            trade_count = proof.get("trade_count", 0) if proof else 0
            new_progress = current_progress + trade_count
        elif task_id == "position_count":
            pos_count = proof.get("position_count", 0) if proof else 0
            new_progress = current_progress + pos_count
        elif task_id == "use_prop":
            use_count = proof.get("use_count", 0) if proof else 0
            new_progress = current_progress + use_count
        elif task_id == "view_leaderboard":
            new_progress = current_progress + 1
        else:
            new_progress = current_progress + 1

        # 更新进度
        progress_data[task_id] = new_progress

        # 检查是否达标
        completed = new_progress >= task["target"]
        claimed = progress_data.get(f"{task_id}_claimed", False)

        # 无论是否达标都先持久化进度（达标时还会写入 _claimed 标记），
        # 否则 claim_task_reward 永远读不到已完成状态。
        if completed and not claimed:
            progress_data[f"{task_id}_claimed"] = True
        progress_json = json.dumps(progress_data)
        await self.db.conn.execute(
            """
            UPDATE user_quests SET daily_task_progress = ? WHERE user_id = ?
            """,
            (progress_json, user_id),
        )
        await self.db._commit()

        if completed and not claimed:
            reward = task["reward"]
            return {
                "ok": True,
                "progress": new_progress,
                "target": task["target"],
                "completed": True,
                "claimed": True,
                "reward": reward,
                "reason": "task_reached_and_claimed",
                "msg": f"任务达标，获得 {reward} 金币奖励",
            }
        elif completed and claimed:
            return {
                "ok": True,
                "progress": new_progress,
                "target": task["target"],
                "completed": True,
                "claimed": True,
                "reward": 0,
                "reason": "already_claimed",
                "msg": f"任务已完成并已领取过奖励",
            }
        else:
            return {
                "ok": True,
                "progress": new_progress,
                "target": task["target"],
                "completed": False,
                "claimed": False,
                "reward": 0,
                "reason": "progress_updated",
                "msg": f"进度已累计：{new_progress}/{task['target']}",
            }

    async def claim_task_reward(self, user_id: str, task_id: str) -> dict:
        """领取任务奖励。

        返回：奖励已标记为已领入账。
        """
        await self.enable(user_id)

        status = await self.status(user_id)
        progress = status["daily_task_progress"]
        progress_data = json.loads(progress) if isinstance(progress, str) else progress

        if task_id not in progress_data:
            return {"ok": False, "error": f"任务不存在: {task_id}"}

        # 标记为已领取
        progress_data[f"{task_id}_claimed"] = True

        reward = progress_data.get(f"{task_id}_reward", 0)

        progress_json = json.dumps(progress_data)
        await self.db.conn.execute(
            """
            UPDATE user_quests SET daily_task_progress = ? WHERE user_id = ?
            """,
            (progress_json, user_id),
        )
        await self.db._commit()

        return {
            "ok": True,
            "reward": reward,
            "claimed": True,
            "reason": "claim_success",
            "msg": f"奖励 {reward} 金币已入账",
        }

    async def settle_day(self, user_id: str) -> dict:
        """日结算：跨日后重置任务进度（未领取的进度清零），保留已领取标记。

        在每日零点由主线程/定时任务调用。
        """
        now = int(time.time())
        today_midnight = now - (now % 86400)

        cursor = await self.db.conn.execute(
            "SELECT * FROM user_quests WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"ok": False, "error": "quest not enabled"}

        last_settlement = row["last_checkin_ts"]

        if last_settlement >= today_midnight:
            return {"ok": True, "already_settled": True, "msg": "今日已结算"}

        # 跨日结算：重置未领取的任务进度，保留已领取标记
        progress_data = json.loads(row["daily_task_progress"])

        # 保留claimed标记，清除进度
        new_progress_data = {}
        for k, v in progress_data.items():
            if k.endswith("_claimed"):
                new_progress_data[k] = v

        # 重置连击
        await self.db.conn.execute(
            """
            UPDATE user_quests 
            SET checkin_streak = 0, last_checkin_ts = ?, daily_task_progress = ?
            WHERE user_id = ?
            """,
            (today_midnight, json.dumps(new_progress_data), user_id),
        )
        await self.db._commit()

        return {
            "ok": True,
            "reset": True,
            "msg": "跨日重置完成：任务进度清零，连击重置",
        }

    async def get_user_quest_info(self, user_id: str) -> dict:
        """返回用户的 quest 完整信息（供前端/UI展示）。"""
        status = await self.status(user_id)
        tasks = await self.get_daily_tasks(user_id)
        return {
            "ok": True,
            "quest_info": status,
            "daily_tasks": tasks,
        }