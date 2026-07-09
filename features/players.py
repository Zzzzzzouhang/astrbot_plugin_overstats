"""玩家数据类指令逻辑：详情卡片 / 对局 / 单局详细 / 历史段位 / 同玩查询。"""
import asyncio
import base64
import logging
from astrbot.api.event import AstrMessageEvent
import aiohttp

logger = logging.getLogger("astrbot")


async def dashen_profile(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """查看玩家详情卡片（支持 快速/竞技 模式）。"""
    bnet_id, mode = plugin._parse_profile_args(arg1, arg2)
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('大神数据'))
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'🔍 正在生成 {target_id} 的<qqbot-cmd-input text="大神数据 " show="大神数据" reference="false" />，可用参数 战网id 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-profile/image', {'bnet_id': target_id, 'mode': mode})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '大神数据'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取玩家详情卡片失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取玩家详情失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='大神数据', error_code=err_code)


async def dashen_match(plugin, event: AstrMessageEvent, bnet_id: str = ''):
    """拉取最近 20 局的对局列表。"""
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('大神对局'))
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'📊 正在拉取 {target_id} 的<qqbot-cmd-input text="大神对局 " show="大神对局" reference="false" />，可用参数 战网id 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-match/image', {'bnet_id': target_id})
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '大神对局'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取最近对局列表失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取最近对局失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='大神对局', error_code=err_code)


async def dashen_match_detail(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
    """查看指定序号的单局多图详细战绩（可加 锐评关/全员关 控制开关）。"""
    CMD = '单局详细'
    banned, ban_remain = await plugin._check_violation_ban(event, CMD)
    if banned:
        yield event.plain_result(plugin._VIOLATION_BAN_MSG.format(command=CMD, remain=plugin._violation_ban_remain_str(ban_remain)))
        return
    positional, kw = plugin._extract_keywords([arg1, arg2, arg3])
    index = 0
    bnet_id = None
    for arg in positional:
        if arg.isdigit():
            digit = int(arg)
            if digit > 20:
                yield plugin._plain_error_result(event, '❌ 错误：单局详细的数字索引不能大于 20！')
                return
            index = max(0, digit - 1) if digit > 0 else 0
        else:
            bnet_id = arg
    if not bnet_id:
        user_id = event.get_sender_id()
        bnet_id = await plugin._get_user_bind_id(user_id)
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, '❌ 请输入战网ID，如：单局详细 Player#12345 1 \n或先使用 /绑定 战网ID，示例：/绑定 Player#12345')
        return
    base_prompt = f'⏳ 正在拉取 {target_id} 第 {index + 1} 局的<qqbot-cmd-input text="单局详细 " show="单局详细" reference="false" />多图详细战绩，可用参数：战网id 序号 查询别人。'
    if kw.get('analyze') is False:
        base_prompt += '（本次已跳过 AI 锐评，出图更快）'
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, base_prompt)
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'bnet_id': target_id, 'index': str(index), 'limit': '20', 'include_fight': True, 'include_previous_season': True, 'show_all_heroes': kw.get('show_all_heroes', True), 'analyze': kw.get('analyze', True)}
    url = f'{plugin.base_url}/dashen-match/detail/replies'
    success = False
    err_code = ''
    try:
        session = await plugin._get_http_session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=600)) as resp:
            if resp.status != 200:
                try:
                    error_data = await resp.json()
                    err_msg = error_data.get('message', '未知后端 service 错误')
                    logger.error(f'获取单局详细失败 HTTP {resp.status}: {err_msg} | bnet={target_id} index={index}')
                    yield plugin._plain_error_result(event, f'❌ 获取单局详细失败：{err_msg}')
                except Exception:
                    logger.error(f'获取单局详细失败 HTTP {resp.status}: 无法解析错误响应 | bnet={target_id} index={index}')
                    yield plugin._plain_error_result(event, f'❌ 后端接口响应异常，状态码: {resp.status}')
                return
            data = await resp.json()
            raw_img_list = data.get('replies', [])
            if not raw_img_list:
                logger.error(f'获取单局详细: replies 为空 | bnet={target_id} index={index}')
                yield plugin._plain_error_result(event, '❌ 未能生成该单局的详细图片链接')
                return
            collected_images: list[bytes] = []
            for u in raw_img_list:
                img_str = ''
                if isinstance(u, dict):
                    if u.get('type') == 'image':
                        img_str = str(u.get('base64', '')).strip()
                    if not img_str:
                        for key in ['url', 'image', 'src', 'path', 'file']:
                            if u.get(key):
                                img_str = str(u.get(key)).strip()
                                break
                else:
                    img_str = str(u).strip()
                if not img_str:
                    continue
                img_data = None
                try:
                    if img_str.startswith('base64://'):
                        img_data = base64.b64decode(img_str.replace('base64://', ''))
                    elif img_str.startswith('data:image') and 'base64,' in img_str:
                        img_data = base64.b64decode(img_str.split('base64,')[1])
                    elif len(img_str) > 100 and (not img_str.startswith('http')) and (not img_str.startswith('/')):
                        padding = len(img_str) % 4
                        if padding:
                            img_str += '=' * (4 - padding)
                        try:
                            img_data = base64.b64decode(img_str)
                        except Exception:
                            pass
                    if not img_data:
                        full_img_url = img_str if img_str.startswith('http') else f"{plugin.base_url.rstrip('/').removesuffix('/api/v2')}{(img_str if img_str.startswith('/') else '/' + img_str)}"
                        async with session.get(full_img_url, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as img_resp:
                            if img_resp.status == 200:
                                img_data = await img_resp.read()
                except Exception as e:
                    logger.error(f'处理图片失败：{e}')
                    continue
                if img_data:
                    collected_images.append(img_data)
            if collected_images:
                success = True
                async for r in plugin._send_multiple_images_result(event, collected_images, CMD):
                    yield r
    except Exception as e:
        logger.error(f'处理单局详细图片异常：{e}')
        yield plugin._plain_error_result(event, '❌ 处理图片请求时发生 system 错误')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='单局详细', error_code=err_code)


