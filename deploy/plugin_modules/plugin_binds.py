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
    from ...deploy import DeployManager
except ImportError:
    # 兜底：插件作为顶层模块加载时相对导入可能失败
    from deploy import DeployManager  # type: ignore[no-redef]

# OW 开庭模块（独立封装，避免主文件臃肿）
try:
    from ...deploy.ow.court import CourtManager
    from ...deploy.ow.shiqu import ShiquManager
except ImportError:
    from deploy.ow.court import CourtManager  # type: ignore[no-redef]
    from deploy.ow.shiqu import ShiquManager  # type: ignore[no-redef]

# 监控模块
try:
    from ...deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader
except ImportError:
    from deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader  # type: ignore[no-redef]

# 是区吗调用日志读取
try:
    from ...deploy.ow.shiqu_log import ShiquCallReader
except ImportError:
    from deploy.ow.shiqu_log import ShiquCallReader  # type: ignore[no-redef]

# Web API helpers
from astrbot.api.web import request, json_response, error_response, stream_response

logger = logging.getLogger("astrbot")

class PluginBinds:

    def _get_binds_file_path(self) -> Path:
        bot_id = "default"
        try:
            if hasattr(self.context, "get_config"):
                bot_identity = self.context.get_config().active_bot_identity
                if bot_identity:
                    bot_id = str(bot_identity)
        except Exception:
            pass
        
        file_path = self.plugin_data_dir / f"binds_{bot_id}.json"
        
        if not file_path.exists():
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=4)
            except Exception as e:
                logger.error(f"创建机器人 [{bot_id}] 的绑定文件失败: {e}")
        return file_path

    def _load_binds(self) -> dict:
        try:
            file_path = self._get_binds_file_path()
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"读取绑定明文文件失败: {e}")
        return {}

    def _save_binds(self, data: dict):
        try:
            file_path = self._get_binds_file_path()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存绑定明文文件失败: {e}")

    async def _get_user_bind_id(self, user_id: str) -> str | None:
        async with self.file_lock:
            binds = self._load_binds()
            user_key = str(user_id)
            if user_key in binds:
                return binds[user_key]
            try:
                old_kv_key = f"bind_{user_id}"
                old_bnet_id = await self.get_kv_data(old_kv_key, None)
                if old_bnet_id:
                    binds[user_key] = str(old_bnet_id)
                    self._save_binds(binds)
                    return old_bnet_id
            except Exception as e:
                logger.error(f"迁移数据失败: {e}")
            return None

    async def _set_user_bind_id(self, user_id: str, bnet_id: str):
        async with self.file_lock:
            binds = self._load_binds()
            binds[str(user_id)] = bnet_id
            self._save_binds(binds)
            try:
                old_kv_key = f"bind_{user_id}"
                await self.delete_kv_data(old_kv_key)
            except Exception:
                pass

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str = "") -> str:
        if input_id and input_id.strip():
            clean_id = input_id.strip().lstrip("@")
            # 去掉机器人昵称前缀（全量适配 @botname 时会拼到参数前）
            nickname = self._get_bot_nickname(event)
            if nickname and clean_id.lower().startswith(nickname.lower()):
                clean_id = clean_id[len(nickname):]
            # 战网ID格式校验：必须包含 # 或 ＃
            if "#" not in clean_id and "＃" not in clean_id:
                return None
            return clean_id
        user_id = event.get_sender_id()
        bind_id = await self._get_user_bind_id(user_id)
        return bind_id

    def _parse_treemap_args(self, arg1: str = "", arg2: str = "") -> tuple[str | None, str | None]:
        bnet_id = None
        season = None
        if arg1 and arg2:
            bnet_id = arg1
            season = arg2
        elif arg1:
            if arg1.isdigit():
                season = arg1
            else:
                bnet_id = arg1
        return bnet_id, season

    def _parse_profile_args(self, arg1: str = "", arg2: str = "") -> tuple[str | None, str]:
        bnet_id = None
        mode = "quick"
        for arg in [arg1, arg2]:
            if not arg:
                continue
            if arg in ["快速", "quick"]:
                mode = "quick"
            elif arg in ["竞技", "competitive"]:
                mode = "competitive"
            else:
                bnet_id = arg
        return bnet_id, mode

    def _extract_keywords(self, args) -> tuple[list[str], dict]:
        """从位置参数中识别中文关键词，返回 (剩下的位置参数, 命中的字段字典)。
        关键词可无序、可省略、可混用；非关键词一律保留为位置参数。"""
        positional, fields = [], {}
        for a in args:
            if a is None:
                continue
            a = str(a).strip()
            if not a:
                continue
            hit = self._KEYWORD_MAP.get(a)
            if hit:
                fields.update(hit)
            else:
                positional.append(a)
        return positional, fields

    @filter.command("大神绑定", alias={'绑定'})
    async def dashen_bind(self, event: AstrMessageEvent, bnet_id: str):
        """绑定战网账号，格式：/绑定 Player#12345。"""
        CMD = "绑定"
        user_id = event.get_sender_id()
        
        new_bind_id = bnet_id.strip().lstrip("@")
        # 去掉机器人昵称前缀（全量适配 @botname 时会拼到参数前）
        nickname = self._get_bot_nickname(event)
        if nickname and new_bind_id.lower().startswith(nickname.lower()):
            new_bind_id = new_bind_id[len(nickname):]
        
        if not new_bind_id or ("#" not in new_bind_id and "＃" not in new_bind_id):
            yield self._plain_error_result(event, "❌ 绑定失败！请输入规范战网 ID，严格区分大小写\n格式：/绑定 战网ID，示例：/绑定 Player#12345")
            if self.monitor:
                asyncio.ensure_future(self.monitor.record_command(CMD, False))
            return
        
        old_bind_id = await self._get_user_bind_id(user_id)
        await self._set_user_bind_id(user_id, new_bind_id)
        
        if not old_bind_id:
            yield event.plain_result(f"✅ 绑定成功！关联战网账号【{new_bind_id}】")
        else:
            yield event.plain_result(f"✅ 更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】")
