"""全局指令并发优先级队列（真实队列）。

替代原按用户粒度的 UserConcurrencyLimiter：
- 全局容量 `capacity`：同时最多 N 条指令在跑（系统级并发上限，保护后端）；
- 等待中的请求按「特权优先、同优先级 FIFO」排序出队；
- 特权（白名单群/人/AstrBot 管理员）插队到队首，普通用户排队队尾；
- 特权仅受全局容量约束（不占 per_user 上限），普通用户额外受 per_user_max 约束；
- 等待超过 `queue_wait_timeout` 秒视为拒绝（SlotRejected）。

特权判定由调用方（main.py `_is_privileged`）传入，本类只负责排队与准入。
"""

import asyncio
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class SlotRejected(Exception):
    """指令被拒绝：个人并发已满（per_user_full）或排队等待超时（queue_timeout）。"""


_Waiter = Tuple[asyncio.Future, Optional[str]]


class GlobalPriorityCommandQueue:
    def __init__(
        self,
        capacity: int = 8,
        per_user_max: int = 3,
        timeout_seconds: int = 300,
        queue_wait_timeout: int = 600,
    ):
        self._capacity = max(1, capacity)
        self._per_user_max = max(0, per_user_max)
        # 运行槽位超时自动释放（兜底，正常由 try/finally release）
        self._timeout_seconds = max(0, timeout_seconds)
        # 排队等待上限（防永久挂起）
        self._queue_wait_timeout = max(1, queue_wait_timeout)
        self._lock = asyncio.Lock()
        self._running = 0
        self._per_user: Dict[str, int] = {}
        # 等待队列：特权队首、普通队尾；每条记录 (future, user_key)
        self._priv_wait: Deque[_Waiter] = deque()
        self._norm_wait: Deque[_Waiter] = deque()
        # 运行槽位起始时间（monotonic），按 user_key 记录（同用户多槽仅记最近一次，兜底用）
        self._running_since: Dict[str, float] = {}
        self._priv_jumped_total = 0

    # ── 配置热生效 ────────────────────────────────────────────

    def update_config(
        self,
        per_user_max: int,
        timeout_seconds: int = 0,
        *,
        capacity: int = 0,
        queue_wait_timeout: int = 0,
    ) -> None:
        """热生效：更新并发上限与超时阈值（不回收已占槽位）。"""
        if capacity:
            self._capacity = max(1, capacity)
        if per_user_max is not None:
            self._per_user_max = max(0, per_user_max)
        if timeout_seconds:
            self._timeout_seconds = max(0, timeout_seconds)
        if queue_wait_timeout:
            self._queue_wait_timeout = max(1, queue_wait_timeout)

    # ── 内部工具（须在持锁时调用）──────────────────────────────

    def _bump_per_user(self, user_key: Optional[str]) -> None:
        if user_key is None:
            return
        self._per_user[user_key] = self._per_user.get(user_key, 0) + 1

    def _key(self, user_key: Optional[str]) -> str:
        return user_key if user_key is not None else "*"

    def _remove_waiter(self, fut: asyncio.Future) -> None:
        for dq in (self._priv_wait, self._norm_wait):
            for i, (f, _) in enumerate(dq):
                if f is fut:
                    del dq[i]
                    return

    def _admit_next_locked(self) -> None:
        """锁内：从等待队列补位，特权队优先；普通补位仍受 per_user 约束。"""
        while self._running < self._capacity:
            # 1) 优先特权队
            if self._priv_wait:
                fut, user_key = self._priv_wait[0]
                if fut.done():  # 已取消/超时移除
                    self._priv_wait.popleft()
                    continue
                self._priv_wait.popleft()
                self._running += 1
                self._bump_per_user(user_key)
                self._running_since[self._key(user_key)] = time.monotonic()
                self._priv_jumped_total += 1
                if not fut.done():
                    fut.set_result(True)
                continue
            # 2) 其次普通队
            if self._norm_wait:
                fut, user_key = self._norm_wait[0]
                if fut.done():
                    self._norm_wait.popleft()
                    continue
                if self._per_user.get(user_key or "", 0) >= self._per_user_max:
                    # 该普通用户个人已达上限：放到队尾让他人先走；仅剩自己则保留队首等待自身释放
                    if len(self._norm_wait) > 1:
                        self._norm_wait.popleft()
                        self._norm_wait.append((fut, user_key))
                        continue
                    break
                self._norm_wait.popleft()
                self._running += 1
                self._bump_per_user(user_key)
                self._running_since[self._key(user_key)] = time.monotonic()
                if not fut.done():
                    fut.set_result(True)
                continue
            break

    # ── 对外接口 ──────────────────────────────────────────────

    async def acquire(self, user_key: Optional[str], *, privileged: bool) -> None:
        """获取一个全局执行槽位。成功返回；被拒绝抛 SlotRejected。

        特权：仅受全局容量约束，满则排队并插队到队首；
        普通：同时受全局容量 + per_user_max 约束，个人已满立即拒绝，否则排队队尾。
        """
        async with self._lock:
            if privileged or user_key is None:
                if self._running < self._capacity:
                    self._running += 1
                    self._bump_per_user(user_key)
                    self._running_since[self._key(user_key)] = time.monotonic()
                    return
                fut = asyncio.get_running_loop().create_future()
                self._priv_wait.append((fut, user_key))
            else:
                if self._per_user.get(user_key, 0) >= self._per_user_max:
                    raise SlotRejected("per_user_full")
                if self._running < self._capacity:
                    self._running += 1
                    self._bump_per_user(user_key)
                    self._running_since[self._key(user_key)] = time.monotonic()
                    return
                fut = asyncio.get_running_loop().create_future()
                self._norm_wait.append((fut, user_key))
        # 锁外 await，避免阻塞其他协程
        try:
            await asyncio.wait_for(fut, self._queue_wait_timeout)
        except asyncio.TimeoutError:
            self._remove_waiter(fut)
            raise SlotRejected("queue_timeout")
        return

    async def release(self, user_key: Optional[str]) -> None:
        """释放该用户一个全局槽位，并尝试补位。"""
        async with self._lock:
            if user_key is not None:
                self._per_user[user_key] = max(0, self._per_user.get(user_key, 0) - 1)
                if self._per_user[user_key] == 0:
                    self._per_user.pop(user_key, None)
            self._running = max(0, self._running - 1)
            self._running_since.pop(self._key(user_key), None)
            self._admit_next_locked()

    async def clear(self) -> None:
        """清空全部槽位与等待者（每日重置兜底）。"""
        async with self._lock:
            for dq in (self._priv_wait, self._norm_wait):
                for fut, _ in dq:
                    if not fut.done():
                        fut.set_exception(SlotRejected("cleared"))
            self._priv_wait.clear()
            self._norm_wait.clear()
            self._running = 0
            self._per_user.clear()
            self._running_since.clear()

    async def expire_all(self) -> int:
        """超时强制释放运行中的槽位（防卡死泄漏），返回释放数量。"""
        if self._timeout_seconds <= 0:
            return 0
        async with self._lock:
            cutoff = time.monotonic() - self._timeout_seconds
            removed = 0
            for key in list(self._running_since.keys()):
                if self._running_since.get(key, 0) < cutoff:
                    self._running = max(0, self._running - 1)
                    if key != "*" and self._per_user.get(key, 0) > 0:
                        self._per_user[key] -= 1
                        if self._per_user[key] == 0:
                            self._per_user.pop(key, None)
                    self._running_since.pop(key, None)
                    removed += 1
            self._admit_next_locked()
            return removed

    def stats(self) -> dict:
        """统计快照（不持有锁，供监控展示参考）。"""
        return {
            "capacity": self._capacity,
            "per_user_max": self._per_user_max,
            "global_running": self._running,
            "waiting": len(self._priv_wait) + len(self._norm_wait),
            "privileged_waiting": len(self._priv_wait),
            "normal_waiting": len(self._norm_wait),
            "active_users": len(self._per_user),
            "privileged_jumped_total": self._priv_jumped_total,
        }
