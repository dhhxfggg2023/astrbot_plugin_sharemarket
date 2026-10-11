"""全局交易锁（内存态）。

一个 QQ 群游戏的撮合吞吐极低，用**单把全局锁**把所有会改动账本/订单簿的操作
（下单、撤单、日终清算）串行化，比按用户加锁简单且没有交叉锁风险。
锁延迟创建，避免在事件循环之外绑定。
"""

from __future__ import annotations

import asyncio

_lock: asyncio.Lock | None = None


def trade_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock
