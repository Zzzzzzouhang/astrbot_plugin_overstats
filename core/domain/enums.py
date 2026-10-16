"""排队与提示领域的枚举与常量类型。

domain 层约定：不依赖 astrbot、不依赖 asyncio、不做任何 IO，
仅用纯数据结构描述业务概念，便于独立单测与复用。
"""

from enum import Enum


class CommandClass(str, Enum):
    """指令轻重分类。

    重指令（是区吗 / 开庭）在后端会放大成数百次 LLM 子请求，
    拥堵时被降权，优先放行轻指令，避免拖垮全部用户。
    """

    LIGHT = "light"
    HEAVY = "heavy"


class SlotOutcome(str, Enum):
    """准入判定结果。"""

    ADMIT = "admit"       # 立即获得全局槽位
    WAIT = "wait"         # 进入等待队列（不拒绝，await 至获得槽位）
    REJECT = "reject"     # 立即拒绝（不入队）


class RejectReason(str, Enum):
    """拒绝原因（同时作为 SlotRejected 的 args[0]，保持与旧实现一致的字符串值）。"""

    PER_USER_FULL = "per_user_full"      # 该用户个人并发已达上限
    QUEUE_TIMEOUT = "queue_timeout"      # 排队等待超时（防永久挂起）
    CLEARED = "cleared"                  # 队列被清空（每日重置 / 重启）
    HEAVY_PAUSED = "heavy_paused"        # 重指令拥堵自动暂停（acquire 锁内精确判定：等待数 > 阈值）

    # 注：队列溢出（等待数达硬上限）不入此枚举——该场景由 handler 直接返回文案，
    # 不抛异常，避免与「个人并发已满」的拒绝语义混淆。
