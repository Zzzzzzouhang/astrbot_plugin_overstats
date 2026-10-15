"""平台端口：把「平台差异」抽象为协议，使 service 层不依赖 astrbot。

新增平台（如 OneBot / 微信）时只需实现本协议，排队与提示逻辑无需改动。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PlatformPort(Protocol):
    """平台能力端口。"""

    def is_group_message(self, event: Any) -> bool:
        """是否群聊消息。"""
        ...

    def is_qq_official(self, event: Any) -> bool:
        """是否 QQ 官方机器人平台（决定 markdown 标签与消息链形态）。"""
        ...

    def is_qq_group_message(self, event: Any) -> bool:
        """是否 QQ 平台群聊消息（提示与图片缓存仅在群聊场景启用）。"""
        ...

    def sender_id(self, event: Any) -> str:
        """发送者 ID（用于 @ 与用户级缓存键）。"""
        ...

    def format_text(self, event: Any, text: str) -> str:
        """按平台处理文本：QQ 官方保留并编码 <qqbot-cmd-input> 标签，其余平台剥离标签。"""
        ...



    async def send_notice(self, event: Any, text: str, *, timeout: float = 15.0) -> None:
        """以「被动回复」方式 @ 发送者发送提示（内部保证不超过 timeout 秒）。"""
        ...
