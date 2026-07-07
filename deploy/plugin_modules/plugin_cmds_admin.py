import os
import aiohttp
import logging
import tempfile
import asyncio
import re
import base64
import json
import time
import inspect
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.event import filter, AstrMessageEvent  # 引入原生消息与事件过滤模块

# 一键部署模块（独立于 main.py，避免主文件臃肿）
try:
    from ...deploy import DeployManager
except ImportError:
    # 兜底：插件作为顶层模块加载时相对导入可能失败
    from deploy import DeployManager  # type: ignore[no-redef]

# OW 开庭模块（独立封装，避免主文件臃肿）
try:
    from ...deploy.ow.court import CourtManager
    from ...deploy.ow.shiqu import ShiquManager
except ImportError:
    from deploy.ow.court import CourtManager  # type: ignore[no-redef]
    from deploy.ow.shiqu import ShiquManager  # type: ignore[no-redef]

# 监控模块
try:
    from ...deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader
except ImportError:
    from deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader  # type: ignore[no-redef]

# 是区吗调用日志读取
try:
    from ...deploy.ow.shiqu_log import ShiquCallReader
except ImportError:
    from deploy.ow.shiqu_log import ShiquCallReader  # type: ignore[no-redef]

# Web API helpers
from astrbot.api.web import request, json_response, error_response, stream_response

logger = logging.getLogger("astrbot")

