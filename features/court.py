"""OW 开庭 / 是区吗 / AI 检测 类指令逻辑（依赖 Ow court / shiqu 业务管理器）。"""
import asyncio
import logging
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot")


async def ow_court(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """OW 开庭：AI 对单局数据进行电竞法庭风格分析。

    与是区吗共用白名单/分级 CD 机制（shiqu_cd_map），CD 独立计时；
    同一用户同一时间 是区吗/开庭 最多一条在执行。普通用户受 normal_enabled 开关控制。
    统计由 court_manager 内部在真实执行结果处记录（成功/失败），此处不再提前记 True。
    """
    CMD = '开庭'
    banned, ban_remain = await plugin._check_violation_ban(event, CMD)
    if banned:
        yield event.plain_result(plugin._VIOLATION_BAN_MSG.format(command=CMD, remain=plugin._violation_ban_remain_str(ban_remain)))
        return
    # 并发限流槽位由 main.py handler 层 _run_with_cmd_slot 统一获取/释放
    async for r in plugin.court_manager.run_court(event, arg1, arg2):
        yield r


async def ow_shiqu(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
    """OW 是区吗：展示上次判定结果。5 分钟内再次发送确认后开启新查询（分级 CD）。可加局数 1~25。
    统计由 shiqu_manager 内部在真实执行结果处记录（成功/失败），此处不再提前记 True。"""
    CMD = '是区吗'
    banned, ban_remain = await plugin._check_violation_ban(event, CMD)
    if banned:
        yield event.plain_result(plugin._VIOLATION_BAN_MSG.format(command=CMD, remain=plugin._violation_ban_remain_str(ban_remain)))
        return
    positional, kw = plugin._extract_keywords([arg1, arg2, arg3])
    bnet_id = ''
    match_count = 0
    is_privileged = plugin._is_whitelisted(event) or plugin._is_astrbot_admin(event)
    for arg in positional:
        if arg.isdigit():
            digit = int(arg)
            if not is_privileged:
                yield event.plain_result('💡 自定义局数仅限白名单/管理员使用，已使用默认局数。')
                continue
            if digit <= 0:
                yield plugin._plain_error_result(event, '❌ 错误：是区吗的局数必须大于 0！')
                return
            if digit > 25:
                yield event.plain_result('💡 局数最大为 25，已自动调整为 25。')
                match_count = 25
            else:
                match_count = digit
        else:
            bnet_id = arg
    # 并发限流槽位由 main.py handler 层 _run_with_cmd_slot 统一获取/释放
    async for r in plugin.shiqu_manager.run(event, bnet_id, match_count=match_count):
        yield r


async def ow_shiqu_result(plugin, event: AstrMessageEvent):
    """OW 是区吗结果：返回上次生成的判定书图片。
    统计由 shiqu_manager.last_result 内部在真实执行结果处记录（成功/失败）。"""
    async for r in plugin.shiqu_manager.last_result(event):
        yield r


async def ow_court_result(plugin, event: AstrMessageEvent):
    """OW 开庭结果：返回上次开庭审理的判决书图片。
    统计由 court_manager.last_result 内部在真实执行结果处记录（成功/失败）。"""
    async for r in plugin.court_manager.last_result(event):
        yield r


async def ow_ai_test(plugin, event: AstrMessageEvent):
    """测试是区吗 LLM API 连通性。"""
    ok, msg = await plugin.shiqu_manager.test_connectivity()
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('owAI检测', ok))
    yield event.plain_result(msg)
