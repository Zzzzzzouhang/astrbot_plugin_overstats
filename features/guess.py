"""OW 猜猜互动游戏：分阶段下发题面，最后揭晓答案。

后端只有 POST /api/v2/ow-guess/replies（JSON），没有 /image 端点：
题目图片以 base64 放在 replies/stages 里，答案在 reveal_replies 里。
插件侧负责按 15 秒节奏把 stages 逐条发出，最后补一条答案。
"""
import asyncio
import base64
import logging
import random
import time

import aiohttp
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")

CMD_NAME = 'ow猜猜'

# 题目阶段间隔（秒）：后端 stages 的 at_seconds 同样是 15 的整数倍
_STAGE_INTERVAL = 15

# 答题窗口：用户键 -> 本局截止 monotonic 时间。
# 窗口内该用户发送的「非指令文本」视为猜猜答案，由未匹配兜底逻辑静默跳过，
# 避免被回复成「❌ xxx 暂时未匹配到指令」。
_ACTIVE_ANSWERS: dict[str, float] = {}


def _answer_key(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id())
    except Exception:
        return ''


def mark_answering(event: AstrMessageEvent, seconds: float) -> float:
    """登记该用户的答题窗口，返回本局截止时间（作为清理票据）。"""
    deadline = time.monotonic() + float(seconds)
    key = _answer_key(event)
    if key and seconds > 0:
        _ACTIVE_ANSWERS[key] = deadline
    return deadline


def clear_answering(event: AstrMessageEvent, deadline: float | None = None) -> None:
    """清理答题窗口；传 deadline 时只清理自己那一局，避免误清用户新开的一局。"""
    key = _answer_key(event)
    if not key:
        return
    if deadline is None:
        _ACTIVE_ANSWERS.pop(key, None)
    elif _ACTIVE_ANSWERS.get(key) == deadline:
        _ACTIVE_ANSWERS.pop(key, None)


def is_answering(event: AstrMessageEvent) -> bool:
    """该用户是否处于猜猜答题窗口内（过期条目顺手清理）。"""
    key = _answer_key(event)
    if not key:
        return False
    deadline = _ACTIVE_ANSWERS.get(key)
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        _ACTIVE_ANSWERS.pop(key, None)
        return False
    return True

# 随机出题池：排除 map_music / hero_conversation。
# 这两类需要服务器额外安装扩展包，且题面与答案都是音频，
# AstrBot 侧无法可靠下发语音，随机命中会出现「只有提示没有题面」。
_RANDOM_POOL = ('地图局部', '地图模糊', '威能', '头像', '英雄描述', '技能描述', '技能名', '颜色')

# 需音频扩展包的题型别名：用户显式指定时直接给明确提示，避免静默失败
_AUDIO_TYPE_ALIASES = {
    '音乐', '地图音乐', '听歌猜地图', 'map_music',
    '对话', '英雄对话', '接话', '猜接话英雄', 'hero_conversation',
}


def _split_replies(replies) -> tuple[list[str], list[bytes]]:
    """把后端 replies 拆成（文本列表, 图片字节列表），忽略音频等无法下发的类型。"""
    texts: list[str] = []
    images: list[bytes] = []
    for item in replies or []:
        if not isinstance(item, dict):
            continue
        kind = item.get('type')
        if kind == 'text':
            text = str(item.get('data') or '').strip()
            if text:
                texts.append(text)
        elif kind == 'image':
            raw = str(item.get('base64') or '').strip()
            if not raw:
                continue
            try:
                images.append(base64.b64decode(raw))
            except Exception:
                logger.warning('OW猜猜题目图片 base64 解码失败')
    return texts, images


async def ow_guess(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """发一道 OW 猜猜题：题面逐级放出，15 秒后揭晓答案。"""
    _gate = await plugin._maintenance_gate(event)
    if _gate:
        yield _gate
        return

    success = False
    answer_deadline = None
    try:
        qtype = ' '.join(x for x in ((arg1 or '').strip(), (arg2 or '').strip()) if x)
        if qtype in _AUDIO_TYPE_ALIASES:
            yield plugin._plain_error_result(
                event,
                '❌ 音乐 / 对话类题目需要音频扩展包且依赖语音下发，当前插件暂不支持。\n'
                '可用题型：地图局部、地图模糊、威能、头像、英雄描述、技能描述、技能名、颜色（不带参数则随机）',
            )
            return
        payload = {'question_type': qtype or random.choice(_RANDOM_POOL)}

        data = None
        session = await plugin._get_http_session()
        url = f'{plugin._resolve_backend_url()}/ow-guess/replies'
        plugin._log_backend_send('/ow-guess/replies', payload)
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                else:
                    try:
                        error_data = await resp.json()
                    except Exception:
                        error_data = None
                    err_msg = (error_data or {}).get('message') or f'后端接口响应异常，状态码: {resp.status}'
                    logger.error(f'OW猜猜出题失败 HTTP {resp.status}: {err_msg} | type={payload["question_type"]}')
                    yield plugin._plain_error_result(event, f'❌ {err_msg}')
                    return
        except Exception as exc:
            logger.error(f'OW猜猜请求异常: {exc} | type={payload["question_type"]}')
            yield plugin._plain_error_result(event, '❌ 获取猜猜题目失败，请稍后重试。')
            return

        if not isinstance(data, dict) or not data.get('ok'):
            err_msg = data.get('message') if isinstance(data, dict) else ''
            yield plugin._plain_error_result(event, f'❌ {err_msg or "猜猜题目生成失败。"}')
            return

        stages = data.get('stages') if isinstance(data.get('stages'), list) else []
        if not stages:
            stages = [{'replies': data.get('replies') or []}]

        # 答题窗口 = 出题到揭晓的时长（阶段间隔 × 阶段数 + 最后一段等待）
        answer_deadline = mark_answering(event, _STAGE_INTERVAL * len(stages) + _STAGE_INTERVAL)

        sent_any = False
        for index, stage in enumerate(stages):
            if index > 0:
                await asyncio.sleep(_STAGE_INTERVAL)
            stage_replies = stage.get('replies') if isinstance(stage, dict) else []
            texts, images = _split_replies(stage_replies)
            # 第一阶段发完整文字提示；后续阶段只在有图时补图。
            # 纯文字题（英雄描述、技能描述）每阶没有图，继续发该阶段的新提示。
            if index == 0 or not images:
                for text in texts[:1]:
                    yield event.plain_result(text)
                    sent_any = True
            for img in images:
                async for r in plugin._send_image_result(event, img, CMD_NAME):
                    yield r
                sent_any = True

        await asyncio.sleep(_STAGE_INTERVAL)
        reveal_texts, _reveal_images = _split_replies(data.get('reveal_replies'))
        if reveal_texts:
            yield event.plain_result(reveal_texts[0])
            sent_any = True
        success = sent_any
        if not sent_any:
            yield plugin._plain_error_result(event, '❌ 未能生成猜猜题目，请换一个题型重试。')
    finally:
        clear_answering(event, answer_deadline)
        # 本指令不经过 _run_with_cmd_slot / _finalize_business_status_prompt，
        # 监控记录需在此补齐，否则「ow猜猜」永远不出现在指令统计里。
        monitor = getattr(plugin, 'monitor', None)
        if monitor:
            asyncio.ensure_future(monitor.record_command(CMD_NAME, success))
