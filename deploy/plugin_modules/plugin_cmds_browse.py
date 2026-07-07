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

class PluginCmdsBrowse:

    @filter.command("商店", alias={'ow商店'})
    async def ow_shop(self, event: AstrMessageEvent):
        """拉取今日精选商店在售皮肤商品。"""
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🛍️ 正在获取今日精选商店皮肤商品...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-shop/image")
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "商店"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取精选商店图片失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="商店", error_code=err_code)

    @filter.command("ow赛事", alias={'赛事'})
    async def ow_esports(self, event: AstrMessageEvent):
        """获取实时职业赛事对阵及赛程信息。"""
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🎮 正在从 Pandascore 获取实时赛事对阵...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-esports/image")
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "ow赛事"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="ow赛事", error_code=err_code)

    @filter.command("获取段位分布")
    async def get_rank_distribution(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """统计天梯全服大盘全英雄数据排行与天梯环境分布（可选模式/段位）。"""
        # /获取段位分布        /获取段位分布 快速       /获取段位分布 快速 大师       /获取段位分布 大师
        _positional, kw = self._extract_keywords([arg1, arg2])
        game_mode = kw.get("game_mode", "competitive")
        mmr = kw.get("mmr", "all")
        mode_label = "快速" if game_mode == "quick" else "竞技"

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"📊 正在统计 {mode_label}（{mmr}）天梯全服大盘全英雄数据排行与环境分布，可用参数：[模式] [段位]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"view": "ranking", "game_mode": game_mode, "mmr": mmr}

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-hero-pick-rate/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "获取段位分布"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "无法获取全服天梯分布排行。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="获取段位分布", error_code=err_code)

    @filter.command("ow活动", alias={'活动'})
    async def ow_activities(self, event: AstrMessageEvent):
        """拉取当前版本限时节日或赛季大活动公告卡片。"""
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🎉 正在拉取当前版本限时节日/赛季大活动公告卡片...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/patch-notes/image", {"patch_kind": "big"})
            if not img_bytes:
                img_bytes, error_data, err_code = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "ow活动"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "暂无正在进行的版本活动公告。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="ow活动", error_code=err_code)

    @filter.command("banpick", alias={'全英雄排行'})
    async def ban_pick_stats(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """获取本周天梯英雄大盘的选禁用排行（可选模式/段位）。"""
        # /banpick        /banpick 快速       /banpick 快速 黄金       /banpick 黄金
        _positional, kw = self._extract_keywords([arg1, arg2])
        game_mode = kw.get("game_mode", "competitive")
        mmr = kw.get("mmr", "all")
        mode_label = "快速" if game_mode == "quick" else "竞技"

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"🚫 正在获取 {mode_label}（{mmr}）本周天梯英雄大盘选禁用排行，可用参数：[模式] [段位]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"view": "ranking", "game_mode": game_mode, "mmr": mmr}

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-hero-pick-rate/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "banpick"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "无法获取全英雄排行。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="banpick", error_code=err_code)

    @filter.command("mappick")
    async def map_pick_stats(self, event: AstrMessageEvent):
        """从最新补丁中检索当前赛季地图池与轮换出场情况。"""
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🗺️ 正在从最新版本补丁中检索当前赛季地图池与轮换出场...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "mappick"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "无法拉取最新地图池分布。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="mappick", error_code=err_code)

    @filter.command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str = ""):
        """检索包含指定关键词的精选上架皮肤商品卡片。"""
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🔍 正在检索包含关键词【{keyword or '最新'}】的精选上架皮肤商品卡片，可用参数：[关键词]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-shop/image")
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "皮肤搜索"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "无法获取精选皮肤卡片。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="皮肤搜索", error_code=err_code)

    @filter.command("ow更新", alias={'版本更新'})
    async def ow_patch_notes(self, event: AstrMessageEvent, kind: str = "latest"):
        """拉取外服更新日志卡片（参数：latest / small / big）。"""
        valid_kinds = ["latest", "small", "big"]
        if kind not in valid_kinds:
            yield self._plain_error_result(event, "❌ 参数错误。支持的日志类型：latest, small, big\n例如：/ow更新 small")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📰 正在拉取外服 {kind} 更新日志卡片，可用参数：[类型]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/patch-notes/image", {"patch_kind": kind})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "ow更新"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取更新日志失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="ow更新", error_code=err_code)
