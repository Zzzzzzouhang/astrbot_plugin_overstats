"""并发准入策略（纯函数）。

把「能否准入 / 因何拒绝」的判断从 asyncio 编排中剥离，
使准入规则可独立单测；service 层只负责在锁内调用这些决策并执行副作用。

v3 重构后无等待队列：获得不到槽位即拒绝（快速失败），不再入队干等——
排队等待正是 QQ 群聊「msg_id 已过期」与后端被瞬时打满的主要来源。
"""

from dataclasses import dataclass

from .enums import RejectReason, SlotOutcome
from .slot import SlotRequest


@dataclass(frozen=True)
class ConcurrencyThresholds:
    """并发相关阈值（可热更新）。

    当前仅保留运行槽位兜底回收时长（秒）：占用超过该时长即视为异常，
    由 service 层周期回收；取值高于正常指令最坏持槽时间
    （≈ HTTP 超时 + 发送 ≈ 16 分钟），正常路径永不触发回收。
    """

    slot_auto_release: float = 3600.0


@dataclass(frozen=True)
class ConcurrencyUsage:
    """准入判定所需的实时用量（由 service 持锁读取后传入，保证判定原子）。

    容量默认值与 CommandConcurrencyService 保持一致（单用户 2 / 轻 0 不限 / 重 10），
    使脱离 service 直接单测策略时看到与生产一致的语义。
    """

    enabled: bool = True
    per_user_max: int = 2
    light_max: int = 0
    heavy_max: int = 10
    light_used: int = 0
    heavy_used: int = 0
    per_user_used: int = 0


@dataclass(frozen=True)
class AdmissionDecision:
    """准入判定结果。"""

    outcome: SlotOutcome
    reason: RejectReason | None = None


def decide_admission(usage: ConcurrencyUsage, request: SlotRequest) -> AdmissionDecision:
    """判定单条指令能否立即获得并发槽位。

    - 个人并发上限（per_user_max）作用于轻/重全部指令（单一计数）；
      特权（白名单群/人/管理员/总开关关闭）与无用户键的事件不受约束；
      per_user_max<=0 表示不限制；
    - 全局槽位按指令轻重分流到独立池：轻指令池与重指令池分别判定；
      容量 <=0 表示不限制（轻指令默认 0，即无限直连后端）；
    - 任一口径不满足 → REJECT（附原因），由 handler 返回对应文案，不排队。
    """
    if (
        usage.enabled
        and not request.privileged
        and request.user_key is not None
        and usage.per_user_max > 0
        and usage.per_user_used >= usage.per_user_max
    ):
        return AdmissionDecision(SlotOutcome.REJECT, RejectReason.PER_USER_FULL)

    if request.heavy:
        if usage.heavy_max > 0 and usage.heavy_used >= usage.heavy_max:
            return AdmissionDecision(SlotOutcome.REJECT, RejectReason.HEAVY_FULL)
    else:
        if usage.light_max > 0 and usage.light_used >= usage.light_max:
            return AdmissionDecision(SlotOutcome.REJECT, RejectReason.LIGHT_FULL)

    return AdmissionDecision(SlotOutcome.ADMIT)
