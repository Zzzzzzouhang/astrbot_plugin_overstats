"""强度 / 云图 / 威能 / 英雄 Pick / 段位分布 类指令逻辑。"""
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def quick_strength(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """评估玩家快速模式下的强度指数（可选对局数 3-12）。"""
    positional, _kw = plugin._extract_keywords([arg1, arg2])
    bnet_id = None
    limit = None
    for arg in positional:
        if arg.isdigit():
            limit = int(arg)
        else:
            bnet_id = arg
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('快速强度') + '\n可选附加对局数（3-12），如 /快速强度 Player#12345 8')
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'⚡ 正在评估 {target_id} 的快速强度指数，可用参数：[战网ID]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'bnet_id': target_id, 'include_previous_season': True}
    if limit is not None:
        payload['limit'] = max(3, min(12, limit))
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-quick-strength/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '快速强度'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取快速强度指数失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取快速强度指数失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='快速强度', error_code=err_code)


async def competitive_strength(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """评估玩家竞技天梯模式下的强度指数（可选对局数 3-12）。"""
    positional, _kw = plugin._extract_keywords([arg1, arg2])
    bnet_id = None
    limit = None
    for arg in positional:
        if arg.isdigit():
            limit = int(arg)
        else:
            bnet_id = arg
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('竞技强度') + '\n可选附加对局数（3-12），如 /竞技强度 Player#12345 8')
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🏆 正在评估 {target_id} 的竞技天梯强度指数，可用参数：[战网ID]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'bnet_id': target_id, 'include_previous_season': True}
    if limit is not None:
        payload['limit'] = max(3, min(12, limit))
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-competitive-strength/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '竞技强度'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取竞技强度指数失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取竞技强度指数失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='竞技强度', error_code=err_code)


async def quick_hero_treemap(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """获取快速模式英雄使用率矩形树图（可选赛季）。"""
    bnet_id, season = plugin._parse_treemap_args(arg1, arg2)
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('快速英雄云图'))
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'📊 正在获取 {target_id} 的快速模式英雄云图，可用参数：[战网ID]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'bnet_id': target_id, 'mode': 'quick', 'include_previous_season': True}
    if season:
        payload['season'] = str(season)
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-hero-treemap/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '快速英雄云图'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取快速英雄云图失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取快速英雄云图失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='快速英雄云图', error_code=err_code)


async def competitive_hero_treemap(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """获取竞技模式英雄使用率矩形树图（可选赛季）。"""
    bnet_id, season = plugin._parse_treemap_args(arg1, arg2)
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('竞技英雄云图'))
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🏆 正在获取 {target_id} 的竞技模式英雄云图，可用参数：[战网ID]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'bnet_id': target_id, 'mode': 'competitive', 'include_previous_season': True}
    if season:
        payload['season'] = str(season)
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-hero-treemap/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '竞技英雄云图'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取竞技英雄云图失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取竞技英雄云图失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='竞技英雄云图', error_code=err_code)


async def ow_hero_perk(plugin, event: AstrMessageEvent, hero_name: str):
    """提取指定英雄的核心威能、机制数据图。"""
    if not hero_name:
        yield plugin._plain_error_result(event, '❌ 请输入英雄名称，如：/威能 闪光')
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🔮 正在提取 {hero_name} 的核心威能数据，可用参数：<英雄名>')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-hero-perk/image', {'hero': hero_name})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '威能'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else f'未能找到英雄【{hero_name}】的威能图。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='威能', error_code=err_code)


async def ow_hero_pick(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
    """读取指定英雄在当前天梯的 Pick 率历史走势图（可选模式/段位）。"""
    positional, kw = plugin._extract_keywords([arg1, arg2, arg3])
    hero_name = positional[0] if positional else ''
    if not hero_name:
        yield plugin._plain_error_result(event, '❌ 请输入英雄名称，如：/ow英雄 闪光\n可选附加：快速/竞技 模式、青铜~英杰 段位，如 /ow英雄 安娜 快速 大师')
        return
    mode_label = '快速' if kw.get('game_mode') == 'quick' else '竞技'
    mmr_label = kw.get('mmr', 'all')
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🔥 正在读取 {hero_name} 的 {mode_label}（{mmr_label}）Pick 率走势图，可用参数：<英雄名> [模式] [段位]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'view': 'history', 'game_mode': kw.get('game_mode', 'competitive'), 'mmr': kw.get('mmr', 'all'), 'hero': hero_name}
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-hero-pick-rate/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, 'ow英雄'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else f'暂时无法获取英雄 {hero_name} 的数据走势。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='ow英雄', error_code=err_code)


async def get_rank_distribution(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """统计天梯全服大盘全英雄数据排行与天梯环境分布（可选模式/段位）。"""
    _positional, kw = plugin._extract_keywords([arg1, arg2])
    game_mode = kw.get('game_mode', 'competitive')
    mmr = kw.get('mmr', 'all')
    mode_label = '快速' if game_mode == 'quick' else '竞技'
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'📊 正在统计 {mode_label}（{mmr}）天梯全服大盘全英雄数据排行与环境分布，可用参数：[模式] [段位]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'view': 'ranking', 'game_mode': game_mode, 'mmr': mmr}
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-hero-pick-rate/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '获取段位分布'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '无法获取全服天梯分布排行。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='获取段位分布', error_code=err_code)