class PluginCmdsAdmin:

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("维护")
    async def maintenance_cmd(self, event: AstrMessageEvent, action: str = ""):
        """设置或取消维护模式（仅 AstrBot 管理员可用）
        用法：/维护 内容 或 /维护 取消
        """
        if not action:
            yield event.plain_result("❌ 请输入维护内容，如：/维护 网易大神维护中，服务暂停，恢复时间未知\n取消维护：/维护 取消")
            return

        if action in ("取消", "关闭", "关", "off"):
            await self._set_maintenance(False)
            yield event.plain_result("✅ 维护模式已关闭")
            return

        # 从完整消息文本中提取 /维护 之后的所有内容（支持空格）
        msg = (event.message_str or "").strip()
        for prefix in ("/维护", "维护"):
            if msg.startswith(prefix):
                full_content = msg[len(prefix):].strip()
                if full_content and full_content not in ("取消", "关闭", "关", "off"):
                    action = full_content
                break

        await self._set_maintenance(True, action)
        yield event.plain_result(f"✅ 维护模式已开启，内容：{action}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow违禁封禁")
    async def violation_ban_cmd(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """管理员封禁 用户+指令（12小时）。用法：/ow违禁封禁 <用户ID> <指令名>"""
        if not arg1 or not arg2:
            yield event.plain_result("❌ 用法：/ow违禁封禁 <用户ID> <指令名>\n如：/ow违禁封禁 qqofficial:1170599013 单局详细")
            return
        ban_key = self._violation_ban_key(str(arg1), str(arg2))
        bans = self._load_violation_bans()
        bans[ban_key] = int(time.time())
        self._save_violation_bans(bans)
        logger.warning(f"[ViolationBan] 管理员封禁 {ban_key}（12小时）")
        yield event.plain_result(f"⛔ 已封禁用户 {arg1} 的【{arg2}】指令（12小时）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow违禁解封")
    async def violation_unban_cmd(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """管理员解除 用户+指令 封禁。用法：/ow违禁解封 <用户ID> <指令名>"""
        if not arg1 or not arg2:
            yield event.plain_result("❌ 用法：/ow违禁解封 <用户ID> <指令名>\n如：/ow违禁解封 qqofficial:1170599013 单局详细")
            return
        ban_key = self._violation_ban_key(str(arg1), str(arg2))
        bans = self._load_violation_bans()
        if ban_key in bans:
            bans.pop(ban_key)
            self._save_violation_bans(bans)
            logger.info(f"[ViolationBan] 管理员解封 {ban_key}")
            yield event.plain_result(f"✅ 已解封用户 {arg1} 的【{arg2}】指令。")
        else:
            yield event.plain_result(f"ℹ️ 用户 {arg1} 的【{arg2}】指令当前未被封禁。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow连接测试")
    async def connection_test_cmd(self, event: AstrMessageEvent):
        """测试 Overstats 后端连接（仅 AstrBot 管理员可用）"""
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow连接测试", True))
        # 从 base_url 构建健康检查地址：去掉 /api/v2 尾部，加上 /healthz
        health_url = self.base_url.rstrip("/")
        for suffix in ("/api/v2", "/api/v1", "/api"):
            if health_url.endswith(suffix):
                health_url = health_url[: -len(suffix)]
                break
        health_url += "/healthz"

        try:
            session = await self._get_http_session()
            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                body = await resp.text()
                if resp.status == 200:
                    yield event.plain_result(body)
                else:
                    yield event.plain_result(f"⚠️ 服务响应异常，状态码: {resp.status}\n{body}")
        except aiohttp.ClientConnectorError:
            yield event.plain_result(f"❌ 连接失败：无法连接到 Overstats 服务，请检查服务是否启动及 API 地址配置。")
        except asyncio.TimeoutError:
            yield event.plain_result(f"❌ 连接超时：请求 10 秒内未收到响应，请检查网络或服务状态。")
        except Exception as e:
            yield event.plain_result(f"❌ 连接测试失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow部署")
    async def ow_deploy_cmd(self, event: AstrMessageEvent):
        """一键部署 Overstats 后端（仅 AstrBot 管理员可用，auto 模式生效）"""
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result(
                "⚠️ 当前为独立链接模式（manual），无需一键部署。\n"
                "如需使用一键部署，请在 AstrBot 插件配置面板将「后端接入模式」改为 auto 后重载插件。"
            )
            return

        yield event.plain_result("⏳ 正在执行一键部署，流程包括：代码获取 → 虚拟环境 → 依赖安装 → 配置生成 → 启动服务 → 健康检查。\n整个过程可能需要 1-5 分钟，请耐心等待...")

        result = await self.deploy_manager.deploy()
        # 构建结果反馈
        status_icon = "✅" if result.success else "❌"
        lines = [f"{status_icon} 部署{'成功' if result.success else '失败'}（耗时 {result.elapsed:.1f}s）", "", result.message]
        if result.logs:
            lines.append("")
            lines.append("📋 部署日志（最后 20 行）：")
            tail_logs = result.logs[-20:] if len(result.logs) > 20 else result.logs
            for log_line in tail_logs:
                lines.append(log_line)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow部署状态")
    async def ow_deploy_status_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow部署状态", True))
        """查看后端部署状态（仅 AstrBot 管理员可用）"""
        status = await self.deploy_manager.get_status()

        state_map = {
            "idle": "⚪ 未部署",
            "deploying": "🔵 部署中",
            "running": "🟢 运行中",
            "stopped": "🟠 已停止",
            "failed": "🔴 失败",
        }
        state_text = state_map.get(status.state, status.state)
        mode_text = "一键托管（auto）" if status.mode == "auto" else "独立链接（manual）"

        lines = [
            f"📊 Overstats 后端部署状态",
            "",
            f"🔌 接入模式: {mode_text}",
            f"📌 运行状态: {state_text}",
        ]
        if status.mode == "auto":
            lines.append(f"🌐 监听地址: {status.backend_host}:{status.backend_port}")
            lines.append(f"📂 后端目录: {status.backend_dir or '未部署'}")
            lines.append(f"🐍 虚拟环境: {'已创建' if status.venv_dir else '未创建'}")
            lines.append(f"🌿 Git 版本: {status.git_commit}")
            lines.append(f"⚙️ 进程状态: {'运行中 (PID=' + str(status.process_pid) + ')' if status.process_alive else '未运行'}")
            if status.last_deploy_time:
                from datetime import datetime as _dt
                deploy_time = _dt.fromtimestamp(status.last_deploy_time).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"🕐 最后部署: {deploy_time}")
            if status.last_error:
                lines.append(f"⚠️ 最后错误: {status.last_error}")
        lines.append("")
        lines.append(f"🔗 当前 base_url: {self.base_url}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow更新后端")
    async def ow_update_backend_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow更新后端", True))
        """更新后端代码并重启（仅 AstrBot 管理员可用，auto 模式生效）"""
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端由您自行部署管理，无法通过插件更新。")
            return

        yield event.plain_result("⏳ 正在更新后端：拉取最新代码 → 安装依赖 → 重新生成配置 → 重启服务...")

        result = await self.deploy_manager.update()
        status_icon = "✅" if result.success else "❌"
        lines = [f"{status_icon} 更新{'成功' if result.success else '失败'}（耗时 {result.elapsed:.1f}s）", "", result.message]
        if result.logs:
            lines.append("")
            lines.append("📋 更新日志（最后 20 行）：")
            tail_logs = result.logs[-20:] if len(result.logs) > 20 else result.logs
            for log_line in tail_logs:
                lines.append(log_line)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow停止后端")
    async def ow_stop_backend_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow停止后端", True))
        """停止后端进程（仅 AstrBot 管理员可用，auto 模式生效）"""
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端由您自行部署管理。")
            return

        result = await self.deploy_manager.stop()
        icon = "✅" if result.success else "❌"
        yield event.plain_result(f"{icon} {result.message}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow重启后端")
    async def ow_restart_backend_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow重启后端", True))
        """重启后端进程（仅 AstrBot 管理员可用，auto 模式生效）"""
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端由您自行部署管理。")
            return

        yield event.plain_result("⏳ 正在重启后端服务...")
        result = await self.deploy_manager.restart()
        status_icon = "✅" if result.success else "❌"
        lines = [f"{status_icon} 重启{'成功' if result.success else '失败'}", "", result.message]
        if result.logs:
            lines.append("")
            lines.append("📋 日志（最后 15 行）：")
            tail_logs = result.logs[-15:] if len(result.logs) > 15 else result.logs
            for log_line in tail_logs:
                lines.append(log_line)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow部署日志")
    async def ow_deploy_logs_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow部署日志", True))
        """查看后端运行日志（仅 AstrBot 管理员可用，auto 模式生效）"""
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端日志请到您部署后端的服务器查看。")
            return

        logs = self.deploy_manager.get_logs(50)
        if not logs:
            yield event.plain_result("📋 暂无后端运行日志。后端启动后日志将在此显示。")
            return

        lines = ["📋 后端运行日志（最近 50 行）：", ""]
        lines.extend(logs)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow后端日志")
    async def ow_backend_logs_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow后端日志", True))
        """查看 Overstats 后端持久化日志文件（仅 AstrBot 管理员可用，auto 模式生效）。

        从专用日志文件中提取最新的 30 条记录。与 /ow部署日志 不同，
        此指令读取持久化日志文件，日志跨后端重启保留。
        """
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端日志请到您部署后端的服务器查看。")
            return

        logs = self.deploy_manager.get_backend_logs(30)
        if not logs:
            yield event.plain_result(
                "📋 暂无 Overstats 后端持久化日志。\n"
                f"日志文件路径: {self.deploy_manager.log_file_path}\n"
                "后端启动并产生输出后日志将写入该文件。"
            )
            return

        lines = [
            f"📋 Overstats 后端持久化日志（最新 {len(logs)} 条）：",
            f"📂 文件: {self.deploy_manager.log_file_path}",
            "",
        ]
        lines.extend(logs)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端")
    async def ow_uninstall_backend_cmd(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow卸载后端", True))
        """卸载 Overstats 后端（仅 AstrBot 管理员可用，auto 模式生效）。

        独立于插件卸载，安全隔离后端资源删除：
        - 先停止后端进程，再删除文件，避免数据库损坏
        - 可选参数：强制 / 仅代码 / 仅venv
        """
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端由您自行部署管理，无法通过插件卸载。")
            return

        # 获取卸载预览
        preview = self.deploy_manager.get_uninstall_preview()
        usage = preview["disk_usage"]

        # 构建预览信息
        lines = [
            "🗑️ 后端卸载预览：",
            "",
            f"📂 后端代码目录: {preview['backend_dir']}",
            f"   {'✅ 存在' if preview['backend_exists'] else '❌ 不存在'}（{usage['backend_code']} MB）",
            f"🐍 虚拟环境目录: {preview['venv_dir']}",
            f"   {'✅ 存在' if preview['venv_exists'] else '❌ 不存在'}（{usage['venv']} MB）",
            f"⚙️ 后端进程: {'🟢 运行中' if preview['process_running'] else '⚪ 未运行'}",
            f"💾 总占用空间: {usage['total']} MB",
        ]

        if preview["db_files"]:
            lines.append("")
            lines.append("⚠️ 以下数据库文件将被删除（不可恢复）：")
            for db_file in preview["db_files"]:
                lines.append(f"   • {db_file}")

        lines.append("")
        lines.append("📋 卸载选项：")
        lines.append("• 回复 /ow卸载后端执行确认 — 删除全部（代码+venv+数据库）")
        lines.append("• 回复 /ow卸载后端执行仅代码 — 仅删除后端代码（保留venv）")
        lines.append("• 回复 /ow卸载后端执行仅venv — 仅删除虚拟环境（保留代码）")
        lines.append("• 回复 /ow卸载后端执行强制 — 强制删除全部（进程无法停止时使用）")
        lines.append("")
        lines.append("❗ 卸载后如需重新使用，请重新执行 /ow部署")

        yield event.plain_result("\n".join(lines))

    async def _uninstall_exec(self, event: AstrMessageEvent, delete_code: bool, delete_venv: bool, force: bool):
        """统一卸载执行逻辑。"""
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端由您自行部署管理。")
            return
        preview = self.deploy_manager.get_uninstall_preview()
        if not preview["backend_exists"] and not preview["venv_exists"]:
            yield event.plain_result("ℹ️ 后端代码和虚拟环境均不存在，无需卸载。")
            return
        yield event.plain_result("⏳ 正在执行后端卸载（先停止进程 → 再删除文件）...")
        result = await self.deploy_manager.uninstall_backend(
            delete_code=delete_code, delete_venv=delete_venv, force=force,
        )
        status_icon = "✅" if result.success else "⚠️"
        lines = [
            f"{status_icon} 卸载{'完成' if result.success else '部分完成'}（释放 {result.freed_space_mb:.1f} MB）",
            "", result.message, "",
        ]
        lines.extend(result.details)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端执行确认")
    async def ow_uninstall_backend_confirm(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow卸载后端执行确认", True))
        """删除全部后端资源（代码+venv+数据库）。"""
        async for msg in self._uninstall_exec(event, delete_code=True, delete_venv=True, force=False):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端执行仅代码")
    async def ow_uninstall_backend_code_only(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow卸载后端执行仅代码", True))
        """仅删除后端代码目录（含数据库），保留虚拟环境。"""
        async for msg in self._uninstall_exec(event, delete_code=True, delete_venv=False, force=False):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端执行仅venv")
    async def ow_uninstall_backend_venv_only(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow卸载后端执行仅venv", True))
        """仅删除虚拟环境，保留后端代码。"""
        async for msg in self._uninstall_exec(event, delete_code=False, delete_venv=True, force=False):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端执行强制")
    async def ow_uninstall_backend_force(self, event: AstrMessageEvent):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("ow卸载后端执行强制", True))
        """强制删除全部后端资源（进程无法正常停止时使用）。"""
        async for msg in self._uninstall_exec(event, delete_code=True, delete_venv=True, force=True):
            yield msg

    @filter.command("群设置")
    async def group_config_cmd(self, event: AstrMessageEvent, action: str = "", value: str = ""):
        if self.monitor: asyncio.ensure_future(self.monitor.record_command("群设置", True))
        """查看/切换群组功能配置: /群设置 或 /群设置 提示 开|关 或 /群设置 追加提示 开|关"""
        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 此命令仅支持在群聊中使用")
            return
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 无法获取群组ID")
            return

        # 确保配置已从 KV 加载
        await self._ensure_group_config_loaded()
        cfg = self._get_group_feature_config(group_id)

        # 无参数时显示当前状态（所有人可查看）
        if not action:
            dp_status = "✅ 开启" if cfg.get("daily_prompt_skip", False) else "❌ 关闭"
            an_status = "✅ 开启" if cfg.get("append_notice", True) else "❌ 关闭"
            text = f"""📋 群组功能配置 (群号: {group_id})

🔔 首次提示后不再提示: {dp_status}
💡 追加交互提示: {an_status}

管理命令（仅管理员可用）：
• <qqbot-cmd-input text="/群设置 提示 开 " show="/群设置 提示 开" reference="false" /> / <qqbot-cmd-input text="/群设置 提示 关 " show="/群设置 提示 关" reference="false" />
• <qqbot-cmd-input text="/群设置 追加提示 开 " show="/群设置 追加提示 开" reference="false" /> / <qqbot-cmd-input text="/群设置 追加提示 关 " show="/群设置 追加提示 关" reference="false" />"""
            yield event.plain_result(self._format_markdown_by_platform(event, text))
            return

        # 有参数时执行设置，需要群管理员权限
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 仅群管理员或群主可以修改群组配置")
            return
        feature_map = {
            "提示": "daily_prompt_skip",
            "首次提示": "daily_prompt_skip",
            "追加提示": "append_notice",
            "追加": "append_notice",
            "交互提示": "append_notice",
        }
        action_map = {
            "开": True, "开启": True, "on": True, "true": True, "1": True,
            "关": False, "关闭": False, "off": False, "false": False, "0": False,
        }

        feature_key = feature_map.get(action)
        if not feature_key:
            yield event.plain_result(f"❌ 未知功能: {action}\n支持的功能: 提示、追加提示")
            return

        if not value:
            yield event.plain_result(f"❌ 请指定操作: 开 或 关\n示例: /群设置 {action} 开")
            return

        enabled = action_map.get(value)
        if enabled is None:
            yield event.plain_result(f"❌ 未知操作: {value}\n支持的操作: 开、关")
            return

        await self._set_group_feature_config(group_id, feature_key, enabled)

        feature_names = {"daily_prompt_skip": "首次提示后不再提示", "append_notice": "追加交互提示"}
        status_text = "✅ 已开启" if enabled else "❌ 已关闭"
        yield event.plain_result(f"✅ 群组 {group_id} 的【{feature_names[feature_key]}】功能已{status_text}")

    @filter.command("全量适配开")
    async def full_adapt_enable(self, event: AstrMessageEvent, nickname: str = ""):
        """开启本群的全量消息适配 — @昵称模式（群管理员专属）。

        用法：/全量适配开 [机器人昵称]
        昵称可选，用于匹配"@昵称 指令"格式的纯文本消息。
        不填则优先自动探测（真实@机器人时获取），其次使用配置面板的「全量适配昵称兜底」。
        如需直接匹配模式（无需 @前缀），请使用 /全量适配开完全匹配。
        """
        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 此命令仅支持在群聊中使用")
            return
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 仅群管理员或群主可操作全量适配开关")
            return
        group_id = self._get_group_id(event)
        nickname = (nickname or "").strip()
        self._set_full_adapt(group_id, True, nickname if nickname else None, mode="nickname")
        eff_nick = nickname or self._get_bot_nickname(event, self._get_full_adapt(group_id)) or "（未设置，将自动探测或使用全局兜底）"
        yield event.plain_result(
            f"✅ 本群（{group_id}）已开启全量消息适配（@昵称模式）。\n"
            f"⚠️ 需群主在「机器人设置 → 机器人可获取消息范围」中开启「获取群内全部消息」，全量适配才实际生效。\n"
            f"📝 触发格式：@机器人昵称 指令（纯文本即可，无需真实@）。\n"
            f"🤖 当前机器人昵称：{eff_nick}\n"
            f"💡 可用 <qqbot-cmd-input text=\"/全量适配开 \" show=\"/全量适配开\" reference=\"false\" /> 重新指定昵称；<qqbot-cmd-input text=\"/全量适配开完全匹配\" show=\"/全量适配开完全匹配\" reference=\"false\" /> 切换为直接匹配；<qqbot-cmd-input text=\"/全量适配关 \" show=\"/全量适配关\" reference=\"false\" /> 关闭。"
        )

    @filter.command("全量适配开完全匹配")
    async def full_adapt_enable_direct(self, event: AstrMessageEvent):
        """开启本群的全量消息适配 — 直接匹配模式（群管理员专属）。

        用法：/全量适配开完全匹配
        开启后群聊中直接发送 table 收录的指令即可触发（如直接发「今日总结」），无需 @机器人昵称。
        """
        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 此命令仅支持在群聊中使用")
            return
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 仅群管理员或群主可操作全量适配开关")
            return
        group_id = self._get_group_id(event)
        self._set_full_adapt(group_id, True, mode="direct")
        yield event.plain_result(
            f"✅ 本群（{group_id}）已开启全量消息适配（直接匹配模式）。\n"
            f"⚠️ 需群主在「机器人设置 → 机器人可获取消息范围」中开启「获取群内全部消息」，全量适配才实际生效。\n"
            f"📝 触发格式：直接发送指令即可（如「今日总结」「对局」等），无需 @机器人昵称。\n"
            f"💡 可用 <qqbot-cmd-input text=\"/全量适配开 \" show=\"/全量适配开\" reference=\"false\" /> 切换为@昵称模式；<qqbot-cmd-input text=\"/全量适配关 \" show=\"/全量适配关\" reference=\"false\" /> 关闭。"
        )

    @filter.command("全量适配关")
    async def full_adapt_disable(self, event: AstrMessageEvent):
        """关闭本群的全量消息适配（群管理员专属）。"""
        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 此命令仅支持在群聊中使用")
            return
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 仅群管理员或群主可操作全量适配开关")
            return
        group_id = self._get_group_id(event)
        self._set_full_adapt(group_id, False)
        yield event.plain_result(f"✅ 本群（{group_id}）已关闭全量消息适配。")

    @filter.command("管理")
    async def admin_menu(self, event: AstrMessageEvent):
        """管理员维护指令菜单（仅 Bot 管理员或群主/群管理员可用）"""
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 此命令仅限 Bot 管理员或群主/群管理员使用")
            return

        text = """🛠️ 管理员维护指令菜单

🔧 **维护管理：**
• <qqbot-cmd-input text="/维护 " show="/维护 内容" reference="false" /> 开启维护模式（如：/维护 服务升级中，暂停服务）
• <qqbot-cmd-input text="/维护 取消 " show="/维护 取消" reference="false" /> 关闭维护模式

⛔ **违规封禁管理：**
• <qqbot-cmd-input text="/ow违禁封禁 " show="/ow违禁封禁 用户ID 指令名" reference="false" /> 封禁用户指定指令12h（如：/ow违禁封禁 qqofficial:1170599013 单局详细）
• <qqbot-cmd-input text="/ow违禁解封 " show="/ow违禁解封 用户ID 指令名" reference="false" /> 解除用户指定指令封禁

⚙️ **群组配置：**
• <qqbot-cmd-input text="/群设置 " show="/群设置" reference="false" /> 查看当前群组功能配置
• <qqbot-cmd-input text="/群设置 提示 开 " show="/群设置 提示 开" reference="false" /> / <qqbot-cmd-input text="/群设置 提示 关 " show="/群设置 提示 关" reference="false" /> 切换首次提示后不再提示
• <qqbot-cmd-input text="/群设置 追加提示 开 " show="/群设置 追加提示 开" reference="false" /> / <qqbot-cmd-input text="/群设置 追加提示 关 " show="/群设置 追加提示 关" reference="false" /> 切换追加交互提示
• <qqbot-cmd-input text="/全量适配开 " show="/全量适配开" reference="false" /> [昵称] / <qqbot-cmd-input text="/全量适配开完全匹配 " show="/全量适配开完全匹配" reference="false" /> / <qqbot-cmd-input text="/全量适配关 " show="/全量适配关" reference="false" /> 全量适配（@昵称/直接匹配/关闭，按群独立）

🔌 **系统诊断（所有模式通用）：**
• <qqbot-cmd-input text="/ow连接测试 " show="/ow连接测试" reference="false" /> 测试 Overstats 后端连接状态
• <qqbot-cmd-input text="/ow部署状态 " show="/ow部署状态" reference="false" /> 查看后端接入模式与运行状态

🚀 **一键部署（仅 auto 托管模式）：**
• <qqbot-cmd-input text="/ow部署 " show="/ow部署" reference="false" /> 一键部署后端（首次使用必执行）
• <qqbot-cmd-input text="/ow更新后端 " show="/ow更新后端" reference="false" /> 拉取最新代码并重启（跟进原项目更新）
• <qqbot-cmd-input text="/ow停止后端 " show="/ow停止后端" reference="false" /> 停止后端进程
• <qqbot-cmd-input text="/ow重启后端 " show="/ow重启后端" reference="false" /> 重启后端（修改配置后需执行）
• <qqbot-cmd-input text="/ow部署日志 " show="/ow部署日志" reference="false" /> 查看后端运行日志（内存，最近50行）
• <qqbot-cmd-input text="/ow后端日志 " show="/ow后端日志" reference="false" /> 查看 Overstats 后端持久化日志（文件，最新30条）

🗑️ **后端卸载（仅 auto 托管模式）：**
• <qqbot-cmd-input text="/ow卸载后端 " show="/ow卸载后端" reference="false" /> 预览卸载影响（空间/数据库/进程）
• <qqbot-cmd-input text="/ow卸载后端执行确认 " show="/ow卸载后端执行确认" reference="false" /> 删除全部后端资源（代码+venv+数据库）
• <qqbot-cmd-input text="/ow卸载后端执行仅代码 " show="/ow卸载后端执行仅代码" reference="false" /> 仅删除后端代码（保留venv）
• <qqbot-cmd-input text="/ow卸载后端执行仅venv " show="/ow卸载后端执行仅venv" reference="false" /> 仅删除虚拟环境（保留代码）
• <qqbot-cmd-input text="/ow卸载后端执行强制 " show="/ow卸载后端执行强制" reference="false" /> 强制删除（进程无法停止时使用）

💡 维护模式开启后，所有指令将直接返回维护内容，不再执行业务逻辑。
💡 一键部署指令需在配置面板切换为 auto 模式后使用，配置修改后需重载插件生效。
💡 后端卸载与插件卸载相互独立，卸载后端不影响插件其他功能（manual 模式仍可用）。"""
        yield event.plain_result(self._format_markdown_by_platform(event, text))

    def _register_monitor_apis(self):
        """注册监控面板的 10 个 Web API 端点。"""
        P = "astrbot_plugin_overstats"  # 必须与 metadata.yaml 的 name 一致
        ctx = self.context
        ctx.register_web_api(f"/{P}/monitor/overview", self._api_monitor_overview, ["GET"], "监控总览")
        ctx.register_web_api(f"/{P}/monitor/commands", self._api_monitor_commands, ["GET"], "指令统计")
        ctx.register_web_api(f"/{P}/monitor/commands/failures", self._api_monitor_cmd_failures, ["GET"], "指令失败原因")
        ctx.register_web_api(f"/{P}/monitor/trend", self._api_monitor_trend, ["GET"], "日趋势")
        ctx.register_web_api(f"/{P}/monitor/hourly", self._api_monitor_hourly, ["GET"], "时段分布")
        ctx.register_web_api(f"/{P}/monitor/errors", self._api_monitor_errors, ["GET"], "错误日志")
        ctx.register_web_api(f"/{P}/monitor/deploy", self._api_monitor_deploy, ["GET"], "部署状态")
        ctx.register_web_api(f"/{P}/monitor/errors/stream", self._api_monitor_errors_stream, ["GET"], "SSE 错误流")
        ctx.register_web_api(f"/{P}/monitor/backend/perf", self._api_monitor_backend_perf, ["GET"], "后端性能")
        ctx.register_web_api(f"/{P}/monitor/backend/upstream", self._api_monitor_backend_upstream, ["GET"], "上游统计")
        ctx.register_web_api(f"/{P}/monitor/shiqu/calls", self._api_monitor_shiqu_calls, ["GET"], "是区吗调用日志")
        ctx.register_web_api(f"/{P}/monitor/rate_limit", self._api_monitor_rate_limit, ["GET"], "限流统计")
        ctx.register_web_api(f"/{P}/monitor/clear", self._api_monitor_clear, ["POST"], "清空统计")

    async def _api_monitor_overview(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        start = request.query.get("start", "", type=str)
        end = request.query.get("end", "", type=str)
        data = await self.monitor.get_overview(start=start or None, end=end or None)
        dm = self.deploy_manager
        try:
            status = dm.status()
        except Exception:
            status = None
        data["deploy"] = {
            "mode": dm.mode,
            "state": status.state if status else "unknown",
            "process_alive": status.process_alive if status else False,
            "backend_port": status.backend_port if status else 0,
            "pid": status.process_pid if status else None,
            "git_commit": status.git_commit if status else "unknown",
            "last_deploy_time": status.last_deploy_time if status else 0,
            "last_error": status.last_error if status else "",
        } if status else {"mode": dm.mode, "state": "unknown"}
        rl_stats = await self.monitor.get_rate_limit_stats()
        data["rate_limit"] = rl_stats
        data["rate_limit_config"] = {
            "cmd_enabled": getattr(self, "_rate_limit_enabled", False),
            "cmd_max": getattr(self, "_rate_limit_max", 3),
            "llm_enabled": getattr(self, "_llm_rate_limit_enabled", False),
            "llm_per_minute": getattr(self, "_llm_rate_limit_per_minute", 10),
        }
        return json_response(data)

    async def _api_monitor_commands(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        category = request.query.get("category", "", type=str)
        search = request.query.get("search", "", type=str)
        start = request.query.get("start", "", type=str)
        end = request.query.get("end", "", type=str)
        data = await self.monitor.get_cmd_stats(category=category, search=search, start=start or None, end=end or None)
        return json_response(data)

    async def _api_monitor_cmd_failures(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        cmd = request.query.get("cmd", "", type=str)
        start = request.query.get("start", "", type=str)
        end = request.query.get("end", "", type=str)
        if not cmd:
            return json_response({"error": "缺少 cmd 参数"})
        reasons = await self.monitor.get_cmd_failure_reasons(cmd, start=start or None, end=end or None)
        return json_response({"cmd": cmd, "reasons": reasons})

    async def _api_monitor_trend(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        cmd = request.query.get("cmd", "", type=str)
        days = request.query.get("days", 7, type=int)
        data = await self.monitor.get_cmd_trend(cmd_name=cmd, days=max(1, min(90, days)))
        return json_response(data)

    async def _api_monitor_hourly(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        date = request.query.get("date", "", type=str)
        data = await self.monitor.get_hourly_distribution(date=date)
        return json_response(data)

    async def _api_monitor_errors(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        limit = request.query.get("limit", 50, type=int)
        offset = request.query.get("offset", 0, type=int)
        level = request.query.get("level", "", type=str)
        rows, total = await self.monitor.get_errors(limit=min(limit, 200), offset=offset, level=level)
        return json_response({"rows": rows, "total": total})

    async def _api_monitor_deploy(self):
        dm = self.deploy_manager
        try:
            status = dm.status()
        except Exception:
            return json_response({"error": "获取部署状态失败"})
        return json_response({
            "mode": dm.mode,
            "state": status.state,
            "backend_dir": str(status.backend_dir) if status.backend_dir else "",
            "backend_port": status.backend_port,
            "backend_host": status.backend_host,
            "git_commit": status.git_commit,
            "process_alive": status.process_alive,
            "process_pid": status.process_pid,
            "last_deploy_time": status.last_deploy_time,
            "last_error": status.last_error,
        })

    async def _api_monitor_errors_stream(self):
        if not self.monitor_sse:
            return error_response("监控未初始化", status_code=503)

        async def stream():
            if self.monitor:
                existing = await self.monitor.get_all_errors()
                import json as _json
                yield f"event: init\ndata: {_json.dumps(existing[-100:], ensure_ascii=False)}\n\n"
            async for event in self.monitor_sse.subscribe(poll_timeout=5.0):
                import json as _json
                if event.get("_heartbeat"):
                    yield ": heartbeat\n\n"
                else:
                    yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"

        return stream_response(stream())

    async def _api_monitor_backend_perf(self):
        db_path = getattr(self, "_req_metrics_db_path", None)
        if not db_path or not str(db_path):
            # 返回预期路径帮助排查
            hint = "未配置 sqlite_db_path" if self.deploy_manager.mode == "manual" else "auto 模式未找到 overstats_backend"
            expected = str(self.plugin_data_dir / "overstats_backend" / "src" / "db" / "request_metrics.sqlite3")
            return json_response({"available": False, "data": [], "hint": hint, "expected_path": expected})
        endpoint = request.query.get("endpoint", "", type=str)
        hours = request.query.get("hours", 24, type=int)
        reader = getattr(self, "_backend_metrics_reader", None) or BackendMetricsReader()
        perf = reader.get_endpoint_perf_stats(str(db_path), endpoint=endpoint, time_range_hours=hours)
        slow = reader.get_slow_endpoints(str(db_path), time_range_hours=hours)
        info = reader.get_db_info(str(db_path))
        return json_response({"available": True, "perf": perf, "slow": slow, "db_info": info})

    async def _api_monitor_backend_upstream(self):
        db_path = getattr(self, "_req_metrics_db_path", None)
        if not db_path or not str(db_path):
            return json_response({"available": False, "data": [], "table_exists": False})
        limit = request.query.get("limit", 30, type=int)
        reader = getattr(self, "_backend_metrics_reader", None) or BackendMetricsReader()
        result = reader.get_upstream_stats(str(db_path), limit=limit)
        return json_response({"available": True, **result})

    async def _api_monitor_rate_limit(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        stats = await self.monitor.get_rate_limit_stats()
        return json_response({
            "stats": stats,
            "config": {
                "cmd_enabled": getattr(self, "_rate_limit_enabled", False),
                "cmd_max": getattr(self, "_rate_limit_max", 3),
                "llm_enabled": getattr(self, "_llm_rate_limit_enabled", False),
                "llm_per_minute": getattr(self, "_llm_rate_limit_per_minute", 10),
            }
        })

    async def _api_monitor_clear(self):
        if not self.monitor:
            return json_response({"error": "监控未初始化"})
        deleted = await self.monitor.clear_all_stats()
        # 同时清空 SSE 队列
        if self.monitor_sse:
            pass  # 队列自动丢弃旧消息
        return json_response({"deleted": deleted, "message": f"已清空 {deleted} 条记录"})

    async def _api_monitor_shiqu_calls(self):
        """GET /monitor/shiqu/calls?limit=30&offset=0&openid=&target_id=&success=&verdict=&search="""
        log_dir = self.plugin_data_dir / "shiqu"

        limit = request.query.get("limit", 30, type=int)
        offset = request.query.get("offset", 0, type=int)
        openid = request.query.get("openid", "", type=str)
        target_id = request.query.get("target_id", "", type=str)
        success_str = request.query.get("success", "")
        verdict = request.query.get("verdict", "", type=str)
        search = request.query.get("search", "", type=str)

        success = None  # type: bool | None
        if success_str.lower() in ("true", "1", "yes"):
            success = True
        elif success_str.lower() in ("false", "0", "no"):
            success = False

        reader = ShiquCallReader()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            reader.query,
            log_dir,
            limit,
            offset,
            openid,
            target_id,
            success,
            verdict,
            search,
        )
        return json_response(result)
