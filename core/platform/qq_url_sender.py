"""QQ 官方「URL 直传」发送：绕过 AstrBot 的 base64 图片分支。

背景：AstrBot 的 QQ 官方适配器 `upload_group_and_c2c_image()` 固定用
`file_data`(base64) 调 `/v2/{groups|users}/.../files`，而 QQ 开放接口本身支持
`url`（平台自行下载转存）。本模块直接用 `event.bot`（botpy Client）的官方 SDK
方法 `post_group_file` / `post_c2c_file` 完成直传，再用 `post_group_message` /
`post_c2c_message` 携带原消息 `msg_id` 做被动回复。

与框架链路的差异（调用方需知晓）：
- 不走 AstrBot 的发送队列、违规异常捕获与 5×30s 重试，需自行兜底；
- `srv_send_msg` 固定 False（True 会占用主动消息频次，群内大概率失败）；
- `msg_seq` 必须每条随机，否则 QQ 会按 (msg_id, msg_seq) 判重拒收。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

logger = logging.getLogger("astrbot")

# QQ 图片（file_type=1）只认 png/jpg
IMAGE_FILE_TYPE = 1
# 富媒体消息类型
MSG_TYPE_MEDIA = 7
# 单张图片上传+投递的整体超时：QQ 侧需先下载 url，留足余量
DEFAULT_TIMEOUT = 35.0


@dataclass
class UrlSendResult:
    """URL 直传结果。"""

    total: int
    sent: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.sent == self.total


def _resolve_api(event):
    """取 botpy 的 BotAPI 实例；返回 (api, 失败原因)。"""
    bot = getattr(event, "bot", None)
    if bot is None:
        return None, "event 上不存在 bot 属性（webhook 版适配器可能不暴露 botpy Client）"
    api = getattr(bot, "api", None)
    if api is None:
        return None, "event.bot 上没有 api 属性"
    if not hasattr(api, "post_group_file"):
        return None, "当前 botpy 版本缺少 post_group_file 方法"
    return api, ""


def _new_msg_seq() -> int:
    """与 AstrBot 同口径的随机序号，规避 (msg_id, msg_seq) 判重。"""
    return random.randint(1, 10000)


async def _upload(api, *, group_openid: str, openid: str, url: str):
    if group_openid:
        return await api.post_group_file(
            group_openid=group_openid,
            file_type=IMAGE_FILE_TYPE,
            url=url,
            srv_send_msg=False,
        )
    return await api.post_c2c_file(
        openid=openid,
        file_type=IMAGE_FILE_TYPE,
        url=url,
        srv_send_msg=False,
    )


async def _deliver(api, *, group_openid: str, openid: str, media, msg_id: str):
    payload: dict = {"msg_type": MSG_TYPE_MEDIA, "media": media, "msg_seq": _new_msg_seq()}
    if msg_id:
        payload["msg_id"] = msg_id
    if group_openid:
        return await api.post_group_message(group_openid=group_openid, **payload)
    return await api.post_c2c_message(openid=openid, **payload)


async def send_images_by_url(
    event,
    urls: list[str],
    *,
    per_image_timeout: float = DEFAULT_TIMEOUT,
) -> UrlSendResult:
    """逐张以 URL 直传并投递；返回成功张数与失败原因。

    注意：一张 = 一次上传 + 一次消息，QQ 对同一 msg_id 的被动回复有 5 条上限，
    调用方需自行限制张数（当前业务上限为 4 张）。
    """
    api, reason = _resolve_api(event)
    if api is None:
        return UrlSendResult(len(urls), 0, reason)

    message_obj = getattr(event, "message_obj", None)
    msg_id = str(getattr(message_obj, "message_id", "") or "")
    group_openid = str(getattr(message_obj, "group_id", "") or "")
    # C2C 场景 group_id 为空，session_id 即 sender openid
    openid = str(getattr(event, "session_id", "") or "")
    if not group_openid and not openid:
        return UrlSendResult(len(urls), 0, "无法确定群 group_openid / 用户 openid")

    sent = 0
    errors: list[str] = []
    started = time.monotonic()
    for index, url in enumerate(urls, start=1):
        upload_ms = deliver_ms = -1
        try:
            _t0 = time.monotonic()
            media = await asyncio.wait_for(
                _upload(api, group_openid=group_openid, openid=openid, url=url),
                timeout=per_image_timeout,
            )
            upload_ms = int((time.monotonic() - _t0) * 1000)
            if not media:
                raise RuntimeError("上传接口返回空 media")
            _t1 = time.monotonic()
            await asyncio.wait_for(
                _deliver(api, group_openid=group_openid, openid=openid, media=media, msg_id=msg_id),
                timeout=per_image_timeout,
            )
            deliver_ms = int((time.monotonic() - _t1) * 1000)
            sent += 1
            logger.info(
                f"[Overstats] URL 直传第{index}/{len(urls)}张: "
                f"upload(files)={upload_ms}ms, deliver(message)={deliver_ms}ms"
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级给调用方决定
            errors.append(f"第{index}张 {type(exc).__name__}: {exc}")
            logger.warning(
                f"[Overstats] URL 直传第{index}/{len(urls)}张失败: "
                f"upload={upload_ms}ms, deliver={deliver_ms}ms, err={exc}"
            )

    total_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        f"[Overstats] URL 直传结束: {sent}/{len(urls)} 张, total={total_ms}ms, "
        f"平均={total_ms // max(1, len(urls))}ms/张"
    )
    return UrlSendResult(len(urls), sent, "; ".join(errors))
