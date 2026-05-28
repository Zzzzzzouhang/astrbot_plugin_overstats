import os
import aiohttp
import logging
import tempfile
import asyncio
import re
import base64
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "1.1.18")
class OverstatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = self.config.get("overstats_api_url", "http://127.0.0.1:18080/api/v2")
        try:
            plugin_name = getattr(self, "name", "overstats_full")
            self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            
            # --- 新增：专属临时图片存放目录 ---
            self.temp_image_dir = self.plugin_data_dir / "temp"
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            
        except Exception as e:
            logger.warning(f"初始化插件专属数据目录失败: {e}")
            self.plugin_data_dir = Path(tempfile.gettempdir())
            self.temp_image_dir = self.plugin_data_dir / "temp"
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str = None) -> str:
        if input_id and input_id.strip():
            return input_id.strip()
        user_id = event.get_sender_id()
        bind_id = await self.get_kv_data(f"bind_{user_id}", None)
        return bind_id

    def _parse_treemap_args(self, arg1: str = None, arg2: str = None) -> tuple[str | None, str | None]:
        """智能解析云图指令的参数组合"""
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

    def _parse_profile_args(self, arg1: str = None, arg2: str = None) -> tuple[str | None, str]:
        """智能解析大神数据的参数组合，默认模式为 competitive"""
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
            try:
                os.remove(path)
                logger.debug(f"临时战绩图片已成功自动清理: {path}")
            except Exception as e:
                logger.warning(f"自动清理临时战绩图片失败: {e}")

    async def _delayed_remove(self, path: str, delay: int):
        await asyncio.sleep(delay)
        self._safe_remove(path)

    def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes, fallback_text: str = ""):
        """单图发送组件，采用 hash 命名和独立目录"""
        if not img_bytes:
            if fallback_text:
                return event.plain_result(fallback_text)
            return event.plain_result("❌ 图片生成失败，且未提供备用文本。")

        try:
            # 确保目录存在
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            
            # 使用内容 hash 作为文件名，避免重复和临时文件堆积
            img_hash = abs(hash(img_bytes))
            img_path = self.temp_image_dir / f"{img_hash}.png"
            img_path.write_bytes(img_bytes)
            
            user_id = event.get_sender_id()
            
            chain = [
                Comp.At(qq=user_id),
                Comp.Plain("\n" if not fallback_text else f"\n{fallback_text}\n"),
                Comp.Image.fromFileSystem(str(img_path))
            ]
            
            # 延迟 30 分钟清理
            asyncio.create_task(self._delayed_remove(str(img_path), 1800))
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f"构建图片消息链时发生严重错误: {e}")
            if fallback_text:
                return event.plain_result(fallback_text)
            return event.plain_result(f"❌ 机器人构建图片组件失败: {e}")

    def _send_multiple_images_result(self, event: AstrMessageEvent, imgs_list: list[bytes]):
        """多图发送组件，采用 hash 命名和独立目录"""
        try:
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            user_id = event.get_sender_id()
            chain = [Comp.At(qq=user_id), Comp.Plain("\n")]
            
            for img_bytes in imgs_list:
                if not img_bytes:
                    continue
                
                img_hash = abs(hash(img_bytes))
                img_path = self.temp_image_dir / f"{img_hash}.png"
                img_path.write_bytes(img_bytes)
                
                chain.append(Comp.Image.fromFileSystem(str(img_path)))
                asyncio.create_task(self._delayed_remove(str(img_path), 1800))
                
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f"构建多图消息链时发生严重错误: {e}")
            return event.plain_result(f"❌ 机器人构建多图组件失败: {e}")

    # 智能全局事件拦截器
    @event_message_type(EventMessageType.ALL)
    async def intercept_text_at(self, event: AstrMessageEvent):
        msg = event.message_str
        if not msg:
            return
            
        is_at_me = False
        clean_msg = msg.strip()
        
        # 1. 检查平台底层组件是否真的艾特了本机器人
        if event.message_obj and event.message_obj.message:
            for comp in event.message_obj.message:
                if comp.__class__.__name__ == "At":
                    for attr in ['qq', 'user_id', 'id', 'target', 'self_id']:
                        if hasattr(comp, attr) and str(getattr(comp, attr)) == str(event.message_obj.self_id):
                            is_at_me = True
                            break
                if is_at_me:
                    break
        
        # 2. 检查是否包含纯文本开头的 “@电子路灯”
        is_text_at_lampion = False
        if clean_msg.startswith("@电子路灯"):
            is_text_at_lampion = True
            clean_msg = clean_msg[len("@电子路灯"):].strip()
        
        # 3. 如果是真正的 At 组件触发，提取出 @ 之后的纯净指令文本
        if is_at_me and clean_msg.startswith("@"):
            match_at_text = re.match(r'^@\S+\s+(.*)$', clean_msg)
            if match_at_text:
                clean_msg = match_at_text.group(1).strip()
            else:
                clean_msg = re.sub(r'^@\S+', '', clean_msg).strip()
        
        # 允许私聊直接触发（不需要艾特）
        is_private = event.message_obj and not event.message_obj.group_id
        
        # 核心触发判定：必须满足【真艾特】或【纯文本@电子路灯】或【私聊】
        is_triggered = is_at_me or is_text_at_lampion or is_private
        
        if not is_triggered:
            return
        
        # 1. 定义战网ID正则 (允许中文/英文/数字/下划线/点/杠 + # + 数字)
        bnet_id_pattern = r'([\w\u4e00-\u9fa5\-\_\.]+#\d+)'
        
        # 2. 在 clean_msg 中搜索该模式
        match = re.search(bnet_id_pattern, clean_msg)
        
        if match and ("绑定" in clean_msg or re.fullmatch(bnet_id_pattern, clean_msg)):
            new_bind_id = match.group(1)
            
            # 【新增修复】如果提取的 ID 开头包含了“绑定”，将其删除
            if new_bind_id.startswith("绑定"):
                new_bind_id = new_bind_id[2:]
                
            user_id = event.get_sender_id()
            old_bind_id = await self.get_kv_data(f"bind_{user_id}", None)
            
            await self.put_kv_data(f"bind_{user_id}", new_bind_id)
            
            if not old_bind_id:
                yield event.plain_result(f"✅ 自动绑定成功！已为您关联战网账号【{new_bind_id}】")
            else:
                yield event.plain_result(f"✅ 自动更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】")
            
            event.stop_event()
            return

        # 检查是否是纯数字或以“单局详细”开头的快捷指令（为了捕捉单独回复数字的场景）
        if clean_msg.isdigit() or (clean_msg.startswith("单局") and any(char.isdigit() for char in clean_msg)):
            # 提取数字
            num_match = re.search(r'\d+', clean_msg)
            if num_match:
                digit = int(num_match.group())
                if digit < 0:
                    digit = 0
                elif digit > 20:
                    yield event.plain_result("❌ 错误：单局详细的数字索引不能大于 20！")
                    event.stop_event()
                    return
                
                # 路由到单局详细处理 (此处修改：直接传入原始数字，不再减 1)
                async for r in self.dashen_match_detail(event, str(digit)):
                    yield r
                event.stop_event()
                return
        
        cmd_map = {
            "owhelp": (self.ow_help, 0),
            "ow帮助": (self.ow_help, 0),
            "OW帮助": (self.ow_help, 0),
            "ow菜单": (self.ow_help, 0),
            
            "大神绑定": (self.dashen_bind, 1),
            "绑定": (self.dashen_bind, 1),
            
            "今日总结": (self.dashen_today, 1),
            "今日": (self.dashen_today, 1),
            "今日数据": (self.dashen_today, 1),
            
            "昨日总结": (self.dashen_yesterday, 1),
            "昨日": (self.dashen_yesterday, 1),
            "昨日数据": (self.dashen_yesterday, 1),
            "昨天数据": (self.dashen_yesterday, 1),
            "昨天": (self.dashen_yesterday, 1),
            
            "周度总结": (self.dashen_week, 1),
            "本周总结": (self.dashen_week, 1),
            "本周数据": (self.dashen_week, 1),
            "本周": (self.dashen_week, 1),
            
            "大神数据": (self.dashen_profile, 'var'),
            "详情卡片": (self.dashen_profile, 'var'),
            "战绩查询": (self.dashen_profile, 'var'),
            
            "大神对局": (self.dashen_match, 1),
            "最近对局": (self.dashen_match, 1),
            
            "单局详细": (self.dashen_match_detail, 'var'),
            "单局": (self.dashen_match_detail, 'var'),
            
            "历史段位": (self.dashen_rank_history, 1),
            "历届段位": (self.dashen_rank_history, 1),
            
            "同玩查询": (self.dashen_sameplay, 2),
            "开黑胜率": (self.dashen_sameplay, 2),
            
            "快速强度": (self.quick_strength, 1),
            "快速强度指数": (self.quick_strength, 1),
            
            "竞技强度": (self.competitive_strength, 1),
            "竞技强度指数": (self.competitive_strength, 1),
            
            "快速英雄云图": (self.quick_hero_treemap, 'var'),
            "快速云图": (self.quick_hero_treemap, 'var'),
            "竞技英雄云图": (self.competitive_hero_treemap, 'var'),
            "竞技云图": (self.competitive_hero_treemap, 'var'),
            
            "威能": (self.ow_hero_perk, 1),
            "ow英雄": (self.ow_hero_pick, 1),
            
            "商店": (self.ow_shop, 0),
            "ow商店": (self.ow_shop, 0),
            
            "ow赛事": (self.ow_esports, 0),
            "赛事": (self.ow_esports, 0),
            
            "获取段位分布": (self.get_rank_distribution, 0),
            
            "ow活动": (self.ow_activities, 0),
            "活动": (self.ow_activities, 0),
            
            "banpick": (self.ban_pick_stats, 0),
            "全英雄排行": (self.ban_pick_stats, 0),
            
            "mappick": (self.map_pick_stats, 0),
            "皮肤搜索": (self.skin_search, 1),

            "ow更新": (self.ow_patch_notes, 1),
            "版本更新": (self.ow_patch_notes, 1),
            "省榜": (self.ow_rank_leaderboard, 2),
            "排行": (self.ow_rank_leaderboard, 2),
            "绝活榜": (self.ow_hero_leaderboard, 2),
            "英雄省榜": (self.ow_hero_leaderboard, 2)
        }

        clean_msg = clean_msg.lstrip('/')
        
        cmd = None
        cmd_args = []
        
        for k in sorted(cmd_map.keys(), key=len, reverse=True):
            if clean_msg.startswith(k):
                cmd = k
                remain = clean_msg[len(k):].strip()
                cmd_args = remain.split() if remain else []
                break
                
        if not cmd:
            parts = clean_msg.split(maxsplit=1)
            if parts and parts[0] in cmd_map:
                cmd = parts[0]
                cmd_args = parts[1].split() if len(parts) > 1 else []
        
        if cmd in cmd_map:
            func, arg_count = cmd_map[cmd]
            try:
                if arg_count == 0:
                    async for r in func(event): yield r
                elif arg_count == 1:
                    passed_arg = cmd_args[0] if cmd_args else None
                    async for r in func(event, passed_arg): yield r
                elif arg_count == 2:
                    if len(cmd_args) >= 2:
                        async for r in func(event, cmd_args[0], cmd_args[1]): yield r
                    else:
                        yield event.plain_result(f"❌ 【{cmd}】指令需要提供两个参数。")
                elif arg_count == 'var':
                    arg1 = cmd_args[0] if len(cmd_args) > 0 else None
                    arg2 = cmd_args[1] if len(cmd_args) > 1 else None
                    async for r in func(event, arg1, arg2): yield r
            except Exception as e:
                logger.error(f"纯文本快捷指令分发执行失败 ({cmd}): {e}")
                yield event.plain_result(f"❌ 执行指令失败: {str(e)}")
            
            event.stop_event()

    @command("owhelp", alias=["ow菜单", "ow帮助", "OW帮助"])
    async def ow_help(self, event: AstrMessageEvent):
        help_text = (
            "📌 Overstats 查询菜单\n"
            "🔗 ➤ 绑定「战网 ID」\n"
            "📋 ➤ 今日 ➤ 昨日 ➤ 本周\n"
            "📊 ➤ 大神数据 ➤ 大神对局 ➤ 单局详细「数字」\n"
            "📈 ➤ 快速强度 ➤ 竞技强度 ➤ 获取段位分布\n"
            "🗺️ ➤ 快速云图 ➤ 竞技云图\n"
            "🏆 ➤ 省榜「省份」「位置」 ➤ 绝活榜「省份」「英雄」\n"
            "⚔️ ➤ 威能「英雄名」 ➤ ow 英雄「英雄名」 ➤ banpick ➤ mappick\n"
            "🌍 ➤ 同玩查询「ID1」「ID2」 ➤ 商店 ➤ 皮肤搜索 ➤ ow 赛事\n"
            "📰 ➤ ow更新「latest/small/big」\n"
            "💡 提示：在「大神对局」图片发出来后，直接回复数字即可看单局详细。"
        )
        yield event.plain_result(help_text)

    @command("大神绑定", alias=["绑定"])
    async def dashen_bind(self, event: AstrMessageEvent, bnet_id: str):
        user_id = event.get_sender_id()
        if not bnet_id or "#" not in bnet_id:
            yield event.plain_result("❌ 绑定失败！请输入规范战网ID，如：Player#12345")
            return
        
        old_bind_id = await self.get_kv_data(f"bind_{user_id}", None)
        new_bind_id = bnet_id.strip()
        await self.put_kv_data(f"bind_{user_id}", new_bind_id)
        
        if not old_bind_id:
            yield event.plain_result(f"✅ 绑定成功！关联战网账号【{new_bind_id}】")
        else:
            yield event.plain_result(f"✅ 更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】")

    @command("今日总结", alias=["今日", "今日数据"])
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：今日总结 Player#12345 或先使用 /绑定 指令")
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

    @command("昨日总结", alias=["昨日", "昨日数据", "昨天数据", "昨天"])
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：昨日总结 Player#12345 或先使用 /绑定 指令")
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

    @command("周度总结", alias=["本周总结", "本周数据", "本周"])
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：周度总结 Player#12345 或先使用 /绑定 指令")
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

    @command("大神数据", alias=["详情卡片", "战绩查询"])
    async def dashen_profile(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        bnet_id, mode = self._parse_profile_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：大神数据 Player#12345 或先使用 /绑定 指令")
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

    @command("大神对局", alias=["最近对局"])
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：大神对局 Player#12345 或先使用 /绑定 指令")
            return
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

    @command("单局详细", alias=["单局"])
    async def dashen_match_detail(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """
        支持以下任意顺序的参数形式：
        /单局详细 1
        /单局详细 1 Player#12345
        /单局详细 Player#12345 1
        """
        index = 0
        bnet_id = None
        
        for arg in [arg1, arg2]:
            if not arg:
                continue
            if arg.isdigit():  # 如果是纯数字，说明是局数索引
                digit = int(arg)
                if digit > 20:
                    yield event.plain_result("❌ 错误：单局详细的数字索引不能大于 20！")
                    return
                index = max(0, digit - 1) if digit > 0 else 0
            else:  # 如果不是纯数字，那必然是战网 ID
                bnet_id = arg


        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：/单局详细 1 Player#12345 或先使用 /绑定 指令")
            return
            
        yield event.plain_result(f"⏳ 正在拉取 {target_id} 第 {index + 1} 局的单局多图详细战绩...")
        
        # 2. 封装请求参数 (保持你原本的逻辑不变)
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
                    
                    # 3. 线性循环处理图片
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
                                try:
                                    img_data = base64.b64decode(img_str)
                                except Exception:
                                    pass
                            
                            if not img_data:
                                full_img_url = img_str if img_str.startswith("http") else f"{self.base_url.rstrip('/').removesuffix('/api/v2')}{img_str if img_str.startswith('/') else '/' + img_str}"
                                async with session.get(full_img_url, timeout=30, ssl=False) as img_resp:
                                    if img_resp.status == 200:
                                        img_data = await img_resp.read()
                                    else:
                                        logger.error(f"下载战绩图片失败: {img_resp.status}, URL: {full_img_url}")

                        except Exception as e:
                            logger.error(f"处理图片失败: {e}")
                            continue

                        if img_data:
                            imgs_list.append(img_data)
                    
                    # 4. 发送最终结果
                    if imgs_list:
                        yield self._send_multiple_images_result(event, imgs_list)
                    else:
                        yield event.plain_result("❌ 未能获取到有效的图片数据")

        except Exception as e:
            logger.error(f"处理单局详细图片异常: {e}")
            yield event.plain_result("❌ 处理图片请求时发生 system 错误")

    @command("历史段位", alias=["历届段位"])
    async def dashen_rank_history(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：历史段位 Player#12345 或先使用 /绑定 指令")
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

    @command("同玩查询", alias=["开黑胜率"])
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

    @command("快速强度", alias=["快速强度指数"])
    async def quick_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：快速强度 Player#12345 或先使用 /绑定 指令")
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

    @command("竞技强度", alias=["竞技强度指数"])
    async def competitive_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：竞技强度 Player#12345 或先使用 /绑定 指令")
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

    @command("快速英雄云图", alias=["快速云图"])
    async def quick_hero_treemap(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：快速英雄云图 Player#12345 或先使用 /绑定 指令")
            return
        
        yield event.plain_result(f"📊 正在获取 {target_id} 的快速模式英雄云图...")
        payload = {
            "bnet_id": target_id,
            "mode": "quick",
            "include_previous_season": True
        }
        if season:
            payload["season"] = str(season)
            
        img_bytes, error_data = await self._fetch_image("/dashen-hero-treemap/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取快速英雄云图失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取快速英雄云图失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @command("竞技英雄云图", alias=["竞技云图"])
    async def competitive_hero_treemap(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，如：竞技英雄云图 Player#12345 或先使用 /绑定 指令")
            return
        
        yield event.plain_result(f"🏆 正在获取 {target_id} 的竞技模式英雄云图...")
        payload = {
            "bnet_id": target_id,
            "mode": "competitive",
            "include_previous_season": True
        }
        if season:
            payload["season"] = str(season)
            
        img_bytes, error_data = await self._fetch_image("/dashen-hero-treemap/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取竞技英雄云图失败。"
            if err_msg and "Could not resolve customerToken" in err_msg:
                yield event.plain_result("❌ 获取竞技英雄云图失败：未查询到id或者id错误")
            else:
                yield event.plain_result(f"❌ {err_msg}")

    @command("威能")
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

    @command("ow英雄")
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

    @command("商店", alias=["ow商店"])
    async def ow_shop(self, event: AstrMessageEvent):
        yield event.plain_result("🛍️ 正在获取今日精选商店皮肤商品...")
        img_bytes, error_data = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取精选商店图片失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("ow赛事", alias=["赛事"])
    async def ow_esports(self, event: AstrMessageEvent):
        yield event.plain_result("🎮 正在从 Pandascore 获取实时赛事对阵...")
        img_bytes, error_data = await self._fetch_image("/ow-esports/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("获取段位分布")
    async def get_rank_distribution(self, event: AstrMessageEvent):
        yield event.plain_result("📊 正在统计天梯全服大盘全英雄数据排行与环境分布...")
        payload = {"view": "ranking", "game_mode": "competitive", "mmr": "all"}
        img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取全服天梯分布排行。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("ow活动", alias=["活动"])
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

    @command("banpick", alias=["全英雄排行"])
    async def ban_pick_stats(self, event: AstrMessageEvent):
        yield event.plain_result("🚫 正在获取本周天梯英雄大盘选禁用排行...")
        payload = {"view": "ranking", "game_mode": "competitive", "mmr": "all"}
        img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取全英雄排行。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("mappick")
    async def map_pick_stats(self, event: AstrMessageEvent):
        yield event.plain_result("🗺️ 正在从最新版本补丁中检索当前赛季地图池与轮换出场...")
        img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法拉取最新地图池分布。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str):
        yield event.plain_result(f"🔍 正在检索包含关键词【{keyword or '最新'}】的精选上架皮肤商品卡片...")
        img_bytes, error_data = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取精选皮肤卡片。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("ow更新", alias=["版本更新"])
    async def ow_patch_notes(self, event: AstrMessageEvent, kind: str = None):
        kind = kind or "latest"
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

    @command("省榜", alias=["排行"])
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

    @command("绝活榜", alias=["英雄省榜"])
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
