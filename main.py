import os
import aiohttp
import logging
import tempfile
import asyncio
import re
import base64
import json
import time
import urllib.parse
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.event import filter, AstrMessageEvent  # 引入原生消息与事件过滤模块

logger = logging.getLogger("astrbot")

@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "1.6.3")
class OverstatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = self.config.get("overstats_api_url", "http://127.0.0.1:18080/api/v2")
        
        self.last_match_bnet_id = {}
        self.file_lock = asyncio.Lock()
        
        try:
            plugin_name = getattr(self, "name", "overstats_full")
            self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            self.temp_image_dir = self.plugin_data_dir / "temp"
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"初始化插件专属数据目录失败: {e}")
            self.plugin_data_dir = Path(tempfile.gettempdir())
            self.temp_image_dir = self.plugin_data_dir / "temp"
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)

    def _is_qq_official(self, event: AstrMessageEvent) -> bool:
        """判断当前消息是否来自 QQ 官方机器人平台"""
        umo = event.unified_msg_origin
        if "qq_official" in str(umo).lower():
            return True
        if hasattr(event, "message_obj") and event.message_obj:
            if "qq_official" in str(event.message_obj).lower():
                return True
        return False

    def _format_markdown_by_platform(self, event: AstrMessageEvent, text: str) -> str:
        """
        根据平台环境动态处理文本：
        如果是 QQ 官方机器人：保留标签并对 text 和 show 属性进行 urlencode
        如果是其他机器人：剥离 <qqbot-cmd-input> 标签，将其转化为普通文本格式
        """
        if self._is_qq_official(event):
            def replacer(match):
                text_val = urllib.parse.quote(match.group(1))
                show_val = urllib.parse.quote(match.group(2))
                return f'<qqbot-cmd-input text="{text_val}" show="{show_val}" reference="false" />'
            
            pattern = r'<qqbot-cmd-input\s+text="([^"]+)"\s+show="([^"]+)"\s+reference="false"\s*/>'
            return re.sub(pattern, replacer, text)
        else:
            def strip_replacer(match):
                text_val = match.group(1).strip()
                show_val = match.group(2).strip()
                if text_val.startswith("/") and not show_val.startswith("/"):
                    return f"{text_val}"
                return f"{text_val}"
            
            pattern = r'<qqbot-cmd-input\s+text="([^"]+)"\s+show="([^"]+)"\s+reference="false"\s*/>'
            return re.sub(pattern, strip_replacer, text)

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

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        if event.message_obj and event.message_obj.group_id:
            return f"g_{event.message_obj.group_id}"
        return f"u_{event.get_sender_id()}"

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str = "") -> str:
        if input_id and input_id.strip():
            return input_id.strip()
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
        mode = "competitive"
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

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 600) -> tuple[bytes | None, dict | None]:
        url = f"{self.base_url}{endpoint}"
        payload = payload or {}
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.read(), None
                    else:
                        try:
                            error_data = await resp.json()
                            logger.error(f"Overstats API 错误: {resp.status} - {error_data}")
                            return None, error_data
                        except:
                            logger.error(f"Overstats API 返回了非 JSON 错误: {resp.status}")
                            return None, {"error": "non_json_error", "message": "API返回非JSON格式错误"}
        except Exception as e:
            logger.error(f"网络请求异常: {e}")
            return None, {"error": "network_error", "message": str(e)}

    def _safe_remove(self, path: str):
        if path and os.path.exists(path):
            try: os.remove(path)
            except Exception: pass

    async def _delayed_remove(self, path: str, delay: int):
        await asyncio.sleep(delay)
        self._safe_remove(path)

    def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes, fallback_text: str = ""):
        if not img_bytes:
            return event.plain_result(fallback_text or "❌ 图片生成失败")
        try:
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            img_hash = abs(hash(img_bytes))
            img_path = self.temp_image_dir / f"{img_hash}.png"
            img_path.write_bytes(img_bytes)
            user_id = event.get_sender_id()
            chain = [Comp.At(qq=user_id), Comp.Plain("\n" if not fallback_text else f"\n{fallback_text}\n"), Comp.Image.fromFileSystem(str(img_path))]
            asyncio.create_task(self._delayed_remove(str(img_path), 2592000))
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f"构建图片消息链时发生错误: {e}")
            return event.plain_result(fallback_text or "❌ 机器人构建图片组件失败")

    async def _send_multiple_images_result(self, event: AstrMessageEvent, imgs_list: list[bytes]):
        try:
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            user_id = event.get_sender_id()
            
            valid_images = [img for img in imgs_list if img]
            if not valid_images:
                yield event.plain_result("❌ 未能获取到有效的图片数据")
                return

            if self._is_qq_official(event):
                for i, img_bytes in enumerate(valid_images):
                    img_path = self.temp_image_dir / f"{abs(hash(img_bytes))}_{time.time_ns()}.png"
                    img_path.write_bytes(img_bytes)
                    
                    if i == 0:
                        chain = [Comp.At(qq=user_id), Comp.Plain("\n"), Comp.Image.fromFileSystem(str(img_path))]
                    else:
                        chain = [Comp.Image.fromFileSystem(str(img_path))]
                        
                    yield event.chain_result(chain)
                    asyncio.create_task(self._delayed_remove(str(img_path), 2592000))
                    await asyncio.sleep(0.5)
            else:
                chain = [Comp.At(qq=user_id), Comp.Plain("\n")]
                for img_bytes in valid_images:
                    img_path = self.temp_image_dir / f"{abs(hash(img_bytes))}_{time.time_ns()}.png"
                    img_path.write_bytes(img_bytes)
                    chain.append(Comp.Image.fromFileSystem(str(img_path)))
                    asyncio.create_task(self._delayed_remove(str(img_path), 2592000))
                yield event.chain_result(chain)
                
        except Exception as e:
            logger.error(f"多图发送逻辑错误: {e}")
            yield event.plain_result("❌ 多图发送失败")  

    # -------------------------------------------------------------------------
    # 指令注册区 (使用 AstrBot 原生 filter)
    # -------------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL) # 新增这行：仅限 QQ 官方机器人平台触发
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE)
    async def handle_direct_text_events(self, event: AstrMessageEvent):
        """处理直接发送的战网ID和单局数字，不使用全局 ALL 监听器"""
        # 获取消息纯文本并去除两端空格
        msg = event.message_str.strip()
        if not msg:
            return
            
        # 1. 拦截单局详细查询：判断是否为纯数字，且在 1 到 20 之间
        if msg.isdigit():
            num = int(msg)
            if 0 < num <= 20:
                event.stop_event() # 匹配成功，终止事件传播

                async for result in self.dashen_match_detail(event, arg1=num):
                    yield result
                return

        # 2. 拦截自动绑定：判断是否符合战网ID的格式 (例如：Player#12345 或 中文ID＃12345)
        # 正则含义：开头至少一个非 #/空格 字符，紧跟 # 或 ＃，结尾是一串数字
        if re.match(r"^[^#＃\s]+[#＃]\d+$", msg):
            event.stop_event() 
            async for result in self.dashen_bind(event, bnet_id=msg):
                yield result
            return
        
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
• <qqbot-cmd-input text="/单局详细 " show="单局详细" reference="false" /> (别称：<qqbot-cmd-input text="/单局 " show="单局" reference="false" />)
• <qqbot-cmd-input text="/同玩查询 " show="同玩查询" reference="false" /> (别称：<qqbot-cmd-input text="/开黑胜率 " show="开黑胜率" reference="false" />)

