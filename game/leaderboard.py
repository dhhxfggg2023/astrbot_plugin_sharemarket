"""排行榜（P0）：收益率为主、总资产为辅（D8）；破产玩家墓碑展示。"""

from __future__ import annotations

from ..storage.db import Database


class LeaderboardService:
    def __init__(self, db: Database):
        self.db = db

    async def top(self, price_of, limit: int = 10) -> list[dict]:
        rows = await self.db.list_accounts()
        entries = []
        for acc in rows:
            positions = await self.db.get_positions(acc["user_id"])
            mv = 0.0
            for pos in positions:
                p = price_of(pos["code"])
                if p is not None:
                    mv += p * pos["quantity"]
            total = acc["cash_available"] + mv
            initial = acc["initial_cash"] or 1.0
            entries.append({
                "user_id": acc["user_id"],
                "name": acc["user_name"],
                "total": total,
                "return_pct": (total - initial) / initial * 100,
                "status": acc["status"],
                "rebirth": acc["rebirth_count"] or 0,
            })
        actives = [e for e in entries if e["status"] == "active"]
        bankrupted = [e for e in entries if e["status"] == "bankrupt"]
        actives.sort(key=lambda e: (-e["return_pct"], -e["total"]))
        bankrupted.sort(key=lambda e: -e["total"])
        return (actives + bankrupted)[:limit]

    def render(self, entries: list[dict]) -> str:
        if not entries:
            return "还没有玩家开户，发送 /炒股 开户 成为第一位股民！"
        lines = ["🏆 股神排行榜（按收益率）"]
        medals = ["🥇", "🥈", "🥉"]
        for i, e in enumerate(entries):
            rank = medals[i] if i < 3 else f"{i + 1}."
            rb = e.get("rebirth", 0)
            rb_tag = f"　🔄重生{rb}次" if rb else ""
            if e["status"] == "bankrupt":
                lines.append(f"{rank} 💀 {e['name']} —— 已破产{rb_tag}")
            else:
                sign = "+" if e["return_pct"] >= 0 else ""
                lines.append(
                    f"{rank} {e['name']}  {sign}{e['return_pct']:.2f}%  "
                    f"总资产 {e['total']:,.0f}{rb_tag}")
        return "\n".join(lines)
