"""对局英雄上下文：记住用户最近一次「大神对局 <英雄>」的筛选。

用途：先发 `大神对局 源氏`，随后 5 分钟内直接发数字 `4`，
按「源氏第 4 场」处理；超过 TTL，或再次发送不带英雄的 `大神对局`，即失效。

内存约束（长期运行不能无限堆积）：
- 纯内存 + TTL，不落盘；
- 条目数上限，达到上限先清过期、再淘汰最早到期的一条；
- 使用 monotonic 时钟，避免系统时间被调整导致条目提前/延后失效；
- 插件卸载时由调用方 clear()。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 512


@dataclass
class _Entry:
    hero: str
    expires_at: float


class MatchHeroContextService:
    """按用户记住「最近一次对局筛选用的英雄」，带 TTL 与容量上限。"""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = max(1.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock or time.monotonic
        self._entries: dict[str, _Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, key: str, hero: str) -> None:
        """记录（或覆盖）该用户的英雄筛选，TTL 从现在开始重新计时。"""
        if not key or not hero:
            return
        now = self._clock()
        if len(self._entries) >= self._max_entries:
            self._prune(now)
            if len(self._entries) >= self._max_entries:
                self._evict_soonest()
        self._entries[key] = _Entry(hero=hero, expires_at=now + self._ttl)

    def get(self, key: str) -> str:
        """取未过期的英雄；过期或不存在返回空串（顺手删除过期条目）。"""
        if not key:
            return ""
        entry = self._entries.get(key)
        if entry is None:
            return ""
        if entry.expires_at <= self._clock():
            self._entries.pop(key, None)
            return ""
        return entry.hero

    def forget(self, key: str) -> None:
        """清除该用户的英雄筛选（无英雄的对局查询、显式切换时调用）。"""
        if key:
            self._entries.pop(key, None)

    def clear(self) -> None:
        """清空全部条目（插件卸载时调用，释放内存）。"""
        self._entries.clear()

    def prune(self) -> int:
        """主动清理过期条目，返回清理数量。"""
        return self._prune(self._clock())

    def _prune(self, now: float) -> int:
        stale = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in stale:
            self._entries.pop(key, None)
        return len(stale)

    def _evict_soonest(self) -> None:
        """容量兜底：淘汰最早到期的一条，保证内存上限。"""
        if not self._entries:
            return
        victim = min(self._entries, key=lambda key: self._entries[key].expires_at)
        self._entries.pop(victim, None)
