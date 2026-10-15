"""platform 层：平台差异适配（QQ 官方 markdown / @ 消息链 / 被动回复）。"""

from .port import PlatformPort

__all__ = ["PlatformPort"]

# AstrBot 适配器依赖 astrbot 运行时，仅在插件环境可导入，故不做顶层导入。
