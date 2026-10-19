"""图片缓存服务（service 层）：24 小时本地缓存索引的读写与清理。

性能优化（针对高峰期每条指令 2 次全量 JSON 读写的热点）：
- 索引以内存为权威：record 只改内存并标脏，避免每条指令一次 read + 一次 write；
- 后台任务按 flush_interval 合并落盘（默认 1s），把 N 次写合并为 1 次；
- 仍保持原子写（tmp + replace），避免落盘中断损坏索引；
- 进程启动时从磁盘加载，崩溃最多丢失最近一次 flush 间隔内的索引条目
  （图片文件本身不受影响）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..domain.image_cache import (
    DEFAULT_TTL,
    MAX_PER_USER,
    ImageCacheEntry,
    append_entry,
    coerce_entry,
    filter_alive,
)

logger = logging.getLogger("astrbot")


class ImageCacheService:
    """用户最近生成图片的本地缓存索引（供群聊回复超时后 @机器人 取回）。"""

    def __init__(
        self,
        index_path: Path,
        *,
        ttl: float = DEFAULT_TTL,
        max_per_user: int = MAX_PER_USER,
        flush_interval: float = 1.0,
    ):
        self._path = Path(index_path)
        self._ttl = ttl
        self._max_per_user = max_per_user
        self._flush_interval = flush_interval
        self._lock = threading.Lock()
        self._index: Dict[str, List[ImageCacheEntry]] = {}
        self._dirty = False
        self._loaded = False
        self._task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────────

    def load(self) -> None:
        """从磁盘加载索引（进程启动时调用一次）。"""
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for user_key, entries in raw.items():
                        if not isinstance(entries, list):
                            continue
                        parsed = [e for e in (coerce_entry(x) for x in entries) if e is not None]
                        if parsed:
                            self._index[user_key] = parsed
        except Exception as e:
            logger.warning(f"读取图片缓存索引失败（按空索引继续）: {e}")
            self._index = {}

    async def start(self) -> None:
        """启动后台合并落盘任务。"""
        self.load()
        if self._task is None:
            try:
                self._task = asyncio.ensure_future(self._flush_loop())
            except Exception as e:
                logger.warning(f"图片缓存落盘任务启动失败: {e}")

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                try:
                    self.flush()
                except Exception as e:
                    logger.error(f"图片缓存索引落盘失败: {e}")
        except asyncio.CancelledError:
            self.flush()
            return

    # ── 读写 ──────────────────────────────────────────────────

    def record(self, user_key: str, path: str, command: str = "") -> None:
        """记录一条新生成的图片（内存中追加并裁剪，标记待落盘）。"""
        entry = ImageCacheEntry(path=str(path), command=command, ts=time.time())
        with self._lock:
            self._load_locked()
            entries = self._index.get(user_key, [])
            trimmed = append_entry(entries, entry, entry.ts, self._ttl, self._max_per_user)
            self._index[user_key] = trimmed
            self._dirty = True

    def list_alive(self, user_key: str) -> List[ImageCacheEntry]:
        """取回该用户仍在保留期内且文件真实存在的条目（按生成时间升序）。"""
        with self._lock:
            self._load_locked()
            entries = self._index.get(user_key, [])
            return filter_alive(entries, time.time(), self._ttl)

    def prune_user(self, user_key: str) -> None:
        """清理该用户过期 / 文件缺失的条目。"""
        with self._lock:
            self._load_locked()
            entries = self._index.get(user_key)
            if not entries:
                return
            alive = filter_alive(entries, time.time(), self._ttl)
            if alive:
                self._index[user_key] = alive
            else:
                self._index.pop(user_key, None)
            self._dirty = True

    def purge_user(self, user_key: str) -> None:
        self.prune_user(user_key)

    def prune_all(self, now_ts: float | None = None) -> int:
        """清理全部过期条目（每日定时调用），返回被移除的条目数。"""
        now = now_ts if now_ts is not None else time.time()
        removed = 0
        with self._lock:
            self._load_locked()
            for user_key in list(self._index.keys()):
                entries = self._index[user_key]
                alive = filter_alive(entries, now, self._ttl)
                removed += len(entries) - len(alive)
                if alive:
                    self._index[user_key] = alive
                else:
                    self._index.pop(user_key, None)
            if removed:
                self._dirty = True
        self.flush()
        return removed

    def flush(self) -> None:
        """把内存索引原子写入磁盘（脏时才写）。"""
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            payload = {k: [e.to_dict() for e in v] for k, v in self._index.items() if v}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as e:
            # 写盘失败时把脏标记置回：否则在下次 record() 之前，内存里这批
            # （可能尚未落盘过的）条目会一直不被重试，进程崩溃即丢失。
            with self._lock:
                self._dirty = True
            logger.error(f"写入图片缓存索引失败: {e}")
