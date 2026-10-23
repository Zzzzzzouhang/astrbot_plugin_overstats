"""群聊提示服务（service 层）：4:50 超时预警的编排与发送。

设计要点（针对线上 25 分钟内 105 次「msg_id 已过期」、11 次「被动回复超限」）：
1. v3 重构取消等待队列（超限即拒）后，「排队中」前置提示一并移除——
   没有排队就没有「排队中」；本服务只保留超时预警；
2. 预警计时起点为「收到用户指令」时刻，与 QQ 被动回复 msg_id 的
   5 分钟交互窗口对齐（4:50 发送，此时 msg_id 仍有效）；
3. 同义去重——已推送过含取图指引的提示时不再重复发送；
4. 战网 ID 解析带短 TTL 缓存：高峰期提示并发高，避免每次都抢文件锁读绑定表。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

from ..domain.concurrency_snapshot import ConcurrencySnapshot
from ..domain.notice import ReplyWindow, decide_timeout_warning
from ..domain.notice_text import (
    GET_IMAGE_TAG,
    build_cmd_tag,
    build_timeout_warning_text,
    extract_bnet_id,
)

logger = logging.getLogger("astrbot")

CONGESTION_RUNNING_THRESHOLD = 3   # 在执行指令数达到此值即视为服务繁忙（预警文案切换）


class ReplyNoticeService:
    """4:50 回复超时预警的发送编排。"""

    def __init__(
        self,
        platform: Any,
        *,
        window: ReplyWindow | None = None,
        send_timeout: float = 15.0,
        bnet_resolver: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
        bind_cache_ttl: float = 60.0,
        running_provider: Optional[Callable[[], ConcurrencySnapshot]] = None,
        congestion_threshold: int = CONGESTION_RUNNING_THRESHOLD,
    ):
        self._platform = platform
        self._window = window or ReplyWindow()
        self._send_timeout = send_timeout
        self._bnet_resolver = bnet_resolver
        self._bind_cache_ttl = bind_cache_ttl
        self._running_provider = running_provider
        self._congestion_threshold = congestion_threshold
        # user_id -> (bnet_id, 写入时刻)：高峰期提示并发高，缓存避免重复 IO
        self._bind_cache: Dict[str, Tuple[str, float]] = {}

    # ── 对外：注册预警任务 ────────────────────────────────────

    def schedule_timeout_warning(
        self,
        *,
        event: Any,
        command_text: str,
        started_at: float,
        done_event: asyncio.Event,
    ) -> "asyncio.Task | None":
        """注册 4:50 超时预警（计时起点为「收到用户指令」时刻）。"""
        try:
            return asyncio.ensure_future(
                self._maybe_warn_reply_timeout(event, command_text, started_at, done_event)
            )
        except Exception as e:  # 事件循环不可用等极端情况
            logger.error(f"超时预警任务创建失败: {e}")
            return None

    # ── 内部：发送 ────────────────────────────────────────────

    async def _maybe_warn_reply_timeout(
        self,
        event: Any,
        command_text: str,
        started_at: float,
        done_event: asyncio.Event,
    ) -> None:
        remaining = self._window.warn_delay(time.monotonic() - started_at)
        if remaining > 0:
            try:
                await asyncio.wait_for(done_event.wait(), timeout=remaining)
                return  # 指令已正常返回，无需预警
            except asyncio.TimeoutError:
                pass
        if not decide_timeout_warning(
            elapsed=time.monotonic() - started_at,
            window=self._window,
            already_notified=done_event.is_set(),
        ):
            return
        try:
            if not self._platform.is_qq_group_message(event):
                return
            running = 0
            if self._running_provider is not None:
                try:
                    running = int(self._running_provider().total_running)
                except Exception:
                    running = 0
            bnet_id = await self.resolve_bnet_id(event, command_text)
            text = build_timeout_warning_text(
                bnet_id=bnet_id,
                cmd_tag=build_cmd_tag(command_text),
                running=running,
                congested=running >= self._congestion_threshold,
                get_tag=GET_IMAGE_TAG,
            )
            await self._platform.send_notice(
                event, self._platform.format_text(event, text), timeout=self._send_timeout
            )
        except Exception as e:
            logger.error(f"回复超时预警发送失败: {e}")

    # ── 战网 ID 解析（带缓存）──────────────────────────────────

    async def resolve_bnet_id(self, event: Any, command_text: str = "") -> str:
        """提示文案用的战网 ID：优先取指令文本中的（无 IO），缺失时回退绑定 ID（带缓存）。"""
        try:
            inline = extract_bnet_id(command_text or "")
        except Exception:
            inline = ""
        if inline:
            return inline
        if self._bnet_resolver is None:
            return ""
        try:
            user_id = self._platform.sender_id(event)
        except Exception:
            return ""
        if not user_id:
            return ""
        now = time.monotonic()
        cached = self._bind_cache.get(user_id)
        if cached is not None:
            value, ts = cached
            if now - ts <= self._bind_cache_ttl:
                return value
        try:
            value = await self._bnet_resolver(user_id)
        except Exception:
            return ""
        value = value or ""
        self._bind_cache[user_id] = (value, now)
        return value

    def invalidate_bind_cache(self, user_id: str = "") -> None:
        """绑定关系变更（绑定 / 解绑）后调用，避免使用过期 ID。"""
        if user_id:
            self._bind_cache.pop(user_id, None)
        else:
            self._bind_cache.clear()

    # ── 供业务层查询 ──────────────────────────────────────────

    def elapsed(self, started_at: float) -> float:
        return time.monotonic() - started_at

    def window(self) -> ReplyWindow:
        return self._window
