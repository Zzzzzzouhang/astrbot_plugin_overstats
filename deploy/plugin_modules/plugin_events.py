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

class PluginEvents:

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE)
    async def handle_direct_text_events(self, event: AstrMessageEvent):
        """处理直接发送的战网 ID、单局数字、绑定指令，以及纯@时返回快速指南。

        兼容 QQ 官方机器人（qq_official / qq_official_webhook）与普通 QQ 机器人（aiocqhttp）。
        群聊中必须真实@机器人才会响应（排除 QQ 官方占位@[At:qq_official]，防止误触发），私聊放行。
        需在配置面板开启「是否开启直接消息处理」开关后生效。
        """
        # 配置开关：未开启直接消息处理时跳过
        if not bool(self.config.get("direct_message_handling_enabled", False)):
            return

        # 兼容 QQ 官方与普通机器人
        platform_name = ""
        try:
            platform_name = event.get_platform_name() or ""
        except Exception:
            pass
        if platform_name not in ("qq_official", "qq_official_webhook", "aiocqhttp"):
            return

        msg = event.message_str.strip() if event.message_str else ""
        is_group = self._is_group_message(event)

        # 群聊必须真实@机器人（排除 QQ 官方占位@），私聊放行，防止误触发
        # 兜底：_is_real_at_bot 可能因 self_id 与 At.qq 格式不匹配返回 False，
        # 但框架已在 WakingCheckStage 阶段判定 is_at_or_wake_command=True，此时直接放行
        if is_group and not self._is_real_at_bot(event):
            if not (hasattr(event, "is_at_or_wake_command") and event.is_at_or_wake_command):
                return

        # 群消息时提前加载群组配置到内存缓存
        if is_group:
            await self._ensure_group_config_loaded()

        # 纯@（无文本）时返回快速指南
        if hasattr(event, "is_at_or_wake_command") and event.is_at_or_wake_command:
            if not msg or msg.isspace():
                yield event.plain_result(self._get_quick_guide(event))
                event.stop_event()
                return

        if not msg:
            return

        if msg.isdigit():
            num = int(msg)
            if 0 < num <= 20:
                async for result in self.dashen_match_detail(event, arg1=str(num)):
                    yield result
                event.stop_event()
                return

        # 无空格指令识别：/今日总结Player#12345 → 按已知命令前缀匹配拆分（必须在 bind 之前，否则会误绑）
        adapt_map = self._ensure_full_adapt_map()
        for cmd_name in sorted(adapt_map.keys(), key=len, reverse=True):  # 长命令优先
            if cmd_name in ("大神绑定", "绑定"):  # 交由下方 bind 检测处理
                continue
            method = adapt_map[cmd_name]
            for prefix in (f"/{cmd_name}", cmd_name):
                if not msg.startswith(prefix) or len(msg) <= len(prefix):
                    continue
                rest = msg[len(prefix):]
                bnet_m = re.match(r'^([^#＃\s]+[#＃]\d+)$', rest)  # 纯战网ID格式
                if not bnet_m:
                    continue
                bnet_id = bnet_m.group(1)
                try:
                    params = [p for p in inspect.signature(method).parameters.values()
                              if p.name not in ("self", "event")]
                except Exception:
                    params = []
                if not params:
                    continue
                args = [bnet_id] + [""] * (len(params) - 1)
                try:
                    async for r in method(event, *args[:len(params)]):
                        yield r
                    event.stop_event()
                    return
                except Exception as e:
                    logger.debug(f"无空格指令派发失败（{cmd_name}）: {e}")
                    return

        bind_match = re.match(r"^(?:/?(?:大神)?绑定\s+)?(?:/?(?:大神)?绑定)?\s*([^#＃\s]+[#＃]\d+)$", msg)
        if bind_match:
            clean_bnet_id = bind_match.group(1)
            async for result in self.dashen_bind(event, bnet_id=clean_bnet_id):
                yield result
            event.stop_event()
            return

        # 未匹配到任何已知指令/模式时，根据配置返回快速指南（仅用户指令，管理指令不触发）
        # 群聊中非真实@bot 的消息（含纯文本 @昵称）交由 full_adaptation_interceptor 处理，此处不拦截
        if (
            hasattr(event, "is_at_or_wake_command") and event.is_at_or_wake_command
            and self.config.get("unmatched_cmd_guide_enabled", False)
        ):
            if not is_group or self._is_real_at_bot(event):
                cmd_first_token = msg.split()[0].lstrip("/")
                if cmd_first_token not in self._admin_cmd_set and cmd_first_token not in self._ensure_full_adapt_map():
                    yield event.plain_result(self._get_quick_guide(event, unmatched_cmd=msg))
                    event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def unmatched_command_guide(self, event: AstrMessageEvent):
        """未匹配指令兜底：非 QQ 平台群聊中 @机器人 发送了未识别的用户指令时，返回快速指南。

        QQ 平台已由 handle_direct_text_events / full_adaptation_interceptor 专门处理，
        此处仅覆盖其他平台（如 Telegram、Discord 等）的 @唤醒未匹配场景。
        """
        if not self.config.get("unmatched_cmd_guide_enabled", False):
            return
        if not hasattr(event, "is_at_or_wake_command") or not event.is_at_or_wake_command:
            return

        # QQ 平台由专属处理器负责，避免重复响应
        platform_name = ""
        try:
            platform_name = event.get_platform_name() or ""
        except Exception:
            pass
        if platform_name in ("qq_official", "qq_official_webhook", "aiocqhttp"):
            return

        msg = event.message_str.strip() if event.message_str else ""
        if not msg:
            return

        cmd_first_token = msg.split()[0].lstrip("/")
        if cmd_first_token in self._admin_cmd_set or cmd_first_token in self._ensure_full_adapt_map():
            return
        if cmd_first_token.isdigit() and 0 < int(cmd_first_token) <= 20:
            return  # 数字快捷指令场景，可能是未被框架识别的对局查询

        yield event.plain_result(self._get_quick_guide(event, unmatched_cmd=msg))
        event.stop_event()

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def full_adaptation_interceptor(self, event: AstrMessageEvent):
        """全量消息适配拦截器：群聊中直接发送 table 指令或"@机器人昵称 指令"格式触发对应命令。

        默认关闭，需群管理员通过 /全量适配开（@昵称模式）或 /全量适配开完全匹配（直接匹配模式）开启。
        按群独立配置，持久化到 JSON。
        支持两种模式：
        - 直接匹配模式：消息与 table 中收录的指令名完全匹配即触发，无需 @机器人昵称
        - @昵称模式：需以 "@机器人昵称 指令" 格式发送纯文本消息

        仅处理纯文本（非真实@提及）；真实@仍走框架原生指令派发，避免重复响应。
        兼容 QQ 官方（含 [At:qq_official] 占位与 [At:<openid>] 真实@两种格式）与普通机器人。
        """
        if not self._is_group_message(event):
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return

        adapt_cfg = self._get_full_adapt(group_id)
        if not adapt_cfg.get("enabled", False):
            return

        # 真实@机器人 → 交给框架原生派发，避免重复响应
        if self._is_real_at_bot(event):
            return

        msg = (event.message_str or "").strip()
        direct_mode = adapt_cfg.get("mode", "nickname") == "direct"

        if direct_mode:
            # 直接匹配模式：消息即为指令文本，直接派发
            # 纯数字不触发（避免群内数字消息误触发数字快捷）
            if not msg or msg.isdigit():
                return
            cmd_text = msg
            # 完全匹配模式下也可能带 @机器人昵称 前缀，尝试剥离
            nickname = self._get_bot_nickname(event, adapt_cfg)
            if nickname:
                prefix = f"@{nickname}"
                if cmd_text.startswith(prefix + " ") or cmd_text.startswith(prefix + "\u3000"):
                    cmd_text = cmd_text[len(prefix):].strip()
                elif cmd_text.startswith(prefix):
                    cmd_text = cmd_text[len(prefix):]
        else:
            # @昵称模式：需要匹配 "@机器人昵称" 前缀
            nickname = self._get_bot_nickname(event, adapt_cfg)
            if not nickname:
                return
            prefix = f"@{nickname}"
            if not (msg == prefix or msg.startswith(prefix + " ") or msg.startswith(prefix + "\u3000")):
                return
            cmd_text = msg[len(prefix):].strip()

            # 纯@昵称（无指令）→ 快速指南
            if not cmd_text:
                yield event.plain_result(self._get_quick_guide(event))
                event.stop_event()
                return

        # 数字快捷：@昵称 5 → 第 5 局单局详细
        if cmd_text.isdigit() and 0 < int(cmd_text) <= 20:
            async for r in self.dashen_match_detail(event, arg1=str(int(cmd_text))):
                yield r
            event.stop_event()
            return

        # 无空格指令识别：@昵称 今日总结Player#12345 → 按已知命令前缀匹配（必须在 bind 之前，否则会误绑）
        adapt_map = self._ensure_full_adapt_map()
        for cmd_name in sorted(adapt_map.keys(), key=len, reverse=True):
            if cmd_name in ("大神绑定", "绑定"):
                continue
            method = adapt_map[cmd_name]
            for prefix in (cmd_name,):  # full_adaptation 场景不带 / 前缀
                if not cmd_text.startswith(prefix) or len(cmd_text) <= len(prefix):
                    continue
                rest = cmd_text[len(prefix):]
                bnet_m = re.match(r'^([^#＃\s]+[#＃]\d+)$', rest)
                if not bnet_m:
                    continue
                bnet_id = bnet_m.group(1)
                try:
                    params = [p for p in inspect.signature(method).parameters.values()
                              if p.name not in ("self", "event")]
                except Exception:
                    params = []
                if not params:
                    continue
                args = [bnet_id] + [""] * (len(params) - 1)
                try:
                    async for r in method(event, *args[:len(params)]):
                        yield r
                    event.stop_event()
                    return
                except Exception as e:
                    logger.debug(f"全量适配无空格派发失败（{cmd_name}）: {e}")
                    return

        # 绑定快捷：@昵称 Player#12345
        bind_m = re.match(r"^(?:/?(?:大神)?绑定\s+)?(?:/?(?:大神)?绑定)?\s*([^#＃\s]+[#＃]\d+)$", cmd_text)
        if bind_m:
            async for r in self.dashen_bind(event, bnet_id=bind_m.group(1)):
                yield r
            event.stop_event()
            return

        # 指令派发：自维护分发表，按方法签名自动适配参数数量（标准库 inspect）
        tokens = cmd_text.split()
        cmd = tokens[0].lstrip("/")  # 兼容用户带 "/" 前缀
        rest = tokens[1:]
        method = self._ensure_full_adapt_map().get(cmd)
        if not method:
            # 未命中本插件全量适配指令，仅@昵称模式下根据配置返回快速指南
            # 直接匹配模式下不回复，避免群内任何消息都触发快捷指南
            if not direct_mode and self.config.get("unmatched_cmd_guide_enabled", False):
                yield event.plain_result(self._get_quick_guide(event, unmatched_cmd=cmd_text))
                event.stop_event()
            return

        # 按方法签名（除 self/event）截取参数，不足补空串（交由方法内部校验给出友好提示）
        try:
            params = [p for p in inspect.signature(method).parameters.values()
                      if p.name not in ("self", "event")]
        except Exception:
            params = []
        n_max = len(params)
        args = rest[:n_max]
        args += [""] * (n_max - len(args))

        try:
            async for r in method(event, *args):
                yield r
            event.stop_event()
        except TypeError as e:
            # 参数不匹配（如必填参数缺失）的兜底提示
            yield self._plain_error_result(event, f"❌ 指令参数不匹配：{e}\n请检查指令用法，可发送 @机器人昵称 获取快速指南。")
            event.stop_event()
        except Exception as e:
            logger.error(f"全量适配派发异常（{cmd}）: {e}")
            yield self._plain_error_result(event, f"❌ 指令执行异常：{e}")
            event.stop_event()

    @filter.command("快速指南", alias={'快捷指令'})
    async def quick_guide_command(self, event: AstrMessageEvent):
        """独立快速指南指令"""
        quick_guide = self._get_quick_guide(event)
        yield event.plain_result(quick_guide)

    @filter.command("所有指令", alias={'别称'})
    async def show_aliases(self, event: AstrMessageEvent):
        """展示所有插件指令及其别称"""
        text = """📋 **【Overstats 指令及别称大全】**

🔹 **基础与绑定类：**
• <qqbot-cmd-input text="/owhelp " show="owhelp" reference="false" /> (别称：<qqbot-cmd-input text="/ow菜单 " show="ow菜单" reference="false" />, <qqbot-cmd-input text="/ow帮助 " show="ow帮助" reference="false" />, <qqbot-cmd-input text="/OW帮助 " show="OW帮助" reference="false" />, <qqbot-cmd-input text="/help " show="help" reference="false" />)
• <qqbot-cmd-input text="/所有指令 " show="所有指令" reference="false" /> (别称：<qqbot-cmd-input text="/别称 " show="别称" reference="false" />)
• <qqbot-cmd-input text="/大神绑定 " show="大神绑定" reference="false" /> (别称：<qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />)

🔹 **数据查询类：**
• <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> (别称：<qqbot-cmd-input text="/详情卡片 " show="详情卡片" reference="false" />, <qqbot-cmd-input text="/战绩查询 " show="战绩查询" reference="false" />, <qqbot-cmd-input text="/数据 " show="数据" reference="false" />)
• <qqbot-cmd-input text="/大神对局 " show="大神对局" reference="false" /> (别称：<qqbot-cmd-input text="/最近对局 " show="最近对局" reference="false" />, <qqbot-cmd-input text="/战绩 " show="战绩" reference="false" />, <qqbot-cmd-input text="/对局 " show="对局" reference="false" />)
• <qqbot-cmd-input text="/单局详细 " show="单局详细" reference="false" /> (别称：<qqbot-cmd-input text="/单局详情 " show="单局详情" reference="false" />, <qqbot-cmd-input text="/单局 " show="单局" reference="false" />)
• <qqbot-cmd-input text="/同玩查询 " show="同玩查询" reference="false" /> (别称：<qqbot-cmd-input text="/开黑胜率 " show="开黑胜率" reference="false" />)

🔹 **总结类：**
• <qqbot-cmd-input text="/今日总结 " show="今日总结" reference="false" /> (别称：<qqbot-cmd-input text="/今日 " show="今日" reference="false" />, <qqbot-cmd-input text="/今日数据 " show="今日数据" reference="false" />)
• <qqbot-cmd-input text="/昨日总结 " show="昨日总结" reference="false" /> (别称：<qqbot-cmd-input text="/昨日 " show="昨日" reference="false" />, <qqbot-cmd-input text="/昨日数据 " show="昨日数据" reference="false" />, <qqbot-cmd-input text="/昨天数据 " show="昨天数据" reference="false" />)
• <qqbot-cmd-input text="/周度总结 " show="周度总结" reference="false" /> (别称：<qqbot-cmd-input text="/本周总结 " show="本周总结" reference="false" />, <qqbot-cmd-input text="/本周数据 " show="本周数据" reference="false" />, <qqbot-cmd-input text="/本周 " show="本周" reference="false" />)

🔹 **图表与排行类：**
• <qqbot-cmd-input text="/历史段位 " show="历史段位" reference="false" /> (别称：<qqbot-cmd-input text="/历届段位 " show="历届段位" reference="false" />)
• <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" /> (别称：<qqbot-cmd-input text="/快速强度指数 " show="快速强度指数" reference="false" />)
• <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" /> (别称：<qqbot-cmd-input text="/竞技强度指数 " show="竞技强度指数" reference="false" />)
• <qqbot-cmd-input text="/快速英雄云图 " show="快速英雄云图" reference="false" /> (别称：<qqbot-cmd-input text="/快速云图 " show="快速云图" reference="false" />)
• <qqbot-cmd-input text="/竞技英雄云图 " show="竞技英雄云图" reference="false" /> (别称：<qqbot-cmd-input text="/竞技云图 " show="竞技云图" reference="false" />)
• <qqbot-cmd-input text="/省榜 " show="省榜" reference="false" /> (别称：<qqbot-cmd-input text="/排行 " show="排行" reference="false" />)
• <qqbot-cmd-input text="/绝活榜 " show="绝活榜" reference="false" /> (别称：<qqbot-cmd-input text="/英雄省榜 " show="英雄省榜" reference="false" />)
• <qqbot-cmd-input text="/banpick " show="banpick" reference="false" /> (别称：<qqbot-cmd-input text="/全英雄排行 " show="全英雄排行" reference="false" />)

🔹 **游戏资讯类：**
• <qqbot-cmd-input text="/威能 " show="威能" reference="false" />, <qqbot-cmd-input text="/ow英雄 " show="ow英雄" reference="false" />, <qqbot-cmd-input text="/获取段位分布 " show="获取段位分布" reference="false" />, <qqbot-cmd-input text="/mappick " show="mappick" reference="false" />, <qqbot-cmd-input text="/皮肤搜索 " show="皮肤搜索" reference="false" />
• <qqbot-cmd-input text="/商店 " show="商店" reference="false" /> (别称：<qqbot-cmd-input text="/ow商店 " show="ow商店" reference="false" />)
• <qqbot-cmd-input text="/ow赛事 " show="ow赛事" reference="false" /> (别称：<qqbot-cmd-input text="/赛事 " show="赛事" reference="false" />)
• <qqbot-cmd-input text="/ow活动 " show="ow活动" reference="false" /> (别称：<qqbot-cmd-input text="/活动 " show="活动" reference="false" />)
• <qqbot-cmd-input text="/ow更新 " show="ow更新" reference="false" /> (别称：<qqbot-cmd-input text="/版本更新 " show="版本更新" reference="false" />)"""
        
        yield event.plain_result(self._format_markdown_by_platform(event, text))

    @filter.command("多图测试")
    async def multi_image_test(self, event: AstrMessageEvent):
        """多图测试：发送多张测试图片。"""
        imgs_list = []
        for img_name in ["test1.png", "test2.png", "test3.png"]:
            img_path = self.plugin_data_dir / img_name
            if img_path.exists():
                try:
                    with open(img_path, "rb") as f:
                        imgs_list.append(f.read())
                except Exception as e:
                    logger.error(f"读取测试图片 {img_name} 失败: {e}")
        
        if not imgs_list:
            yield self._plain_error_result(event, "❌ 未能读取到测试图片。")
            return
            
        async for r in self._send_multiple_images_result(event, imgs_list, "多图测试"):
            yield r

    @filter.command("单图测试")
    async def single_image_test(self, event: AstrMessageEvent):
        """单图测试：发送单张 test1.png 测试图片。"""
        img_path = self.plugin_data_dir / "test1.png"
        if not img_path.exists():
            yield self._plain_error_result(event, "❌ 未能读取到测试图片 test1.png。")
            return
        try:
            with open(img_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            logger.error(f"读取测试图片 test1.png 失败: {e}")
            yield self._plain_error_result(event, "❌ 读取测试图片失败。")
            return
        async for r in self._send_image_result(event, img_bytes, "单图测试"):
            yield r

    @filter.command("owhelp", alias={'ow菜单', 'ow帮助', 'OW帮助', 'help'})
    async def ow_help(self, event: AstrMessageEvent):
        """显示 Overstats 查询菜单，列出所有常用指令入口。"""
        help_text = """📌 Overstats 查询菜单
🔗 ➤ <qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />示例：/绑定 Player#12345
📋 ➤ <qqbot-cmd-input text="/今日总结 " show="今日" reference="false" /> ➤ <qqbot-cmd-input text="/昨日总结 " show="昨日" reference="false" /> ➤ <qqbot-cmd-input text="/周度总结 " show="本周" reference="false" />
📊 ➤ <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="/大神对局 " show="大神对局" reference="false" /> ➤ <qqbot-cmd-input text="/单局详细 " show="单局详细" reference="false" />「数字」可加 锐评关/全员关
📈 ➤ <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" />「可选对局数」 ➤ <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" />「可选对局数」 ➤ <qqbot-cmd-input text="/获取段位分布 " show="获取段位分布" reference="false" />「可选 快速/竞技 段位」
🗺️ ➤ <qqbot-cmd-input text="/快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="/竞技英雄云图 " show="竞技云图" reference="false" /> ➤ <qqbot-cmd-input text="/历史段位 " show="历史段位" reference="false" />
🏆 ➤ <qqbot-cmd-input text="/省榜 " show="省榜" reference="false" />「省份」「位置」 ➤ <qqbot-cmd-input text="/绝活榜 " show="绝活榜" reference="false" />「省份」「英雄」可加 开放
⚔️ ➤ <qqbot-cmd-input text="/威能 " show="威能" reference="false" />「英雄名」 ➤ <qqbot-cmd-input text="/ow英雄 " show="ow 英雄" reference="false" />「英雄名」可加 快速/竞技 段位 ➤ <qqbot-cmd-input text="/banpick " show="banpick" reference="false" />「可选 快速/竞技 段位」 ➤ <qqbot-cmd-input text="/mappick " show="mappick" reference="false" />
🌍 ➤ <qqbot-cmd-input text="/同玩查询 " show="同玩查询" reference="false" />「ID1」「ID2」 ➤ <qqbot-cmd-input text="/商店 " show="商店" reference="false" /> ➤ <qqbot-cmd-input text="/皮肤搜索 " show="皮肤搜索" reference="false" /> ➤ <qqbot-cmd-input text="/ow赛事 " show="ow 赛事" reference="false" />
📰 ➤ <qqbot-cmd-input text="/ow更新 " show="ow更新" reference="false" />「latest/small/big」
🧪 ➤ <qqbot-cmd-input text="/ow是区吗 " show="ow是区吗" reference="false" />「战网ID」 ➤ <qqbot-cmd-input text="/ow是区吗结果 " show="ow是区吗结果" reference="false" />
🎁 ➤ 段位关键词：青铜/白银/黄金/白金/钻石/大师/宗师/英杰
💡 大部分指令都可以带[战网id]参数，查询对应的数据，无需重新绑定
💡 发送 <qqbot-cmd-input text="/别称 " show="别称" reference="false" /> 可查看所有指令对应别称列表。"""
        
        yield event.plain_result(self._format_markdown_by_platform(event, help_text))
