"""图片缓存领域：条目结构与存活 / 裁剪判定（纯逻辑）。

文件是否存在的判定以 `exists` 回调注入，使本模块可在无 IO 环境下单测，
同时保持「空路径不得视为有效」的既有修复（Path('') 会解析为当前目录而恒为真）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, TypeVar

T = TypeVar("T")

DEFAULT_TTL = 86400.0     # 图片本地缓存保留时长（24 小时）
MAX_PER_USER = 5          # 单用户最多保留张数

# 合法图片文件头魔数（与 QQ 富媒体支持的格式对齐）。
# 单一数据源：main.py 的 _validate_image_bytes / _is_valid_cached_image_file
# 与 domain 的存活判定共用，避免两套口径漂移。
IMAGE_MAGIC_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",        # JPEG
    b"GIF87a",              # GIF
    b"GIF89a",              # GIF
    b"BM",                  # BMP
    b"RIFF",                # WebP 容器（另校验 WEBP 标记）
)


def is_valid_image_head(head: bytes) -> bool:
    """判断文件头 bytes 是否为合法图片格式（纯函数，无 IO）。

    RIFF 容器需进一步校验 offset 8..12 为 WEBP（否则可能是 AVI/WAV 等）。
    """
    if not head:
        return False
    if head[:4] == b"RIFF":
        return head[8:12] == b"WEBP"
    return any(head.startswith(prefix) for prefix in IMAGE_MAGIC_PREFIXES)


def _default_exists(path: str) -> bool:
    """缓存文件有效判定：是文件 + 非空 + 文件头是合法图片格式。

    0 字节残留文件（写入中断 / 并发覆盖竞态的产物）与历史 bug 期
    「200+JSON 错误体当 .png 落盘」的非空损坏文件，都会被当成有效缓存
    补发，上传 QQ 富媒体接口后返回 None，触发 AstrBot 5 次×30s 重试风暴。
    在存活判定源头统一排除，list_alive / prune_user / prune_all 口径一致，
    损坏条目在下次 prune 时真正被清除（而非靠 24h TTL 自然过期）。
    """
    if not path:
        return False
    cached = Path(path)
    try:
        if not cached.is_file() or cached.stat().st_size <= 0:
            return False
        with open(cached, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return is_valid_image_head(head)


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
    """条目是否有效：path 非空、文件是合法图片（存在且非空且文件头合法）、且在保留期内。"""
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
