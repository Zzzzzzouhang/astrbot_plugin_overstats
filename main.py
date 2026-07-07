import os
import aiohttp
import logging
import tempfile
import asyncio
import re
import base64
import json
import time
import inspect
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.event import filter, AstrMessageEvent  # 引入原生消息与事件过滤模块

# 一键部署模块（独立于 main.py，避免主文件臃肿）
try:
    from .deploy import DeployManager
except ImportError:
    # 兜底：插件作为顶层模块加载时相对导入可能失败
    from deploy import DeployManager  # type: ignore[no-redef]

# OW 开庭模块（独立封装，避免主文件臃肿）
try:
    from .deploy.ow.court import CourtManager
    from .deploy.ow.shiqu import ShiquManager
except ImportError:
    from deploy.ow.court import CourtManager  # type: ignore[no-redef]
    from deploy.ow.shiqu import ShiquManager  # type: ignore[no-redef]

# 监控模块
try:
    from .deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader
except ImportError:
    from deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader  # type: ignore[no-redef]

# 是区吗调用日志读取
try:
    from .deploy.ow.shiqu_log import ShiquCallReader
except ImportError:
    from deploy.ow.shiqu_log import ShiquCallReader  # type: ignore[no-redef]

# Web API helpers
from astrbot.api.web import request, json_response, error_response, stream_response

logger = logging.getLogger("astrbot")

# 指令实现按职责拆分到 deploy/plugin_modules/ 下的各 mixin 模块，运行时通过多继承合并为 OverstatsPlugin
try:
    from .deploy.plugin_modules.plugin_core import PluginCore
    from .deploy.plugin_modules.plugin_binds import PluginBinds
    from .deploy.plugin_modules.plugin_events import PluginEvents
    from .deploy.plugin_modules.plugin_cmds_query import PluginCmdsQuery
    from .deploy.plugin_modules.plugin_cmds_browse import PluginCmdsBrowse
    from .deploy.plugin_modules.plugin_cmds_admin import PluginCmdsAdmin
except ImportError:  # 插件作为顶层模块加载时相对导入可能失败
    from deploy.plugin_modules.plugin_core import PluginCore          # type: ignore[no-redef]
    from deploy.plugin_modules.plugin_binds import PluginBinds         # type: ignore[no-redef]
    from deploy.plugin_modules.plugin_events import PluginEvents        # type: ignore[no-redef]
    from deploy.plugin_modules.plugin_cmds_query import PluginCmdsQuery  # type: ignore[no-redef]
    from deploy.plugin_modules.plugin_cmds_browse import PluginCmdsBrowse # type: ignore[no-redef]
    from deploy.plugin_modules.plugin_cmds_admin import PluginCmdsAdmin   # type: ignore[no-redef]


@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "2.6.5")
class OverstatsPlugin(Star, PluginCore, PluginBinds, PluginEvents, PluginCmdsQuery, PluginCmdsBrowse, PluginCmdsAdmin):
    """Overstats 全指令插件。

    实现分散在 plugin_*.py 的 mixin 中，本类仅做聚合，便于维护。
    """
    pass
