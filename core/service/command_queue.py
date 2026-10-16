"""指令排队服务（service 层）：全局槽位 + 真实等待队列的 asyncio 编排。

职责边界：
- 本类只做「编排」：锁、future、超时、取消、补位；
- 「准入 / 拒绝 / 暂停 / 降权」的判定全部委托 domain 层纯函数；
- 状态统计通过计数器维护（O(1)），避免每次 stats() 遍历等待队列——
  高峰期 stats() 被每条指令与监控面板高频调用，原实现每次 O(n) 遍历。

对外行为与旧 `GlobalPriorityCommandQueue` 保持一致：
- 全局容量为系统级并发上限，所有用户（含特权）均受约束；
- 特权（白名单 / 管理员）插队优先，且不受 per_user 个人上限约束；
- 普通用户个人并发已满立即拒绝，否则排队等待（不拒绝）；
- 拥堵（等待 > 阈值）时重指令降权：轻特权 > 重特权 > 轻普通 > 重普通。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from ..domain.enums import CommandClass, RejectReason, SlotOutcome
from ..domain.queue_policy import (
    QueueThresholds,
    decide_admission,
    should_pause_heavy,
    should_pause_heavy_at,
    should_reject_overflow,
)
from ..domain.queue_snapshot import QueueSnapshot
from ..domain.slot import SlotRejected, SlotRequest, SlotTicket


@dataclass
class _Waiter:
    """等待中的一次申请。

    alive=False 表示已被取消 / 超时移除（惰性删除）：出队时丢弃即可，
    使「移除等待者」从 O(n) 线性查找降为 O(1) 标记。
    """

    seq: int
    future: "asyncio.Future"
    user_key: Optional[str]
    request: SlotRequest
    alive: bool = True


class CommandQueueService:
    """全局指令并发优先级队列服务。"""

    def __init__(
        self,
        capacity: int = 5,
        per_user_max: int = 3,
        thresholds: QueueThresholds | None = None,
    ):
        self._capacity = max(1, capacity)
        self._per_user_max = max(0, per_user_max)
        self._thresholds = thresholds or QueueThresholds()

        self._lock = asyncio.Lock()
        self._running = 0
        self._per_user: Dict[str, int] = {}
        # 运行槽位明细：slot_id -> (user_key, started_monotonic)
        # 按槽位（而非按用户）记录，使超时回收能精确释放长时间占用的每一个槽位，
        # 避免同一用户多槽时只回收其一导致的容量泄漏。
        self._slots: Dict[int, Tuple[Optional[str], float]] = {}
        self._next_slot_id = 1

        self._priv_wait: Deque[_Waiter] = deque()
        self._norm_wait: Deque[_Waiter] = deque()
        self._waiter_index: Dict[int, _Waiter] = {}
        self._seq = 0
        self._priv_jumped_total = 0

        # 计数器：避免 stats() 的 O(n) 遍历
        self._priv_count = 0
        self._norm_count = 0
        self._heavy_waiting = 0
        self._reclaimed: Dict[str, int] = {}

    # ── 配置热生效 ────────────────────────────────────────────

    def update_config(
        self,
        per_user_max: int,
        timeout_seconds: float = 0,
        *,
        capacity: int = 0,
        queue_wait_timeout: float = 0,
    ) -> None:
        """热生效：更新并发上限与超时阈值（不回收已占槽位）。"""
        if capacity:
            self._capacity = max(1, capacity)
        if per_user_max is not None:
            self._per_user_max = max(0, per_user_max)
        if timeout_seconds:
            self._thresholds = QueueThresholds(
                overflow=self._thresholds.overflow,
                heavy_pause=self._thresholds.heavy_pause,
                heavy_deprio=self._thresholds.heavy_deprio,
                queue_wait_timeout=self._thresholds.queue_wait_timeout,
                slot_auto_release=timeout_seconds,
            )
        if queue_wait_timeout:
            self._thresholds = QueueThresholds(
                overflow=self._thresholds.overflow,
                heavy_pause=self._thresholds.heavy_pause,
                heavy_deprio=self._thresholds.heavy_deprio,
                queue_wait_timeout=queue_wait_timeout,
                slot_auto_release=self._thresholds.slot_auto_release,
            )

    def update_thresholds(self, thresholds: QueueThresholds) -> None:
        self._thresholds = thresholds

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def thresholds(self) -> QueueThresholds:
        return self._thresholds

    # ── 快照 ──────────────────────────────────────────────────

    def snapshot(self) -> QueueSnapshot:
        """统计快照（不持有锁，供决策与监控展示参考）。"""
        return QueueSnapshot(
            capacity=self._capacity,
            per_user_max=self._per_user_max,
            running=self._running,
            waiting=self._priv_count + self._norm_count,
            privileged_waiting=self._priv_count,
            normal_waiting=self._norm_count,
            heavy_waiting=self._heavy_waiting,
            active_users=len(self._per_user),
            reclaimed_pending=sum(self._reclaimed.values()),
            privileged_jumped_total=self._priv_jumped_total,
        )

    def stats(self) -> dict:
        """兼容旧字段名的统计字典。"""
        return self.snapshot().to_stats_dict()

    def per_user_saturated(self, user_key: Optional[str]) -> bool:
        """无锁快速预检：该用户个人并发是否已达上限（仅用于提示文案决策，不作准入依据）。"""
        if self._per_user_max <= 0 or user_key is None:
            return False
        return self._per_user.get(user_key, 0) >= self._per_user_max

    # ── 主流程 ────────────────────────────────────────────────

    async def acquire(self, user_key: Optional[str], *, privileged: bool = False, heavy: bool = False) -> SlotTicket:
        """获取一个全局执行槽位。成功返回凭证；被拒绝抛 SlotRejected。"""
        request = SlotRequest.of(user_key, privileged=privileged, heavy=heavy)
        async with self._lock:
            if request.privileged or request.user_key is None:
                if self._running < self._capacity:
                    return self._occupy_locked(request)
                fut = self._enqueue_locked(self._priv_wait, "priv", request)
            else:
                decision = decide_admission(
                    snapshot=self.snapshot(),
                    request=request,
                    per_user_used=self._per_user.get(request.user_key, 0),
                )
                if decision.outcome is SlotOutcome.REJECT:
                    raise SlotRejected(decision.reason)
                if decision.outcome is SlotOutcome.ADMIT:
                    return self._occupy_locked(request)
                # 入队前锁内精确判定重指令拥堵暂停：等待数已 > 阈值则直接拒绝、不再入队。
                # 在锁内以实时计数判定，避免 handler 快照式判定在并发下的「超额」
                # （高峰期多个请求同一时刻都读到 ≈阈值而全部入队，队列冲到远高于阈值）。
                if should_pause_heavy_at(self._priv_count + self._norm_count, request, self._thresholds):
                    raise SlotRejected(RejectReason.HEAVY_PAUSED)
                fut = self._enqueue_locked(self._norm_wait, "norm", request)

        # 锁外 await，避免阻塞其他协程。
        # 补位方会把真实凭证（含 slot_id）置入 future，必须原样返回——
        # 否则调用方拿到的 slot_id 为 0，无法在 release 时精确释放对应槽位。
        try:
            ticket = await asyncio.wait_for(fut, self._thresholds.queue_wait_timeout)
        except asyncio.TimeoutError:
            self._discard_waiter(fut)
            raise SlotRejected(RejectReason.QUEUE_TIMEOUT)
        except asyncio.CancelledError:
            # 外部取消（bot 重启 / 平台取消排队中的任务）时必须移除等待项，
            # 否则后续补位会给已无等待者的 future 置位，使并发计数永久通胀。
            self._discard_waiter(fut)
            raise
        return ticket if isinstance(ticket, SlotTicket) else SlotTicket(
            user_key=user_key, slot_id=0, acquired_at=time.monotonic()
        )

    async def release(self, user_key: Optional[str], slot_id: int = 0) -> None:
        """释放一个全局槽位并尝试补位。

        slot_id>0 时精确释放该槽位；否则按用户 FIFO 释放其最早占用的槽位
        （兼容只传 user_key 的旧调用点）。
        若该用户有被 `expire_all()` 提前回收的槽位（reclaimed 计数），
        本次 release 只抵扣、不再递减计数，避免重复递减导致并发计数永久偏低。
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
            target = slot_id if slot_id > 0 else self._oldest_slot_locked(user_key)
            self._free_slot_locked(target)
            self._admit_next_locked()

    # ── 兜底维护 ──────────────────────────────────────────────

    async def clear(self) -> None:
        """清空全部槽位与等待者（每日重置兜底）。"""
        async with self._lock:
            for dq in (self._priv_wait, self._norm_wait):
                for w in dq:
                    if not w.future.done():
                        w.future.set_exception(SlotRejected(RejectReason.CLEARED))
            self._priv_wait.clear()
            self._norm_wait.clear()
            self._waiter_index.clear()
            self._running = 0
            self._per_user.clear()
            self._slots.clear()
            self._reclaimed.clear()
            self._priv_count = 0
            self._norm_count = 0
            self._heavy_waiting = 0

    async def expire_all(self) -> int:
        """超时强制释放运行中的槽位（防卡死泄漏），返回释放数量。

        被回收的槽位记入 reclaimed：对应指令日后真正结束时的 release() 只抵扣、
        不再重复递减，保证回收幂等。
        """
        if self._thresholds.slot_auto_release <= 0:
            return 0
        async with self._lock:
            cutoff = time.monotonic() - self._thresholds.slot_auto_release
            removed = 0
            for slot_id, (uk, started) in list(self._slots.items()):
                if started >= cutoff:
                    continue
                self._slots.pop(slot_id, None)
                self._running = max(0, self._running - 1)
                if uk is not None:
                    self._per_user[uk] = max(0, self._per_user.get(uk, 0) - 1)
                    if self._per_user[uk] == 0:
                        self._per_user.pop(uk, None)
                    self._reclaimed[uk] = self._reclaimed.get(uk, 0) + 1
                removed += 1
            # 与原实现一致：无条件尝试补位（防御——任何原因导致的空闲槽位都应放出），
            # 否则等待者可能挂起到 queue_wait_timeout 才被拒绝。
            self._admit_next_locked()
            return removed

    # ── 锁内工具 ──────────────────────────────────────────────

    def _occupy_locked(self, request: SlotRequest) -> SlotTicket:
        self._running += 1
        if request.user_key is not None:
            self._per_user[request.user_key] = self._per_user.get(request.user_key, 0) + 1
        slot_id = self._next_slot_id
        self._next_slot_id += 1
        now = time.monotonic()
        self._slots[slot_id] = (request.user_key, now)
        return SlotTicket(user_key=request.user_key, slot_id=slot_id, acquired_at=now)

    def _free_slot_locked(self, slot_id: int) -> None:
        entry = self._slots.pop(slot_id, None)
        if entry is None:
            return
        uk = entry[0]
        self._running = max(0, self._running - 1)
        if uk is not None:
            self._per_user[uk] = max(0, self._per_user.get(uk, 0) - 1)
            if self._per_user[uk] == 0:
                self._per_user.pop(uk, None)

    def _oldest_slot_locked(self, user_key: Optional[str]) -> int:
        """按 FIFO 找出该用户最早占用的槽位；找不到返回 0。"""
        best_id, best_ts = 0, None
        for slot_id, (uk, started) in self._slots.items():
            if uk != user_key:
                continue
            if best_ts is None or started < best_ts:
                best_id, best_ts = slot_id, started
        return best_id

    def _enqueue_locked(self, dq: Deque[_Waiter], lane: str, request: SlotRequest) -> "asyncio.Future":
        self._seq += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        waiter = _Waiter(seq=self._seq, future=fut, user_key=request.user_key, request=request)
        dq.append(waiter)
        self._waiter_index[id(fut)] = waiter
        if lane == "priv":
            self._priv_count += 1
        else:
            self._norm_count += 1
        if request.heavy:
            self._heavy_waiting += 1
        return fut

    def _discard_waiter(self, fut: "asyncio.Future") -> None:
        """标记等待者为失效（O(1)，无需线性查找 deque），并即时修正计数。

        元素仍留在 deque 中，由后续补位流程丢弃；计数只在此处扣除一次，
        出队时不再重复扣除（见 `_take_locked`）。
        """
        waiter = self._waiter_index.pop(id(fut), None)
        if waiter is None or not waiter.alive:
            return
        waiter.alive = False
        self._decrement_waiter_counters(waiter)

    def _decrement_waiter_counters(self, waiter: _Waiter) -> None:
        if waiter.request.privileged:
            self._priv_count = max(0, self._priv_count - 1)
        else:
            self._norm_count = max(0, self._norm_count - 1)
        if waiter.request.heavy:
            self._heavy_waiting = max(0, self._heavy_waiting - 1)

    def _take_locked(self, dq: Deque[_Waiter], index: int) -> _Waiter:
        """从队列中摘除指定等待者并修正计数（仅对仍活跃者扣减，失效者已在 discard 时扣过）。"""
        waiter = dq[index]
        del dq[index]
        self._waiter_index.pop(id(waiter.future), None)
        if waiter.alive:
            self._decrement_waiter_counters(waiter)
        return waiter

    def _drop_dead_locked(self, dq: Deque[_Waiter]) -> None:
        """丢弃队首已失效（被取消 / 超时）的等待者，计数无需再扣。"""
        while dq and not dq[0].alive:
            waiter = dq.popleft()
            self._waiter_index.pop(id(waiter.future), None)

    def _norm_has_admittable(self) -> bool:
        """普通队列中是否存在（未失效且个人未达上限的）可放行者。

        用于避免「全员个人已达上限」时无限轮转；此时应直接放行队首等待自身释放。
        """
        for w in self._norm_wait:
            if not w.alive:
                continue
            if self._per_user.get(w.user_key or "", 0) < self._per_user_max:
                return True
        return False

    def _admit_next_locked(self) -> None:
        """从等待队列补位。

        - 非拥堵（等待 <= 阈值）：特权优先、普通 FIFO；
        - 拥堵（等待 > 阈值）：特权优先于普通，同层级内重指令降权：
          轻特权 > 重特权 > 轻普通 > 重普通。
        - 普通队列仍受 per_user 约束（达上限且仍有可放行者则轮转队尾）。
        """
        while self._running < self._capacity:
            self._drop_dead_locked(self._priv_wait)
            self._drop_dead_locked(self._norm_wait)

            total_waiting = self._priv_count + self._norm_count
            congested = total_waiting > self._thresholds.heavy_deprio
            if congested:
                candidates = [
                    (self._priv_wait, "priv", False),
                    (self._priv_wait, "priv", True),
                    (self._norm_wait, "norm", False),
                    (self._norm_wait, "norm", True),
                ]
            else:
                candidates = [
                    (self._priv_wait, "priv", None),
                    (self._norm_wait, "norm", None),
                ]

            picked: Optional[Tuple[Deque[_Waiter], int]] = None
            for dq, lane, match_heavy in candidates:
                for i in range(len(dq)):
                    w = dq[i]
                    if not w.alive:
                        self._take_locked(dq, i)
                        picked = None
                        break
                    if match_heavy is not None and w.request.heavy != match_heavy:
                        continue
                    if lane == "norm" and self._per_user.get(w.user_key or "", 0) >= self._per_user_max:
                        if len(dq) > 1 and self._norm_has_admittable():
                            dq.append(dq[i])
                            del dq[i]
                            picked = None
                            break
                    picked = (dq, i)
                    break
                if picked is not None:
                    break

            if picked is None:
                break

            dq, i = picked
            w = self._take_locked(dq, i)
            if dq is self._priv_wait:
                self._priv_jumped_total += 1

            self._running += 1
            if w.user_key is not None:
                self._per_user[w.user_key] = self._per_user.get(w.user_key, 0) + 1
            slot_id = self._next_slot_id
            self._next_slot_id += 1
            now = time.monotonic()
            self._slots[slot_id] = (w.user_key, now)
            if not w.future.done():
                w.future.set_result(SlotTicket(user_key=w.user_key, slot_id=slot_id, acquired_at=now))
