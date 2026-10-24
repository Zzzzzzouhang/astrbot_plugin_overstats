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


async def _upload(api, *, group_openid: str, openid: str, url: str, srv_send_msg: bool = False):
    if group_openid:
        return await api.post_group_file(
            group_openid=group_openid,
            file_type=IMAGE_FILE_TYPE,
            url=url,
            srv_send_msg=srv_send_msg,
        )
    return await api.post_c2c_file(
        openid=openid,
        file_type=IMAGE_FILE_TYPE,
        url=url,
        srv_send_msg=srv_send_msg,
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
    concurrency: int = 1,
    srv_send_msg: bool = False,
    retry: int = 1,
    label: str = "",
    user: str = "",
) -> UrlSendResult:
    """以 URL 直传并投递；返回成功张数与失败原因。

    一张 = 一次 files 上传 + 一次 message 投递。实测每段各约 2~3 秒固定 RTT。

    - concurrency：默认 1（串行）。实测并发 2~3 虽能把 3 张从 16s 压到 10~11s，
      但 QQ `/files` 接口会出现资源争抢（upload 从 2.5s 拉到 6~8s），并偶发
      「富媒体文件上传超时」，故默认保守串行，由调用方显式开启。
    - srv_send_msg：True 时 files 接口直接投递，省掉 message 往返（每张约省 2~3s），
      但会占用主动消息频次，群内不一定有权限。
    - retry：单张失败自动重试次数，默认 1（应对偶发上传超时）。

    副作用：并发投递时图片到达顺序不保证与 urls 顺序一致。

    注意：QQ 对同一 msg_id 的被动回复有 5 条上限，调用方需自行限制张数
    （当前业务上限为 4 张）。
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

    total = len(urls)
    concurrency = max(1, min(int(concurrency or 1), max(1, total)))
    retry = max(0, int(retry))
    semaphore = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    details: dict[int, tuple[int, int]] = {}

    async def _send_one(index: int, url: str) -> None:
        async with semaphore:
            last_exc: BaseException | None = None
            stage = "upload"
            for attempt in range(retry + 1):
                upload_ms = deliver_ms = -1
                stage = "upload"
                try:
                    _t0 = time.monotonic()
                    media = await asyncio.wait_for(
                        _upload(
                            api,
                            group_openid=group_openid,
                            openid=openid,
                            url=url,
                            srv_send_msg=srv_send_msg,
                        ),
                        timeout=per_image_timeout,
                    )
                    upload_ms = int((time.monotonic() - _t0) * 1000)
                    if not media:
                        raise RuntimeError("上传接口返回空 media")
                    # srv_send_msg=True 时 files 接口已直接投递，无需再调 message
                    if not srv_send_msg:
                        stage = "deliver"
                        _t1 = time.monotonic()
                        await asyncio.wait_for(
                            _deliver(
                                api,
                                group_openid=group_openid,
                                openid=openid,
                                media=media,
                                msg_id=msg_id,
                            ),
                            timeout=per_image_timeout,
                        )
                        deliver_ms = int((time.monotonic() - _t1) * 1000)
                    details[index] = (upload_ms, deliver_ms)
                    return
                except Exception as exc:  # noqa: BLE001 - 失败后按配置重试
                    last_exc = exc
                    if attempt < retry:
                        await asyncio.sleep(1.0 + attempt)
            raise RuntimeError(
                f"第{index}张({stage}) {type(last_exc).__name__}: {last_exc}"
            )

    results = await asyncio.gather(
        *(_send_one(index, url) for index, url in enumerate(urls, start=1)),
        return_exceptions=True,
    )
    errors = [str(result) for result in results if isinstance(result, BaseException)]
    sent = total - len(errors)
    total_ms = int((time.monotonic() - started) * 1000)
    tag = f"[{label}]" if label else ""
    who = f"用户={user} " if user else ""

    # 一次调用只留一条日志：成功给结果与耗时，失败把逐张原因拼在后面
    if errors:
        logger.warning(
            f"[Overstats] URL 直传{tag} {who}{sent}/{total} 张成功, total={total_ms}ms, "
            f"失败: {'; '.join(errors)}"
        )
    elif total == 1:
        upload_ms, deliver_ms = details.get(1, (-1, -1))
        tail = f"upload={upload_ms}ms, srv 直发" if srv_send_msg else (
            f"upload={upload_ms}ms, deliver={deliver_ms}ms"
        )
        logger.info(f"[Overstats] URL 直传{tag} {who}1/1 张成功, total={total_ms}ms ({tail})")
    else:
        logger.info(
            f"[Overstats] URL 直传{tag} {who}{total}/{total} 张成功, total={total_ms}ms, "
            f"平均={total_ms // max(1, total)}ms/张, 并发={concurrency}"
        )
    return UrlSendResult(total, sent, "; ".join(errors))
