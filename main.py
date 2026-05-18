import os
import aiohttp
import logging
import tempfile
import threading
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "1.1.6")
class OverstatsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_url = "http://host.docker.internal:18080/api/v2"
        
        # 1. 依照官方规范，定义插件专属的大文件/媒体数据存储目录
        try:
            # self.name 在 v4.9.2+ 可用，若低于此版本将降级使用注册的名称
            plugin_name = getattr(self, "name", "overstats_full")
            self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"初始化插件专属数据目录失败，将使用系统默认缓存目录: {e}")
            self.plugin_data_dir = Path(tempfile.gettempdir())

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str = None) -> str:
        """辅助方法：获取战网ID，优先使用输入值，否则读取官方 KV 绑定的数据"""
        if input_id and input_id.strip():
            return input_id.strip()
        
        user_id = event.get_sender_id()
        # 核心改动：改用官方异步 get_kv_data 获取持久化战网 ID，找不到默认返回 None
        bind_id = await self.get_kv_data(f"bind_{user_id}", None)
        return bind_id

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 600) -> bytes:
        """辅助方法：请求指定的图片 API 并返回二进制字节流"""
        url = f"{self.base_url}{endpoint}"
        payload = payload or {}
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        try:
                            error_data = await resp.json()
                            logger.error(f"Overstats API 错误: {resp.status} - {error_data}")
                        except:
                            logger.error(f"Overstats API 返回了非 JSON 错误: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"网络请求异常: {e}")
            return None

    def _safe_remove(self, path: str):
        """延迟删除文件的安全回调函数"""
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logger.debug(f"临时战绩图片已成功自动清理: {path}")
            except Exception as e:
                logger.warning(f"自动清理临时战绩图片失败: {e}")

    def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes):
        """
        通过标准的 Comp.Image.fromFileSystem 发送消息链。
        将大图片文件规范存放于插件独立目录下，并在 30 分钟（1800秒）后安全自动销毁。
        """
        try:
            # 依照官方存储规范，在插件独立目录下创建唯一的临时图片文件
            with tempfile.NamedTemporaryFile(dir=str(self.plugin_data_dir), delete=False, suffix=".png") as f:
                f.write(img_bytes)
                temp_file_path = f.name
            
            # 使用官方标准的本地文件图片组件
            chain = [
                Comp.Image.fromFileSystem(temp_file_path)
            ]
            
            # 开启异步定时器，30分钟（1800秒）后自动在后台删除该临时文件
            threading.Timer(1800.0, self._safe_remove, args=[temp_file_path]).start()
            
            # 使用官方标准的 chain_result 返回消息链
            return event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"构建图片消息链时发生严重错误: {e}")
            return event.plain_result(f"❌ 机器人构建图片组件失败: {e}")

    # ==================== 1. 基础与管理指令 ====================

    @command("owhelp")
    async def ow_help(self, event: AstrMessageEvent):
        '''显示所有守望先锋查询指令菜单'''
        help_text = (
            "📌 守望先锋 Overstats 查询菜单：\n"
            "=======================\n"
            "👉【数据/战绩总结】\n"
            "   /大神绑定 [战网ID] - 绑定QQ与战网账号\n"
            "   /大神数据 (战网ID) - 查询玩家详情卡片\n"
            "   /大神对局 (战网ID) - 查询最近对局列表\n"
            "   /今日总结 (战网ID) - 查询今日战绩总结\n"
            "   /昨日总结 (战网ID) - 查询昨日战绩数据\n"
            "   /周度总结 (战网ID) - 查询本周数据总结 (耗时较长)\n"
            "   /历史段位 (战网ID) - 查询历届赛季段位\n"
            "   /同玩查询 [战网ID1] [战网ID2] - 查询两人开黑胜率\n"
            "👉【指数/英雄分析】\n"
            "   /快速强度指数 (战网ID) - 查看快速对局表现\n"
            "   /竞技强度指数 (战网ID) - 查看竞技对局表现\n"
            "   /威能 [英雄名] - 查询英雄当前版本前二核心威能\n"
            "   /ow英雄 [英雄名] - 英雄详细梯队与pick率\n"
            "   /获取段位分布 /banpick /mappick\n"
            "👉【综合/赛事】\n"
            "   /商店 - 查看今日守望先锋精选商店\n"
            "   /ow赛事 - 查看当前正在进行的 OWCS 赛事\n"
            "   /ow活动 /皮肤搜索\n"
            "=======================\n"
            "💡 提示：带()的参数代表如果您绑定了账号，则可以省略不填。"
        )
        yield event.plain_result(help_text)

    @command("大神绑定")
    async def dashen_bind(self, event: AstrMessageEvent, bnet_id: str):
        '''绑定您的QQ与战网ID。格式：/大神绑定 名字#51234'''
        user_id = event.get_sender_id()
        if "#" not in bnet_id:
            yield event.plain_result("❌ 绑定失败！请输入规范的战网ID，必须包含 # 号和数字（如：Player#12345）")
            return
        
        # 核心改动：改用官方原生异步 put_kv_data 方法将战网 ID 永久存入官方数据库中
        await self.put_kv_data(f"bind_{user_id}", bnet_id.strip())
        
        yield event.plain_result(f"✅ 绑定成功！关联战网账号【{bnet_id}】，后续查询可直接省略输入ID。")

    # ==================== 2. 战绩与核心数据 ====================

    @command("今日总结")
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询今日战绩总结'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"⏳ 正在计算 {target_id} 的今日战绩总结...")
        img_bytes = await self._fetch_image("/dashen-summary/today/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取今日总结失败。")

    @command("昨日总结")
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询昨日战绩总结'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"⏳ 正在统计 {target_id} 的昨日战绩数据...")
        img_bytes = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取昨日总结失败，可能昨日未登录游戏。")

    @command("周度总结", alias=["本周总结"])
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询玩家本周的大数据战绩总结'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"📊 正在生成 {target_id} 的本周战绩大数据总结，耗时较长（约30-60秒），请稍候...")
        img_bytes = await self._fetch_image("/dashen-summary/week/image", {"bnet_id": target_id}, timeout=900)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取周度总结失败，请检查服务日志或是否请求超时。")

    @command("大神数据")
    async def dashen_profile(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询玩家详情卡片'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"🔍 正在生成 {target_id} 的玩家详情...")
        img_bytes = await self._fetch_image("/dashen-profile/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取玩家详情卡片失败。")

    @command("大神对局")
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询最近对局列表'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"📊 正在拉取 {target_id} 的最近对局...")
        img_bytes = await self._fetch_image("/dashen-match/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取最近对局列表失败。")

    @command("历史段位")
    async def dashen_rank_history(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询历届赛季历史段位'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"📜 正在追溯 {target_id} 的历史段位记录...")
        img_bytes = await self._fetch_image("/dashen-rank-history/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取历史段位失败。")

    @command("同玩查询")
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str, p2: str):
        '''查询两位玩家的开黑同玩记录。格式：/同玩查询 玩家A#123 玩家B#456'''
        yield event.plain_result(f"👥 正在分析 {p1} 与 {p2} 的同玩胜率...")
        payload = {"player1_bnet_id": p1, "player2_bnet_id": p2}
        img_bytes = await self._fetch_image("/dashen-sameplay/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 无法获取同玩查询数据，请检查两个ID是否输入正确。")

    # ==================== 3. 强度指数与技能威能 ====================

    @command("快速强度指数")
    async def quick_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询快速对局表现与强度指数'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"⚡ 正在评估 {target_id} 的快速强度指数...")
        img_bytes = await self._fetch_image("/dashen-quick-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取快速强度指数失败。")

    @command("竞技强度指数")
    async def competitive_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        '''查询竞技/天梯对局表现与强度指数'''
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定 进行绑定。")
            return
        
        yield event.plain_result(f"🏆 正在评估 {target_id} 的竞技天梯强度指数...")
        img_bytes = await self._fetch_image("/dashen-competitive-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取竞技强度指数失败。")

    @command("威能")
    async def ow_hero_perk(self, event: AstrMessageEvent, hero_name: str):
        '''查询指定英雄当前版本的前二核心威能天赋'''
        yield event.plain_result(f"🔮 正在提取 {hero_name} 的核心威能数据...")
        img_bytes = await self._fetch_image("/ow-hero-perk/image", {"hero": hero_name})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result(f"❌ 未能找到英雄【{hero_name}】的威能图。")

    @command("ow英雄")
    async def ow_hero_pick(self, event: AstrMessageEvent, hero_name: str):
        '''查询英雄当前的胜率、登场率走势'''
        yield event.plain_result(f"🔥 正在读取 {hero_name} 的天梯 Pick 率走势图...")
        payload = {"view": "history", "game_mode": "competitive", "mmr": "all", "hero": hero_name}
        img_bytes = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 暂时无法获取英雄 {hero_name} 的数据走势。")

    # ==================== 4. 商店、赛事与其余指令 ====================

    @command("商店")
    async def ow_shop(self, event: AstrMessageEvent):
        '''查询当前特惠和精选商店商品'''
        yield event.plain_result("🛍️ 正在获取今日精选商店皮肤商品...")
        img_bytes = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取精选商店图片失败。")

    @command("ow赛事")
    async def ow_esports(self, event: AstrMessageEvent):
        '''查询当前正在进行的 OWCS 等守望先锋职业赛事'''
        yield event.plain_result("🎮 正在从 Pandascore 获取实时赛事对阵...")
        img_bytes = await self._fetch_image("/ow-esports/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。")

    # ==================== 5. 暂未开放/文档外接口占位处理 ====================

    @command("获取段位分布")
    async def get_rank_distribution(self, event: AstrMessageEvent):
        '''获取天梯各段位玩家比例分布'''
        yield event.plain_result("📊 正在统计天梯全服段位分布数据... [该模块正在开发对接中]")

    @command("ow活动")
    async def ow_activities(self, event: AstrMessageEvent):
        '''查看守望先锋当前限时活动'''
        yield event.plain_result("🎉 正在拉取当前版本节日/赛季活动公告... [本地接口升级中]")

    @command("banpick")
    async def ban_pick_stats(self, event: AstrMessageEvent):
        '''查询天梯或比赛中英雄的禁用与选择率排行'''
        yield event.plain_result("🚫 正在获取本周天梯英雄大盘选禁用排行...")
        payload = {"view": "ranking", "game_mode": "competitive", "mmr": "all"}
        img_bytes = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 无法获取全英雄排行。")

    @command("mappick")
    async def map_pick_stats(self, event: AstrMessageEvent):
        '''查询地图热度与胜率排行'''
        yield event.plain_result("🗺️ 正在拉取全模式地图胜率及出场分布... [当前接口暂不可用]")

    @command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str):
        '''搜索特定皮肤所属礼包或价格'''
        yield event.plain_result(f"🔍 正在检索包含关键词【{keyword}】的英雄外观与内购历史... [开发中]")
