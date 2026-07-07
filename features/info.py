"""资讯 / 活动 / 商店 / 排行 / 地图 类指令逻辑。"""
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def ow_shop(plugin, event: AstrMessageEvent):
    """拉取今日精选商店在售皮肤商品。"""
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, '🛍️ 正在获取今日精选商店皮肤商品...')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-shop/image')
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '商店'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取精选商店图片失败。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='商店', error_code=err_code)


async def ow_esports(plugin, event: AstrMessageEvent):
    """获取实时职业赛事对阵及赛程信息。"""
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, '🎮 正在从 Pandascore 获取实时赛事对阵...')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-esports/image')
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, 'ow赛事'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='ow赛事', error_code=err_code)


async def ow_activities(plugin, event: AstrMessageEvent):
    """拉取当前版本限时节日或赛季大活动公告卡片。"""
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, '🎉 正在拉取当前版本限时节日/赛季大活动公告卡片...')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/patch-notes/image', {'patch_kind': 'big'})
        if not img_bytes:
            img_bytes, error_data, err_code = await plugin._fetch_image('/patch-notes/image', {'patch_kind': 'latest'})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, 'ow活动'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '暂无正在进行的版本活动公告。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='ow活动', error_code=err_code)


async def ban_pick_stats(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """获取本周天梯英雄大盘的选禁用排行（可选模式/段位）。"""
    _positional, kw = plugin._extract_keywords([arg1, arg2])
    game_mode = kw.get('game_mode', 'competitive')
    mmr = kw.get('mmr', 'all')
    mode_label = '快速' if game_mode == 'quick' else '竞技'
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🚫 正在获取 {mode_label}（{mmr}）本周天梯英雄大盘选禁用排行，可用参数：[模式] [段位]')
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
            async for r in plugin._send_image_result(event, img_bytes, 'banpick'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '无法获取全英雄排行。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='banpick', error_code=err_code)


async def map_pick_stats(plugin, event: AstrMessageEvent):
    """从最新补丁中检索当前赛季地图池与轮换出场情况。"""
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, '🗺️ 正在从最新版本补丁中检索当前赛季地图池与轮换出场...')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/patch-notes/image', {'patch_kind': 'latest'})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, 'mappick'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '无法拉取最新地图池分布。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='mappick', error_code=err_code)


async def skin_search(plugin, event: AstrMessageEvent, keyword: str = ''):
    """检索包含指定关键词的精选上架皮肤商品卡片。"""
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f"🔍 正在检索包含关键词【{keyword or '最新'}】的精选上架皮肤商品卡片，可用参数：[关键词]")
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/ow-shop/image')
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '皮肤搜索'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '无法获取精选皮肤卡片。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='皮肤搜索', error_code=err_code)


async def ow_patch_notes(plugin, event: AstrMessageEvent, kind: str = 'latest'):
    """拉取外服更新日志卡片（参数：latest / small / big）。"""
    valid_kinds = ['latest', 'small', 'big']
    if kind not in valid_kinds:
        yield plugin._plain_error_result(event, '❌ 参数错误。支持的日志类型：latest, small, big\n例如：/ow更新 small')
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'📰 正在拉取外服 {kind} 更新日志卡片，可用参数：[类型]')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/patch-notes/image', {'patch_kind': kind})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, 'ow更新'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取更新日志失败。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='ow更新', error_code=err_code)


async def ow_rank_leaderboard(plugin, event: AstrMessageEvent, province: str, role: str):
    """获取指定地区的大神天梯省榜（位置：tank / dps / healer / open）。"""
    if not province or not role:
        yield plugin._plain_error_result(event, '❌ 请输入省份名称 and 职责位置，例如：/省榜 北京 tank\n(支持的位置: tank / dps / healer / open)')
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🏆 正在获取 {province} 地区 【{role}】 位置的大神天梯省榜，可用参数：<省份> <职责>')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'province': province, 'role': role}
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-rank-leaderboard/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '省榜'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取天梯省榜失败。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='省榜', error_code=err_code)


async def ow_hero_leaderboard(plugin, event: AstrMessageEvent, province: str, hero: str, arg3: str = ''):
    """获取指定地区特定英雄的大神专精绝活榜（可选开放队列模式）。"""
    _positional, kw = plugin._extract_keywords([province, hero, arg3])
    province = _positional[0] if len(_positional) >= 1 else province
    hero = _positional[1] if len(_positional) >= 2 else hero
    if not province or not hero:
        yield plugin._plain_error_result(event, '❌ 请输入省份和英雄名称，例如：/绝活榜 北京 猎空\n可选附加 开放（开放队列模式），如 /绝活榜 北京 猎空 开放')
        return
    mode = kw.get('lb_mode', 'preset')
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f"🎖️ 正在获取 {province} 地区 【{hero}】（{('开放队列' if mode == 'open' else '预设')}）的大神英雄专精绝活榜...")
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'province': province, 'hero': hero, 'mode': mode}
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-hero-leaderboard/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '绝活榜'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取英雄绝活榜失败。'
            yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='绝活榜', error_code=err_code)
