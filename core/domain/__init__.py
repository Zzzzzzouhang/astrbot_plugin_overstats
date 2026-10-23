"""domain 层：并发准入与提示的纯业务逻辑（无 IO、无框架依赖）。

分层约定：
- domain 只描述「是什么 / 该怎么判」：值对象、策略纯函数、文案构造；
- service 负责「怎么做」：asyncio 编排、锁、定时器、文件 IO；
- platform 负责「怎么发」：平台差异（QQ 官方 markdown / @ 消息链 / 被动回复）。
"""

from .enums import CommandClass, RejectReason, SlotOutcome
from .image_cache import (
    DEFAULT_TTL,
    IMAGE_MAGIC_PREFIXES,
    MAX_PER_USER,
    ImageCacheEntry,
    append_entry,
    coerce_entry,
    filter_alive,
    is_alive,
    is_valid_image_head,
    most_recent,
)
from .notice import ReplyWindow, decide_timeout_warning, should_skip_status_prompt
from .notice_text import (
    GET_IMAGE_TAG,
    build_cmd_tag,
    build_timeout_warning_text,
    command_label,
    extract_bnet_id,
)
from .concurrency_policy import (
    AdmissionDecision,
    ConcurrencyThresholds,
    ConcurrencyUsage,
    decide_admission,
)
from .concurrency_snapshot import ConcurrencySnapshot
from .slot import SlotRejected, SlotRequest, SlotTicket

__all__ = [
    # enums
    "CommandClass", "RejectReason", "SlotOutcome",
    # slot
    "SlotRequest", "SlotTicket", "SlotRejected",
    # snapshot / policy
    "ConcurrencySnapshot", "ConcurrencyThresholds", "ConcurrencyUsage", "AdmissionDecision",
    "decide_admission",
    # notice
    "ReplyWindow",
    "decide_timeout_warning", "should_skip_status_prompt",
    "GET_IMAGE_TAG", "build_cmd_tag", "build_timeout_warning_text",
    "command_label", "extract_bnet_id",
    # image cache
    "DEFAULT_TTL", "MAX_PER_USER", "ImageCacheEntry",
    "append_entry", "coerce_entry", "filter_alive", "is_alive", "most_recent",
    "IMAGE_MAGIC_PREFIXES", "is_valid_image_head",
]
