"""赛季总结指令逻辑。

后端 `/api/v2/dashen-summary/season/image` 是 prototype 接口，参数体系与
today/week 完全不同：不认 bnet_id，必须在 payload 里自带

- ``token``：大神账号 token（DASHEN_ACCOUNTS 里的那个，不是小程序 xcxtoken）；
- ``roleId``：目标玩家的数字 bnetId（纯数字）；
- ``season``：可选，缺省等于「当前赛季 - 1」。

插件手上只有 ``Name#1234``，所以先用 ``/dashen-profile``（JSON，不渲染）换出
``resolved.bnet_id`` 作为 roleId；token 直接取账号池里第一个可用值，
账号与角色是否匹配交给上游判定，失败时把后端错误原样抛给用户。
"""
import logging

from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")

CMD_NAME = '赛季总结'
_PARAM_HINT = '可用参数：战网id 赛季号（赛季号省略默认上一赛季）'
_NO_ACCOUNT_HINT = (
    '❌ 赛季总结需要大神账号 token，当前插件配置里没有可用账号。\n'
    '请在插件配置 →「大神账号配置」中填写至少一个账号（role_id + token）后重试。'
)


def _parse_accounts(plugin) -> list[dict]:
    """合并插件配置里的 WebUI 账号与批量文本账号（批量优先）。"""
    accounts: list[dict] = []
    raw = plugin.config.get('dashen_accounts') or []
    if isinstance(raw, list):
        accounts.extend(item for item in raw if isinstance(item, dict))
    bulk = str(plugin.config.get('dashen_accounts_bulk', '') or '').strip()
    if bulk:
        try:
            try:
                from ..deploy.ops.config_writer import _parse_bulk_accounts
            except ImportError:
                from deploy.ops.config_writer import _parse_bulk_accounts
            accounts.extend(_parse_bulk_accounts(bulk))
        except Exception as exc:  # 批量文本格式异常不应阻断主流程
            logger.warning(f'解析批量大神账号失败: {exc}')
    return accounts


def _first_token(plugin) -> str:
    """取账号池里第一个可用 token；没有则返回空串。"""
    for account in _parse_accounts(plugin):
        token = str(account.get('token') or '').strip()
        if token:
            return token
    return ''


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

    token = _first_token(plugin)
    if not token:
        yield plugin._plain_error_result(event, _NO_ACCOUNT_HINT)
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
        # 1) 换数字 bnetId：season 端点只认 roleId，不认 Name#1234
        profile, profile_err = await plugin._post_json('/dashen-profile', {'bnet_id': target_id})
        if not profile or not profile.get('ok'):
            err_msg = (profile_err or {}).get('message') or '解析玩家数字 ID 失败。'
            err_code = (profile_err or {}).get('error', '')
            if 'Could not resolve customerToken' in str(err_msg):
                yield plugin._plain_error_result(event, plugin._id_resolve_err('赛季总结失败'))
            else:
                yield plugin._plain_error_result(event, f'❌ {err_msg}')
            return
        role_id = str((profile.get('resolved') or {}).get('bnet_id') or '').strip()
        if not role_id.isdigit():
            yield plugin._plain_error_result(
                event, '❌ 未能解析出该玩家的数字 ID（roleId），赛季总结暂时无法生成。',
            )
            return

        # 2) 请求赛季报告图片；账号与角色是否匹配由上游判定，失败原样透出
        payload = {'token': token, 'roleId': role_id}
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