async def dashen_rank_history(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """追溯玩家的历史天梯段位记录（可选赛季范围）。"""
    positional, _kw = plugin._extract_keywords([arg1, arg2])
    bnet_id = None
    season_nums: list[int] = []
    for arg in positional:
        if arg.isdigit():
            season_nums.append(int(arg))
        else:
            bnet_id = arg
    target_id = await plugin._get_bnet_id(event, bnet_id)
    if not target_id:
        yield plugin._plain_error_result(event, plugin._bnet_err('历史段位') + '\n可选附加 起始 终止 赛季，如 /历史段位 Player#12345 15 22')
        return
    season_hint = ''
    if len(season_nums) == 1:
        season_hint = f'（从 S{season_nums[0]} 起）'
    elif len(season_nums) >= 2:
        season_hint = f'（S{season_nums[0]} ~ S{season_nums[1]})'
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'📜 正在追溯 {target_id} 的<qqbot-cmd-input text="历史段位 " show="历史段位" reference="false" />记录{season_hint}，可用参数 战网id 赛季号 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'bnet_id': target_id}
    if len(season_nums) >= 1:
        payload['start_season'] = season_nums[0]
    if len(season_nums) >= 2:
        payload['end_season'] = season_nums[1]
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-rank-history/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '历史段位'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '获取历史段位失败。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('获取历史段位失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='历史段位', error_code=err_code)


async def dashen_sameplay(plugin, event: AstrMessageEvent, p1: str = '', p2: str = ''):
    """深度分析两位玩家一同游玩开黑时的战绩与胜率。"""
    p1 = (p1 or '').strip()
    p2 = (p2 or '').strip()
    bound_id = await plugin._get_bnet_id(event) or ''
    if p1 and p2:
        pass
    elif p1 and (not p2):
        p2 = p1
        p1 = bound_id
    elif not p1 and p2:
        p1 = bound_id
    else:
        p1 = bound_id
    if not p1:
        yield plugin._plain_error_result(event, '❌ 请先绑定战网ID（/绑定 Player#12345）或输入对战网ID，可用参数：p1战网id p2战网id')
        return
    if not p2:
        yield plugin._plain_error_result(event, '❌ 缺少第二个战网ID，可用参数：p1战网id p2战网id\n示例：/同玩查询 Player#12345 OtherPlayer#67890')
        return
    status_text, prompt_token, _maintenance_stop = await plugin._prepare_business_status_prompt(event, f'👥 正在分析 {p1} 与 {p2} 的<qqbot-cmd-input text="同玩查询 " show="同玩查询" reference="false" />胜率，可用参数：战网ID1 战网ID2 查询别人。')
    if status_text:
        yield event.plain_result(status_text)
    if _maintenance_stop:
        return
    payload = {'player1_bnet_id': p1, 'player2_bnet_id': p2}
    success = False
    err_code = ''
    try:
        img_bytes, error_data, err_code = await plugin._fetch_image('/dashen-sameplay/image', payload)
        if img_bytes:
            success = True
            async for r in plugin._send_image_result(event, img_bytes, '同玩查询'):
                yield r
        else:
            err_msg = error_data.get('message') if error_data else '无法获取同玩查询数据，请检查两个ID是否输入正确。'
            if err_msg and 'Could not resolve customerToken' in err_msg:
                yield plugin._plain_error_result(event, plugin._id_resolve_err('同玩查询失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
    finally:
        await plugin._finalize_business_status_prompt(prompt_token, success, cmd_name='同玩查询', error_code=err_code)
