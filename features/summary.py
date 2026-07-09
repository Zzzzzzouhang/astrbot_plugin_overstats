"""战绩总结类指令逻辑：今日 / 昨日 / 周度总结。"""
import asyncio
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def dashen_today(plugin, event: AstrMessageEvent, bnet_id: str = ''):
    """生成过去 24 小时内的对局大数据总结卡片。"""
    CMD = '今日总结'
    banned, ban_remain = await plugin._check_violation_ban(event, CMD)
    if banned:
        yield event.plain_result(plugin._VIOLATION_BAN_MSG.format(command=CMD, remain=plugin._violation_ban_remain_str(ban_remain)))
        return
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('今日总结'))
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'⏳ 正在计算 {target_id} 的<qqbot-cmd-input text="今日总结 " show="今日总结" reference="false" />，可用参数 战网id 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-summary/today/image', {'bnet_id': target_id})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '今日总结'):
                yield r
        elif error_data and error_data.get('error') == 'summary_empty' and (error_data.get('details', {}).get('scope') == 'today'):
            yield event.plain_result(f'ℹ️ {target_id} 在过去的 24 小时内没有<qqbot-cmd-input text="今日总结 " show="今日总结" reference="false" />对局记录，尝试生成<qqbot-cmd-input text="昨日总结 " show="昨日总结" reference="false" />...')
            img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-summary/yesterday/image', {'bnet_id': target_id})
            if img_bytes:
                success = True
                async for r in plugin._send_image_result(event, img_bytes, '今日总结'):
                    yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取昨日总结失败，可能昨日没有对局记录。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取昨日总结失败'))
            elif error_data and error_data.get('error') == 'summary_empty':
                yield plugin._plain_error_result(event, f'❌ {target_id} 在过去 48 小时内没有对局记录')
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='今日总结', error_code=err_code)


async def dashen_yesterday(plugin, event: AstrMessageEvent, bnet_id: str = '', _skip_status_prompt: bool = False):
    """统计并生成昨日战绩数据卡片。"""
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('昨日总结'))
        return
    prompt_token = None
    _maintenance_stop = False
    if _skip_status_prompt:
        status_text = None
    else:
        status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'⏳ 正在统计 {target_id} 的<qqbot-cmd-input text="昨日总结 " show="昨日总结" reference="false" />，可用参数 战网id 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-summary/yesterday/image', {'bnet_id': target_id})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '昨日总结'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取昨日总结失败，可能昨日未登录游戏。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取昨日总结失败'))
            elif error_data and error_data.get('error') == 'summary_empty':
                yield plugin._plain_error_result(event, f'❌ {target_id} 在昨日没有对局记录')
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='昨日总结', error_code=err_code)


async def dashen_week(plugin, event: AstrMessageEvent, bnet_id: str = ''):
    """统计本周战绩大数据总结，耗时较长（约 30-60 秒）。"""
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('周度总结'))
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'📊 正在生成 {target_id} 的<qqbot-cmd-input text="本周总结 " show="本周总结" reference="false" />，可用参数 战网id 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-summary/week/image', {'bnet_id': target_id}, timeout=900)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '周度总结'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取周度总结失败，请检查服务日志或是否请求超时。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取周度总结失败'))
            elif error_data and error_data.get('error') == 'summary_empty' and (error_data.get('details', {}).get('scope') == 'week'):
                yield plugin._plain_error_result(event, f'❌ {target_id} 在过去 7 天内没有对局记录')
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='周度总结', error_code=err_code)
