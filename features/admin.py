"""管理员 / 维护 / 部署 / 卸载 类指令逻辑。"""
import asyncio
import time
import logging
from datetime import datetime
from astrbot.api.event import AstrMessageEvent
import aiohttp

logger = logging.getLogger("astrbot")


async def maintenance_cmd(plugin, event: AstrMessageEvent, action: str = ''):
    """设置或取消维护模式（仅 AstrBot 管理员可用）
        用法：/维护 内容 或 /维护 取消
        """
    if not action:
        yield event.plain_result('❌ 请输入维护内容，如：/维护 网易大神维护中，服务暂停，恢复时间未知\n取消维护：/维护 取消')
        return
    if action in ('取消', '关闭', '关', 'off'):
        await plugin._set_maintenance(False)
        yield event.plain_result('✅ 维护模式已关闭')
        return
    msg = (event.message_str or '').strip()
    for prefix in ('/维护', '维护'):
        if msg.startswith(prefix):
            full_content = msg[len(prefix):].strip()
            if full_content and full_content not in ('取消', '关闭', '关', 'off'):
                action = full_content
            break
    await plugin._set_maintenance(True, action)
    yield event.plain_result(f'✅ 维护模式已开启，内容：{action}')


async def violation_ban_cmd(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """管理员封禁 用户+指令（12小时）。用法：/ow违禁封禁 <用户ID> <指令名>"""
    if not arg1 or not arg2:
        yield event.plain_result('❌ 用法：/ow违禁封禁 <用户ID> <指令名>\n如：/ow违禁封禁 qqofficial:1170599013 单局详细')
        return
    ban_key = plugin._violation_ban_key(str(arg1), str(arg2))
    bans = plugin._load_violation_bans()
    bans[ban_key] = int(time.time())
    plugin._save_violation_bans(bans)
    logger.warning(f'[ViolationBan] 管理员封禁 {ban_key}（12小时）')
    yield event.plain_result(f'⛔ 已封禁用户 {arg1} 的【{arg2}】指令（12小时）。')


async def violation_unban_cmd(plugin, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
    """管理员解除 用户+指令 封禁。用法：/ow违禁解封 <用户ID> <指令名>"""
    if not arg1 or not arg2:
        yield event.plain_result('❌ 用法：/ow违禁解封 <用户ID> <指令名>\n如：/ow违禁解封 qqofficial:1170599013 单局详细')
        return
    ban_key = plugin._violation_ban_key(str(arg1), str(arg2))
    bans = plugin._load_violation_bans()
    if ban_key in bans:
        bans.pop(ban_key)
        plugin._save_violation_bans(bans)
        logger.info(f'[ViolationBan] 管理员解封 {ban_key}')
        yield event.plain_result(f'✅ 已解封用户 {arg1} 的【{arg2}】指令。')
    else:
        yield event.plain_result(f'ℹ️ 用户 {arg1} 的【{arg2}】指令当前未被封禁。')


async def connection_test_cmd(plugin, event: AstrMessageEvent):
    """测试 Overstats 后端连接（仅 AstrBot 管理员可用）"""
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow连接测试', True))
    health_url = plugin.base_url.rstrip('/')
    for suffix in ('/api/v2', '/api/v1', '/api'):
        if health_url.endswith(suffix):
            health_url = health_url[:-len(suffix)]
            break
    health_url += '/healthz'
    try:
        session = await plugin._get_http_session()
        async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.text()
            if resp.status == 200:
                yield event.plain_result(body)
            else:
                yield event.plain_result(f'⚠️ 服务响应异常，状态码: {resp.status}\n{body}')
    except aiohttp.ClientConnectorError:
        yield event.plain_result(f'❌ 连接失败：无法连接到 Overstats 服务，请检查服务是否启动及 API 地址配置。')
    except asyncio.TimeoutError:
        yield event.plain_result(f'❌ 连接超时：请求 10 秒内未收到响应，请检查网络或服务状态。')
    except Exception as e:
        yield event.plain_result(f'❌ 连接测试失败：{e}')


async def ow_deploy_cmd(plugin, event: AstrMessageEvent):
    """一键部署 Overstats 后端（仅 AstrBot 管理员可用，auto 模式生效）"""
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式（manual），无需一键部署。\n如需使用一键部署，请在 AstrBot 插件配置面板将「后端接入模式」改为 auto 后重载插件。')
        return
    yield event.plain_result('⏳ 正在执行一键部署，流程包括：代码获取 → 虚拟环境 → 依赖安装 → 配置生成 → 启动服务 → 健康检查。\n整个过程可能需要 1-5 分钟，请耐心等待...')
    result = await plugin.deploy_manager.deploy()
    status_icon = '✅' if result.success else '❌'
    lines = [f"{status_icon} 部署{('成功' if result.success else '失败')}（耗时 {result.elapsed:.1f}s）", '', result.message]
    if result.logs:
        lines.append('')
        lines.append('📋 部署日志（最后 20 行）：')
        tail_logs = result.logs[-20:] if len(result.logs) > 20 else result.logs
        for log_line in tail_logs:
            lines.append(log_line)
    yield event.plain_result('\n'.join(lines))


async def ow_deploy_status_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow部署状态', True))
    '查看后端部署状态（仅 AstrBot 管理员可用）'
    status = await plugin.deploy_manager.get_status()
    state_map = {'idle': '⚪ 未部署', 'deploying': '🔵 部署中', 'running': '🟢 运行中', 'stopped': '🟠 已停止', 'failed': '🔴 失败'}
    state_text = state_map.get(status.state, status.state)
    mode_text = '一键托管（auto）' if status.mode == 'auto' else '独立链接（manual）'
    lines = [f'📊 Overstats 后端部署状态', '', f'🔌 接入模式: {mode_text}', f'📌 运行状态: {state_text}']
    if status.mode == 'auto':
        lines.append(f'🌐 监听地址: {status.backend_host}:{status.backend_port}')
        lines.append(f"📂 后端目录: {status.backend_dir or '未部署'}")
        lines.append(f"🐍 虚拟环境: {('已创建' if status.venv_dir else '未创建')}")
        lines.append(f'🌿 Git 版本: {status.git_commit}')
        lines.append(f"⚙️ 进程状态: {('运行中 (PID=' + str(status.process_pid) + ')' if status.process_alive else '未运行')}")
        if status.last_deploy_time:
            deploy_time = datetime.fromtimestamp(status.last_deploy_time).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f'🕐 最后部署: {deploy_time}')
        if status.last_error:
            lines.append(f'⚠️ 最后错误: {status.last_error}')
    lines.append('')
    lines.append(f'🔗 当前 base_url: {plugin.base_url}')
    yield event.plain_result('\n'.join(lines))


async def ow_update_backend_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow更新后端', True))
    '更新后端代码并重启（仅 AstrBot 管理员可用，auto 模式生效）'
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端由您自行部署管理，无法通过插件更新。')
        return
    yield event.plain_result('⏳ 正在更新后端：拉取最新代码 → 安装依赖 → 重新生成配置 → 重启服务...')
    result = await plugin.deploy_manager.update()
    status_icon = '✅' if result.success else '❌'
    lines = [f"{status_icon} 更新{('成功' if result.success else '失败')}（耗时 {result.elapsed:.1f}s）", '', result.message]
    if result.logs:
        lines.append('')
        lines.append('📋 更新日志（最后 20 行）：')
        tail_logs = result.logs[-20:] if len(result.logs) > 20 else result.logs
        for log_line in tail_logs:
            lines.append(log_line)
    yield event.plain_result('\n'.join(lines))


async def ow_stop_backend_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow停止后端', True))
    '停止后端进程（仅 AstrBot 管理员可用，auto 模式生效）'
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端由您自行部署管理。')
        return
    result = await plugin.deploy_manager.stop()
    icon = '✅' if result.success else '❌'
    yield event.plain_result(f'{icon} {result.message}')


async def ow_restart_backend_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow重启后端', True))
    '重启后端进程（仅 AstrBot 管理员可用，auto 模式生效）'
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端由您自行部署管理。')
        return
    yield event.plain_result('⏳ 正在重启后端服务...')
    result = await plugin.deploy_manager.restart()
    status_icon = '✅' if result.success else '❌'
    lines = [f"{status_icon} 重启{('成功' if result.success else '失败')}", '', result.message]
    if result.logs:
        lines.append('')
        lines.append('📋 日志（最后 15 行）：')
        tail_logs = result.logs[-15:] if len(result.logs) > 15 else result.logs
        for log_line in tail_logs:
            lines.append(log_line)
    yield event.plain_result('\n'.join(lines))


async def ow_deploy_logs_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow部署日志', True))
    '查看后端运行日志（仅 AstrBot 管理员可用，auto 模式生效）'
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端日志请到您部署后端的服务器查看。')
        return
    logs = plugin.deploy_manager.get_logs(50)
    if not logs:
        yield event.plain_result('📋 暂无后端运行日志。后端启动后日志将在此显示。')
        return
    lines = ['📋 后端运行日志（最近 50 行）：', '']
    lines.extend(logs)
    yield event.plain_result('\n'.join(lines))


async def ow_backend_logs_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow后端日志', True))
    '查看 Overstats 后端持久化日志文件（仅 AstrBot 管理员可用，auto 模式生效）。\n\n        从专用日志文件中提取最新的 30 条记录。与 /ow部署日志 不同，\n        此指令读取持久化日志文件，日志跨后端重启保留。\n        '
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端日志请到您部署后端的服务器查看。')
        return
    logs = plugin.deploy_manager.get_backend_logs(30)
    if not logs:
        yield event.plain_result(f'📋 暂无 Overstats 后端持久化日志。\n日志文件路径: {plugin.deploy_manager.log_file_path}\n后端启动并产生输出后日志将写入该文件。')
        return
    lines = [f'📋 Overstats 后端持久化日志（最新 {len(logs)} 条）：', f'📂 文件: {plugin.deploy_manager.log_file_path}', '']
    lines.extend(logs)
    yield event.plain_result('\n'.join(lines))


async def ow_uninstall_backend_cmd(plugin, event: AstrMessageEvent):
    if plugin.monitor:
        asyncio.ensure_future(plugin.monitor.record_command('ow卸载后端', True))
    '卸载 Overstats 后端（仅 AstrBot 管理员可用，auto 模式生效）。\n\n        独立于插件卸载，安全隔离后端资源删除：\n        - 先停止后端进程，再删除文件，避免数据库损坏\n        - 可选参数：强制 / 仅代码 / 仅venv\n        '
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端由您自行部署管理，无法通过插件卸载。')
        return
    preview = plugin.deploy_manager.get_uninstall_preview()
    usage = preview['disk_usage']
    lines = ['🗑️ 后端卸载预览：', '', f"📂 后端代码目录: {preview['backend_dir']}", f"   {('✅ 存在' if preview['backend_exists'] else '❌ 不存在')}（{usage['backend_code']} MB）", f"🐍 虚拟环境目录: {preview['venv_dir']}", f"   {('✅ 存在' if preview['venv_exists'] else '❌ 不存在')}（{usage['venv']} MB）", f"⚙️ 后端进程: {('🟢 运行中' if preview['process_running'] else '⚪ 未运行')}", f"💾 总占用空间: {usage['total']} MB"]
    if preview['db_files']:
        lines.append('')
        lines.append('⚠️ 以下数据库文件将被删除（不可恢复）：')
        for db_file in preview['db_files']:
            lines.append(f'   • {db_file}')
    lines.append('')
    lines.append('📋 卸载选项：')
    lines.append('• 回复 /ow卸载后端执行确认 — 删除全部（代码+venv+数据库）')
    lines.append('• 回复 /ow卸载后端执行仅代码 — 仅删除后端代码（保留venv）')
    lines.append('• 回复 /ow卸载后端执行仅venv — 仅删除虚拟环境（保留代码）')
    lines.append('• 回复 /ow卸载后端执行强制 — 强制删除全部（进程无法停止时使用）')
    lines.append('')
    lines.append('❗ 卸载后如需重新使用，请重新执行 /ow部署')
    yield event.plain_result('\n'.join(lines))


async def uninstall_exec(plugin, event: AstrMessageEvent, delete_code: bool, delete_venv: bool, force: bool):
    """统一卸载执行逻辑。"""
    if not plugin.deploy_manager.is_auto_mode:
        yield event.plain_result('⚠️ 当前为独立链接模式，后端由您自行部署管理。')
        return
    preview = plugin.deploy_manager.get_uninstall_preview()
    if not preview['backend_exists'] and (not preview['venv_exists']):
        yield event.plain_result('ℹ️ 后端代码和虚拟环境均不存在，无需卸载。')
        return
    yield event.plain_result('⏳ 正在执行后端卸载（先停止进程 → 再删除文件）...')
    result = await plugin.deploy_manager.uninstall_backend(delete_code=delete_code, delete_venv=delete_venv, force=force)
    status_icon = '✅' if result.success else '⚠️'
    lines = [f"{status_icon} 卸载{('完成' if result.success else '部分完成')}（释放 {result.freed_space_mb:.1f} MB）", '', result.message, '']
    lines.extend(result.details)
    yield event.plain_result('\n'.join(lines))
