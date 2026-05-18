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
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 默认参数改为 http://127.0.0.1:18080/api/v2
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

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 600) -> bytes:
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

    @command("owhelp")
    async def ow_help(self, event: AstrMessageEvent):
        help_text = (
            "📌 守望先锋 Overstats 查询菜单：\n"
            "=======================\n"
            "👉【数据/战绩总结】\n"
            "   /大神绑定 [战网ID] - 绑定QQ与战网账号\n"
            "   /大神数据 (战网ID) - 查询玩家详情卡片\n"
            "   /大神对局 (战网ID) - 查询最近对局列表\n"
            "   /今日总结 (战网ID) - 查询今日战绩总结\n"
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
        if "#" not in bnet_id:
            yield event.plain_result("❌ 绑定失败！请输入规范战网ID，如：Player#12345")
            return
        await self.put_kv_data(f"bind_{user_id}", bnet_id.strip())
        yield event.plain_result(f"✅ 绑定成功！关联战网账号【{bnet_id}】")

    @command("今日总结")
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"⏳ 正在计算 {target_id} 的今日战绩总结...")
        img_bytes = await self._fetch_image("/dashen-summary/today/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取今日总结失败。")

    @command("昨日总结")
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"⏳ 正在统计 {target_id} 的昨日战绩数据...")
        img_bytes = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取昨日总结失败，可能昨日未登录游戏。")

    @command("周度总结", alias=["本周总结"])
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"📊 正在生成 {target_id} 的本周战绩大数据总结，耗时较长（约30-60秒），请稍候...")
        img_bytes = await self._fetch_image("/dashen-summary/week/image", {"bnet_id": target_id}, timeout=900)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取周度总结失败，请检查服务日志或是否请求超时。")

    @command("大神数据")
    async def dashen_profile(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"🔍 正在生成 {target_id} 的玩家详情...")
        img_bytes = await self._fetch_image("/dashen-profile/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取玩家详情卡片失败。")

    @command("大神对局")
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"📊 正在拉取 {target_id} 的最近对局...")
        img_bytes = await self._fetch_image("/dashen-match/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取最近对局列表失败。")

    @command("历史段位")
    async def dashen_rank_history(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"📜 正在追溯 {target_id} 的历史段位记录...")
        img_bytes = await self._fetch_image("/dashen-rank-history/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取历史段位失败。")

    @command("同玩查询")
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str, p2: str):
        yield event.plain_result(f"👥 正在分析 {p1} 与 {p2} 的同玩胜率...")
        payload = {"player1_bnet_id": p1, "player2_bnet_id": p2}
        img_bytes = await self._fetch_image("/dashen-sameplay/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 无法获取同玩查询数据，请检查两个ID是否输入正确。")

    @command("快速强度指数")
    async def quick_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"⚡ 正在评估 {target_id} 的快速强度指数...")
        img_bytes = await self._fetch_image("/dashen-quick-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取快速强度指数失败。")

    @command("竞技强度指数")
    async def competitive_strength(self, event: AstrMessageEvent, bnet_id: str = None):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result("❌ 请输入战网ID，或先使用 /大神绑定")
            return
        yield event.plain_result(f"🏆 正在评估 {target_id} 的竞技天梯强度指数...")
        img_bytes = await self._fetch_image("/dashen-competitive-strength/image", {"bnet_id": target_id})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取竞技强度指数失败。")

    @command("威能")
    async def ow_hero_perk(self, event: AstrMessageEvent, hero_name: str):
        yield event.plain_result(f"🔮 正在提取 {hero_name} 的核心威能数据...")
        img_bytes = await self._fetch_image("/ow-hero-perk/image", {"hero": hero_name})
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result(f"❌ 未能找到英雄【{hero_name}】的威能图。")

    @command("ow英雄")
    async def ow_hero_pick(self, event: AstrMessageEvent, hero_name: str):
        yield event.plain_result(f"🔥 正在读取 {hero_name} 的天梯 Pick 率走势图...")
        payload = {"view": "history", "game_mode": "competitive", "mmr": "all", "hero": hero_name}
        img_bytes = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result(f"❌ 暂时无法获取英雄 {hero_name} 的数据走势。")

    @command("商店")
    async def ow_shop(self, event: AstrMessageEvent):
        yield event.plain_result("🛍️ 正在获取今日精选商店皮肤商品...")
        img_bytes = await self._fetch_image("/ow-shop/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 获取精选商店图片失败。")

    @command("ow赛事")
    async def ow_esports(self, event: AstrMessageEvent):
        yield event.plain_result("🎮 正在从 Pandascore 获取实时赛事对阵...")
        img_bytes = await self._fetch_image("/ow-esports/image")
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。")

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
        img_bytes = await self._fetch_image("/ow-hero-pick-rate/image", payload)
        if img_bytes:
            yield self._send_image_result(event, img_bytes)
        else:
            yield event.plain_result("❌ 无法获取全英雄排行。")

    @command("mappick")
    async def map_pick_stats(self, event: AstrMessageEvent):
        yield event.plain_result("🗺️ 正在拉取全模式地图胜率及出场分布... [当前接口暂不可用]")

    @command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str):
        yield event.plain_result(f"🔍 正在检索包含关键词【{keyword}】的英雄外观与内购历史... [开发中]")
