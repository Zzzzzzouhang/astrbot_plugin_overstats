from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import aiohttp  # 异步请求，不阻塞机器人
import asyncio

# Overstats 服务地址
BASE_URL = "http://127.0.0.1:18080"

@register("overstats_ow", "OverstatsOW", "守望先锋战绩&段位查询", "1.0.0")
class OverstatsOWPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.session = None  # 异步会话

    async def initialize(self):
        # 创建异步HTTP会话
        self.session = aiohttp.ClientSession()
        logger.info("Overstats OW 插件已加载")

    # 战绩总览图
    @filter.command("ow")
    async def query_summary(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(" ", 1)
        if len(args) < 2 or "#" not in args[1]:
            yield event.plain_result("用法：/ow 战网ID#数字\n例：/ow 海盐冰淇淋#5911")
            return
        
        tag = args[1]
        try:
            async with self.session.post(
                f"{BASE_URL}/api/v2/summary/image",
                json={"battletag": tag},
                timeout=20
            ) as resp:
                if resp.status != 200:
                    yield event.plain_result("查询失败，请检查Overstats服务是否正常运行")
                    return
                # 读取图片二进制
                img_data = await resp.read()
                yield event.image_result(img_data)
                
        except asyncio.TimeoutError:
            yield event.plain_result("查询超时，Overstats服务响应过慢")
        except Exception as e:
            logger.error(f"战绩查询错误: {str(e)}")
            yield event.plain_result(f"查询出错：{str(e)}")

    # 段位历史图
    @filter.command("owrank")
    async def query_rank(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(" ", 1)
        if len(args) < 2 or "#" not in args[1]:
            yield event.plain_result("用法：/owrank 战网ID#数字\n例：/owrank 海盐冰淇淋#5911")
            return
        
        bnet_id = args[1]
        payload = {
            "limit": 12,
            "include_previous_season": True,
            "bnet_id": bnet_id
        }
        
        try:
            async with self.session.post(
                f"{BASE_URL}/api/v2/rank/image",
                json=payload,
                timeout=20
            ) as resp:
                if resp.status != 200:
                    yield event.plain_result("段位查询失败")
                    return
                img_data = await resp.read()
                yield event.image_result(img_data)
                
        except asyncio.TimeoutError:
            yield event.plain_result("段位查询超时")
        except Exception as e:
            logger.error(f"段位查询错误: {str(e)}")
            yield event.plain_result(f"段位查询出错：{str(e)}")

    async def terminate(self):
        # 优雅关闭会话
        if self.session:
            await self.session.close()
        logger.info("Overstats OW 插件已卸载")
