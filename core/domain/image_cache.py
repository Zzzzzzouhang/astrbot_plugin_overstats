"""图片缓存领域：条目结构与存活 / 裁剪判定（纯逻辑）。

文件是否存在的判定以 `exists` 回调注入，使本模块可在无 IO 环境下单测，
同时保持「空路径不得视为有效」的既有修复（Path('') 会解析为当前目录而恒为真）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

DEFAULT_TTL = 86400.0     # 图片本地缓存保留时长（24 小时）
MAX_PER_USER = 5          # 单用户最多保留张数


def _default_exists(path: str) -> bool:
    from pathlib import Path

    return bool(path) and Path(path).is_file()


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
