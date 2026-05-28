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
        bnet_id, season = None, None
        if arg1 and arg2:
            bnet_id, season = arg1, arg2
        elif arg1:
            if arg1.isdigit(): season = arg1
            else: bnet_id = arg1
        return bnet_id, season

    def _parse_profile_args(self, arg1: str = None, arg2: str = None) -> tuple[str | None, str]:
        bnet_id, mode = None, "competitive"
        for arg in [arg1, arg2]:
            if not arg: continue
            if arg in ["快速", "quick"]: mode = "quick"
            elif arg in ["竞技", "competitive"]: mode = "competitive"
            else: bnet_id = arg
        return bnet_id, mode

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 600) -> tuple[bytes | None, dict | None]:
        url = f"{self.base_url}{endpoint}"
        payload = payload or {}
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200: return await resp.read(), None
                    else:
                        error_data = await resp.json()
                        return None, error_data
        except Exception as e:
            logger.error(f"网络请求异常: {e}")
            return None, {"error": "network_error", "message": str(e)}

    def _safe_remove(self, path: str):
        if path and os.path.exists(path):
            try: os.remove(path)
            except Exception as e: logger.warning(f"自动清理临时战绩图片失败: {e}")

    async def _delayed_remove(self, path: str, delay: int):
        await asyncio.sleep(delay)
        self._safe_remove(path)

    def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes, fallback_text: str = ""):
        if not img_bytes: return event.plain_result(fallback_text or "❌ 图片生成失败")
        try:
            img_hash = abs(hash(img_bytes))
            img_path = self.temp_image_dir / f"{img_hash}.png"
            img_path.write_bytes(img_bytes)
            chain = [Comp.At(qq=event.get_sender_id()), Comp.Plain("\n" + fallback_text + "\n" if fallback_text else "\n"), Comp.Image.fromFileSystem(str(img_path))]
            asyncio.create_task(self._delayed_remove(str(img_path), 1800))
            return event.chain_result(chain)
        except Exception as e: return event.plain_result(f"❌ 组件错误: {e}")

    def _send_multiple_images_result(self, event: AstrMessageEvent, imgs_list: list[bytes]):
        try:
            chain = [Comp.At(qq=event.get_sender_id()), Comp.Plain("\n")]
            for img_bytes in imgs_list:
                img_hash = abs(hash(img_bytes))
                img_path = self.temp_image_dir / f"{img_hash}.png"
                img_path.write_bytes(img_bytes)
                chain.append(Comp.Image.fromFileSystem(str(img_path)))
                asyncio.create_task(self._delayed_remove(str(img_path), 1800))
            return event.chain_result(chain)
        except Exception as e: return event.plain_result(f"❌ 多图组件错误: {e}")

    @event_message_type(EventMessageType.ALL)
    async def intercept_text_at(self, event: AstrMessageEvent):
        msg = event.message_str
        if not msg: return
        # (此处省略详细的触发逻辑代码，与你原稿保持一致，确保缩进在类方法内)
        # ... (请将你原稿中 intercept_text_at 的主体逻辑放在这里) ...

    # 确保所有指令函数都保持在类级别（缩进一致）
    @command("单局详细", alias=["单局"])
    async def dashen_match_detail(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        # ... (你的逻辑)
        try:
            # ... (网络请求代码)
            pass
        except Exception as e:
            logger.error(f"处理单局详细图片异常: {e}")
            yield event.plain_result("❌ 处理图片请求时发生系统错误")

    @command("历史段位", alias=["历届段位"])
    async def dashen_rank_history(self, event: AstrMessageEvent, bnet_id: str = None):
        # 后续函数均正常定义在此处...
        pass
