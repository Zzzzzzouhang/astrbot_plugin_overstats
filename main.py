import aiohttp
import logging
from astrbot.api.all import *

# 配置日志
logger = logging.getLogger("astrbot")

@register("overstats", "YourName", "Overstats 本地服务查询插件", "1.0.0")
class OverstatsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 根据你的 API 文档配置本地服务地址
        self.base_url = "http://127.0.0.1:18080/api/v2"

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 30) -> bytes:
        """
        内部辅助方法：请求指定的图片 API 并返回二进制字节流。
        """
        url = f"{self.base_url}{endpoint}"
        payload = payload or {}
        
        try:
            # 针对如 /week 这种耗时较长的接口，支持自定义超时时间
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        error_data = await resp.json()
                        logger.error(f"Overstats API Error: {resp.status} - {error_data}")
                        return None
        except Exception as e:
            logger.error(f"请求 Overstats API 时发生异常: {e}")
            return None

    @command("今日商店", alias=["owshop"])
    async def ow_shop(self, event: AstrMessageEvent):
        '''查询守望先锋今日商店'''
        yield event.plain_result("正在获取今日商店，请稍候...")
        
        img_bytes = await self._fetch_image("/ow-shop/image")
        
        if img_bytes:
            # 使用 Image.from_bytes() 将二进制流转换为图片组件
            yield event.make_result().message(Image.from_bytes(img_bytes))
        else:
            yield event.plain_result("❌ 获取今日商店失败，请检查本地服务是否运行。")

    @command("补丁快讯", alias=["patch"])
    async def patch_notes(self, event: AstrMessageEvent, patch_kind: str = "latest"):
        '''查询游戏补丁快讯。可选参数: latest(默认), small, big'''
        yield event.plain_result(f"正在获取 {patch_kind} 版本的补丁快讯...")
        
        payload = {"patch_kind": patch_kind}
        img_bytes = await self._fetch_image("/patch-notes/image", payload=payload)
        
        if img_bytes:
            yield event.make_result().message(Image.from_bytes(img_bytes))
        else:
            yield event.plain_result("❌ 获取补丁快讯失败，请检查后台服务。")

    @command("玩家详情", alias=["profile"])
    async def dashen_profile(self, event: AstrMessageEvent, bnet_id: str):
        '''查询玩家详情。参数: 战网ID (例如 Gulee#5667)'''
        yield event.plain_result(f"正在生成 {bnet_id} 的玩家详情卡片...")
        
        payload = {"bnet_id": bnet_id}
        img_bytes = await self._fetch_image("/dashen-profile/image", payload=payload)
        
        if img_bytes:
            yield event.make_result().message(Image.from_bytes(img_bytes))
        else:
            yield event.plain_result(f"❌ 获取失败。可能是战网 ID 不正确，或者该账号未绑定/未公开。")

    @command("今日总结", alias=["today"])
    async def dashen_today_summary(self, event: AstrMessageEvent, bnet_id: str):
        '''查询玩家今日战绩总结。参数: 战网ID (例如 Gulee#5667)'''
        yield event.plain_result(f"正在生成 {bnet_id} 的今日战绩总结...")
        
        payload = {"bnet_id": bnet_id}
        img_bytes = await self._fetch_image("/dashen-summary/today/image", payload=payload)
        
        if img_bytes:
            yield event.make_result().message(Image.from_bytes(img_bytes))
        else:
            yield event.plain_result("❌ 获取总结失败，可能是今天没有打游戏或服务无响应。")

    @command("本周总结", alias=["week"])
    async def dashen_week_summary(self, event: AstrMessageEvent, bnet_id: str):
        '''查询玩家本周战绩总结。由于数据量大，耗时较长。参数: 战网ID'''
        yield event.plain_result(f"正在生成 {bnet_id} 的本周战绩总结，这可能需要约 1 分钟时间，请耐心等待...")
        
        payload = {"bnet_id": bnet_id}
        # 根据 API 文档，week 请求较慢，此处覆盖默认的 30s 超时，设置为 90s
        img_bytes = await self._fetch_image("/dashen-summary/week/image", payload=payload, timeout=90)
        
        if img_bytes:
            yield event.make_result().message(Image.from_bytes(img_bytes))
        else:
            yield event.plain_result("❌ 生成本周总结失败（或请求超时）。")

    @command("守望英雄", alias=["hero"])
    async def hero_perk(self, event: AstrMessageEvent, hero_name: str):
        '''查询英雄威能/天赋信息。参数: 英雄名称 (例如 安娜)'''
        yield event.plain_result(f"正在查询 {hero_name} 的威能数据...")
        
        payload = {"hero": hero_name}
        img_bytes = await self._fetch_image("/ow-hero-perk/image", payload=payload)
        
        if img_bytes:
            yield event.make_result().message(Image.from_bytes(img_bytes))
        else:
            yield event.plain_result(f"❌ 找不到英雄 {hero_name} 的数据，请检查名称是否正确。")
