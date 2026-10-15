"""群聊提示服务（service 层）：排队提示与超时预警的编排与发送。

设计要点（针对线上 25 分钟内 105 次「msg_id 已过期」、11 次「被动回复超限」）：
1. 告知动作前移——入队即推「排队中」提示，msg_id 仍然有效，送达率最高；
2. 同义去重——排队提示发送成功后取消 4:50 预警，避免同一用户收到两条；
3. 抑制注定失败的发送：已推过排队提示且窗口已过期时，跳过业务层状态提示
   （由 domain.should_skip_status_prompt 判定），减少失败与平台限流压力；
4. 战网 ID 解析带短 TTL 缓存：高峰期提示并发高，避免每次都抢文件锁读绑定表。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

from ..domain.notice import ReplyWindow, decide_timeout_warning
from ..domain.notice_text import (
    GET_IMAGE_TAG,
    build_cmd_tag,
    build_queue_waiting_text,
    build_timeout_warning_text,
    extract_bnet_id,
)
from ..domain.queue_snapshot import QueueSnapshot

logger = logging.getLogger("astrbot")

CONGESTION_WAITING_THRESHOLD = 3   # 排队人数达到此值即视为拥堵（预警文案切换）


class ReplyNoticeService:
    """「排队中」提示与 4:50 超时预警的发送编排。"""

    def __init__(
        self,
        platform: Any,
        *,
        window: ReplyWindow | None = None,
        send_timeout: float = 15.0,
        bnet_resolver: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
        bind_cache_ttl: float = 60.0,
        waiting_provider: Optional[Callable[[], QueueSnapshot]] = None,
        congestion_threshold: int = CONGESTION_WAITING_THRESHOLD,
    ):
        self._platform = platform
        self._window = window or ReplyWindow()
        self._send_timeout = send_timeout
        self._bnet_resolver = bnet_resolver
        self._bind_cache_ttl = bind_cache_ttl
        self._waiting_provider = waiting_provider
        self._congestion_threshold = congestion_threshold
        # user_id -> (bnet_id, 写入时刻)：高峰期提示并发高，缓存避免重复 IO
        self._bind_cache: Dict[str, Tuple[str, float]] = {}

    # ── 对外：注册提示任务 ────────────────────────────────────

    def schedule_queue_notice(
        self,
        *,
        event: Any,
        command_text: str,
        waiting: int,
        done_event: asyncio.Event,
    ) -> "asyncio.Task | None":
        """入队前推送「排队中」提示（后台任务，不阻塞入队）。"""
        try:
            return asyncio.ensure_future(
                self._send_queue_notice(event, command_text, waiting, done_event)
            )
        except Exception as e:  # 事件循环不可用等极端情况
            logger.error(f"排队中提示任务创建失败: {e}")
            return None

    def schedule_timeout_warning(
        self,
        *,
        event: Any,
        command_text: str,
        started_at: float,
        done_event: asyncio.Event,
    ) -> "asyncio.Task | None":
        """注册 4:50 超时预警（计时起点为「收到用户指令」时刻，含排队等待）。"""
        try:
            return asyncio.ensure_future(
                self._maybe_warn_reply_timeout(event, command_text, started_at, done_event)
            )
        except Exception as e:
            logger.error(f"超时预警任务创建失败: {e}")
            return None

    # ── 内部：发送 ────────────────────────────────────────────

    async def _send_queue_notice(
        self,
        event: Any,
        command_text: str,
        waiting: int,
        done_event: asyncio.Event,
    ) -> None:
        try:
            if done_event.is_set():
                return
            bnet_id = await self.resolve_bnet_id(event, command_text)
            text = build_queue_waiting_text(
                bnet_id=bnet_id,
                cmd_tag=build_cmd_tag(command_text),
                waiting=waiting,
                get_tag=GET_IMAGE_TAG,
            )
            # 紧邻发送前再确认一次：缩小「检查 → 实际发出」之间的竞态窗口
            if done_event.is_set():
                return
            await self._platform.send_notice(
                event, self._platform.format_text(event, text), timeout=self._send_timeout
            )
            done_event.set()  # 已含取图指引，取消 4:50 的同义预警
        except Exception as e:
            logger.error(f"排队中提示发送失败: {e}")

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
                return  # 指令已正常返回（或已推送排队提示），无需预警
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
            waiting = 0
            if self._waiting_provider is not None:
                try:
                    waiting = int(self._waiting_provider().waiting)
                except Exception:
                    waiting = 0
            bnet_id = await self.resolve_bnet_id(event, command_text)
            text = build_timeout_warning_text(
                bnet_id=bnet_id,
                cmd_tag=build_cmd_tag(command_text),
                waiting=waiting,
                congested=waiting >= self._congestion_threshold,
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
