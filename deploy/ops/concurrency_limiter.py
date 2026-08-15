"""按用户粒度的指令并发限制器（内聚实现）。

单结构实现：`user_key -> deque[monotonic 时间戳]`
- 并发数 = `len(deque)`，无需与独立计数器同步；
- 时间戳 FIFO：过期项必然在队首，清理为 O(1) 平均（`popleft`）；
- 超时自动释放：该用户下次 `acquire` 时惰性清理（`_expire_stale_locked`），
  也可由外部定时调用 `expire_all()` 全局回收；
- `per_user_max <= 0` 表示不限制（不计数）；`timeout_seconds <= 0` 表示不启用超时清理。
"""

import asyncio
import time
from collections import deque
from typing import Deque, Dict, Optional


class UserConcurrencyLimiter:
    def __init__(self, per_user_max: int = 3, timeout_seconds: int = 300):
        self._per_user_max = max(0, per_user_max)
        self._timeout_seconds = max(0, timeout_seconds)
        self._lock = asyncio.Lock()
        self._slots: Dict[str, Deque[float]] = {}

    def update_config(self, per_user_max: int, timeout_seconds: int) -> None:
        """热生效：更新并发上限与超时阈值（不回收已占槽位）。"""
        self._per_user_max = max(0, per_user_max)
        self._timeout_seconds = max(0, timeout_seconds)

    # ── 内部工具（须在持锁时调用）──────────────────────────────

    def _expire_stale_locked(self, user_key: str) -> None:
        """锁内：FIFO 清理该用户过期槽位（过期项必然在队首）。"""
        if self._timeout_seconds <= 0:
            return
        ts = self._slots.get(user_key)
        if not ts:
            return
        cutoff = time.monotonic() - self._timeout_seconds
        while ts and ts[0] < cutoff:
            ts.popleft()
        if not ts:
            self._slots.pop(user_key, None)

    # ── 对外接口 ──────────────────────────────────────────────

    async def acquire(self, user_key: Optional[str]) -> bool:
        """尝试获取一个槽位。

        user_key 为 None 或 per_user_max<=0 时视为不限制（不计数，恒成功）。
        返回 True=成功；False=该用户并发已满。
        """
        async with self._lock:
            if user_key is None or self._per_user_max <= 0:
                return True
            self._expire_stale_locked(user_key)
            ts = self._slots.get(user_key)
            if ts is None:
                self._slots[user_key] = deque([time.monotonic()])
                return True
            if len(ts) >= self._per_user_max:
                return False
            ts.append(time.monotonic())
            return True

    async def release(self, user_key: Optional[str]) -> None:
        """释放该用户一个槽位（FIFO：移除最早获取的，与过期清理语义一致）。"""
        async with self._lock:
            if user_key is None:
                return
            ts = self._slots.get(user_key)
            if ts:
                ts.popleft()
                if not ts:
                    self._slots.pop(user_key, None)

    async def clear(self) -> None:
        """清空全部槽位（每日重置兜底）。"""
        async with self._lock:
            self._slots.clear()

    async def expire_all(self) -> int:
        """清理所有用户的过期槽位，返回清理数量（供后台任务/诊断使用）。"""
        async with self._lock:
            if self._timeout_seconds <= 0:
                return 0
            cutoff = time.monotonic() - self._timeout_seconds
            removed = 0
            for key in list(self._slots.keys()):
                ts = self._slots[key]
                while ts and ts[0] < cutoff:
                    ts.popleft()
                    removed += 1
                if not ts:
                    self._slots.pop(key, None)
            return removed

    def stats(self) -> dict:
        """统计快照（不持有锁，供监控展示参考）。"""
        return {
            'per_user_max': self._per_user_max,
            'timeout_seconds': self._timeout_seconds,
            'active_users': len(self._slots),
            'active_slots': sum(len(v) for v in self._slots.values()),
        }
