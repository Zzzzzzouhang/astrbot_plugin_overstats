"""赛季总结指令逻辑。

后端 `/api/v2/dashen-summary/season/image` 与 today/week 一致：只传 bnet_id，
数字 roleId 与账号 token 全部由后端解析（账号取自凭据池，与其它指令共用轮换
与失效冷却）。插件只需附带可选的 ``season``（缺省等于「当前赛季 - 1」）。
"""
from astrbot.api.event import AstrMessageEvent

CMD_NAME = '赛季总结'
_PARAM_HINT = '可用参数：战网id 赛季号（赛季号省略默认上一赛季）'


async def ow_season_summary(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """生成指定赛季的赛季总结卡片（默认上一赛季）。"""
    bnet_id = ''
    season = None
    for arg in (arg1, arg2):
        arg = (arg or '').strip()
        if not arg:
            continue
        if arg.isdigit() and season is None:
            season = int(arg)
        else:
            bnet_id = arg

    target_id, _gate = await plugin._gate_bnet_id(
        event, bnet_id, CMD_NAME, '可选附加赛季号，如：赛季总结 Player#12345 22',
    )
    if _gate:
        yield _gate
        return

    season_label = f'S{season} ' if season is not None else '上一'
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(
        event, f'📅 正在生成 {target_id} 的{season_label}赛季总结卡片，{_PARAM_HINT}',
    )
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return

    success = False
    err_code = ''
    try:
        # 与今日总结一致：只传 bnet_id，数字 roleId 与账号由后端解析
        payload = {'bnet_id': target_id}
        if season is not None:
            payload['season'] = season
        img_bytes, error_data, err_code = await plugin._fetch_image(
            '/dashen-summary/season/image', payload, timeout=180,
        )
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, CMD_NAME):
                yield r
        else:
            err_msg = (error_data or {}).get('message') or '未能生成赛季总结卡片。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name=CMD_NAME, error_code=err_code)
