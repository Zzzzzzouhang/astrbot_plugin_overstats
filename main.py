import os
import aiohttp
import logging
import tempfile
import threading
import re
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "1.1.11")
class OverstatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = self.config.get("overstats_api_url", "http://127.0.0.1:18080/api/v2")
        try:
            plugin_name = getattr(self, "name", "overstats_full")
            self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"初始化插件专属数据目录失败: {e}")
            self.plugin_data_dir = Path(tempfile.gettempdir())

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str = None) -> str:
        if input_id and input_id.strip():
            return input_id.strip()
        user_id = event.get_sender_id()
        bind_id = await self.get_kv_data(f"bind_{user_id}", None)
        return bind_id

    # 改造后的_fetch_image：返回(图片字节, 错误详情)元组
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

    def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes):
        try:
            with tempfile.NamedTemporaryFile(dir=str(self.plugin_data_dir), delete=False, suffix=".png") as f:
                f.write(img_bytes)
                temp_file_path = f.name
            chain = [Comp.Image.fromFileSystem(temp_file_path)]
            threading.Timer(1800.0, self._safe_remove, args=[temp_file_path]).start()
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f"构建图片消息链时发生严重错误: {e}")
            return event.plain_result(f"❌ 机器人构建图片组件失败: {e}")

    # 简化后的全局拦截器：仅支持纯文本@电子路灯
    @event_message_type(EventMessageType.ALL)
    async def intercept_text_at(self, event: AstrMessageEvent):
        msg = event.message_str
        if not msg:
            return
        
        # 仅检查是否包含纯文本 "@电子路灯"
        if "@电子路灯" not in msg:
            return
        
        # 剥离掉称呼本身
        clean_msg = msg.replace("@电子路灯", "").strip()
        
        # 逻辑1：如果剩下的纯粹是一个符合规范的战网ID，则触发自动绑定
        if re.match(r'^[\w\u4e00-\u9fa5\-\s]+#\d+$', clean_msg):
            user_id = event.get_sender_id()
            old_bind_id = await self.get_kv_data(f"bind_{user_id}", None)
            new_bind_id = clean_msg.strip()
            
            await self.put_kv_data(f"bind_{user_id}", new_bind_id)
            
            if not old_bind_id:
                yield event.plain_result(f"✅ 自动绑定成功！已为您关联战网账号【{new_bind_id}】")
            else:
                yield event.plain_result(f"✅ 自动更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】")
            
            # 阻止消息继续传播
            event.stop_propagation()
            return
        
        # 逻辑2：如果是带了指令（如：今日总结 或 今日总结 某个ID）
        parts = clean_msg.split(maxsplit=1)
        if not parts:
            return
        
        cmd = parts[0]
        # 获取指令后面的参数（如果有的话）
        cmd_args = parts[1].split() if len(parts) > 1 else []
        # 路由映射表：纯文本指令 -> (对应的方法, 所需固定参数个数)
        cmd_map = {
            "今日总结": (self.dashen_today, 1),
            "昨日总结": (self.dashen_yesterday, 1),
            "周度总结": (self.dashen_week, 1),
            "本周总结": (self.dashen_week, 1),
            "大神数据": (self.dashen_profile, 1),
            "大神对局": (self.dashen_match, 1),
            "历史段位": (self.dashen_rank_history, 1),
            "快速强度指数": (self.quick_strength, 1),
            "竞技强度指数": (self.competitive_strength, 1),
            "威能": (self.ow_hero_perk, 1),
            "ow英雄": (self.ow_hero_pick, 1),
            "皮肤搜索": (self.skin_search, 1),
            "大神绑定": (self.dashen_bind, 1),
            "同玩查询": (self.dashen_sameplay, 2),
            "商店": (self.ow_shop, 0),
            "ow赛事": (self.ow_esports, 0),
            "ow活动": (self.ow_activities, 0),
            "banpick": (self.ban_pick_stats, 0),
            "mappick": (self.map_pick_stats, 0),
            "owhelp": (self.ow_help, 0),
            "获取段位分布": (self.get_rank_distribution, 0)
        }
        
        if cmd in cmd_map:
            func, arg_count = cmd_map[cmd]
            try:
                if arg_count == 0:
                    async for r in func(event): yield r
                elif arg_count == 1:
                    # 参数可选的指令传第一个参数或 None，如果是必须带参数的指令（如威能）交由原函数自己内部处理
                    passed_arg = cmd_args[0] if cmd_args else None
                    async for r in func(event, passed_arg): yield r
                elif arg_count == 2:
                    if len(cmd_args) >= 2:
                        async for r in func(event, cmd_args[0], cmd_args[1]): yield r
                    else:
                        yield event.plain_result(f"❌ 【{cmd}】指令需要提供两个参数（例如：同玩查询 ID1 ID2）。")
            except Exception as e:
                logger.error(f"纯文本快捷指令分发执行失败 ({cmd}): {e}")
            
            # 阻止消息继续传播
            event.stop_propagation()

    @command("owhelp")
    async def ow_help(self, event: AstrMessageEvent):
        help_text = (
            "📌 守望先锋 Overstats 查询菜单：\n"
            "=======================\n"
            "👉【数据/战绩总结】\n"
            "   /大神绑定 [战网ID] - 绑定QQ与战网账号\n"
            "   @电子路灯 [战网ID] - 快速自动绑定/更新战网账号\n"
            "   /大神数据 (战网ID) - 查询玩家详情卡片\n"
            "   /大神对局 (战网ID) - 查询最近对局列表\n"
            "   /今日总结 (战网ID) - 查询今日战绩总结（无记录自动查昨日）\n"
            "   /昨日总结 (战网ID) - 查询昨日战绩数据\n"
            "   /周度总结 (战网ID) - 查询本周数据总结\n"
            "   /历史段位 (战网ID) - 查询历届赛季段位\n"
            "   /同玩查询 [战网ID1] [战网ID2] - 查询两人开黑胜率\n"
            "👉【指数/英雄分析】\n"
            "   /快速强度指数 (战网ID)\n"
            "   /竞技强度指数 (战网ID)\n"
            "   /威能 [英雄名]\n"
            "   /ow英雄 [英雄名]\n"
            "👉【综合/赛事】\n"
            "   /商店 /ow赛事 /ow活动 /皮肤搜索\n"
            "=======================\n"
            "💡 带()可省略，需先绑定账号。"
        )
        yield event.plain_result(help_text)

    @command("大神绑定")
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

    # 改造后的今日总结：自动处理空记录并转昨日总结
    @command("今日总结")
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 或 @电子路灯 [战网ID]")
            return
        
        yield event.plain_result(f"⏳ 正在计算 {target_id} 的今日战绩总结...")
        img_bytes, error_data = await self._fetch_image("/dashen-summary/today/image", {"bnet_id": target_id})
        
        if img_bytes:
            # 正常返回今日总结图片
            yield self._send_image_result(event, img_bytes)
        elif error_data and error_data.get("error") == "summary_empty" and error_data.get("details", {}).get("scope") == "today":
            # 今日无对局，自动转昨日总结
            yield event.plain_result(f"ℹ️ 你在过去的 24 小时内没有对局记录，尝试生成昨日总结...")
            async for result in self.dashen_yesterday(event, target_id):
                yield result
        else:
            # 其他错误情况
            err_msg = error_data.get("message") if error_data else "未知错误"
            yield event.plain_result(f"❌ 获取今日总结失败：{err_msg}")

    @command("昨日总结")
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"⏳ 正在统计 {target_id} 的昨日战绩数据...")
        img_bytes, error_data = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取昨日总结失败，可能昨日未登录游戏。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("周度总结", alias=["本周总结"])
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"📊 正在生成 {target_id} 的本周战绩大数据总结，耗时较长（约30-60秒），请稍候...")
        img_bytes, error_data = await self._fetch_image("/dashen-summary/week/image", {"bnet_id": target_id}, timeout=900)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取周度总结失败，请检查服务日志或是否请求超时。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("大神数据")
    async def dashen_profile(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"🔍 正在生成 {target_id} 的玩家详情...")
        img_bytes, error_data = await self._fetch_image("/dashen-profile/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取玩家详情卡片失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("大神对局")
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"📊 正在拉取 {target_id} 的最近对局...")
        img_bytes, error_data = await self._fetch_image("/dashen-match/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取最近对局列表失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("历史段位")
    async def dashen_rank_history(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"📜 正在追溯 {target_id} 的历史段位记录...")
        img_bytes, error_data = await self._fetch_image("/dashen-rank-history/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取历史段位失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("同玩查询")
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str, p2: str):
        yield event.plain_result(f"👥 正在分析 {p1} 与 {p2} 的同玩胜率...")
        payload = {"player1_bnet_id": p1, "player2_bnet_id": p2}
        img_bytes, error_data = await self._fetch_image("/dashen-sameplay/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "无法获取同玩查询数据，请检查两个ID是否输入正确。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("快速强度指数")
    async def quick_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"⚡ 正在评估 {target_id} 的快速强度指数...")
        img_bytes, error_data = await self._fetch_image("/dashen-quick-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取快速强度指数失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("竞技强度指数")
    async def competitive_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"🏆 正在评估 {target_id} 的竞技天梯强度指数...")
        img_bytes, error_data = await self._fetch_image("/dashen-competitive-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取竞技强度指数失败。"
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

    @command("商店")
    async def ow_shop(self, event: AstrMessageEvent):
        yield event.plain_result("🛍️ 正在获取今日精选商店皮肤商品...")
        img_bytes, error_data = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            err_msg = error_data.get("message") if error_data else "获取精选商店图片失败。"
            yield event.plain_result(f"❌ {err_msg}")

    @command("ow赛事")
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
        yield event.plain_result("📊 正在统计天梯全服段位分布数据... [该模块正在开发对接中]")

    @command("ow活动")
    async def ow_activities(self, event: AstrMessageEvent):
        yield event.plain_result("🎉 正在拉取当前版本节日/赛季活动公告... [本地接口升级中]")

    @command("banpick")
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
        yield event.plain_result("🗺️ 正在拉取全模式地图胜率及出场分布... [当前接口暂不可用]")

    @command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str):
        if not keyword:
            yield event.plain_result("❌ 请输入搜索关键词")
            return
        yield event.plain_result(f"🔍 正在检索包含关键词【{keyword}】的英雄外观与内购历史... [开发中]")
