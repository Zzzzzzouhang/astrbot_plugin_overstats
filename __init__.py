import requests
from astrbot.api import register, AstrBotMessage, MessageType
from astrbot.api.plugin import astrbot_plugin

# 你的 Overstats 本地地址（你已经跑通）
OVERSTATS_URL = "http://127.0.0.1:18080/api/v2/summary/image"

@register("overstats_ow", "OW战绩查询", "调用Overstats生成守望先锋战绩图", "1.0.0")
class OverstatsOWPlugin:
    def __init__(self, context):
        self.context = context

    @astrbot_plugin.command("ow", help_text="查询守望先锋战绩，格式：/ow 战网ID#数字")
    async def query_ow(self, msg: AstrBotMessage, param: str):
        if not param or "#" not in param:
            return "格式错误！请发送：/ow 战网ID#数字（例：/ow Test#12345）"

        try:
            # 调用你本地的Overstats生成图片
            resp = requests.post(OVERSTATS_URL, json={"battletag": param.strip()}, timeout=15)
            if resp.status_code != 200:
                return "查询失败：接口异常或战网ID错误"
            
            # AstrBot发送图片
            return self.context.image(resp.content)
        except Exception as e:
            return f"出错：{str(e)}"
