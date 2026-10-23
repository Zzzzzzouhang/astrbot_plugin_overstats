"""指令并发控制服务（service 层）：轻/重独立全局槽位 + 按用户并发 + 超时自动释放。

职责边界：
- 本类只做「编排」：锁、槽位表、超时回收、每日重置；
- 「能否准入 / 因何拒绝」的判定全部委托 domain 层纯函数 decide_admission；
- 状态经计数器与双槽位表维护，snapshot() O(1)（高峰期被每条指令与监控面板高频调用）。

v3 重构（取代旧 CommandQueueService 的真实等待队列）：
- 轻/重指令全局槽位相互独立：轻指令池默认 0（不限制、直连后端），
  重指令池默认 10；超限即拒（抛 SlotRejected），不再入队干等——
  排队等待正是 QQ 群聊「msg_id 已过期」与后端被瞬时打满的主要来源；
- 按用户并发上限（per_user_max，默认 2）跨轻/重单一计数，
  特权（白名单群/人/管理员/总开关关闭）豁免；全局容量对所有人一致生效；
- 运行槽位超时自动释放保留：expire_all 回收占用超过 slot_auto_release
  的槽位，回收幂等（reclaimed 抵扣），正常路径永不触发。
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional, Tuple

from ..domain.concurrency_policy import (
    ConcurrencyThresholds,
    ConcurrencyUsage,
    decide_admission,
)
from ..domain.concurrency_snapshot import ConcurrencySnapshot
from ..domain.enums import SlotOutcome
from ..domain.slot import SlotRejected, SlotRequest, SlotTicket

# 单池槽位明细：slot_id -> (user_key, started_monotonic)
_PoolSlots = Dict[int, Tuple[Optional[str], float]]


class CommandConcurrencyService:
    """全局指令并发控制服务（无队列，超限即拒）。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        per_user_max: int = 2,
        light_max: int = 0,
        heavy_max: int = 10,
        thresholds: ConcurrencyThresholds | None = None,
    ):
        self._lock = asyncio.Lock()
        self._enabled = enabled
        self._per_user_max = max(0, per_user_max)
        self._light_max = max(0, light_max)
        self._heavy_max = max(0, heavy_max)
        self._thresholds = thresholds or ConcurrencyThresholds()

        # 按用户并发计数（跨轻/重单一计数）与被回收槽位的抵扣账
        self._per_user: Dict[str, int] = {}
        self._reclaimed: Dict[str, int] = {}
        # 轻/重两个独立全局池
        self._light_slots: _PoolSlots = {}
        self._heavy_slots: _PoolSlots = {}
        self._next_slot_id = 1

    # ── 配置热生效 ────────────────────────────────────────────

    def update_config(
        self,
        *,
        enabled: bool = True,
        per_user_max: int = 0,
        light_max: int = 0,
        heavy_max: int = 0,
        slot_auto_release: float = 0,
    ) -> None:
        """热生效：更新并发上限与回收阈值（不回收已占槽位）。"""
        self._enabled = enabled
        if per_user_max is not None:
            self._per_user_max = max(0, per_user_max)
        if light_max is not None:
            self._light_max = max(0, light_max)
        if heavy_max is not None:
            self._heavy_max = max(0, heavy_max)
        if slot_auto_release:
            self._thresholds = ConcurrencyThresholds(
                slot_auto_release=slot_auto_release,
            )

    @property
    def thresholds(self) -> ConcurrencyThresholds:
        return self._thresholds

    # ── 快照 ──────────────────────────────────────────────────

    def snapshot(self) -> ConcurrencySnapshot:
        """统计快照（不持有锁，供决策与监控展示参考）。"""
        return ConcurrencySnapshot(
            enabled=self._enabled,
            per_user_max=self._per_user_max,
            light_max=self._light_max,
            heavy_max=self._heavy_max,
            light_running=len(self._light_slots),
            heavy_running=len(self._heavy_slots),
            active_users=len(self._per_user),
            reclaimed_pending=sum(self._reclaimed.values()),
        )

    def stats(self) -> dict:
        """兼容旧字段名的统计字典（global_running 等），供监控面板使用。"""
        return self.snapshot().to_stats_dict()

    def per_user_saturated(self, user_key: Optional[str]) -> bool:
        """无锁快速预检：该用户个人并发是否已达上限（仅用于提示文案决策，不作准入依据）。"""
        if not self._enabled or self._per_user_max <= 0 or user_key is None:
            return False
        return self._per_user.get(user_key, 0) >= self._per_user_max

    # ── 主流程 ────────────────────────────────────────────────

    async def acquire(self, user_key: Optional[str], *, privileged: bool = False, heavy: bool = False) -> SlotTicket:
        """获取一个全局并发槽位。成功返回凭证；被拒绝抛 SlotRejected（不排队、不等待）。"""
        request = SlotRequest.of(user_key, privileged=privileged, heavy=heavy)
        async with self._lock:
            decision = decide_admission(
                ConcurrencyUsage(
                    enabled=self._enabled,
                    per_user_max=self._per_user_max,
                    light_max=self._light_max,
                    heavy_max=self._heavy_max,
                    light_used=len(self._light_slots),
                    heavy_used=len(self._heavy_slots),
                    per_user_used=self._per_user.get(user_key, 0) if user_key else 0,
                ),
                request,
            )
            if decision.outcome is SlotOutcome.REJECT:
                raise SlotRejected(decision.reason)
            return self._occupy_locked(request)

    async def release(self, user_key: Optional[str], slot_id: int = 0) -> None:
        """释放一个全局槽位。

        slot_id>0 时精确释放该槽位；否则按用户 FIFO 释放其最早占用的槽位
        （兼容只传 user_key 的旧调用点）。
        若该用户有被 expire_all() 提前回收的槽位（reclaimed 计数），
        本次 release 只抵扣、不再递减计数，避免回收后重复递减导致计数永久偏低。
        """
        async with self._lock:
            if user_key is not None and self._reclaimed.get(user_key, 0) > 0:
                left = self._reclaimed[user_key] - 1
                if left > 0:
                    self._reclaimed[user_key] = left
                else:
                    self._reclaimed.pop(user_key, None)
                return
            target = slot_id if slot_id > 0 else self._oldest_slot_locked(user_key)
            self._free_slot_locked(target)

    # ── 兜底维护 ──────────────────────────────────────────────

    async def clear(self) -> None:
        """清空全部槽位与计数（每日凌晨 4 点重置兜底）。"""
        async with self._lock:
            self._per_user.clear()
            self._reclaimed.clear()
            self._light_slots.clear()
            self._heavy_slots.clear()

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
            for pool in (self._light_slots, self._heavy_slots):
                for slot_id, (uk, started) in list(pool.items()):
                    if started >= cutoff:
                        continue
                    pool.pop(slot_id, None)
                    if uk is not None:
                        self._per_user[uk] = max(0, self._per_user.get(uk, 0) - 1)
                        if self._per_user[uk] == 0:
                            self._per_user.pop(uk, None)
                        self._reclaimed[uk] = self._reclaimed.get(uk, 0) + 1
                    removed += 1
            return removed

    # ── 锁内工具 ──────────────────────────────────────────────

    def _pool_for(self, request: SlotRequest) -> _PoolSlots:
        return self._heavy_slots if request.heavy else self._light_slots

    def _occupy_locked(self, request: SlotRequest) -> SlotTicket:
        pool = self._pool_for(request)
        slot_id = self._next_slot_id
        self._next_slot_id += 1
        now = time.monotonic()
        pool[slot_id] = (request.user_key, now)
        if request.user_key is not None:
            self._per_user[request.user_key] = self._per_user.get(request.user_key, 0) + 1
        return SlotTicket(user_key=request.user_key, slot_id=slot_id, acquired_at=now)

    def _free_slot_locked(self, slot_id: int) -> None:
        for pool in (self._light_slots, self._heavy_slots):
            entry = pool.pop(slot_id, None)
            if entry is None:
                continue
            uk = entry[0]
            if uk is not None:
                self._per_user[uk] = max(0, self._per_user.get(uk, 0) - 1)
                if self._per_user[uk] == 0:
                    self._per_user.pop(uk, None)
            return

    def _oldest_slot_locked(self, user_key: Optional[str]) -> int:
        """按 FIFO 找出该用户最早占用的槽位（轻/重两池合并）；找不到返回 0。"""
        best_id, best_ts = 0, None
        for pool in (self._light_slots, self._heavy_slots):
            for slot_id, (uk, started) in pool.items():
                if uk != user_key:
                    continue
                if best_ts is None or started < best_ts:
                    best_id, best_ts = slot_id, started
        return best_id
