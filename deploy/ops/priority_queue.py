"""全局指令并发优先级队列（兼容门面）。

实现已迁移至分层架构：
- `core/domain/queue_policy.py` + `queue_snapshot.py`：准入 / 拒绝 / 降权的纯策略；
- `core/service/command_queue.py`：锁、future、补位、回收的 asyncio 编排。

本模块保留原有类名与 API（`acquire` / `release` / `clear` / `expire_all` /
`per_user_saturated` / `stats`），供既有调用点与既有测试继续使用，行为不变。
新增代码请直接使用 `core.service.command_queue.CommandQueueService`。
"""

import sys
from pathlib import Path

try:
    # 包内加载（AstrBot 以 data.plugins.<name> 包导入）时优先用相对导入，
    # 与本包内其他模块（main.py 的 `.core.*`）保持同一模块树——
    # 若此处按顶层 `core.` 绝对导入，会加载出第二个同名模块树，
    # 其 SlotRejected 与 CommandQueueService 抛出的不是同一个类，
    # 调用方的 `except SlotRejected` 将永远匹配不到（历史 bug）。
    from ...core.domain.queue_policy import QueueThresholds
    from ...core.domain.slot import SlotRejected
    from ...core.service.command_queue import CommandQueueService
except ImportError:  # 按文件路径直接加载（如单测 importlib）时无包上下文
    _PLUGIN_ROOT = str(Path(__file__).resolve().parents[2])
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    from core.domain.queue_policy import QueueThresholds
    from core.domain.slot import SlotRejected
    from core.service.command_queue import CommandQueueService

__all__ = ["GlobalPriorityCommandQueue", "SlotRejected"]


class GlobalPriorityCommandQueue:
    """`CommandQueueService` 的兼容包装（保持旧构造参数与行为）。"""

    def __init__(
        self,
        capacity: int = 5,
        per_user_max: int = 3,
        timeout_seconds: int = 300,
        queue_wait_timeout: int = 600,
        heavy_deprio_threshold: int = 5,
    ):
        self._service = CommandQueueService(
            capacity=capacity,
            per_user_max=per_user_max,
            thresholds=QueueThresholds(
                heavy_deprio=heavy_deprio_threshold,
                queue_wait_timeout=queue_wait_timeout,
                slot_auto_release=timeout_seconds,
            ),
        )

    # ── 配置热生效 ────────────────────────────────────────────

    def update_config(
        self,
        per_user_max: int,
        timeout_seconds: int = 0,
        *,
        capacity: int = 0,
        queue_wait_timeout: int = 0,
    ) -> None:
        self._service.update_config(
            per_user_max,
            timeout_seconds,
            capacity=capacity,
            queue_wait_timeout=queue_wait_timeout,
        )

    # ── 队列操作 ──────────────────────────────────────────────

    async def acquire(self, user_key, *, privileged: bool = False, heavy: bool = False):
        return await self._service.acquire(user_key, privileged=privileged, heavy=heavy)

    async def release(self, user_key) -> None:
        await self._service.release(user_key)

    async def clear(self) -> None:
        await self._service.clear()

    async def expire_all(self) -> int:
        return await self._service.expire_all()

    def per_user_saturated(self, user_key) -> bool:
        return self._service.per_user_saturated(user_key)

    def stats(self) -> dict:
        return self._service.stats()

    @property
    def _capacity(self) -> int:
        """后端信号量与全局队列容量对齐时使用。"""
        return self._service.capacity
