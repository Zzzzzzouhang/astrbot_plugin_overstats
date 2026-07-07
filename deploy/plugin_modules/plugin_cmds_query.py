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

class PluginCmdsQuery:

    @filter.command("今日总结", alias={'今日', '今日数据'})
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = ""):
        """生成过去 24 小时内的对局大数据总结卡片。"""
        CMD = "今日总结"
        # ── 违规封禁检查 ──
        banned, ban_remain = await self._check_violation_ban(event, CMD)
        if banned:
            yield event.plain_result(self._VIOLATION_BAN_MSG.format(command=CMD, remain=self._violation_ban_remain_str(ban_remain)))
            return

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("今日总结"))
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"⏳ 正在计算 {target_id} 的今日战绩总结，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-summary/today/image", {"bnet_id": target_id})

            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "今日总结"):
                    yield r
            elif error_data and error_data.get("error") == "summary_empty" and error_data.get("details", {}).get("scope") == "today":
                yield event.plain_result(f"ℹ️ {target_id} 在过去的 24 小时内没有对局记录，尝试生成昨日总结...")
                img_bytes, error_data, err_code = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
                if img_bytes:
                    success = True
                    async for r in self._send_image_result(event, img_bytes, "今日总结"):
                        yield r
                else:
                    err_msg = error_data.get("message") if error_data else "获取昨日总结失败，可能昨日没有对局记录。"
                    if err_msg and "Could not resolve customerToken" in err_msg:
                        yield self._plain_error_result(event, self._id_resolve_err("获取昨日总结失败"))
                    elif error_data and error_data.get("error") == "summary_empty":
                        yield self._plain_error_result(event, f"❌ {target_id} 在过去 48 小时内没有对局记录")
                    else:
                        yield self._plain_error_result(event, f"❌ {err_msg}")
            else:
                err_msg = error_data.get("message", "未知错误") if error_data else "未知错误"
                if "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取今日总结失败"))
                else:
                    yield self._plain_error_result(event, f"❌ 获取今日总结失败：{err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="今日总结", error_code=err_code)

    @filter.command("昨日总结", alias={'昨日', '昨日数据', '昨天数据'})
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = "", _skip_status_prompt: bool = False):
        """统计并生成昨日战绩数据卡片。"""
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("昨日总结"))
            return
        prompt_token = None
        _maintenance_stop = False
        if _skip_status_prompt:
            status_text = None
        else:
            status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"⏳ 正在统计 {target_id} 的昨日战绩数据，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "昨日总结"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取昨日总结失败，可能昨日未登录游戏。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取昨日总结失败"))
                elif error_data and error_data.get("error") == "summary_empty":
                    yield self._plain_error_result(event, f"❌ {target_id} 在昨日没有对局记录")
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="昨日总结", error_code=err_code)

    @filter.command("周度总结", alias={'本周总结', '本周数据', '本周'})
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = ""):
        """统计本周战绩大数据总结，耗时较长（约 30-60 秒）。"""
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("周度总结"))
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📊 正在生成 {target_id} 的本周战绩大数据总结，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-summary/week/image", {"bnet_id": target_id}, timeout=900)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "周度总结"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取周度总结失败，请检查服务日志或是否请求超时。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取周度总结失败"))
                elif error_data and error_data.get("error") == "summary_empty" and error_data.get("details", {}).get("scope") == "week":
                    yield self._plain_error_result(event, f"❌ {target_id} 在过去 7 天内没有对局记录")
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="周度总结", error_code=err_code)

    @filter.command("大神数据", alias={'详情卡片', '战绩查询', '数据'})
    async def dashen_profile(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """查看玩家详情卡片（支持 快速/竞技 模式）。"""
        bnet_id, mode = self._parse_profile_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("大神数据"))
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🔍 正在生成 {target_id} 的玩家详情，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-profile/image", {"bnet_id": target_id, "mode": mode})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "大神数据"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取玩家详情卡片失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取玩家详情失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="大神数据", error_code=err_code)

    @filter.command("大神对局", alias={'最近对局', '战绩', '对局'})
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = ""):
        """拉取最近 20 局的对局列表。"""
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("大神对局"))
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📊 正在拉取 {target_id} 的最近对局，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-match/image", {"bnet_id": target_id})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "大神对局"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取最近对局列表失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取最近对局失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="大神对局", error_code=err_code)

    @filter.command("单局详细", alias={'单局', '单局详情'})
    async def dashen_match_detail(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        """查看指定序号的单局多图详细战绩（可加 锐评关/全员关 控制开关）。"""
        CMD = "单局详细"
        # ── 违规封禁检查 ──
        banned, ban_remain = await self._check_violation_ban(event, CMD)
        if banned:
            yield event.plain_result(self._VIOLATION_BAN_MSG.format(command=CMD, remain=self._violation_ban_remain_str(ban_remain)))
            return

        # 先识别中文关键词：锐评关 / 全员关 / 锐评（默认即锐评，可显式给）
        positional, kw = self._extract_keywords([arg1, arg2, arg3])
        index = 0
        bnet_id = None

        for arg in positional:
            if arg.isdigit():
                digit = int(arg)
                if digit > 20:
                    yield self._plain_error_result(event, "❌ 错误：单局详细的数字索引不能大于 20！")
                    return
                index = max(0, digit - 1) if digit > 0 else 0
            else:
                bnet_id = arg

        if not bnet_id:
            # 使用用户绑定的战网ID
            user_id = event.get_sender_id()
            bnet_id = await self._get_user_bind_id(user_id)

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/单局详细 1 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return

        # 提示文案：关闭锐评时告知用户本次更快
        base_prompt = f"⏳ 正在拉取 {target_id} 第 {index + 1} 局的单局多图详细战绩，可用参数：<序号> [战网ID]"
        if kw.get("analyze") is False:
            base_prompt += "（本次已跳过 AI 锐评，出图更快）"
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, base_prompt)
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {
            "bnet_id": target_id,
            "index": str(index),
            "limit": "20",
            "include_fight": True,
            "include_previous_season": True,
            # 默认开启，命中「全员关」「锐评关」时关闭
            "show_all_heroes": kw.get("show_all_heroes", True),
            "analyze": kw.get("analyze", True)
        }
        
        url = f"{self.base_url}/dashen-match/detail/replies"

        success = False
        err_code = ""
        try:
            session = await self._get_http_session()
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    try:
                        error_data = await resp.json()
                        err_msg = error_data.get("message", "未知后端 service 错误")
                        logger.error(f"获取单局详细失败 HTTP {resp.status}: {err_msg} | bnet={target_id} index={index}")
                        yield self._plain_error_result(event, f"❌ 获取单局详细失败：{err_msg}")
                    except Exception:
                        logger.error(f"获取单局详细失败 HTTP {resp.status}: 无法解析错误响应 | bnet={target_id} index={index}")
                        yield self._plain_error_result(event, f"❌ 后端接口响应异常，状态码: {resp.status}")
                    return

                data = await resp.json()
                raw_img_list = data.get("replies", [])
                
                if not raw_img_list:
                    logger.error(f"获取单局详细: replies 为空 | bnet={target_id} index={index}")
                    yield self._plain_error_result(event, "❌ 未能生成该单局的详细图片链接")
                    return

                # 收集所有图片数据，最后用多图消息链一次性发送
                collected_images: list[bytes] = []
                for u in raw_img_list:
                    img_str = ""
                    if isinstance(u, dict):
                        if u.get("type") == "image":
                            img_str = str(u.get("base64", "")).strip()
                        if not img_str:
                            for key in ["url", "image", "src", "path", "file"]:
                                if u.get(key):
                                    img_str = str(u.get(key)).strip()
                                    break
                    else:
                        img_str = str(u).strip()

                    if not img_str:
                        continue

                    img_data = None
                    try:
                        if img_str.startswith("base64://"):
                            img_data = base64.b64decode(img_str.replace("base64://", ""))
                        elif img_str.startswith("data:image") and "base64," in img_str:
                            img_data = base64.b64decode(img_str.split("base64,")[1])
                        elif len(img_str) > 100 and not img_str.startswith("http") and not img_str.startswith("/"):
                            padding = len(img_str) % 4
                            if padding: img_str += '=' * (4 - padding)
                            try: img_data = base64.b64decode(img_str)
                            except Exception: pass
                        
                        if not img_data:
                            full_img_url = img_str if img_str.startswith("http") else f"{self.base_url.rstrip('/').removesuffix('/api/v2')}{img_str if img_str.startswith('/') else '/' + img_str}"
                            async with session.get(full_img_url, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as img_resp:
                                if img_resp.status == 200:
                                    img_data = await img_resp.read()
                    except Exception as e:
                        logger.error(f"处理图片失败：{e}")
                        continue

                    if img_data:
                        collected_images.append(img_data)

                if collected_images:
                    success = True
                    async for r in self._send_multiple_images_result(event, collected_images, CMD):
                        yield r
        except Exception as e:
            logger.error(f"处理单局详细图片异常：{e}")
            yield self._plain_error_result(event, "❌ 处理图片请求时发生 system 错误")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="单局详细", error_code=err_code)

    @filter.command("ow开庭", alias={'开庭'})
    async def ow_court(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """OW 开庭：AI 对单局数据进行电竞法庭风格分析（测试阶段，仅白名单/管理员可用）。"""
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow开庭", True))
        CMD = "开庭"
        # ── 违规封禁检查 ──
        banned, ban_remain = await self._check_violation_ban(event, CMD)
        if banned:
            yield event.plain_result(self._VIOLATION_BAN_MSG.format(command=CMD, remain=self._violation_ban_remain_str(ban_remain)))
            return
        async with self._rate_limit_slot(event) as slot_ok:
            if slot_ok is None:
                yield event.plain_result(self._RATE_LIMIT_REJECT_MSG)
                return
            async for r in self.court_manager.run_court(event, arg1, arg2):
                yield r

    @filter.command("ow是区吗", alias={'是区吗'})
    async def ow_shiqu(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        """OW 是区吗：展示上次判定结果。5 分钟内再次发送确认后开启新查询（分级 CD）。可加局数 1~25。"""
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow是区吗", True))
        # 智能拆分：数字→局数，非数字→战网ID（参数可无序）
        positional, kw = self._extract_keywords([arg1, arg2, arg3])
        bnet_id = ""
        match_count = 0
        is_privileged = self._is_whitelisted(event) or self._is_astrbot_admin(event)

        for arg in positional:
            if arg.isdigit():
                digit = int(arg)
                if not is_privileged:
                    yield event.plain_result("💡 自定义局数仅限白名单/管理员使用，已使用默认局数。")
                    continue
                if digit <= 0:
                    yield self._plain_error_result(event, "❌ 错误：是区吗的局数必须大于 0！")
                    return
                if digit > 25:
                    yield event.plain_result("💡 局数最大为 25，已自动调整为 25。")
                    match_count = 25
                else:
                    match_count = digit
            else:
                bnet_id = arg

        async with self._rate_limit_slot(event) as slot_ok:
            if slot_ok is None:
                yield event.plain_result(self._RATE_LIMIT_REJECT_MSG)
                return
            async for r in self.shiqu_manager.run(event, bnet_id, match_count=match_count):
                yield r

    @filter.command("ow是区吗结果", alias={'是区吗结果'})
    async def ow_shiqu_result(self, event: AstrMessageEvent):
        """OW 是区吗结果：返回上次生成的判定书图片。"""
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow是区吗结果", True))
        async for r in self.shiqu_manager.last_result(event):
            yield r

    @filter.command("owAI检测", alias={'AI检测'})
    async def ow_ai_test(self, event: AstrMessageEvent):
        """测试是区吗 LLM API 连通性。"""
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("owAI检测", True))
        ok, msg = await self.shiqu_manager.test_connectivity()
        yield event.plain_result(msg)

    @filter.command("历史段位", alias={'历届段位'})
    async def dashen_rank_history(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """追溯玩家的历史天梯段位记录（可选赛季范围）。"""
        # /历史段位            /历史段位 Player#12345
        # /历史段位 15 22（起始赛季 终止赛季）       /历史段位 Player#12345 15 22
        positional, _kw = self._extract_keywords([arg1, arg2])
        bnet_id = None
        season_nums: list[int] = []
        for arg in positional:
            if arg.isdigit():
                season_nums.append(int(arg))
            else:
                bnet_id = arg

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("历史段位") + "\n可选附加 起始 终止 赛季，如 /历史段位 Player#12345 15 22")
            return

        season_hint = ""
        if len(season_nums) == 1:
            season_hint = f"（从 S{season_nums[0]} 起）"
        elif len(season_nums) >= 2:
            season_hint = f"（S{season_nums[0]} ~ S{season_nums[1]})"

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"📜 正在追溯 {target_id} 的历史段位记录{season_hint}，可用参数：[战网ID] [赛季号]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id}
        # 起止赛季：按出现顺序赋值；仅给一个数字时作为起始
        if len(season_nums) >= 1:
            payload["start_season"] = season_nums[0]
        if len(season_nums) >= 2:
            payload["end_season"] = season_nums[1]

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-rank-history/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "历史段位"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取历史段位失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取历史段位失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="历史段位", error_code=err_code)

    @filter.command("同玩查询", alias={'开黑胜率'})
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str = "", p2: str = ""):
        """深度分析两位玩家一同游玩开黑时的战绩与胜率。

        用法：/同玩查询 [p1战网id] [p2战网id]
        - 两个都填 → 查询指定两位玩家的同玩数据
        - 只填一个 → 以你绑定的战网ID为 p1，输入的为 p2
        - 都不填   → 以你绑定的战网ID为 p1，需额外输入 p2
        """
        p1 = (p1 or "").strip()
        p2 = (p2 or "").strip()

        # 获取绑定的战网ID（兜底用）
        bound_id = await self._get_bnet_id(event) or ""

        # 解析逻辑：两个都填就原样用；只填一个时绑定ID作 p1
        if p1 and p2:
            pass  # 两个都填，原样使用
        elif p1 and not p2:
            # 只填了一个 → 作为 p2，绑定ID 作为 p1
            p2 = p1
            p1 = bound_id
        elif not p1 and p2:
            # 只填了 p2 → 绑定ID 作为 p1
            p1 = bound_id
        else:
            # 都没填 → 绑定ID 作为 p1
            p1 = bound_id

        if not p1:
            yield self._plain_error_result(event, "❌ 请先绑定战网ID（/绑定 Player#12345）或输入对战网ID，可用参数：[p1战网id] [p2战网id]")
            return
        if not p2:
            yield self._plain_error_result(event, "❌ 缺少第二个战网ID，可用参数：[p1战网id] [p2战网id]\n示例：/同玩查询 Player#12345 OtherPlayer#67890")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"👥 正在分析 {p1} 与 {p2} 的同玩胜率，可用参数：<战网ID1> <战网ID2>")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"player1_bnet_id": p1, "player2_bnet_id": p2}
        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-sameplay/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "同玩查询"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "无法获取同玩查询数据，请检查两个ID是否输入正确。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("同玩查询失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="同玩查询", error_code=err_code)

    @filter.command("快速强度", alias={'快速强度指数'})
    async def quick_strength(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """评估玩家快速模式下的强度指数（可选对局数 3-12）。"""
        # /快速强度            /快速强度 Player#12345         /快速强度 8（对局数3-12）
        # /快速强度 Player#12345 8
        positional, _kw = self._extract_keywords([arg1, arg2])
        bnet_id = None
        limit = None
        for arg in positional:
            if arg.isdigit():
                limit = int(arg)
            else:
                bnet_id = arg

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("快速强度") + "\n可选附加对局数（3-12），如 /快速强度 Player#12345 8")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"⚡ 正在评估 {target_id} 的快速强度指数，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "include_previous_season": True}
        if limit is not None:
            payload["limit"] = max(3, min(12, limit))

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-quick-strength/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "快速强度"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取快速强度指数失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取快速强度指数失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="快速强度", error_code=err_code)

    @filter.command("竞技强度", alias={'竞技强度指数'})
    async def competitive_strength(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """评估玩家竞技天梯模式下的强度指数（可选对局数 3-12）。"""
        # /竞技强度            /竞技强度 Player#12345         /竞技强度 8（对局数3-12）
        # /竞技强度 Player#12345 8
        positional, _kw = self._extract_keywords([arg1, arg2])
        bnet_id = None
        limit = None
        for arg in positional:
            if arg.isdigit():
                limit = int(arg)
            else:
                bnet_id = arg

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("竞技强度") + "\n可选附加对局数（3-12），如 /竞技强度 Player#12345 8")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🏆 正在评估 {target_id} 的竞技天梯强度指数，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "include_previous_season": True}
        if limit is not None:
            payload["limit"] = max(3, min(12, limit))

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-competitive-strength/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "竞技强度"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取竞技强度指数失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取竞技强度指数失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="竞技强度", error_code=err_code)

    @filter.command("快速英雄云图", alias={'快速云图'})
    async def quick_hero_treemap(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """获取快速模式英雄使用率矩形树图（可选赛季）。"""
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("快速英雄云图"))
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📊 正在获取 {target_id} 的快速模式英雄云图，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "mode": "quick", "include_previous_season": True}
        if season: payload["season"] = str(season)

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-hero-treemap/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "快速英雄云图"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取快速英雄云图失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取快速英雄云图失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="快速英雄云图", error_code=err_code)

    @filter.command("竞技英雄云图", alias={'竞技云图'})
    async def competitive_hero_treemap(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """获取竞技模式英雄使用率矩形树图（可选赛季）。"""
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, self._bnet_err("竞技英雄云图"))
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🏆 正在获取 {target_id} 的竞技模式英雄云图，可用参数：[战网ID]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "mode": "competitive", "include_previous_season": True}
        if season: payload["season"] = str(season)

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-hero-treemap/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "竞技英雄云图"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取竞技英雄云图失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取竞技英雄云图失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="竞技英雄云图", error_code=err_code)

    @filter.command("威能")
    async def ow_hero_perk(self, event: AstrMessageEvent, hero_name: str):
        """提取指定英雄的核心威能、机制数据图。"""
        if not hero_name:
            yield self._plain_error_result(event, "❌ 请输入英雄名称，如：/威能 闪光")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🔮 正在提取 {hero_name} 的核心威能数据，可用参数：<英雄名>")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-hero-perk/image", {"hero": hero_name})
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "威能"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else f"未能找到英雄【{hero_name}】的威能图。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="威能", error_code=err_code)

    @filter.command("ow英雄")
    async def ow_hero_pick(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        """读取指定英雄在当前天梯的 Pick 率历史走势图（可选模式/段位）。"""
        # /ow英雄 安娜  /ow英雄 安娜 快速  /ow英雄 安娜 大师  /ow英雄 安娜 快速 大师
        positional, kw = self._extract_keywords([arg1, arg2, arg3])
        hero_name = positional[0] if positional else ""
        if not hero_name:
            yield self._plain_error_result(event, "❌ 请输入英雄名称，如：/ow英雄 闪光\n可选附加：快速/竞技 模式、青铜~英杰 段位，如 /ow英雄 安娜 快速 大师")
            return

        # 默认竞技全段位，可被 快速/竞技 与分段关键词覆盖
        mode_label = "快速" if kw.get("game_mode") == "quick" else "竞技"
        mmr_label = kw.get("mmr", "all")
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"🔥 正在读取 {hero_name} 的 {mode_label}（{mmr_label}）Pick 率走势图，可用参数：<英雄名> [模式] [段位]")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {
            "view": "history",
            "game_mode": kw.get("game_mode", "competitive"),
            "mmr": kw.get("mmr", "all"),
            "hero": hero_name
        }

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/ow-hero-pick-rate/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "ow英雄"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else f"暂时无法获取英雄 {hero_name} 的数据走势。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="ow英雄", error_code=err_code)

    @filter.command("省榜", alias={'排行'})
    async def ow_rank_leaderboard(self, event: AstrMessageEvent, province: str, role: str):
        """获取指定地区的大神天梯省榜（位置：tank / dps / healer / open）。"""
        if not province or not role:
            yield self._plain_error_result(event, "❌ 请输入省份名称 and 职责位置，例如：/省榜 北京 tank\n(支持的位置: tank / dps / healer / open)")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🏆 正在获取 {province} 地区 【{role}】 位置的大神天梯省榜，可用参数：<省份> <职责>")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"province": province, "role": role}

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-rank-leaderboard/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "省榜"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取天梯省榜失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="省榜", error_code=err_code)

    @filter.command("绝活榜", alias={'英雄省榜'})
    async def ow_hero_leaderboard(self, event: AstrMessageEvent, province: str, hero: str, arg3: str = ""):
        """获取指定地区特定英雄的大神专精绝活榜（可选开放队列模式）。"""
        # /绝活榜 北京 猎空        /绝活榜 北京 猎空 开放（开放=开放队列模式）
        _positional, kw = self._extract_keywords([province, hero, arg3])
        # 关键词识别后还原省/英雄（关键词被剔除，剩下的前两个就是省份与英雄）
        province = _positional[0] if len(_positional) >= 1 else province
        hero = _positional[1] if len(_positional) >= 2 else hero
        if not province or not hero:
            yield self._plain_error_result(event, "❌ 请输入省份和英雄名称，例如：/绝活榜 北京 猎空\n可选附加 开放（开放队列模式），如 /绝活榜 北京 猎空 开放")
            return

        mode = kw.get("lb_mode", "preset")
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"🎖️ 正在获取 {province} 地区 【{hero}】（{'开放队列' if mode == 'open' else '预设'}）的大神英雄专精绝活榜...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"province": province, "hero": hero, "mode": mode}

        success = False
        err_code = ""
        try:
            img_bytes, error_data, err_code = await self._fetch_image("/dashen-hero-leaderboard/image", payload)
            if img_bytes:
                success = True
                async for r in self._send_image_result(event, img_bytes, "绝活榜"):
                    yield r
            else:
                err_msg = error_data.get("message") if error_data else "获取英雄绝活榜失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success, cmd_name="绝活榜", error_code=err_code)
