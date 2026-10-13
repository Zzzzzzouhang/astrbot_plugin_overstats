"""全局指令并发优先级队列（真实队列）。

替代原按用户粒度的 UserConcurrencyLimiter：
- 全局容量 `capacity`：同时最多 N 条指令在跑（系统级并发上限，保护后端）；
- 等待中的请求按「特权优先、同优先级 FIFO」排序出队；
- 特权（白名单群/人/AstrBot 管理员）插队到队首，普通用户排队队尾；
- 特权仅受全局容量约束（不占 per_user 上限），普通用户额外受 per_user_max 约束；
- 等待超过 `queue_wait_timeout` 秒视为拒绝（SlotRejected）；
- 拥堵降权（`heavy_deprio_threshold`）：当等待中指令数 > 阈值时，重指令
  （如 是区吗/开庭，调用方传 heavy=True）被降权，优先放行轻指令，避免重指令
  在拥堵时挤占全局槽位、拖垮所有用户。

特权判定由调用方（main.py `_is_privileged`）传入，重指令判定由调用方
（main.py `_run_with_cmd_slot` 的 heavy 参数）传入，本类只负责排队与准入。
"""

import asyncio
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class SlotRejected(Exception):
    """指令被拒绝：个人并发已满（per_user_full）或排队等待超时（queue_timeout）。"""


# (future, user_key, heavy) —— heavy=True 表示重指令（是区吗/开庭等），拥堵时降权
_Waiter = Tuple[asyncio.Future, Optional[str], bool]


