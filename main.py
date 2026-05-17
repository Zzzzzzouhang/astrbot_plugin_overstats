from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import requests

# Overstats 服务地址
BASE_URL = "http://127.0.0.1:18080"

@register("overstats_ow", "OverstatsOW", "守望先锋战绩&段位查询", "1.0.0")
class OverstatsOWPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        logger.info("Overstats 插件已加载")

    # 战绩总览图
    @filter.command("ow")
    async def query_summary(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(" ", 1)
        if len(args) < 2 or "#" not in args[1]:
            yield event.plain_result("用法：/ow 战网ID#数字\n例：/ow 海盐冰淇淋#5911")
            return
        tag = args[1]
        try:
            resp = requests.post(
                f"{BASE_URL}/api/v2/summary/image",
                json={"battletag": tag},
                timeout=20
            )
            if resp.status_code != 200:
                yield event.plain_result("查询失败")
                return
            yield event.image_result(resp.content)
        except Exception as e:
            yield event.plain_result(f"错误：{str(e)}")

    # 段位历史图（用你给的参数）
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
            resp = requests.post(
                f"{BASE_URL}/api/v2/rank/image",
                json=payload,
                timeout=20
            )
            if resp.status_code != 200:
                yield event.plain_result("段位查询失败")
                return
            yield event.image_result(resp.content)
        except Exception as e:
            yield event.plain_result(f"错误：{str(e)}")

    async def terminate(self):
        pass