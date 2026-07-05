"""Overstats 后端一键部署模块。

封装 Overstats 后端的自动化部署、生命周期管理、配置生成与卸载逻辑，
独立于 main.py，避免主插件文件臃肿。
"""
from .manager import DeployManager, DeployResult, DeployStatus
from .monitor import MonitorCollector, MonitorLogHandler, MonitorSSEQueue
from .backend_metrics import BackendMetricsReader
from .shiqu_log import ShiquCallReader
from .uninstaller import BackendUninstaller, UninstallResult

__all__ = [
    "DeployManager", "DeployResult", "DeployStatus",
    "MonitorCollector", "MonitorLogHandler", "MonitorSSEQueue",
    "BackendMetricsReader", "ShiquCallReader",
    "BackendUninstaller", "UninstallResult",
]
