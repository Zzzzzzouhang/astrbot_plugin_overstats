"""英雄资料类指令逻辑：英雄百科（维基概览 / 问答）、英雄对比。"""
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def ow_hero_wiki(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
    """英雄百科：只给英雄名出概览卡，追加问题则出问答卡。"""
    hero = (arg1 or '').strip()
    if not hero:
        _gate = await plugin._maintenance_gate(event)
        if _gate:
            yield _gate
            return
        yield plugin._plain_error_result(
            event,
            '❌ 请输入英雄名称，如：ow百科 安娜\n'
            '追问英雄细节可附加问题，如：ow百科 安娜 她的威能有哪些',
        )
        return
    # 问题最多取两段：后端按「有 question 即问答模式」分流，不做其它语义解析
    question = ' '.join(x for x in ((arg2 or '').strip(), (arg3 or '').strip()) if x)
    payload = {'hero': hero}
    if question:
        payload['question'] = question
    kind = '英雄问答' if question else '英雄概览'
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(
        event, f'📖 正在生成【{hero}】的{kind}卡片，可用参数：英雄名 [问题]'
    )
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-hero-wiki/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, 'ow百科'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else f'未能生成英雄【{hero}】的百科卡片。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='ow百科', error_code=err_code)


async def ow_hero_compare(
    plugin, event: AstrMessageEvent,
    arg1: str = '', arg2: str = '', arg3: str = '', arg4: str = '', arg5: str = '',
):
    """英雄对比：比较两名玩家指定英雄（缺省则各自时长前三）的核心指标。

    参数（可无序）: 玩家1 玩家2 [英雄] [快速/竞技] [赛季号]
    只给一个玩家时，另一位用当前绑定账号补位。
    """
    positional, kw = plugin._extract_keywords([arg1, arg2, arg3, arg4, arg5])
    mode = kw.get('game_mode', 'quick')
    players: list[str] = []
    hero = ''
    season = None
    for item in positional:
        if item.isdigit() and season is None:
            season = int(item)
        elif ('#' in item or '＃' in item) and len(players) < 2:
            players.append(item)
        elif not hero:
            hero = item

    if len(players) == 1:
        bound_id = await plugin._get_bnet_id(event) or ''
        if bound_id:
            players = [bound_id, players[0]]

    if len(players) < 2:
        _gate = await plugin._maintenance_gate(event)
        if _gate:
            yield _gate
            return
        yield plugin._plain_error_result(
            event,
            '❌ 请输入两名玩家的战网ID，如：英雄对比 PlayerA#1234 PlayerB#5678 安娜\n'
            '第二个ID省略时默认与自己（需先绑定）对比；可选附加 英雄名 / 快速·竞技 / 赛季号',
        )
        return

    payload = {'player1': players[0], 'player2': players[1], 'mode': mode}
    if hero:
        payload['hero'] = hero
    if season is not None:
        payload['season'] = season

    mode_label = '快速' if mode == 'quick' else '竞技'
    detail = f'【{hero}】' if hero else '（双方时长前三英雄）'
    extra_hint = f'，赛季 S{season}' if season is not None else ''
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(
        event,
        f'⚖️ 正在对比 {players[0]} 与 {players[1]} 的{detail}{mode_label}英雄数据{extra_hint}，'
        f'可用参数：玩家1 玩家2 英雄 模式 赛季',
    )
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-hero-compare/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '英雄对比'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '无法获取英雄对比数据，请检查两个ID是否输入正确。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('英雄对比失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='英雄对比', error_code=err_code)