class GlobalPriorityCommandQueue:
    def __init__(
        self,
        capacity: int = 5,
        per_user_max: int = 3,
        timeout_seconds: int = 300,
        queue_wait_timeout: int = 600,
        heavy_deprio_threshold: int = 5,
    ):
        self._capacity = max(1, capacity)
        self._per_user_max = max(0, per_user_max)
        # 运行槽位超时自动释放（兜底，正常由 try/finally release）
        self._timeout_seconds = max(0, timeout_seconds)
        # 排队等待上限（防永久挂起）
        self._queue_wait_timeout = max(1, queue_wait_timeout)
        # 拥堵降权阈值：等待指令数 > 此值时，重指令降权（优先放行轻指令）
        self._heavy_deprio_threshold = max(0, heavy_deprio_threshold)
        self._lock = asyncio.Lock()
        self._running = 0
        self._per_user: Dict[str, int] = {}
        # 等待队列：特权队首、普通队尾；每条记录 (future, user_key, heavy)
        self._priv_wait: Deque[_Waiter] = deque()
        self._norm_wait: Deque[_Waiter] = deque()
        # 运行槽位起始时间（monotonic），按 user_key 记录（同用户多槽仅记最近一次，兜底用）
        self._running_since: Dict[str, float] = {}
        # 已被 expire_all() 提前回收、但对应指令仍在执行的槽位数（按 user_key）：
        # 这些指令后续 release 时只抵扣、不再递减计数，避免重复递减使并发计数永久偏低。
        self._reclaimed: Dict[str, int] = {}
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
            for i, (f, _, _) in enumerate(dq):
                if f is fut:
                    del dq[i]
                    return

    def _norm_has_admittable(self) -> bool:
        """锁内：普通队列中是否存在（未取消且个人未达上限的）可放行者。

        用于避免「全员个人已达上限」时无限轮转；此时应直接放行队首等待自身释放。
        """
        for f, user_key, _ in self._norm_wait:
            if f.done():
                continue
            if self._per_user.get(user_key or "", 0) < self._per_user_max:
                return True
        return False

    def _admit_next_locked(self) -> None:
        """锁内：从等待队列补位。

        - 非拥堵（waiting <= 阈值）：特权优先、普通 FIFO（原行为）；
        - 拥堵（waiting > 阈值）：特权优先于普通，同特权层级内重指令（heavy=True）降权、
          轻指令优先：轻特权 > 重特权 > 轻普通 > 重普通。
        普通队列仍受 per_user_max 约束（达上限且仍有可放行者则轮转队尾）。
        """
        while self._running < self._capacity:
            total_waiting = len(self._priv_wait) + len(self._norm_wait)
            congested = total_waiting > self._heavy_deprio_threshold

            # (deque, match_heavy) —— match_heavy=None 表示不限；否则只匹配该 heavy 值
            if congested:
                # 拥堵：特权优先于普通，同特权内轻指令优先（重指令降权）
                # 顺序：轻特权 > 重特权 > 轻普通 > 重普通
                candidates = [
                    (self._priv_wait, False),
                    (self._priv_wait, True),
                    (self._norm_wait, False),
                    (self._norm_wait, True),
                ]
            else:
                # 非拥堵：特权优先（不限 heavy），其次普通
                candidates = [
                    (self._priv_wait, None),
                    (self._norm_wait, None),
                ]

            picked: Optional[Tuple[Deque[_Waiter], int]] = None
            for dq, match_heavy in candidates:
                for i in range(len(dq)):
                    fut, user_key, heavy = dq[i]
                    if fut.done():  # 已取消/超时移除
                        del dq[i]
                        picked = None
                        break  # 重新扫描（删除了一个 done）
                    if match_heavy is not None and heavy != match_heavy:
                        continue
                    # 普通队列 per_user 约束
                    if dq is self._norm_wait and self._per_user.get(user_key or "", 0) >= self._per_user_max:
                        # 仍有可放行者则轮转队尾，否则放行队首（等待自身释放）
                        if len(dq) > 1 and self._norm_has_admittable():
                            dq.append(dq[i])
                            del dq[i]
                            picked = None
                            break  # 重新扫描
                    picked = (dq, i)
                    break
                if picked is not None:
                    break

            if picked is None:
                break

            dq, i = picked
            fut, user_key, _heavy = dq[i]
            del dq[i]
            self._running += 1
            self._bump_per_user(user_key)
            self._running_since[self._key(user_key)] = time.monotonic()
            if dq is self._priv_wait:
                self._priv_jumped_total += 1
            if not fut.done():
                fut.set_result(True)

    # ── 对外接口 ──────────────────────────────────────────────

    async def acquire(self, user_key: Optional[str], *, privileged: bool, heavy: bool = False) -> None:
        """获取一个全局执行槽位。成功返回；被拒绝抛 SlotRejected。

        特权：仅受全局容量约束，满则排队并插队到队首；
        普通：同时受全局容量 + per_user_max 约束，个人已满立即拒绝，否则排队队尾；
        heavy：重指令标记（是区吗/开庭等），用于拥堵时降权优先放行轻指令。
        """
        async with self._lock:
            if privileged or user_key is None:
                if self._running < self._capacity:
                    self._running += 1
                    self._bump_per_user(user_key)
                    self._running_since[self._key(user_key)] = time.monotonic()
                    return
                fut = asyncio.get_running_loop().create_future()
                self._priv_wait.append((fut, user_key, heavy))
            else:
                if self._per_user.get(user_key, 0) >= self._per_user_max:
                    raise SlotRejected("per_user_full")
                if self._running < self._capacity:
                    self._running += 1
                    self._bump_per_user(user_key)
                    self._running_since[self._key(user_key)] = time.monotonic()
                    return
                fut = asyncio.get_running_loop().create_future()
                self._norm_wait.append((fut, user_key, heavy))
        # 锁外 await，避免阻塞其他协程
        try:
            await asyncio.wait_for(fut, self._queue_wait_timeout)
        except asyncio.TimeoutError:
            self._remove_waiter(fut)
            raise SlotRejected("queue_timeout")
        except asyncio.CancelledError:
            # 外部取消（bot 重启 / 平台取消排队中的任务）：必须把 deque 中的 fut 移除，
            # 否则后续 release -> _admit_next_locked 会给已无等待者的 fut 调 set_result(True)，
            # 使 _running / _per_user 永久 +1 通胀；反复触发可使全局容量耗尽乃至死锁。
            self._remove_waiter(fut)
            raise
        return

    async def release(self, user_key: Optional[str]) -> None:
        """释放该用户一个全局槽位，并尝试补位。

        若该用户有被 `expire_all()` 提前回收的槽位（`_reclaimed` 计数），本次 release
        只做抵扣、不再递减 `_running` / `_per_user`（回收时已递减过），避免重复递减
        造成并发计数永久偏低；抵扣后仍尝试补位，因为名额实际已释放。
        """
        async with self._lock:
            if user_key is not None and self._reclaimed.get(user_key, 0) > 0:
                left = self._reclaimed[user_key] - 1
                if left > 0:
                    self._reclaimed[user_key] = left
                else:
                    self._reclaimed.pop(user_key, None)
                self._admit_next_locked()
                return
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
                for fut, _, _ in dq:
                    if not fut.done():
                        fut.set_exception(SlotRejected("cleared"))
            self._priv_wait.clear()
            self._norm_wait.clear()
            self._running = 0
            self._per_user.clear()
            self._running_since.clear()
            self._reclaimed.clear()

    async def expire_all(self) -> int:
        """超时强制释放运行中的槽位（防卡死泄漏），返回释放数量。

        被回收的槽位会记入 `_reclaimed`：对应指令日后真正结束时，其 `release()` 只抵扣、
        不再重复递减计数（否则 `_running` 会永久偏低，护栏被悄悄削弱）。
        """
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
                    if key != "*":
                        self._reclaimed[key] = self._reclaimed.get(key, 0) + 1
                    self._running_since.pop(key, None)
                    removed += 1
            self._admit_next_locked()
            return removed

    def per_user_saturated(self, user_key: Optional[str]) -> bool:
        """无锁快速预检：该用户个人并发是否已达上限。

        仅供调用方（main.py）决定「是否值得先推送排队提示」等文案决策，**不作为准入依据**
        ——真正的准入判断仍由 acquire() 在锁内完成，避免并发竞态。
        per_user_max<=0（不限制）或 user_key 为 None（不计数）时恒为 False。
        """
        if self._per_user_max <= 0 or user_key is None:
            return False
        return self._per_user.get(user_key, 0) >= self._per_user_max

    def stats(self) -> dict:
        """统计快照（不持有锁，供监控展示参考）。"""
        heavy_waiting = 0
        for f, _, h in list(self._priv_wait) + list(self._norm_wait):
            if not f.done() and h:
                heavy_waiting += 1
        return {
            "capacity": self._capacity,
            "per_user_max": self._per_user_max,
            "global_running": self._running,
            "waiting": len(self._priv_wait) + len(self._norm_wait),
            "privileged_waiting": len(self._priv_wait),
            "normal_waiting": len(self._norm_wait),
            "heavy_waiting": heavy_waiting,
            "active_users": len(self._per_user),
            "reclaimed_pending": sum(self._reclaimed.values()),
            "privileged_jumped_total": self._priv_jumped_total,
        }
