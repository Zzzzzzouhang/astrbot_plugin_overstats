"""并发与提示领域的枚举与常量类型。

domain 层约定：不依赖 astrbot、不依赖 asyncio、不做任何 IO，
仅用纯数据结构描述业务概念，便于独立单测与复用。
"""

from enum import Enum


class CommandClass(str, Enum):
    """指令轻重分类。

    重指令（是区吗 / 开庭）在后端会放大成数百次 LLM 子请求，
    独占独立的重指令并发池；轻指令（其余全部业务指令）走轻指令池，
    默认不限制、直连后端。两个池容量独立配置、互不挤占。
    """

    LIGHT = "light"
    HEAVY = "heavy"


class SlotOutcome(str, Enum):
    """准入判定结果。"""

    ADMIT = "admit"       # 立即获得全局槽位
    REJECT = "reject"     # 被拒绝（个人并发已满 / 全局槽位已满），不入队、不等待


class RejectReason(str, Enum):
    """拒绝原因（同时作为 SlotRejected 的 args[0]，保持字符串值稳定）。"""

    PER_USER_FULL = "per_user_full"      # 该用户个人并发已达上限
    LIGHT_FULL = "light_full"            # 轻指令全局并发已满
    HEAVY_FULL = "heavy_full"            # 重指令（是区吗/开庭）全局并发已满
