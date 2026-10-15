"""AstrBot 平台适配器：PlatformPort 的 AstrBot 实现。

这是唯一允许依赖 astrbot 的 core 子模块（适配器职责），
domain / service 层均不直接依赖框架。
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.all import MessageChain

from .port import PlatformPort

_TAG_INPUT = re.compile(
    r'<qqbot-cmd-input\s+text="([^"]+)"\s+show="([^"]+)"\s+reference="false"\s*/>'
)
_TAG_ENTER = re.compile(r'<qqbot-cmd-enter\s+text="([^"]+)"\s*/>')


class AstrBotPlatformAdapter:
    """把 AstrBot 事件与消息链封装为统一的平台能力。"""

    # ── 场景判定 ──────────────────────────────────────────────

    def is_group_message(self, event: Any) -> bool:
        try:
            if getattr(event, "message_obj", None) and getattr(event.message_obj, "group_id", ""):
                return True
        except Exception:
            pass
        return "GROUP_MESSAGE" in str(getattr(event, "unified_msg_origin", "")).upper()

    def is_qq_official(self, event: Any) -> bool:
        try:
            pn = event.get_platform_name()
            if pn and "qq_official" in str(pn).lower():
                return True
        except Exception:
            pass
        umo = str(getattr(event, "unified_msg_origin", "")).lower()
        if "qq_official" in umo or "qqofficial" in umo:
            return True
        try:
            if getattr(event, "message_obj", None):
                s = str(event.message_obj).lower()
                if "qq_official" in s or "qqofficial" in s:
                    return True
        except Exception:
            pass
        return False

    def is_qq_group_message(self, event: Any) -> bool:
        if not self.is_group_message(event):
            return False
        try:
            platform_name = event.get_platform_name()
            if "qq" in str(platform_name).lower():
                return True
        except Exception:
            pass
        return "qq" in str(getattr(event, "unified_msg_origin", "")).lower()

    def sender_id(self, event: Any) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return ""

    # ── 文本格式化 ────────────────────────────────────────────

    def format_text(self, event: Any, text: str) -> str:
        """QQ 官方保留并对标签 urlencode；其他平台剥离标签转为纯文本。"""
        if self.is_qq_official(event):
            text = _TAG_INPUT.sub(
                lambda m: f'<qqbot-cmd-input text="{urllib.parse.quote(m.group(1))}" '
                          f'show="{urllib.parse.quote(m.group(2))}" reference="false" />',
                text,
            )
            text = _TAG_ENTER.sub(
                lambda m: f'<qqbot-cmd-enter text="{urllib.parse.quote(m.group(1))}" />', text
            )
            return text

        text = _TAG_INPUT.sub(lambda m: m.group(1).strip(), text)
        text = _TAG_ENTER.sub(lambda m: m.group(1).strip(), text)
        return text

    # ── 发送 ──────────────────────────────────────────────────

    def build_at_chain(self, event: Any, text: str) -> MessageChain:
        """构造 @发送者 + 提示文本的消息链。

        QQ 官方机器人需以 markdown 消息类型发送，方能渲染 **加粗** 与可点击指令标签；
        其他平台（onebot/QQNT 等）无 markdown 类型，用 Plain 发送（标签已被剥离）。
        """
        user_id = self.sender_id(event)
        if self.is_qq_official(event) and hasattr(Comp, "Markdown"):
            return MessageChain([Comp.At(qq=user_id), Comp.Markdown(text)])
        return MessageChain([Comp.At(qq=user_id), Comp.Plain(text)])

    async def send_notice(self, event: Any, text: str, *, timeout: float = 15.0) -> None:
        """以「被动回复」方式 @ 发送者发送提示。

        event.send 以原消息 msg_id 作回复目标，规避群内主动消息所需的 admin 权限；
        包一层超时，避免平台发送阻塞导致后台任务（及其捕获的 event）被长期持有。
        """
        await asyncio.wait_for(event.send(self.build_at_chain(event, text)), timeout=timeout)


# AstrBotPlatformAdapter 实现 PlatformPort 协议（无需继承，避免与 Protocol 的元类冲突）
