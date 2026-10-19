"""图片缓存领域：条目结构与存活 / 裁剪判定（纯逻辑）。

文件是否存在的判定以 `exists` 回调注入，使本模块可在无 IO 环境下单测，
同时保持「空路径不得视为有效」的既有修复（Path('') 会解析为当前目录而恒为真）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, TypeVar

T = TypeVar("T")

DEFAULT_TTL = 86400.0     # 图片本地缓存保留时长（24 小时）
MAX_PER_USER = 5          # 单用户最多保留张数


def _default_exists(path: str) -> bool:
    from pathlib import Path

    if not path:
        return False
    cached = Path(path)
    # 同时要求「是文件」且「非空」：0 字节残留文件（历史写入中断 / 并发覆盖
    # 竞态的产物）若被当成有效缓存补发，上传 QQ 富媒体接口会返回 None，
    # 触发 AstrBot 5 次×30s 重试风暴，故在存活判定源头直接排除。
    try:
        return cached.is_file() and cached.stat().st_size > 0
    except OSError:
        return False


@dataclass(frozen=True)
class ImageCacheEntry:
    path: str
    command: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {"path": self.path, "command": self.command, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: dict) -> "ImageCacheEntry":
        return cls(path=str(data.get("path") or ""), command=str(data.get("command") or ""), ts=float(data.get("ts") or 0.0))


def is_alive(
    entry: ImageCacheEntry,
    now: float,
    ttl: float = DEFAULT_TTL,
    exists: Callable[[str], bool] = _default_exists,
) -> bool:
    """条目是否有效：path 非空且指向真实文件、且在保留期内。"""
    if not entry.path:
        return False
    if now - entry.ts > ttl:
        return False
    try:
        return bool(exists(entry.path))
    except Exception:
        return False


def filter_alive(
    entries: Iterable[ImageCacheEntry],
    now: float,
    ttl: float = DEFAULT_TTL,
    exists: Callable[[str], bool] = _default_exists,
) -> List[ImageCacheEntry]:
    """过滤出仍可补发的条目（保持原有顺序）。"""
    return [e for e in entries if is_alive(e, now, ttl, exists)]


def append_entry(
    entries: List[ImageCacheEntry],
    entry: ImageCacheEntry,
    now: float,
    ttl: float = DEFAULT_TTL,
    max_keep: int = MAX_PER_USER,
) -> List[ImageCacheEntry]:
    """追加新条目并裁剪：先过滤过期项，再仅保留最近 max_keep 条。"""
    merged = list(entries) + [entry]
    alive = filter_alive(merged, now, ttl, exists=lambda p: True)  # 写入时不校验文件，仅按时间裁剪
    return alive[-max_keep:] if max_keep > 0 else alive


def coerce_entry(data: object) -> Optional[ImageCacheEntry]:
    """把索引中的原始 dict 转为条目；畸形数据返回 None。"""
    if not isinstance(data, dict):
        return None
    path = data.get("path")
    if not path:
        return None
    return ImageCacheEntry.from_dict(data)


def most_recent(items: List[T], n: int) -> List[T]:
    """按「生成时间升序」（旧→新）的列表中取最近 n 条（末尾 n 条）；n<=0 返回空。

    供「获取图片」补发使用：只发送最近 n 张、其余保留可再次取回。锁定「取最新」方向，
    避免误用 items[:n] 取到最旧的 n 张（旧图往往用户已看过，真正想取回的是刚超时的那几张）。
    """
    if n <= 0:
        return []
    return list(items)[-n:]
