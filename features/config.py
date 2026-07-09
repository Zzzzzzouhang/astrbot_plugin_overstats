"""群组配置 / 全量消息适配 类指令逻辑。"""
import asyncio
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def group_config_cmd(plugin, event: AstrMessageEvent, action: str = '', value: str = ''):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('群设置', True))
    '查看/切换群组功能配置: /群设置 或 /群设置 提示 开|关 或 /群设置 追加提示 开|关'
    if not plugin._is_group_message(event):
        yield event.plain_result('⚠️ 此命令仅支持在群聊中使用')
        return
    group_id = plugin._get_group_id(event)
    if not group_id:
        yield event.plain_result('❌ 无法获取群组ID')
        return
    await plugin._ensure_group_config_loaded()
    cfg = plugin._get_group_feature_config(group_id)
    if not action:
        dp_status = '✅ 开启' if cfg.get('daily_prompt_skip', False) else '❌ 关闭'
        an_status = '✅ 开启' if cfg.get('append_notice', True) else '❌ 关闭'
        text = f'📋 群组功能配置 (群号: {group_id})\n\n🔔 首次提示后不再提示: {dp_status}\n💡 追加交互提示: {an_status}\n\n管理命令（仅管理员可用）：\n• <qqbot-cmd-input text="群设置 提示 开 " show="群设置 提示 开" reference="false" /> / <qqbot-cmd-input text="群设置 提示 关 " show="群设置 提示 关" reference="false" />\n• <qqbot-cmd-input text="群设置 追加提示 开 " show="群设置 追加提示 开" reference="false" /> / <qqbot-cmd-input text="群设置 追加提示 关 " show="群设置 追加提示 关" reference="false" />'
        yield event.plain_result(plugin._format_markdown_by_platform(event, text))
        return
    if not plugin._is_group_admin(event):
        yield event.plain_result('⚠️ 仅群管理员或群主可以修改群组配置')
        return
    feature_map = {'提示': 'daily_prompt_skip', '首次提示': 'daily_prompt_skip', '追加提示': 'append_notice', '追加': 'append_notice', '交互提示': 'append_notice'}
    action_map = {'开': True, '开启': True, 'on': True, 'true': True, '1': True, '关': False, '关闭': False, 'off': False, 'false': False, '0': False}
    feature_key = feature_map.get(action)
    if not feature_key:
        yield event.plain_result(f'❌ 未知功能: {action}\n支持的功能: 提示、追加提示')
        return
    if not value:
        yield event.plain_result(f'❌ 请指定操作: 开 或 关\n示例: /群设置 {action} 开')
        return
    enabled = action_map.get(value)
    if enabled is None:
        yield event.plain_result(f'❌ 未知操作: {value}\n支持的操作: 开、关')
        return
    await plugin._set_group_feature_config(group_id, feature_key, enabled)
    feature_names = {'daily_prompt_skip': '首次提示后不再提示', 'append_notice': '追加交互提示'}
    status_text = '✅ 已开启' if enabled else '❌ 已关闭'
    yield event.plain_result(f'✅ 群组 {group_id} 的【{feature_names[feature_key]}】功能已{status_text}')


async def full_adapt_enable(plugin, event: AstrMessageEvent, nickname: str = ''):
    """开启本群的全量消息适配 — @昵称模式（群管理员专属）。"""
    if not plugin._is_group_message(event):
        yield event.plain_result('⚠️ 此命令仅支持在群聊中使用')
        return
    if not plugin._is_group_admin(event):
        yield event.plain_result('⚠️ 仅群管理员或群主可操作全量适配开关')
        return
    group_id = plugin._get_group_id(event)
    nickname = (nickname or '').strip()
    plugin._set_full_adapt(group_id, True, nickname if nickname else None, mode='nickname')
    eff_nick = nickname or plugin._get_bot_nickname(event, plugin._get_full_adapt(group_id)) or '（未设置，将自动探测或使用全局兜底）'
    yield event.plain_result(f'✅ 本群（{group_id}）已开启全量消息适配（@昵称模式）。\n⚠️ 需群主在「机器人设置 → 机器人可获取消息范围」中开启「获取群内全部消息」，全量适配才实际生效。\n📝 触发格式：@机器人昵称 指令（纯文本即可，无需真实@）。\n🤖 当前机器人昵称：{eff_nick}\n💡 可用 <qqbot-cmd-input text="全量适配开 " show="全量适配开" reference="false" /> 重新指定昵称；<qqbot-cmd-input text="全量适配开完全匹配" show="全量适配开完全匹配" reference="false" /> 切换为直接匹配；<qqbot-cmd-input text="全量适配关 " show="全量适配关" reference="false" /> 关闭。')


async def full_adapt_enable_direct(plugin, event: AstrMessageEvent):
    """开启本群的全量消息适配 — 直接匹配模式（群管理员专属）。"""
    if not plugin._is_group_message(event):
        yield event.plain_result('⚠️ 此命令仅支持在群聊中使用')
        return
    if not plugin._is_group_admin(event):
        yield event.plain_result('⚠️ 仅群管理员或群主可操作全量适配开关')
        return
    group_id = plugin._get_group_id(event)
    plugin._set_full_adapt(group_id, True, mode='direct')
    yield event.plain_result(f'✅ 本群（{group_id}）已开启全量消息适配（直接匹配模式）。\n⚠️ 需群主在「机器人设置 → 机器人可获取消息范围」中开启「获取群内全部消息」，全量适配才实际生效。\n📝 触发格式：直接发送指令即可（如「今日总结」「对局」等），无需 @机器人昵称。\n💡 可用 <qqbot-cmd-input text="全量适配开 " show="全量适配开" reference="false" /> 切换为@昵称模式；<qqbot-cmd-input text="全量适配关 " show="全量适配关" reference="false" /> 关闭。')


async def full_adapt_disable(plugin, event: AstrMessageEvent):
    """关闭本群的全量消息适配（群管理员专属）。"""
    if not plugin._is_group_message(event):
        yield event.plain_result('⚠️ 此命令仅支持在群聊中使用')
        return
    if not plugin._is_group_admin(event):
        yield event.plain_result('⚠️ 仅群管理员或群主可操作全量适配开关')
        return
    group_id = plugin._get_group_id(event)
    plugin._set_full_adapt(group_id, False)
    yield event.plain_result(f'✅ 本群（{group_id}）已关闭全量消息适配。')
