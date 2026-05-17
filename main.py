from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import httpx
from typing import Optional

class OWDshenPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_url = "http://127.0.0.1:18080/api/v2"
        self.timeout = 30  # 请求超时时间(秒)
        
        # 初始化HTTP客户端
        self.client = httpx.AsyncClient(timeout=self.timeout)
        
        logger.info("守望先锋大神数据插件已加载")

    async def terminate(self):
        """插件退出时清理资源（AstrBot v4.5+ 标准方法）"""
        await self.client.aclose()
        logger.info("守望先锋大神数据插件已卸载")

    async def _fetch_image(self, endpoint: str) -> Optional[bytes]:
        """通用的图片获取方法"""
        try:
            url = f"{self.base_url}{endpoint}"
            response = await self.client.post(url)
            
            if response.status_code == 200:
                # 检查响应内容类型是否为图片
                content_type = response.headers.get("Content-Type", "")
                if content_type.startswith("image/"):
                    return response.content
                else:
                    logger.error(f"API返回非图片内容: {content_type}")
                    return None
            else:
                logger.error(f"API请求失败: {url}, 状态码: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"请求API时发生错误: {str(e)}")
            return None

    async def _send_image_result(self, event: AstrMessageEvent, endpoint: str, error_msg: str = "获取图片失败，请稍后再试"):
        """通用的发送图片结果方法"""
        image_data = await self._fetch_image(endpoint)
        if image_data:
            yield event.image_result(image_data)
        else:
            yield event.plain_result(error_msg)

    # ==================== 大神资料相关 ====================
    @filter.command("profile", aliases=["大神资料", "dashen-profile"])
    async def cmd_profile(self, event: AstrMessageEvent):
        """获取大神资料图片"""
        async for result in self._send_image_result(event, "/dashen-profile/image"):
            yield result

    # ==================== 大神对局相关 ====================
    @filter.command("match", aliases=["大神对局", "dashen-match"])
    async def cmd_match(self, event: AstrMessageEvent):
        """获取大神对局图片"""
        async for result in self._send_image_result(event, "/dashen-match/image"):
            yield result

    @filter.command("match-detail", aliases=["对局详情", "dashen-match-detail"])
    async def cmd_match_detail(self, event: AstrMessageEvent):
        """获取大神对局详情图片"""
        async for result in self._send_image_result(event, "/dashen-match/detail/image"):
            yield result

    # ==================== 大神同玩相关 ====================
    @filter.command("sameplay", aliases=["大神同玩", "dashen-sameplay"])
    async def cmd_sameplay(self, event: AstrMessageEvent):
        """获取大神同玩图片"""
        async for result in self._send_image_result(event, "/dashen-sameplay/image"):
            yield result

    @filter.command("sameplay-detail", aliases=["同玩详情", "dashen-sameplay-detail"])
    async def cmd_sameplay_detail(self, event: AstrMessageEvent):
        """获取大神同玩详情图片"""
        async for result in self._send_image_result(event, "/dashen-sameplay/detail/image"):
            yield result

    # ==================== 排名历史相关 ====================
    @filter.command("rank-history", aliases=["排名历史", "dashen-rank-history"])
    async def cmd_rank_history(self, event: AstrMessageEvent):
        """获取排名历史图片"""
        async for result in self._send_image_result(event, "/dashen-rank-history/image"):
            yield result

    # ==================== 实力相关 ====================
    @filter.command("quick-strength", aliases=["快速实力", "dashen-quick-strength"])
    async def cmd_quick_strength(self, event: AstrMessageEvent):
        """获取快速模式实力图片"""
        async for result in self._send_image_result(event, "/dashen-quick-strength/image"):
            yield result

    @filter.command("competitive-strength", aliases=["竞技实力", "cstrength", "dashen-competitive-strength"])
    async def cmd_competitive_strength(self, event: AstrMessageEvent):
        """获取竞技模式实力图片"""
        async for result in self._send_image_result(event, "/dashen-competitive-strength/image"):
            yield result

    # ==================== 排行榜相关 ====================
    @filter.command("rank-leaderboard", aliases=["排名榜", "leaderboard", "dashen-rank-leaderboard"])
    async def cmd_rank_leaderboard(self, event: AstrMessageEvent):
        """获取排名排行榜图片"""
        async for result in self._send_image_result(event, "/dashen-rank-leaderboard/image"):
            yield result

    @filter.command("hero-leaderboard", aliases=["英雄榜", "hero", "dashen-hero-leaderboard"])
    async def cmd_hero_leaderboard(self, event: AstrMessageEvent):
        """获取英雄排行榜图片"""
        async for result in self._send_image_result(event, "/dashen-hero-leaderboard/image"):
            yield result

    # ==================== 英雄数据相关 ====================
    @filter.command("hero-pick-rate", aliases=["英雄选取率", "pickrate", "ow-hero-pick-rate"])
    async def cmd_hero_pick_rate(self, event: AstrMessageEvent):
        """获取英雄选取率图片"""
        async for result in self._send_image_result(event, "/ow-hero-pick-rate/image"):
            yield result

    # ==================== 每日总结相关 ====================
    @filter.command("today-summary", aliases=["今日总结", "today", "dashen-summary-today"])
    async def cmd_today_summary(self, event: AstrMessageEvent):
        """获取今日总结图片"""
        async for result in self._send_image_result(event, "/dashen-summary/today/image"):
            yield result

    @filter.command("yesterday-summary", aliases=["昨日总结", "yesterday", "dashen-summary-yesterday"])
    async def cmd_yesterday_summary(self, event: AstrMessageEvent):
        """获取昨日总结图片"""
        async for result in self._send_image_result(event, "/dashen-summary/yesterday/image"):
            yield result

    @filter.command("week-summary", aliases=["本周总结", "week", "dashen-summary-week"])
    async def cmd_week_summary(self, event: AstrMessageEvent):
        """获取本周总结图片"""
        async for result in self._send_image_result(event, "/dashen-summary/week/image"):
            yield result

    # ==================== 电竞相关 ====================
    @filter.command("esports", aliases=["电竞资讯", "ow-esports"])
    async def cmd_esports(self, event: AstrMessageEvent):
        """获取电竞资讯图片"""
        async for result in self._send_image_result(event, "/ow-esports/image"):
            yield result

    # ==================== 商店相关 ====================
    @filter.command("shop", aliases=["商店", "ow-shop"])
    async def cmd_shop(self, event: AstrMessageEvent):
        """获取商店信息图片"""
        async for result in self._send_image_result(event, "/ow-shop/image"):
            yield result

    # ==================== 更新日志相关 ====================
    @filter.command("patch-notes", aliases=["更新日志", "patch", "ow-patch-notes"])
    async def cmd_patch_notes(self, event: AstrMessageEvent):
        """获取更新日志图片"""
        async for result in self._send_image_result(event, "/patch-notes/image"):
            yield result

    # ==================== 帮助命令 ====================
    @filter.command("ow-help", aliases=["守望帮助", "owdashen-help"])
    async def cmd_help(self, event: AstrMessageEvent):
        """显示守望先锋大神数据插件帮助"""
        help_text = """
🎮 守望先锋大神数据插件帮助 🎮

📊 大神资料:
/profile /大神资料 - 获取大神资料图片

⚔️ 大神对局:
/match /大神对局 - 获取大神对局图片
/match-detail /对局详情 - 获取大神对局详情图片

👥 大神同玩:
/sameplay /大神同玩 - 获取大神同玩图片
/sameplay-detail /同玩详情 - 获取大神同玩详情图片

📈 排名与实力:
/rank-history /排名历史 - 获取排名历史图片
/quick-strength /快速实力 - 获取快速模式实力图片
/competitive-strength /竞技实力 - 获取竞技模式实力图片

🏆 排行榜:
/rank-leaderboard /排名榜 - 获取排名排行榜图片
/hero-leaderboard /英雄榜 - 获取英雄排行榜图片

📉 英雄数据:
/hero-pick-rate /英雄选取率 - 获取英雄选取率图片

📅 每日总结:
/today-summary /今日总结 - 获取今日总结图片
/yesterday-summary /昨日总结 - 获取昨日总结图片
/week-summary /本周总结 - 获取本周总结图片

🎬 其他:
/esports /电竞资讯 - 获取电竞资讯图片
/shop /商店 - 获取商店信息图片
/patch-notes /更新日志 - 获取更新日志图片

/ow-help /守望帮助 - 显示此帮助信息
        """
        yield event.plain_result(help_text.strip())
