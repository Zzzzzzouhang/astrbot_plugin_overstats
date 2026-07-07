"""绑定相关指令逻辑。"""
import asyncio
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def dashen_bind(plugin, event: AstrMessageEvent, bnet_id: str):
    """绑定战网账号，格式：/绑定 Player#12345。"""
    CMD = '绑定'
    user_id = event.get_sender_id()
    new_bind_id = bnet_id.strip().lstrip('@')
    nickname = plugin._get_bot_nickname(event)
    if nickname and new_bind_id.lower().startswith(nickname.lower()):
        new_bind_id = new_bind_id[len(nickname):]
    if not new_bind_id or ('#' not in new_bind_id and '＃' not in new_bind_id):
        yield plugin._plain_error_result(event, '❌ 绑定失败！请输入规范战网 ID，严格区分大小写\n格式：/绑定 战网ID，示例：/绑定 Player#12345')
        if plugin.monitor:
            asyncio.ensure_future(plugin.monitor.record_command(CMD, False))
        return
    old_bind_id = await plugin._get_user_bind_id(user_id)
    await plugin._set_user_bind_id(user_id, new_bind_id)
    if not old_bind_id:
        yield event.plain_result(f'✅ 绑定成功！关联战网账号【{new_bind_id}】')
    else:
        yield event.plain_result(f'✅ 更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】')
