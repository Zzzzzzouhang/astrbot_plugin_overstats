"""Overstats 插件核心分层包：domain（纯逻辑）/ service（编排）/ platform（平台适配）。

分层目的：把「排队与提示」从 main.py 单体与 deploy/ops 杂项中剥离，
使排队规则、提示时机、文案构造成为可独立单测的纯逻辑，
同时把平台的差异（QQ 官方 markdown / @ 消息链）收敛到 platform 适配器。

导入约定（与插件其余模块一致的双路径）：
    try:
        from .core.service import CommandQueueService
    except ImportError:
        from core.service import CommandQueueService
"""

from . import domain, platform, service  # noqa: F401  平台层适配器需显式导入（依赖 astrbot）

__all__ = ["domain", "service", "platform"]
