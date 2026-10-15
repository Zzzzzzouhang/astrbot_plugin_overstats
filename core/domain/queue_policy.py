"""排队准入策略（纯函数）。

把「能否准入 / 是否拒绝 / 是否暂停」的判断从 asyncio 编排中剥离，
使排队规则可独立单测；service 层只负责在锁内调用这些决策并执行副作用。
"""

from dataclasses import dataclass

from .enums import RejectReason, SlotOutcome
from .queue_snapshot import QueueSnapshot
from .slot import SlotRequest


@dataclass(frozen=True)
class QueueThresholds:
    """排队相关阈值（均可热更新）。"""

    overflow: int = 50                  # 全局等待数 >= 此值：普通指令直接拒绝
    heavy_pause: int = 10               # 重指令拥堵暂停阈值（等待数 > 此值）
    heavy_deprio: int = 5               # 拥堵降权阈值（等待数 > 此值时重指令降权）
    queue_wait_timeout: float = 600.0   # 排队等待上限（秒），超时视为拒绝
    slot_auto_release: float = 600.0    # 运行槽位占用超时（秒），兜底回收


@dataclass(frozen=True)
class AdmissionDecision:
    """准入判定结果。"""

    outcome: SlotOutcome
    reason: "RejectReason | None" = None


def should_reject_overflow(snapshot: QueueSnapshot, request: SlotRequest, thresholds: QueueThresholds) -> bool:
    """全局队列溢出保护：等待数达到硬上限时拒绝后续普通指令，避免无限堆积拖垮后端。

    特权（白名单 / 管理员 / 限流关闭）不受限。
    """
    return not request.privileged and snapshot.waiting >= thresholds.overflow


def should_pause_heavy(snapshot: QueueSnapshot, request: SlotRequest, thresholds: QueueThresholds) -> bool:
    """拥堵时重指令（是区吗 / 开庭）自动暂停：等待数 > 阈值即直接返回，不再入队。"""
    return request.heavy and not request.privileged and snapshot.waiting > thresholds.heavy_pause


def decide_admission(
    *,
    snapshot: QueueSnapshot,
    request: SlotRequest,
    per_user_used: int,
) -> AdmissionDecision:
    """判定单条指令的准入方式。

    - 普通用户且个人并发已满 → 立即拒绝（per_user_full）；特权用户不受个人上限约束；
    - 有空闲全局槽位 → 立即准入；
    - 否则 → 进入等待队列（不拒绝）。
    """
    if not request.privileged and request.user_key is not None and snapshot.per_user_max > 0:
        if per_user_used >= snapshot.per_user_max:
            return AdmissionDecision(SlotOutcome.REJECT, RejectReason.PER_USER_FULL)
    if snapshot.has_free_slot:
        return AdmissionDecision(SlotOutcome.ADMIT)
    return AdmissionDecision(SlotOutcome.WAIT)
