"""赛季总结指令逻辑。

后端 `/api/v2/dashen-summary/season/image` 是 prototype 接口，参数体系与
today/week 完全不同：不认 bnet_id，必须在 payload 里自带

- ``token``：大神账号 token（DASHEN_ACCOUNTS 里的那个，不是小程序 xcxtoken）；
- ``roleId``：目标玩家的数字 bnetId（纯数字）；
- ``season``：可选，缺省等于「当前赛季 - 1」。

插件手上只有 ``Name#1234``，所以先用 ``/dashen-profile``（JSON，不渲染）换出
``resolved.bnet_id`` 作为 roleId；token 从账号池随机挑选，账号相关失败时自动
换下一个账号重试（与后端 DashenAPIClient「随机选可用账号」的语义对齐），
账号与角色是否匹配交给上游判定，其余业务错误原样抛给用户。
"""
import logging
import random

from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")

CMD_NAME = '赛季总结'
_PARAM_HINT = '可用参数：战网id 赛季号（赛季号省略默认上一赛季）'
_NO_ACCOUNT_HINT = (
    '❌ 赛季总结需要大神账号 token，当前插件配置里没有可用账号。\n'
    '请在插件配置 →「大神账号配置」中填写至少一个账号（role_id + token）后重试。'
)
# 单次指令最多尝试的账号数（随机选取）：全部账号都试会在上游故障时拖长等待
_SEASON_TOKEN_ATTEMPTS = 3
# 仅「账号相关」失败才换号重试；无报告 / 赛季不存在等业务错误换号也无意义
_RETRY_WITH_OTHER_ACCOUNT = {
    'season_report_unavailable',
    'season_report_upstream_error',
}


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


def _account_tokens(plugin) -> list[str]:
    """账号池全部可用 token（去重，保持配置顺序）；空列表表示未配置账号。"""
    seen: set[str] = set()
    tokens: list[str] = []
    for account in _parse_accounts(plugin):
        token = str(account.get('token') or '').strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


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

    tokens = _account_tokens(plugin)
    if not tokens:
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

        # 2) 请求赛季报告图片：账号随机打散 + 账号相关失败自动换号重试。
        # 后端普通 dashen 请求是随机选可用账号（失效冷却），赛季接口却由调用方直传
        # token——写死第一个账号时，该账号过期会让整个功能失效（其他指令不受影响）。
        attempt_tokens = tokens[:]
        random.shuffle(attempt_tokens)
        attempt_limit = min(len(attempt_tokens), _SEASON_TOKEN_ATTEMPTS)
        img_bytes = None
        error_data = None
        for attempt_index, token in enumerate(attempt_tokens[:attempt_limit], start=1):
            payload = {'token': token, 'roleId': role_id}
            if season is not None:
                payload['season'] = season
            # retry=False：换号本身就是重试，避免每个账号再叠一次 10s 自动重试
            img_bytes, error_data, err_code = await plugin._fetch_image(
                '/dashen-summary/season/image', payload, timeout=180, retry=False,
            )
            if img_bytes:
                break
            if str((error_data or {}).get('error') or '') not in _RETRY_WITH_OTHER_ACCOUNT:
                break
            if attempt_index < attempt_limit:
                logger.warning(
                    f'[Overstats] 赛季总结第 {attempt_index}/{attempt_limit} 个账号失败'
                    f'（{(error_data or {}).get("error")}），换号重试'
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