🔹 **总结类：**
• <qqbot-cmd-input text="/今日总结 " show="今日总结" reference="false" /> (别称：<qqbot-cmd-input text="/今日 " show="今日" reference="false" />, <qqbot-cmd-input text="/今日数据 " show="今日数据" reference="false" />)
• <qqbot-cmd-input text="/昨日总结 " show="昨日总结" reference="false" /> (别称：<qqbot-cmd-input text="/昨日 " show="昨日" reference="false" />, <qqbot-cmd-input text="/昨日数据 " show="昨日数据" reference="false" />, <qqbot-cmd-input text="/昨天数据 " show="昨天数据" reference="false" />, <qqbot-cmd-input text="/昨天 " show="昨天" reference="false" />)
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
            yield event.plain_result("❌ 未能读取到测试图片。")
            return
            
        async for r in self._send_multiple_images_result(event, imgs_list):
            yield r

    @filter.command("owhelp", alias={'ow菜单', 'ow帮助', 'OW帮助', 'help'})
    async def ow_help(self, event: AstrMessageEvent):
        help_text = """📌 Overstats 查询菜单
🔗 ➤ <qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />「战网 ID」
📋 ➤ <qqbot-cmd-input text="/今日总结 " show="今日" reference="false" /> ➤ <qqbot-cmd-input text="/昨日总结 " show="昨日" reference="false" /> ➤ <qqbot-cmd-input text="/周度总结 " show="本周" reference="false" />
📊 ➤ <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="/大神对局 " show="大神对局" reference="false" /> ➤ <qqbot-cmd-input text="/单局详细 " show="单局详细" reference="false" />「数字」
📈 ➤ <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" /> ➤ <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" /> ➤ <qqbot-cmd-input text="/获取段位分布 " show="获取段位分布" reference="false" />
🗺️ ➤ <qqbot-cmd-input text="/快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="/竞技英雄云图 " show="竞技云图" reference="false" />
🏆 ➤ <qqbot-cmd-input text="/省榜 " show="省榜" reference="false" />「省份」「位置」 ➤ <qqbot-cmd-input text="/绝活榜 " show="绝活榜" reference="false" />「省份」「英雄」
⚔️ ➤ <qqbot-cmd-input text="/威能 " show="威能" reference="false" />「英雄名」 ➤ <qqbot-cmd-input text="/ow英雄 " show="ow 英雄" reference="false" />「英雄名」 ➤ <qqbot-cmd-input text="/banpick " show="banpick" reference="false" /> ➤ <qqbot-cmd-input text="/mappick " show="mappick" reference="false" />
🌍 ➤ <qqbot-cmd-input text="/同玩查询 " show="同玩查询" reference="false" />「ID1」「ID2」 ➤ <qqbot-cmd-input text="/商店 " show="商店" reference="false" /> ➤ <qqbot-cmd-input text="/皮肤搜索 " show="皮肤搜索" reference="false" /> ➤ <qqbot-cmd-input text="/ow赛事 " show="ow 赛事" reference="false" />
📰 ➤ <qqbot-cmd-input text="/ow更新 " show="ow更新" reference="false" />「latest/small/big」
💡 发送 <qqbot-cmd-input text="/别称 " show="别称" reference="false" /> 可查看所有指令对应别称列表。"""
        
        yield event.plain_result(self._format_markdown_by_platform(event, help_text))

    @filter.command("大神绑定", alias={'绑定'})
    async def dashen_bind(self, event: AstrMessageEvent, bnet_id: str):
        user_id = event.get_sender_id()
        if not bnet_id or ("#" not in bnet_id and "＃" not in bnet_id):
            yield event.plain_result("❌ 绑定失败！请输入规范战网ID\n格式：/绑定 战网ID（中间必须加空格）\n示例：/绑定 Player#12345")
            return
        
        old_bind_id = await self._get_user_bind_id(user_id)
        new_bind_id = bnet_id.strip()
        await self._set_user_bind_id(user_id, new_bind_id)
        
        if not old_bind_id:
            yield event.plain_result(f"✅ 绑定成功！关联战网账号【{new_bind_id}】")
        else:
            yield event.plain_result(f"✅ 更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】")

    @filter.command("今日总结", alias={'今日', '今日数据'})
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/今日总结 Player#12345 或先使用 /绑定 指令")
            return
        
        yield event.plain_result(f"⏳ 正在计算 {target_id} 的今日战绩总结...")
        img_bytes, error_data = await self._fetch_image("/dashen-summary/today/image", {"bnet_id": target_id})
        
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        elif error_data and error_data.get("error") == "summary_empty" and error_data.get("details", {}).get("scope") == "today":
            yield event.plain_result(f"ℹ️ 你在过去的 24 小时内没有对局记录，尝试生成昨日总结...")
            async for result in self.dashen_yesterday(event, target_id):
                yield result
        else:
            err_msg = error_data.get("message", "未知错误") if error_data else "未知错误"
            if "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取今日总结失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ 获取今日总结失败：{err_msg}")

    @filter.command("昨日总结", alias={'昨日', '昨日数据', '昨天数据', '昨天'})
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/昨日总结 Player#12345 或先使用 /绑定 指令")
            return
        yield event.plain_result(f"⏳ 正在统计 {target_id} 的昨日战绩数据...")
        img_bytes, error_data = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取昨日总结失败，可能昨日未登录游戏。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取昨日总结失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("周度总结", alias={'本周总结', '本周数据', '本周'})
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/周度总结 Player#12345 或先使用 /绑定 指令")
            return
        yield event.plain_result(f"📊 正在生成 {target_id} 的本周战绩大数据总结，耗时较长（约30-60秒），请稍候...")
        img_bytes, error_data = await self._fetch_image("/dashen-summary/week/image", {"bnet_id": target_id}, timeout=900)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取周度总结失败，请检查服务日志或是否请求超时。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取周度总结失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("大神数据", alias={'详情卡片', '战绩查询', '数据'})
    async def dashen_profile(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        bnet_id, mode = self._parse_profile_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/大神数据 Player#12345 或先使用 /绑定 指令")
            return
        yield event.plain_result(f"🔍 正在生成 {target_id} 的玩家详情...")
        img_bytes, error_data = await self._fetch_image("/dashen-profile/image", {"bnet_id": target_id, "mode": mode})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取玩家详情卡片失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取玩家详情失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("大神对局", alias={'最近对局', '战绩', '对局'})
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/大神对局 Player#12345 或先使用 /绑定 指令")
            return
            
        session_id = self._get_session_id(event)
        self.last_match_bnet_id[session_id] = target_id

        yield event.plain_result(f"📊 正在拉取 {target_id} 的最近对局...")
        img_bytes, error_data = await self._fetch_image("/dashen-match/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取最近对局列表失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取最近对局失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("单局详细", alias={'单局'})
    async def dashen_match_detail(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        index = 0
        bnet_id = None
        
        for arg in [arg1, arg2]:
            if not arg:
                continue
            if arg.isdigit(): 
                digit = int(arg)
                if digit > 20:
                    yield event.plain_result("❌ 错误：单局详细的数字索引不能大于 20！")
                    return
                index = max(0, digit - 1) if digit > 0 else 0
            else: 
                bnet_id = arg

        if not bnet_id:
            session_id = self._get_session_id(event)
            bnet_id = self.last_match_bnet_id.get(session_id)

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/单局详细 1 Player#12345 或先使用 /绑定 指令")
            return
            
        yield event.plain_result(f"⏳ 正在拉取 {target_id} 第 {index + 1} 局的单局多图详细战绩...")
        
        payload = {
            "bnet_id": target_id,
            "index": str(index),
            "limit": "20",
            "include_fight": True,
            "include_previous_season": True,
            "show_all_heroes": True,
            "analyze": True
        }
        
        url = f"{self.base_url}/dashen-match/detail/replies"
        
        try:
            client_timeout = aiohttp.ClientTimeout(total=600)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        try:
                            error_data = await resp.json()
                            err_msg = error_data.get("message", "未知后端 service 错误")
                            yield event.plain_result(f"❌ 获取单局详细失败：{err_msg}")
                        except Exception:
                            yield event.plain_result(f"❌ 后端接口响应异常，状态码: {resp.status}")
                        return

                    data = await resp.json()
                    raw_img_list = data.get("replies", [])
                    
                    if not raw_img_list:
                        yield event.plain_result("❌ 未能生成该单局的详细图片链接")
                        return

                    imgs_list = []
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
                                async with session.get(full_img_url, timeout=30, ssl=False) as img_resp:
                                    if img_resp.status == 200:
                                        img_data = await img_resp.read()
                        except Exception as e:
                            logger.error(f"处理图片失败: {e}")
                            continue

                        if img_data:
                            imgs_list.append(img_data)
                    
                    if imgs_list:
                        async for r in self._send_multiple_images_result(event, imgs_list):
                            yield r
                    else:
                        yield event.plain_result("❌ 未能获取到有效的图片数据")
        except Exception as e:
            logger.error(f"处理单局详细图片异常: {e}")
            yield event.plain_result("❌ 处理图片请求时发生 system 错误")

    @filter.command("历史段位", alias={'历届段位'})
    async def dashen_rank_history(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/历史段位 Player#12345 或先使用 /绑定 指令")
            return
        yield event.plain_result(f"📜 正在追溯 {target_id} 的历史段位记录...")
        img_bytes, error_data = await self._fetch_image("/dashen-rank-history/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取历史段位失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取历史段位失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("同玩查询", alias={'开黑胜率'})
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str, p2: str):
        yield event.plain_result(f"👥 正在分析 {p1} 与 {p2} 的同玩胜率...")
        payload = {"player1_bnet_id": p1, "player2_bnet_id": p2}
        img_bytes, error_data = await self._fetch_image("/dashen-sameplay/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取同玩查询数据，请检查两个ID是否输入正确。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 同玩查询失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("快速强度", alias={'快速强度指数'})
    async def quick_strength(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/快速强度 Player#12345 或先使用 /绑定 指令")
            return
        yield event.plain_result(f"⚡ 正在评估 {target_id} 的快速强度指数...")
        img_bytes, error_data = await self._fetch_image("/dashen-quick-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取快速强度指数失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取快速强度指数失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("竞技强度", alias={'竞技强度指数'})
    async def competitive_strength(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/竞技强度 Player#12345 或先使用 /绑定 指令")
            return
        yield event.plain_result(f"🏆 正在评估 {target_id} 的竞技天梯强度指数...")
        img_bytes, error_data = await self._fetch_image("/dashen-competitive-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取竞技强度指数失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取竞技强度指数失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("快速英雄云图", alias={'快速云图'})
    async def quick_hero_treemap(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/快速英雄云图 Player#12345 或先使用 /绑定 指令")
            return
        
        yield event.plain_result(f"📊 正在获取 {target_id} 的快速模式英雄云图...")
        payload = {"bnet_id": target_id, "mode": "quick", "include_previous_season": True}
        if season: payload["season"] = str(season)
            
        img_bytes, error_data = await self._fetch_image("/dashen-hero-treemap/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取快速英雄云图失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取快速英雄云图失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("竞技英雄云图", alias={'竞技云图'})
    async def competitive_hero_treemap(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/竞技英雄云图 Player#12345 或先使用 /绑定 指令")
            return
        
        yield event.plain_result(f"🏆 正在获取 {target_id} 的竞技模式英雄云图...")
        payload = {"bnet_id": target_id, "mode": "competitive", "include_previous_season": True}
        if season: payload["season"] = str(season)
            
        img_bytes, error_data = await self._fetch_image("/dashen-hero-treemap/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取竞技英雄云图失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取竞技英雄云图失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @filter.command("威能")
    async def ow_hero_perk(self, event: AstrMessageEvent, hero_name: str):
        if not hero_name:
            yield event.plain_result("❌ 请输入英雄名称，如：/威能 源氏")
            return
        yield event.plain_result(f"🔮 正在提取 {hero_name} 的核心威能数据...")
        img_bytes, error_data = await self._fetch_image("/ow-hero-perk/image", {"hero": hero_name})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else f"未能找到英雄【{hero_name}】的威能图。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("ow英雄")
    async def ow_hero_pick(self, event: AstrMessageEvent, hero_name: str):
        if not hero_name:
            yield event.plain_result("❌ 请输入英雄名称，如：/ow英雄 源氏")
            return
        yield event.plain_result(f"🔥 正在读取 {hero_name} 的天梯 Pick 率走势图...")
        payload = {"view": "history", "game_mode": "competitive", "mmr": "all", "hero": hero_name}
        img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else f"暂时无法获取英雄 {hero_name} 的数据走势。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("商店", alias={'ow商店'})
    async def ow_shop(self, event: AstrMessageEvent):
        yield event.plain_result("🛍️ 正在获取今日精选商店皮肤商品...")
        img_bytes, error_data = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取精选商店图片失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("ow赛事", alias={'赛事'})
    async def ow_esports(self, event: AstrMessageEvent):
        yield event.plain_result("🎮 正在从 Pandascore 获取实时赛事对阵...")
        img_bytes, error_data = await self._fetch_image("/ow-esports/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("获取段位分布")
    async def get_rank_distribution(self, event: AstrMessageEvent):
        yield event.plain_result("📊 正在统计天梯全服大盘全英雄数据排行与环境分布...")
        payload = {"view": "ranking", "game_mode": "competitive", "mmr": "all"}
        img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取全服天梯分布排行。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("ow活动", alias={'活动'})
    async def ow_activities(self, event: AstrMessageEvent):
        yield event.plain_result("🎉 正在拉取当前版本限时节日/赛季大活动公告卡片...")
        img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "big"})
        if not img_bytes:
            img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "暂无正在进行的版本活动公告。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("banpick", alias={'全英雄排行'})
    async def ban_pick_stats(self, event: AstrMessageEvent):
        yield event.plain_result("🚫 正在获取本周天梯英雄大盘选禁用排行...")
        payload = {"view": "ranking", "game_mode": "competitive", "mmr": "all"}
        img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取全英雄排行。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("mappick")
    async def map_pick_stats(self, event: AstrMessageEvent):
        yield event.plain_result("🗺️ 正在从最新版本补丁中检索当前赛季地图池与轮换出场...")
        img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法拉取最新地图池分布。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str = ""):
        yield event.plain_result(f"🔍 正在检索包含关键词【{keyword or '最新'}】的精选上架皮肤商品卡片...")
        img_bytes, error_data = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取精选皮肤卡片。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("ow更新", alias={'版本更新'})
    async def ow_patch_notes(self, event: AstrMessageEvent, kind: str = "latest"):
        valid_kinds = ["latest", "small", "big"]
        if kind not in valid_kinds:
            yield event.plain_result("❌ 参数错误。支持的日志类型：latest, small, big\n例如：/ow更新 small")
            return
            
        yield event.plain_result(f"📰 正在拉取外服 {kind} 更新日志卡片...")
        img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": kind})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取更新日志失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("省榜", alias={'排行'})
    async def ow_rank_leaderboard(self, event: AstrMessageEvent, province: str, role: str):
        if not province or not role:
            yield event.plain_result("❌ 请输入省份名称 and 职责位置，例如：/省榜 北京 tank\n(支持的位置: tank / dps / healer / open)")
            return
            
        yield event.plain_result(f"🏆 正在获取 {province} 地区 【{role}】 位置的大神天梯省榜...")
        payload = {"province": province, "role": role}
        img_bytes, error_data = await self._fetch_image("/dashen-rank-leaderboard/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取天梯省榜失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @filter.command("绝活榜", alias={'英雄省榜'})
    async def ow_hero_leaderboard(self, event: AstrMessageEvent, province: str, hero: str):
        if not province or not hero:
            yield event.plain_result("❌ 请输入省份和英雄名称，例如：/绝活榜 北京 猎空")
            return
            
        yield event.plain_result(f"🎖️ 正在获取 {province} 地区 【{hero}】 的大神英雄专精绝活榜...")
        payload = {"province": province, "hero": hero, "mode": "preset"}
        img_bytes, error_data = await self._fetch_image("/dashen-hero-leaderboard/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取英雄绝活榜失败。"
            yield event.plain_result(f"❌ {err_msg}")
