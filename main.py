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
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from astrbot.api.all import *
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.event import filter, AstrMessageEvent  # 引入原生消息与事件过滤模块

# 一键部署模块（独立于 main.py，避免主文件臃肿）
try:
    from .deploy import DeployManager
except ImportError:
    # 兜底：插件作为顶层模块加载时相对导入可能失败
    from deploy import DeployManager  # type: ignore[no-redef]

logger = logging.getLogger("astrbot")

@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "2.2.0")
class OverstatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.file_lock = asyncio.Lock()
        self.daily_group_prompt_suffix = "[今日已提示，群内后续指令不再提示将直接返回结果]"
        self.daily_group_prompt_notice = '群内使用请手动<qqbot-cmd-input text=" " show="@机器人" reference="false"/>或点击<qqbot-cmd-input text="快捷指令" show="快捷指令" reference="false"/>按钮，纯文本无法识别。'
        self.error_append_notice = '如需重新发起指令，请手动<qqbot-cmd-input text=" " show="@机器人" reference="false"/>或点击<qqbot-cmd-input text="快捷指令" show="快捷指令" reference="false"/>按钮，纯文本无法识别。'
        self.daily_prompt_pending_users: set[str] = set()
        self.id_resolve_error_hint = "未查询到id或者id错误，id严格区分大小写"
        
        try:
            plugin_name = getattr(self, "name", "overstats_full")
            self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            self.temp_image_dir = self.plugin_data_dir / "temp"
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"初始化插件专属数据目录失败: {e}")
            self.plugin_data_dir = Path(tempfile.gettempdir())
            self.temp_image_dir = self.plugin_data_dir / "temp"
            self.temp_image_dir.mkdir(parents=True, exist_ok=True)

        # 一键部署管理器（auto 模式下托管后端生命周期，manual 模式下仅做状态查询）
        # 必须在 plugin_data_dir 初始化后实例化，依赖数据目录存放后端代码与 venv
        self.deploy_manager = DeployManager(
            plugin_data_dir=self.plugin_data_dir,
            config=config,
            context=context,
        )
        # 根据部署模式解析 base_url：
        # - auto 模式：指向本地托管的后端 http://host:port/api/v2
        # - manual 模式：使用用户填写的 overstats_api_url
        self.base_url = self.deploy_manager.resolve_base_url()
        logger.info(f"[Overstats] 接入模式: {self.deploy_manager.mode}, base_url={self.base_url}")

        self.daily_prompt_state_file = self.plugin_data_dir / "daily_group_prompt_state.json"
        self.daily_prompt_state = self._load_daily_prompt_state()
        self.daily_prompt_reset_task = asyncio.create_task(self._daily_prompt_reset_loop())

        # 复用 aiohttp.ClientSession（懒加载，首次请求时创建）
        self._http_session: aiohttp.ClientSession | None = None

        # 定时清理临时图片（每天凌晨执行一次，替代逐张延迟删除）
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())

        # 群组级别配置（使用 AstrBot KV 存储持久化，内存缓存加速读取）
        self.group_config: dict | None = None  # None 表示尚未从 KV 加载

        # 维护模式状态（使用 AstrBot KV 存储持久化，惰性加载）
        self._maintenance_state: dict | None = None

        # 全量适配配置（按群独立开关 + 机器人昵称，持久化到 JSON 文件）
        self.full_adapt_config_file = self.plugin_data_dir / "full_adaptation_config.json"
        self.full_adapt_config: dict | None = None  # 惰性加载
        self._bot_nickname_cache: str | None = None  # 自动探测的机器人昵称缓存
        # 全量适配指令分发表：指令名/别名 -> 方法引用（懒构建，避免 __init__ 时方法未绑定）
        self._full_adapt_map: dict | None = None

        # auto 模式下按需自动启动后端（异步，不阻塞插件初始化）
        # 保存 task 引用，便于 terminate 时取消，避免遗留异步任务
        self._auto_start_task = asyncio.create_task(self.deploy_manager.maybe_auto_start())

    def _current_prompt_cycle_date(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        if now.time() < dt_time(hour=4):
            now -= timedelta(days=1)
        return now.date().isoformat()

    def _seconds_until_next_prompt_reset(self, now: datetime | None = None) -> float:
        now = now or datetime.now()
        next_reset = datetime.combine(now.date(), dt_time(hour=4))
        if now >= next_reset:
            next_reset += timedelta(days=1)
        return max((next_reset - now).total_seconds(), 1.0)

    def _build_empty_daily_prompt_state(self, cycle_date: str | None = None) -> dict:
        return {
            "cycle_date": cycle_date or self._current_prompt_cycle_date(),
            "prompted_users": {}
        }

    def _load_daily_prompt_state(self) -> dict:
        default_state = self._build_empty_daily_prompt_state()
        try:
            if self.daily_prompt_state_file.exists():
                with open(self.daily_prompt_state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("prompted_users"), dict):
                    data.setdefault("cycle_date", default_state["cycle_date"])
                    return data
        except Exception as e:
            logger.error(f"读取每日首次提示状态失败: {e}")

        self._save_daily_prompt_state(default_state)
        return default_state

    def _save_daily_prompt_state(self, data: dict | None = None):
        payload = data or self.daily_prompt_state
        try:
            with open(self.daily_prompt_state_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存每日首次提示状态失败: {e}")

    def _ensure_daily_prompt_state_current_unlocked(self):
        current_cycle_date = self._current_prompt_cycle_date()
        if self.daily_prompt_state.get("cycle_date") != current_cycle_date:
            self.daily_prompt_state = self._build_empty_daily_prompt_state(current_cycle_date)
            self._save_daily_prompt_state()
            self.daily_prompt_pending_users.clear()

    def _reset_daily_prompt_state_unlocked(self):
        self.daily_prompt_state = self._build_empty_daily_prompt_state()
        self._save_daily_prompt_state()
        self.daily_prompt_pending_users.clear()
        logger.info("已重置群聊每日首次提示状态")

    async def _daily_prompt_reset_loop(self):
        try:
            while True:
                await asyncio.sleep(self._seconds_until_next_prompt_reset())
                async with self.file_lock:
                    self._reset_daily_prompt_state_unlocked()
        except asyncio.CancelledError:
            logger.info("群聊每日首次提示重置任务已停止")
        except Exception as e:
            logger.error(f"群聊每日首次提示重置任务异常: {e}")

    _GROUP_CONFIG_KV_KEY = "group_feature_config"

    async def _ensure_group_config_loaded(self):
        """确保群组配置已从 KV 存储加载到内存（惰性加载，仅在首次调用时读取）"""
        if self.group_config is not None:
            return
        try:
            data = await self.get_kv_data(self._GROUP_CONFIG_KV_KEY, None)
            if isinstance(data, dict):
                self.group_config = data
            else:
                self.group_config = {}
        except Exception as e:
            logger.error(f"从 KV 读取群组功能配置失败: {e}")
            self.group_config = {}

    async def _save_group_config(self):
        """将内存中的群组配置持久化到 KV 存储"""
        try:
            await self.put_kv_data(self._GROUP_CONFIG_KV_KEY, self.group_config)
        except Exception as e:
            logger.error(f"保存群组功能配置到 KV 失败: {e}")

    def _get_group_id(self, event: AstrMessageEvent) -> str | None:
        """从事件中获取群组ID"""
        try:
            if getattr(event, "message_obj", None) and getattr(event.message_obj, "group_id", ""):
                return str(event.message_obj.group_id)
        except Exception:
            pass
        return None

    def _is_astrbot_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为 AstrBot 系统管理员（配置面板中的 admins_id）。

        与 _is_group_admin 不同，此方法仅检查 AstrBot 全局管理员列表，
        不包含群主/群管理员。用于维护模式等需要系统管理员权限的场景。
        """
        try:
            sender_id = str(event.get_sender_id())
            admin_ids = []
            if hasattr(self.context, "get_config"):
                cfg = self.context.get_config()
                admin_ids = [str(uid) for uid in getattr(cfg, "admins_id", [])]
            if sender_id in admin_ids:
                return True
        except Exception:
            pass
        # AstrMessageEvent 携带的 admin 标记（@filter.permission_type(ADMIN) 触发的）
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return False

    def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为群管理员/群主 或 AstrBot 管理员"""
        sender_id = ""
        # 1. 检查 AstrBot 管理员列表
        try:
            sender_id = str(event.get_sender_id())
            admin_ids = []
            if hasattr(self.context, "get_config"):
                cfg = self.context.get_config()
                admin_ids = [str(uid) for uid in getattr(cfg, "admins_id", [])]
            if sender_id in admin_ids:
                return True
        except Exception:
            pass

        # 2. 检查 QQ 群角色（OneBot raw_message 格式）
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw:
                # OneBot/aiocqhttp: raw_message 是 dict，含 sender.role
                if isinstance(raw, dict):
                    sender_data = raw.get("sender", {})
                    role = str(sender_data.get("role", "")).lower()
                    logger.debug(f"群管理员检查(OneBot dict): sender_id={sender_id}, role={role}")
                    if role in ("owner", "admin"):
                        return True
                # QQ Official 等平台: raw_message 可能有不同结构
                elif hasattr(raw, "sender"):
                    role = str(getattr(raw.sender, "role", "")).lower()
                    logger.debug(f"群管理员检查(raw.sender): role={role}")
                    if role in ("owner", "admin", "群主", "管理员"):
                        return True
                else:
                    logger.debug(f"群管理员检查: raw_message 类型={type(raw).__name__}, 无法提取角色")
        except Exception as e:
            logger.debug(f"群管理员检查 raw_message 异常: {e}")

        # 3. 检查 AstrMessageEvent 是否携带 admin 标记
        try:
            if event.is_admin():
                return True
        except Exception:
            pass

        return False

    def _get_group_feature_config(self, group_id: str) -> dict:
        """获取指定群组的功能配置（从内存缓存读取），未配置时返回默认值"""
        if self.group_config is None:
            # 尚未加载，返回默认值（后续 async 方法会触发加载）
            return {"daily_prompt_skip": False, "append_notice": True}
        return self.group_config.get(str(group_id), {
            "daily_prompt_skip": False,
            "append_notice": True
        })

    async def _set_group_feature_config(self, group_id: str, feature: str, enabled: bool):
        """设置指定群组的某个功能开关并持久化"""
        await self._ensure_group_config_loaded()
        gid = str(group_id)
        if gid not in self.group_config:
            self.group_config[gid] = {"daily_prompt_skip": False, "append_notice": True}
        self.group_config[gid][feature] = enabled
        await self._save_group_config()

    # ==================== 全量适配（@机器人昵称 + 指令） ====================

    def _load_full_adapt_config(self) -> dict:
        """从 JSON 文件加载全量适配配置（惰性，带内存缓存）"""
        if self.full_adapt_config is not None:
            return self.full_adapt_config
        try:
            if self.full_adapt_config_file.exists():
                with open(self.full_adapt_config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.full_adapt_config = data if isinstance(data, dict) else {}
            else:
                self.full_adapt_config = {}
        except Exception as e:
            logger.error(f"读取全量适配配置失败: {e}")
            self.full_adapt_config = {}
        return self.full_adapt_config

    def _save_full_adapt_config(self):
        try:
            with open(self.full_adapt_config_file, "w", encoding="utf-8") as f:
                json.dump(self.full_adapt_config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存全量适配配置失败: {e}")

    def _get_full_adapt(self, group_id: str) -> dict:
        """获取指定群的全量适配配置"""
        return self._load_full_adapt_config().get(
            str(group_id), {"enabled": False, "nickname": ""}
        )

    def _set_full_adapt(self, group_id: str, enabled: bool, nickname: str | None = None):
        """设置指定群的全量适配开关（可选更新昵称）并持久化到 JSON"""
        cfg = self._load_full_adapt_config()
        gid = str(group_id)
        cur = cfg.get(gid, {"enabled": False, "nickname": ""})
        cur["enabled"] = enabled
        if nickname is not None and nickname.strip():
            cur["nickname"] = nickname.strip()
        cur.setdefault("nickname", "")
        cfg[gid] = cur
        self._save_full_adapt_config()

    def _is_real_at_bot(self, event: AstrMessageEvent) -> bool:
        """检测消息是否真实@了机器人（排除 QQ 官方占位@）。

        QQ 官方在「被动消息/全量推送」开启时，未真实@机器人的群消息也会带占位 At，
        此时 self_id 为 "qq_official"/"unknown_selfid"；真实@时 self_id 为机器人真实 ID
        （如 76092C918FC3D8D6204C848C07AC3E31，即机器人的 openid/标识）。
        普通机器人（aiocqhttp）self_id 为机器人 QQ 号，真实@时 At.qq 与之一致。
        """
        try:
            self_id = str(event.get_self_id())
        except Exception:
            return False
        # QQ 官方占位@：self_id 为占位符，并非真实@
        if not self_id or self_id in ("qq_official", "unknown_selfid"):
            return False
        try:
            for comp in event.get_messages():
                if isinstance(comp, Comp.At) and str(getattr(comp, "qq", "")) == self_id:
                    return True
        except Exception:
            pass
        return False

    def _get_bot_nickname(self, event: AstrMessageEvent, adapt_cfg: dict | None = None) -> str:
        """解析机器人昵称：群独立配置 > 自动探测(真实@时的 At.name) > 全局配置兜底"""
        # 1. 群独立配置
        if adapt_cfg and adapt_cfg.get("nickname"):
            return adapt_cfg["nickname"]
        # 2. 自动探测：当前消息真实@机器人时取 At.name
        try:
            self_id = str(event.get_self_id())
            if self_id and self_id not in ("qq_official", "unknown_selfid"):
                for comp in event.get_messages():
                    if isinstance(comp, Comp.At) and str(getattr(comp, "qq", "")) == self_id:
                        name = (getattr(comp, "name", "") or "").strip()
                        if name:
                            self._bot_nickname_cache = name
                            return name
        except Exception:
            pass
        # 3. 缓存的自动探测结果
        if self._bot_nickname_cache:
            return self._bot_nickname_cache
        # 4. 全局配置兜底
        return (self.config.get("full_adaptation_nickname", "") or "").strip()

    _MAINTENANCE_KV_KEY = "maintenance_state"

    async def _ensure_maintenance_loaded(self):
        """确保维护模式状态已从 KV 加载到内存"""
        if self._maintenance_state is not None:
            return
        try:
            data = await self.get_kv_data(self._MAINTENANCE_KV_KEY, None)
            if isinstance(data, dict):
                self._maintenance_state = data
            else:
                self._maintenance_state = {"enabled": False, "content": ""}
        except Exception as e:
            logger.error(f"读取维护模式状态失败: {e}")
            self._maintenance_state = {"enabled": False, "content": ""}

    async def _set_maintenance(self, enabled: bool, content: str = ""):
        """设置或取消维护模式并持久化"""
        await self._ensure_maintenance_loaded()
        self._maintenance_state["enabled"] = enabled
        self._maintenance_state["content"] = content if enabled else ""
        try:
            await self.put_kv_data(self._MAINTENANCE_KV_KEY, self._maintenance_state)
        except Exception as e:
            logger.error(f"保存维护模式状态失败: {e}")

    async def _check_maintenance(self, event: AstrMessageEvent) -> str | None:
        """集中维护模式检查，返回维护提示文本或 None（放行）。

        若维护模式启用且发送者非 AstrBot 管理员，返回维护内容；
        否则返回 None，允许正常执行业务逻辑。
        调用方应在 yield 维护文本后 return，不再继续执行业务。
        """
        await self._ensure_maintenance_loaded()
        if self._maintenance_state and self._maintenance_state.get("enabled"):
            if not self._is_astrbot_admin(event):
                return self._maintenance_state.get("content", "系统维护中")
        return None

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        try:
            if getattr(event, "message_obj", None) and getattr(event.message_obj, "group_id", ""):
                return True
        except Exception:
            pass
        return "GROUP_MESSAGE" in str(getattr(event, "unified_msg_origin", "")).upper()

    def _is_qq_group_message(self, event: AstrMessageEvent) -> bool:
        if not self._is_group_message(event):
            return False

        try:
            platform_name = event.get_platform_name()
            if "qq" in str(platform_name).lower():
                return True
        except Exception:
            pass

        return "qq" in str(getattr(event, "unified_msg_origin", "")).lower()

    def _append_group_interaction_notice(self, event: AstrMessageEvent, text: str) -> str:
        if not self._is_qq_group_message(event):
            return text
        # 检查群组级别 append_notice 配置（从内存缓存读取，若未加载则使用默认值 True）
        group_id = self._get_group_id(event)
        if group_id and self.group_config is not None:
            cfg = self._get_group_feature_config(group_id)
            if not cfg.get("append_notice", True):
                return text
        notice = self._format_markdown_by_platform(event, self.error_append_notice)
        if notice in text:
            return text
        return f"{text}\n💡 {notice}"

    def _plain_error_result(self, event: AstrMessageEvent, text: str):
        return event.plain_result(self._append_group_interaction_notice(event, text))

    def _id_resolve_err(self, prefix: str) -> str:
        """统一的 ID 解析失败提示文案，修改 id_resolve_error_hint 即可全局生效"""
        return f"❌ {prefix}：{self.id_resolve_error_hint}"

    async def _prepare_business_status_prompt(self, event: AstrMessageEvent, base_text: str) -> tuple[str | None, str | None, bool]:
        """准备业务状态提示。返回 (提示文本, 跟踪token, 是否应提前终止)。

        当 should_stop=True 时（维护模式），调用方应 yield 文本后 return，不再执行业务逻辑。
        AstrBot 管理员不受维护模式限制，可正常使用数据查询指令。
        """
        # 维护模式检查：激活时返回维护内容并标记提前终止（群聊/私聊均生效）
        # AstrBot 管理员绕过维护限制，可正常使用数据查询指令
        await self._ensure_maintenance_loaded()
        if self._maintenance_state and self._maintenance_state.get("enabled"):
            if self._is_astrbot_admin(event):
                # 管理员不受维护模式限制，正常执行业务逻辑
                pass
            else:
                return self._maintenance_state.get("content", "系统维护中"), None, True

        if not self._is_group_message(event):
            return base_text, None, False

        # 确保群组配置已加载
        await self._ensure_group_config_loaded()
        group_id = self._get_group_id(event)
        cfg = self._get_group_feature_config(group_id) if group_id else {"daily_prompt_skip": False, "append_notice": True}

        daily_prompt_enabled = cfg.get("daily_prompt_skip", False)
        append_notice_enabled = cfg.get("append_notice", True)

        # 首次提示后不再提示 功能：已提示过的用户直接跳过加载提示
        user_id = None
        if daily_prompt_enabled:
            user_id = str(event.get_sender_id())
            async with self.file_lock:
                self._ensure_daily_prompt_state_current_unlocked()
                prompted_users = self.daily_prompt_state.setdefault("prompted_users", {})
                if prompted_users.get(user_id) or user_id in self.daily_prompt_pending_users:
                    # 今日已提示过，不再显示加载提示，直接返回结果
                    return None, None, False
                else:
                    self.daily_prompt_pending_users.add(user_id)

        # 构建状态提示文本
        prompt_text = base_text

        # 追加「首次提示后不再提示」标记文案（仅首次提示时显示，由 daily_prompt_skip 控制）
        if daily_prompt_enabled and user_id:
            prompt_text = f"{prompt_text}\n{self.daily_group_prompt_suffix}"

        # 追加「交互提示」文案（由 append_notice 独立控制，与首次提示功能解耦）
        if append_notice_enabled and self._is_qq_group_message(event):
            notice = self._format_markdown_by_platform(event, self.daily_group_prompt_notice)
            if notice not in prompt_text:
                prompt_text = f"{prompt_text}\n💡 {notice}"

        return prompt_text, user_id if (daily_prompt_enabled and user_id) else None, False

    async def _finalize_business_status_prompt(self, reservation_user_id: str | None, success: bool):
        if not reservation_user_id:
            return

        async with self.file_lock:
            self._ensure_daily_prompt_state_current_unlocked()
            self.daily_prompt_pending_users.discard(reservation_user_id)
            if success:
                prompted_users = self.daily_prompt_state.setdefault("prompted_users", {})
                prompted_users[reservation_user_id] = True
                self._save_daily_prompt_state()

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """获取复用的 aiohttp.ClientSession（懒加载，自动重建已关闭的会话）"""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600),
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            )
        return self._http_session

    async def terminate(self):
        """插件卸载/停用时清理资源（AstrBot 生命周期方法）"""
        # 取消可能仍在运行的自动启动任务（避免卸载时遗留异步任务）
        auto_task = getattr(self, "_auto_start_task", None)
        if auto_task and not auto_task.done():
            auto_task.cancel()
            try:
                await auto_task
            except (asyncio.CancelledError, Exception):
                pass
        # 停止一键托管的后端进程（auto 模式下）
        try:
            if hasattr(self, "deploy_manager"):
                await self.deploy_manager.cleanup()
        except Exception as e:
            logger.warning(f"[Overstats] 停止后端进程异常: {e}")
        # 取消后台任务
        for task_attr in ("daily_prompt_reset_task", "_cleanup_task"):
            task = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # 关闭 HTTP 会话
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    def _is_qq_official(self, event: AstrMessageEvent) -> bool:
        """判断当前消息是否来自 QQ 官方机器人平台"""
        umo = event.unified_msg_origin
        if "qq_official" in str(umo).lower():
            return True
        if hasattr(event, "message_obj") and event.message_obj:
            if "qq_official" in str(event.message_obj).lower():
                return True
        return False

    def _format_markdown_by_platform(self, event: AstrMessageEvent, text: str) -> str:
        """
        根据平台环境动态处理文本：
        如果是 QQ 官方机器人：保留标签并对 text 和 show 属性进行 urlencode
        如果是其他机器人：剥离 <qqbot-cmd-input> 和 <qqbot-cmd-enter> 标签，将其转化为普通文本格式
        """
        if self._is_qq_official(event):
            def replacer_input(match):
                text_val = urllib.parse.quote(match.group(1))
                show_val = urllib.parse.quote(match.group(2))
                return f'<qqbot-cmd-input text="{text_val}" show="{show_val}" reference="false" />'

            def replacer_enter(match):
                text_val = urllib.parse.quote(match.group(1))
                return f'<qqbot-cmd-enter text="{text_val}" />'
            
            text = re.sub(r'<qqbot-cmd-input\s+text="([^"]+)"\s+show="([^"]+)"\s+reference="false"\s*/>', replacer_input, text)
            text = re.sub(r'<qqbot-cmd-enter\s+text="([^"]+)"\s*/>', replacer_enter, text)
            return text
        else:
            def strip_replacer_input(match):
                text_val = match.group(1).strip()
                return f"{text_val}"

            def strip_replacer_enter(match):
                text_val = match.group(1).strip()
                return f"{text_val}"
            
            text = re.sub(r'<qqbot-cmd-input\s+text="([^"]+)"\s+show="([^"]+)"\s+reference="false"\s*/>', strip_replacer_input, text)
            text = re.sub(r'<qqbot-cmd-enter\s+text="([^"]+)"\s*/>', strip_replacer_enter, text)
            return text

    def _get_binds_file_path(self) -> Path:
        bot_id = "default"
        try:
            if hasattr(self.context, "get_config"):
                bot_identity = self.context.get_config().active_bot_identity
                if bot_identity:
                    bot_id = str(bot_identity)
        except Exception:
            pass
        
        file_path = self.plugin_data_dir / f"binds_{bot_id}.json"
        
        if not file_path.exists():
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=4)
            except Exception as e:
                logger.error(f"创建机器人 [{bot_id}] 的绑定文件失败: {e}")
        return file_path

    def _load_binds(self) -> dict:
        try:
            file_path = self._get_binds_file_path()
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"读取绑定明文文件失败: {e}")
        return {}

    def _save_binds(self, data: dict):
        try:
            file_path = self._get_binds_file_path()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存绑定明文文件失败: {e}")

    async def _get_user_bind_id(self, user_id: str) -> str | None:
        async with self.file_lock:
            binds = self._load_binds()
            user_key = str(user_id)
            if user_key in binds:
                return binds[user_key]
            try:
                old_kv_key = f"bind_{user_id}"
                old_bnet_id = await self.get_kv_data(old_kv_key, None)
                if old_bnet_id:
                    binds[user_key] = str(old_bnet_id)
                    self._save_binds(binds)
                    return old_bnet_id
            except Exception as e:
                logger.error(f"迁移数据失败: {e}")
            return None

    async def _set_user_bind_id(self, user_id: str, bnet_id: str):
        async with self.file_lock:
            binds = self._load_binds()
            binds[str(user_id)] = bnet_id
            self._save_binds(binds)
            try:
                old_kv_key = f"bind_{user_id}"
                await self.delete_kv_data(old_kv_key)
            except Exception:
                pass

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str = "") -> str:
        if input_id and input_id.strip():
            clean_id = input_id.strip()
            # 战网ID格式校验：必须包含 # 或 ＃
            if "#" not in clean_id and "＃" not in clean_id:
                return None
            return clean_id
        user_id = event.get_sender_id()
        bind_id = await self._get_user_bind_id(user_id)
        return bind_id

    def _parse_treemap_args(self, arg1: str = "", arg2: str = "") -> tuple[str | None, str | None]:
        bnet_id = None
        season = None
        if arg1 and arg2:
            bnet_id = arg1
            season = arg2
        elif arg1:
            if arg1.isdigit():
                season = arg1
            else:
                bnet_id = arg1
        return bnet_id, season

    def _parse_profile_args(self, arg1: str = "", arg2: str = "") -> tuple[str | None, str]:
        bnet_id = None
        mode = "quick"
        for arg in [arg1, arg2]:
            if not arg:
                continue
            if arg in ["快速", "quick"]:
                mode = "quick"
            elif arg in ["竞技", "competitive"]:
                mode = "competitive"
            else:
                bnet_id = arg
        return bnet_id, mode

    # 普通用户友好的中文关键词表：命中即从位置参数中剔除，避免和英雄名/省份/战网ID冲突
    # 「大师」「钻石」等绝不会是英雄名或省份；「锐评关」「全员关」是自造词，无歧义
    _KEYWORD_MAP = {
        # 模式（pick率、强度图、段位分布）
        "快速": {"game_mode": "quick"},
        "竞技": {"game_mode": "competitive"},
        # 段位（mmr）
        "青铜": {"mmr": "Bronze"},
        "白银": {"mmr": "Silver"},
        "黄金": {"mmr": "Gold"},
        "白金": {"mmr": "Platinum"},
        "钻石": {"mmr": "Diamond"},
        "大师": {"mmr": "Master"},
        "宗师": {"mmr": "Grandmaster"},
        "英杰": {"mmr": "Champion"},
        # 绝活榜模式
        "开放": {"lb_mode": "open"},
        "预设": {"lb_mode": "preset"},
        # 单局详细开关
        "锐评": {"analyze": True},
        "锐评关": {"analyze": False},
        "全员关": {"show_all_heroes": False},
    }

    def _extract_keywords(self, args) -> tuple[list[str], dict]:
        """从位置参数中识别中文关键词，返回 (剩下的位置参数, 命中的字段字典)。
        关键词可无序、可省略、可混用；非关键词一律保留为位置参数。"""
        positional, fields = [], {}
        for a in args:
            if a is None:
                continue
            a = str(a).strip()
            if not a:
                continue
            hit = self._KEYWORD_MAP.get(a)
            if hit:
                fields.update(hit)
            else:
                positional.append(a)
        return positional, fields

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 600) -> tuple[bytes | None, dict | None]:
        url = f"{self.base_url}{endpoint}"
        payload = payload or {}
        try:
            session = await self._get_http_session()
            # 若请求超时与默认不同，使用单次覆盖
            req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout != 600 else None
            async with session.post(url, json=payload, timeout=req_timeout) as resp:
                if resp.status == 200:
                    return await resp.read(), None
                else:
                    try:
                        error_data = await resp.json()
                        logger.error(f"Overstats API 错误: {resp.status} - {error_data}")
                        return None, error_data
                    except:
                        logger.error(f"Overstats API 返回了非 JSON 错误: {resp.status}")
                        return None, {"error": "non_json_error", "message": "API返回非JSON格式错误"}
        except Exception as e:
            logger.error(f"网络请求异常: {e}")
            return None, {"error": "network_error", "message": str(e)}

    async def _periodic_cleanup_loop(self):
        """定时清理临时图片目录中超过 7 天的文件（每天执行一次）"""
        try:
            while True:
                await asyncio.sleep(86400)  # 24 小时
                try:
                    now = time.time()
                    count = 0
                    for f in self.temp_image_dir.iterdir():
                        if f.is_file() and (now - f.stat().st_mtime) > 7 * 86400:
                            try:
                                f.unlink()
                                count += 1
                            except Exception:
                                pass
                    if count:
                        logger.info(f"已清理 {count} 个过期临时图片文件")
                except Exception as e:
                    logger.error(f"临时文件清理异常: {e}")
        except asyncio.CancelledError:
            pass

    def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes, fallback_text: str = ""):
        if not img_bytes:
            return self._plain_error_result(event, fallback_text or "❌ 图片生成失败")
        try:
            img_hash = abs(hash(img_bytes))
            img_path = self.temp_image_dir / f"{img_hash}.png"
            img_path.write_bytes(img_bytes)
            user_id = event.get_sender_id()
            chain = [Comp.At(qq=user_id), Comp.Plain("\n" if not fallback_text else f"\n{fallback_text}\n"), Comp.Image.fromFileSystem(str(img_path))]
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f"构建图片消息链时发生错误: {e}")
            return self._plain_error_result(event, fallback_text or "❌ 机器人构建图片组件失败")

    async def _send_multiple_images_result(self, event: AstrMessageEvent, imgs_list: list[bytes]):
        try:
            user_id = event.get_sender_id()
            
            valid_images = [img for img in imgs_list if img]
            if not valid_images:
                yield self._plain_error_result(event, "❌ 未能获取到有效的图片数据")
                return

            chain = [Comp.At(qq=user_id), Comp.Plain("\n")]
            for img_bytes in valid_images:
                img_path = self.temp_image_dir / f"{abs(hash(img_bytes))}_{time.time_ns()}.png"
                img_path.write_bytes(img_bytes)
                chain.append(Comp.Image.fromFileSystem(str(img_path)))
            yield event.chain_result(chain)
                
        except Exception as e:
            logger.error(f"多图发送逻辑错误: {e}")
            yield self._plain_error_result(event, "❌ 多图发送失败")  

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE)
    async def handle_direct_text_events(self, event: AstrMessageEvent):
        """处理直接发送的战网 ID、单局数字、绑定指令，以及纯@时返回快速指南。

        兼容 QQ 官方机器人（qq_official / qq_official_webhook）与普通 QQ 机器人（aiocqhttp）。
        群聊中必须真实@机器人才会响应（排除 QQ 官方占位@[At:qq_official]，防止误触发），私聊放行。
        需在配置面板开启「是否开启直接消息处理」开关后生效。
        """
        # 配置开关：未开启直接消息处理时跳过
        if not bool(self.config.get("direct_message_handling_enabled", False)):
            return

        # 兼容 QQ 官方与普通机器人
        platform_name = ""
        try:
            platform_name = event.get_platform_name() or ""
        except Exception:
            pass
        if platform_name not in ("qq_official", "qq_official_webhook", "aiocqhttp"):
            return

        is_group = self._is_group_message(event)

        # 群聊必须真实@机器人（排除 QQ 官方占位@），私聊放行，防止误触发
        if is_group and not self._is_real_at_bot(event):
            return

        msg = event.message_str.strip() if event.message_str else ""

        # 群消息时提前加载群组配置到内存缓存
        if is_group:
            await self._ensure_group_config_loaded()

        # 纯@（无文本）时返回快速指南
        if hasattr(event, "is_at_or_wake_command") and event.is_at_or_wake_command:
            if not msg or msg.isspace():
                yield event.plain_result(self._get_quick_guide(event))
                event.stop_event()
                return

        if not msg:
            return

        if msg.isdigit():
            num = int(msg)
            if 0 < num <= 20:
                async for result in self.dashen_match_detail(event, arg1=str(num)):
                    yield result
                event.stop_event()
                return

        bind_match = re.match(r"^(?:/?(?:大神)?绑定\s+)?(?:/?(?:大神)?绑定)?\s*([^#＃\s]+[#＃]\d+)$", msg)
        if bind_match:
            clean_bnet_id = bind_match.group(1)  # 极其精准地提取真正的战网 ID
            async for result in self.dashen_bind(event, bnet_id=clean_bnet_id):
                yield result
            event.stop_event()
            return

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def full_adaptation_interceptor(self, event: AstrMessageEvent):
        """全量消息适配拦截器：群聊中以"@机器人昵称 指令"格式的纯文本消息也能触发对应指令。

        默认关闭，需群管理员通过 /全量适配开 开启（按群独立，持久化到 JSON）。
        仅处理纯文本@昵称（非真实@提及）；真实@仍走框架原生指令派发，避免重复响应。
        兼容 QQ 官方（含 [At:qq_official] 占位与 [At:<openid>] 真实@两种格式）与普通机器人。

        实现说明：AstrBot 的指令派发在 WakingCheckStage 阶段即依据 is_at_or_wake_command
        与 message_str 完成匹配，早于任何插件处理器执行；纯文本"@昵称 指令"无法被框架识别为
        唤醒，故此处维护插件自身的指令分发表（不依赖框架内部 API），手动解析并调用对应方法。
        """
        if not self._is_group_message(event):
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return

        adapt_cfg = self._get_full_adapt(group_id)
        if not adapt_cfg.get("enabled", False):
            return

        # 真实@机器人 → 交给框架原生派发，避免重复响应
        if self._is_real_at_bot(event):
            return

        nickname = self._get_bot_nickname(event, adapt_cfg)
        if not nickname:
            return

        msg = (event.message_str or "").strip()
        prefix = f"@{nickname}"
        # 匹配纯文本 "@昵称" 前缀（后接空格/全角空格，或即为昵称本身）
        if not (msg == prefix or msg.startswith(prefix + " ") or msg.startswith(prefix + "\u3000")):
            return

        cmd_text = msg[len(prefix):].strip()

        # 纯@昵称（无指令）→ 快速指南
        if not cmd_text:
            yield event.plain_result(self._get_quick_guide(event))
            event.stop_event()
            return

        # 数字快捷：@昵称 5 → 第 5 局单局详细
        if cmd_text.isdigit() and 0 < int(cmd_text) <= 20:
            async for r in self.dashen_match_detail(event, arg1=str(int(cmd_text))):
                yield r
            event.stop_event()
            return

        # 绑定快捷：@昵称 Player#12345
        bind_m = re.match(r"^(?:/?(?:大神)?绑定\s+)?(?:/?(?:大神)?绑定)?\s*([^#＃\s]+[#＃]\d+)$", cmd_text)
        if bind_m:
            async for r in self.dashen_bind(event, bnet_id=bind_m.group(1)):
                yield r
            event.stop_event()
            return

        # 指令派发：自维护分发表，按方法签名自动适配参数数量（标准库 inspect）
        tokens = cmd_text.split()
        cmd = tokens[0].lstrip("/")  # 兼容用户带 "/" 前缀
        rest = tokens[1:]
        method = self._ensure_full_adapt_map().get(cmd)
        if not method:
            return  # 未命中本插件指令，交回后续流程

        # 按方法签名（除 self/event）截取参数，不足补空串（交由方法内部校验给出友好提示）
        try:
            params = [p for p in inspect.signature(method).parameters.values()
                      if p.name not in ("self", "event")]
        except Exception:
            params = []
        n_max = len(params)
        args = rest[:n_max]
        args += [""] * (n_max - len(args))

        try:
            async for r in method(event, *args):
                yield r
            event.stop_event()
        except TypeError as e:
            # 参数不匹配（如必填参数缺失）的兜底提示
            yield self._plain_error_result(event, f"❌ 指令参数不匹配：{e}\n请检查指令用法，可发送 @机器人昵称 获取快速指南。")
            event.stop_event()
        except Exception as e:
            logger.error(f"全量适配派发异常（{cmd}）: {e}")
            yield self._plain_error_result(event, f"❌ 指令执行异常：{e}")
            event.stop_event()

    def _ensure_full_adapt_map(self) -> dict:
        """懒构建全量适配指令分发表：指令名/别名 -> 方法引用。

        仅收录业务查询指令；管理/部署类指令需真实@机器人触发（避免权限绕过）。
        表与 @filter.command 声明保持同步，新增业务指令时在此追加一行即可。
        """
        if self._full_adapt_map is not None:
            return self._full_adapt_map
        table = [
            # (指令名 + 别称, 方法引用)
            (["owhelp", "ow菜单", "ow帮助", "OW帮助", "help"], self.ow_help),
            (["所有指令", "别称"], self.show_aliases),
            (["快速指南", "快捷指令"], self.quick_guide_command),
            (["大神绑定", "绑定"], self.dashen_bind),
            (["今日总结", "今日", "今日数据"], self.dashen_today),
            (["昨日总结", "昨日", "昨日数据", "昨天数据", "昨天"], self.dashen_yesterday),
            (["周度总结", "本周总结", "本周数据", "本周"], self.dashen_week),
            (["大神数据", "详情卡片", "战绩查询", "数据"], self.dashen_profile),
            (["大神对局", "最近对局", "战绩", "对局"], self.dashen_match),
            (["单局详细", "单局"], self.dashen_match_detail),
            (["历史段位", "历届段位"], self.dashen_rank_history),
            (["同玩查询", "开黑胜率"], self.dashen_sameplay),
            (["快速强度", "快速强度指数"], self.quick_strength),
            (["竞技强度", "竞技强度指数"], self.competitive_strength),
            (["快速英雄云图", "快速云图"], self.quick_hero_treemap),
            (["竞技英雄云图", "竞技云图"], self.competitive_hero_treemap),
            (["威能"], self.ow_hero_perk),
            (["ow英雄"], self.ow_hero_pick),
            (["商店", "ow商店"], self.ow_shop),
            (["ow赛事", "赛事"], self.ow_esports),
            (["获取段位分布"], self.get_rank_distribution),
            (["ow活动", "活动"], self.ow_activities),
            (["banpick", "全英雄排行"], self.ban_pick_stats),
            (["mappick"], self.map_pick_stats),
            (["皮肤搜索"], self.skin_search),
            (["ow更新", "版本更新"], self.ow_patch_notes),
            (["省榜", "排行"], self.ow_rank_leaderboard),
            (["绝活榜", "英雄省榜"], self.ow_hero_leaderboard),
        ]
        m = {}
        for names, method in table:
            for n in names:
                m[n] = method
        self._full_adapt_map = m
        return m

    def _get_quick_guide(self, event: AstrMessageEvent) -> str:
        """根据平台返回快速指南"""
        text = """📌 Overstats 快速指南

🔗 ➤ <qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />示例：/绑定 Player#12345
📊 ➤ <qqbot-cmd-input text="/今日总结 " show="今日总结" reference="false" /> ➤ <qqbot-cmd-input text="/本周总结 " show="本周总结" reference="false" />
📈 ➤ <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="/大神对局 " show="对局" reference="false" />
💪 ➤ <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" /> ➤ <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" />
☁️ ➤ <qqbot-cmd-input text="/快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="/竞技英雄云图 " show="竞技云图" reference="false" />
📋 全部功能 <qqbot-cmd-input text="/owhelp " show="owhelp" reference="false" />

💡 必须@机器人，不能复制纯文本识别不到"""
        return self._format_markdown_by_platform(event, text)

    @filter.command("快速指南", alias={'快捷指令'})
    async def quick_guide_command(self, event: AstrMessageEvent):
        """独立快速指南指令"""
        quick_guide = self._get_quick_guide(event)
        yield event.plain_result(quick_guide)

    @filter.command("所有指令", alias={'别称'})
    async def show_aliases(self, event: AstrMessageEvent):
        """展示所有插件指令及其别称"""
        text = """📋 **【Overstats 指令及别称大全】**

🔹 **基础与绑定类：**
• <qqbot-cmd-input text="/owhelp " show="owhelp" reference="false" /> (别称：<qqbot-cmd-input text="/ow菜单 " show="ow菜单" reference="false" />, <qqbot-cmd-input text="/ow帮助 " show="ow帮助" reference="false" />, <qqbot-cmd-input text="/OW帮助 " show="OW帮助" reference="false" />, <qqbot-cmd-input text="/help " show="help" reference="false" />)
• <qqbot-cmd-input text="/所有指令 " show="所有指令" reference="false" /> (别称：<qqbot-cmd-input text="/别称 " show="别称" reference="false" />)
• <qqbot-cmd-input text="/大神绑定 " show="大神绑定" reference="false" /> (别称：<qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />)

🔹 **数据查询类：**
• <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> (别称：<qqbot-cmd-input text="/详情卡片 " show="详情卡片" reference="false" />, <qqbot-cmd-input text="/战绩查询 " show="战绩查询" reference="false" />, <qqbot-cmd-input text="/数据 " show="数据" reference="false" />)
• <qqbot-cmd-input text="/大神对局 " show="大神对局" reference="false" /> (别称：<qqbot-cmd-input text="/最近对局 " show="最近对局" reference="false" />, <qqbot-cmd-input text="/战绩 " show="战绩" reference="false" />, <qqbot-cmd-input text="/对局 " show="对局" reference="false" />)
• <qqbot-cmd-input text="/单局详细 " show="单局详细" reference="false" /> (别称：<qqbot-cmd-input text="/单局 " show="单局" reference="false" />)
• <qqbot-cmd-input text="/同玩查询 " show="同玩查询" reference="false" /> (别称：<qqbot-cmd-input text="/开黑胜率 " show="开黑胜率" reference="false" />)

🔹 **总结类：**
• <qqbot-cmd-input text="/今日总结 " show="今日总结" reference="false" /> (别称：<qqbot-cmd-input text="/今日 " show="今日" reference="false" />, <qqbot-cmd-input text="/今日数据 " show="今日数据" reference="false" />)
• <qqbot-cmd-input text="/昨日总结 " show="昨日总结" reference="false" /> (别称：<qqbot-cmd-input text="/昨日 " show="昨日" reference="false" />, <qqbot-cmd-input text="/昨日数据 " show="昨日数据" reference="false" />, <qqbot-cmd-input text="/昨天数据 " show="昨天数据" reference="false" />, <qqbot-cmd-input text="/昨天 " show="昨天" reference="false" />)
• <qqbot-cmd-input text="/周度总结 " show="周度总结" reference="false" /> (别称：<qqbot-cmd-input text="/本周总结 " show="本周总结" reference="false" />, <qqbot-cmd-input text="/本周数据 " show="本周数据" reference="false" />, <qqbot-cmd-input text="/本周 " show="本周" reference="false" />)

🔹 **图表与排行类：**
• <qqbot-cmd-input text="/历史段位 " show="历史段位" reference="false" /> (别称：<qqbot-cmd-input text="/历届段位 " show="历届段位" reference="false" />)
• <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" /> (别称：<qqbot-cmd-input text="/快速强度指数 " show="快速强度指数" reference="false" />)
• <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" /> (别称：<qqbot-cmd-input text="/竞技强度指数 " show="竞技强度指数" reference="false" />)
• <qqbot-cmd-input text="/快速英雄云图 " show="快速英雄云图" reference="false" /> (别称：<qqbot-cmd-input text="/快速云图 " show="快速云图" reference="false" />)
• <qqbot-cmd-input text="/竞技英雄云图 " show="竞技英雄云图" reference="false" /> (别称：<qqbot-cmd-input text="/竞技云图 " show="竞技云图" reference="false" />)
• <qqbot-cmd-input text="/省榜 " show="省榜" reference="false" /> (别称：<qqbot-cmd-input text="/排行 " show="排行" reference="false" />)
• <qqbot-cmd-input text="/绝活榜 " show="绝活榜" reference="false" /> (别称：<qqbot-cmd-input text="/英雄省榜 " show="英雄省榜" reference="false" />)
• <qqbot-cmd-input text="/banpick " show="banpick" reference="false" /> (别称：<qqbot-cmd-input text="/全英雄排行 " show="全英雄排行" reference="false" />)

🔹 **游戏资讯类：**
• <qqbot-cmd-input text="/威能 " show="威能" reference="false" />, <qqbot-cmd-input text="/ow英雄 " show="ow英雄" reference="false" />, <qqbot-cmd-input text="/获取段位分布 " show="获取段位分布" reference="false" />, <qqbot-cmd-input text="/mappick " show="mappick" reference="false" />, <qqbot-cmd-input text="/皮肤搜索 " show="皮肤搜索" reference="false" />
• <qqbot-cmd-input text="/商店 " show="商店" reference="false" /> (别称：<qqbot-cmd-input text="/ow商店 " show="ow商店" reference="false" />)
• <qqbot-cmd-input text="/ow赛事 " show="ow赛事" reference="false" /> (别称：<qqbot-cmd-input text="/赛事 " show="赛事" reference="false" />)
• <qqbot-cmd-input text="/ow活动 " show="ow活动" reference="false" /> (别称：<qqbot-cmd-input text="/活动 " show="活动" reference="false" />)
• <qqbot-cmd-input text="/ow更新 " show="ow更新" reference="false" /> (别称：<qqbot-cmd-input text="/版本更新 " show="版本更新" reference="false" />)"""
        
        yield event.plain_result(self._format_markdown_by_platform(event, text))

    @filter.command("多图测试")
    async def multi_image_test(self, event: AstrMessageEvent):
        imgs_list = []
        for img_name in ["test1.png", "test2.png", "test3.png"]:
            img_path = self.plugin_data_dir / img_name
            if img_path.exists():
                try:
                    with open(img_path, "rb") as f:
                        imgs_list.append(f.read())
                except Exception as e:
                    logger.error(f"读取测试图片 {img_name} 失败: {e}")
        
        if not imgs_list:
            yield self._plain_error_result(event, "❌ 未能读取到测试图片。")
            return
            
        async for r in self._send_multiple_images_result(event, imgs_list):
            yield r

    @filter.command("owhelp", alias={'ow菜单', 'ow帮助', 'OW帮助', 'help'})
    async def ow_help(self, event: AstrMessageEvent):
        help_text = """📌 Overstats 查询菜单
🔗 ➤ <qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />示例：/绑定 Player#12345
📋 ➤ <qqbot-cmd-input text="/今日总结 " show="今日" reference="false" /> ➤ <qqbot-cmd-input text="/昨日总结 " show="昨日" reference="false" /> ➤ <qqbot-cmd-input text="/周度总结 " show="本周" reference="false" />
📊 ➤ <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="/大神对局 " show="大神对局" reference="false" /> ➤ <qqbot-cmd-input text="/单局详细 " show="单局详细" reference="false" />「数字」可加 锐评关/全员关
📈 ➤ <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" />「可选对局数」 ➤ <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" />「可选对局数」 ➤ <qqbot-cmd-input text="/获取段位分布 " show="获取段位分布" reference="false" />「可选 快速/竞技 段位」
🗺️ ➤ <qqbot-cmd-input text="/快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="/竞技英雄云图 " show="竞技云图" reference="false" />
🏆 ➤ <qqbot-cmd-input text="/省榜 " show="省榜" reference="false" />「省份」「位置」 ➤ <qqbot-cmd-input text="/绝活榜 " show="绝活榜" reference="false" />「省份」「英雄」可加 开放
⚔️ ➤ <qqbot-cmd-input text="/威能 " show="威能" reference="false" />「英雄名」 ➤ <qqbot-cmd-input text="/ow英雄 " show="ow 英雄" reference="false" />「英雄名」可加 快速/竞技 段位 ➤ <qqbot-cmd-input text="/banpick " show="banpick" reference="false" />「可选 快速/竞技 段位」 ➤ <qqbot-cmd-input text="/mappick " show="mappick" reference="false" />
🌍 ➤ <qqbot-cmd-input text="/同玩查询 " show="同玩查询" reference="false" />「ID1」「ID2」 ➤ <qqbot-cmd-input text="/商店 " show="商店" reference="false" /> ➤ <qqbot-cmd-input text="/皮肤搜索 " show="皮肤搜索" reference="false" /> ➤ <qqbot-cmd-input text="/ow赛事 " show="ow 赛事" reference="false" />
📰 ➤ <qqbot-cmd-input text="/ow更新 " show="ow更新" reference="false" />「latest/small/big」
🎁 ➤ 段位关键词：青铜/白银/黄金/白金/钻石/大师/宗师/英杰
💡 发送 <qqbot-cmd-input text="/别称 " show="别称" reference="false" /> 可查看所有指令对应别称列表。"""
        
        yield event.plain_result(self._format_markdown_by_platform(event, help_text))

    @filter.command("大神绑定", alias={'绑定'})
    async def dashen_bind(self, event: AstrMessageEvent, bnet_id: str):
        user_id = event.get_sender_id()
        
        new_bind_id = bnet_id.strip()
        
        if not new_bind_id or ("#" not in new_bind_id and "＃" not in new_bind_id):
            yield self._plain_error_result(event, "❌ 绑定失败！请输入规范战网 ID，严格区分大小写\n格式：/绑定 战网ID，示例：/绑定 Player#12345")
            return
        
        old_bind_id = await self._get_user_bind_id(user_id)
        await self._set_user_bind_id(user_id, new_bind_id)
        
        if not old_bind_id:
            yield event.plain_result(f"✅ 绑定成功！关联战网账号【{new_bind_id}】")
        else:
            yield event.plain_result(f"✅ 更新绑定成功！已将您的战网账号从【{old_bind_id}】更新为【{new_bind_id}】")

    @filter.command("今日总结", alias={'今日', '今日数据'})
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/今日总结 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"⏳ 正在计算 {target_id} 的今日战绩总结...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-summary/today/image", {"bnet_id": target_id})

            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            elif error_data and error_data.get("error") == "summary_empty" and error_data.get("details", {}).get("scope") == "today":
                yield event.plain_result(f"ℹ️ {target_id} 在过去的 24 小时内没有对局记录，尝试生成昨日总结...")
                img_bytes, error_data = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
                if img_bytes:
                    success = True
                    yield self._send_image_result(event, img_bytes)
                else:
                    err_msg = error_data.get("message") if error_data else "获取昨日总结失败，可能昨日没有对局记录。"
                    if err_msg and "Could not resolve customerToken" in err_msg:
                        yield self._plain_error_result(event, self._id_resolve_err("获取昨日总结失败"))
                    elif error_data and error_data.get("error") == "summary_empty":
                        yield self._plain_error_result(event, f"❌ {target_id} 在过去 48 小时内没有对局记录")
                    else:
                        yield self._plain_error_result(event, f"❌ {err_msg}")
            else:
                err_msg = error_data.get("message", "未知错误") if error_data else "未知错误"
                if "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取今日总结失败"))
                else:
                    yield self._plain_error_result(event, f"❌ 获取今日总结失败：{err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("昨日总结", alias={'昨日', '昨日数据', '昨天数据', '昨天'})
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = "", _skip_status_prompt: bool = False):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/昨日总结 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return
        prompt_token = None
        _maintenance_stop = False
        if _skip_status_prompt:
            status_text = None
        else:
            status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"⏳ 正在统计 {target_id} 的昨日战绩数据...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-summary/yesterday/image", {"bnet_id": target_id})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取昨日总结失败，可能昨日未登录游戏。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取昨日总结失败"))
                elif error_data and error_data.get("error") == "summary_empty":
                    yield self._plain_error_result(event, f"❌ {target_id} 在昨日没有对局记录")
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("周度总结", alias={'本周总结', '本周数据', '本周'})
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/周度总结 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📊 正在生成 {target_id} 的本周战绩大数据总结...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-summary/week/image", {"bnet_id": target_id}, timeout=900)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取周度总结失败，请检查服务日志或是否请求超时。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取周度总结失败"))
                elif error_data and error_data.get("error") == "summary_empty" and error_data.get("details", {}).get("scope") == "week":
                    yield self._plain_error_result(event, f"❌ {target_id} 在过去 7 天内没有对局记录")
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("大神数据", alias={'详情卡片', '战绩查询', '数据'})
    async def dashen_profile(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        bnet_id, mode = self._parse_profile_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/大神数据 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🔍 正在生成 {target_id} 的玩家详情...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-profile/image", {"bnet_id": target_id, "mode": mode})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取玩家详情卡片失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取玩家详情失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("大神对局", alias={'最近对局', '战绩', '对局'})
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = ""):
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/大神对局 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📊 正在拉取 {target_id} 的最近对局...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-match/image", {"bnet_id": target_id})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取最近对局列表失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取最近对局失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("单局详细", alias={'单局'})
    async def dashen_match_detail(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        # 先识别中文关键词：锐评关 / 全员关 / 锐评（默认即锐评，可显式给）
        positional, kw = self._extract_keywords([arg1, arg2, arg3])
        index = 0
        bnet_id = None

        for arg in positional:
            if arg.isdigit():
                digit = int(arg)
                if digit > 20:
                    yield self._plain_error_result(event, "❌ 错误：单局详细的数字索引不能大于 20！")
                    return
                index = max(0, digit - 1) if digit > 0 else 0
            else:
                bnet_id = arg

        if not bnet_id:
            # 使用用户绑定的战网ID
            user_id = event.get_sender_id()
            bnet_id = await self._get_user_bind_id(user_id)

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/单局详细 1 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return

        # 提示文案：关闭锐评时告知用户本次更快
        base_prompt = f"⏳ 正在拉取 {target_id} 第 {index + 1} 局的单局多图详细战绩..."
        if kw.get("analyze") is False:
            base_prompt += "（本次已跳过 AI 锐评，出图更快）"
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, base_prompt)
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {
            "bnet_id": target_id,
            "index": str(index),
            "limit": "20",
            "include_fight": True,
            "include_previous_season": True,
            # 默认开启，命中「全员关」「锐评关」时关闭
            "show_all_heroes": kw.get("show_all_heroes", True),
            "analyze": kw.get("analyze", True)
        }
        
        url = f"{self.base_url}/dashen-match/detail/replies"

        success = False
        try:
            session = await self._get_http_session()
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    try:
                        error_data = await resp.json()
                        err_msg = error_data.get("message", "未知后端 service 错误")
                        yield self._plain_error_result(event, f"❌ 获取单局详细失败：{err_msg}")
                    except Exception:
                        yield self._plain_error_result(event, f"❌ 后端接口响应异常，状态码: {resp.status}")
                    return

                data = await resp.json()
                raw_img_list = data.get("replies", [])
                
                if not raw_img_list:
                    yield self._plain_error_result(event, "❌ 未能生成该单局的详细图片链接")
                    return

                # 收集所有图片数据，最后用多图消息链一次性发送
                collected_images: list[bytes] = []
                for u in raw_img_list:
                    img_str = ""
                    if isinstance(u, dict):
                        if u.get("type") == "image":
                            img_str = str(u.get("base64", "")).strip()
                        if not img_str:
                            for key in ["url", "image", "src", "path", "file"]:
                                if u.get(key):
                                    img_str = str(u.get(key)).strip()
                                    break
                    else:
                        img_str = str(u).strip()

                    if not img_str:
                        continue

                    img_data = None
                    try:
                        if img_str.startswith("base64://"):
                            img_data = base64.b64decode(img_str.replace("base64://", ""))
                        elif img_str.startswith("data:image") and "base64," in img_str:
                            img_data = base64.b64decode(img_str.split("base64,")[1])
                        elif len(img_str) > 100 and not img_str.startswith("http") and not img_str.startswith("/"):
                            padding = len(img_str) % 4
                            if padding: img_str += '=' * (4 - padding)
                            try: img_data = base64.b64decode(img_str)
                            except Exception: pass
                        
                        if not img_data:
                            full_img_url = img_str if img_str.startswith("http") else f"{self.base_url.rstrip('/').removesuffix('/api/v2')}{img_str if img_str.startswith('/') else '/' + img_str}"
                            async with session.get(full_img_url, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as img_resp:
                                if img_resp.status == 200:
                                    img_data = await img_resp.read()
                    except Exception as e:
                        logger.error(f"处理图片失败：{e}")
                        continue

                    if img_data:
                        collected_images.append(img_data)

                if collected_images:
                    success = True
                    async for r in self._send_multiple_images_result(event, collected_images):
                        yield r
        except Exception as e:
            logger.error(f"处理单局详细图片异常：{e}")
            yield self._plain_error_result(event, "❌ 处理图片请求时发生 system 错误")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("历史段位", alias={'历届段位'})
    async def dashen_rank_history(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        # /历史段位            /历史段位 Player#12345
        # /历史段位 15 22（起始赛季 终止赛季）       /历史段位 Player#12345 15 22
        positional, _kw = self._extract_keywords([arg1, arg2])
        bnet_id = None
        season_nums: list[int] = []
        for arg in positional:
            if arg.isdigit():
                season_nums.append(int(arg))
            else:
                bnet_id = arg

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/历史段位 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345\n可选附加 起始 终止 赛季，如 /历史段位 Player#12345 15 22")
            return

        season_hint = ""
        if len(season_nums) == 1:
            season_hint = f"（从 S{season_nums[0]} 起）"
        elif len(season_nums) >= 2:
            season_hint = f"（S{season_nums[0]} ~ S{season_nums[1]})"

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"📜 正在追溯 {target_id} 的历史段位记录{season_hint}...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id}
        # 起止赛季：按出现顺序赋值；仅给一个数字时作为起始
        if len(season_nums) >= 1:
            payload["start_season"] = season_nums[0]
        if len(season_nums) >= 2:
            payload["end_season"] = season_nums[1]

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-rank-history/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取历史段位失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取历史段位失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("同玩查询", alias={'开黑胜率'})
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str, p2: str):
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"👥 正在分析 {p1} 与 {p2} 的同玩胜率...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"player1_bnet_id": p1, "player2_bnet_id": p2}
        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-sameplay/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "无法获取同玩查询数据，请检查两个ID是否输入正确。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("同玩查询失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("快速强度", alias={'快速强度指数'})
    async def quick_strength(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        # /快速强度            /快速强度 Player#12345         /快速强度 8（对局数3-12）
        # /快速强度 Player#12345 8
        positional, _kw = self._extract_keywords([arg1, arg2])
        bnet_id = None
        limit = None
        for arg in positional:
            if arg.isdigit():
                limit = int(arg)
            else:
                bnet_id = arg

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/快速强度 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345\n可选附加对局数（3-12），如 /快速强度 Player#12345 8")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"⚡ 正在评估 {target_id} 的快速强度指数...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "include_previous_season": True}
        if limit is not None:
            payload["limit"] = max(3, min(12, limit))

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-quick-strength/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取快速强度指数失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取快速强度指数失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("竞技强度", alias={'竞技强度指数'})
    async def competitive_strength(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        # /竞技强度            /竞技强度 Player#12345         /竞技强度 8（对局数3-12）
        # /竞技强度 Player#12345 8
        positional, _kw = self._extract_keywords([arg1, arg2])
        bnet_id = None
        limit = None
        for arg in positional:
            if arg.isdigit():
                limit = int(arg)
            else:
                bnet_id = arg

        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/竞技强度 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345\n可选附加对局数（3-12），如 /竞技强度 Player#12345 8")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🏆 正在评估 {target_id} 的竞技天梯强度指数...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "include_previous_season": True}
        if limit is not None:
            payload["limit"] = max(3, min(12, limit))

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-competitive-strength/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取竞技强度指数失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取竞技强度指数失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("快速英雄云图", alias={'快速云图'})
    async def quick_hero_treemap(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/快速英雄云图 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📊 正在获取 {target_id} 的快速模式英雄云图...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "mode": "quick", "include_previous_season": True}
        if season: payload["season"] = str(season)

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-hero-treemap/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取快速英雄云图失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取快速英雄云图失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("竞技英雄云图", alias={'竞技云图'})
    async def competitive_hero_treemap(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        bnet_id, season = self._parse_treemap_args(arg1, arg2)
        target_id = await self._get_bnet_id(event, bnet_id)
        if not target_id:
            yield self._plain_error_result(event, "❌ 请输入战网ID，如：/竞技英雄云图 Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🏆 正在获取 {target_id} 的竞技模式英雄云图...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"bnet_id": target_id, "mode": "competitive", "include_previous_season": True}
        if season: payload["season"] = str(season)

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-hero-treemap/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取竞技英雄云图失败。"
                if err_msg and "Could not resolve customerToken" in err_msg:
                    yield self._plain_error_result(event, self._id_resolve_err("获取竞技英雄云图失败"))
                else:
                    yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("威能")
    async def ow_hero_perk(self, event: AstrMessageEvent, hero_name: str):
        if not hero_name:
            yield self._plain_error_result(event, "❌ 请输入英雄名称，如：/威能 闪光")
            return
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🔮 正在提取 {hero_name} 的核心威能数据...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-hero-perk/image", {"hero": hero_name})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else f"未能找到英雄【{hero_name}】的威能图。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("ow英雄")
    async def ow_hero_pick(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        # /ow英雄 安娜  /ow英雄 安娜 快速  /ow英雄 安娜 大师  /ow英雄 安娜 快速 大师
        positional, kw = self._extract_keywords([arg1, arg2, arg3])
        hero_name = positional[0] if positional else ""
        if not hero_name:
            yield self._plain_error_result(event, "❌ 请输入英雄名称，如：/ow英雄 闪光\n可选附加：快速/竞技 模式、青铜~英杰 段位，如 /ow英雄 安娜 快速 大师")
            return

        # 默认竞技全段位，可被 快速/竞技 与分段关键词覆盖
        mode_label = "快速" if kw.get("game_mode") == "quick" else "竞技"
        mmr_label = kw.get("mmr", "all")
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"🔥 正在读取 {hero_name} 的 {mode_label}（{mmr_label}）Pick 率走势图...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {
            "view": "history",
            "game_mode": kw.get("game_mode", "competitive"),
            "mmr": kw.get("mmr", "all"),
            "hero": hero_name
        }

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else f"暂时无法获取英雄 {hero_name} 的数据走势。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("商店", alias={'ow商店'})
    async def ow_shop(self, event: AstrMessageEvent):
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🛍️ 正在获取今日精选商店皮肤商品...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-shop/image")
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取精选商店图片失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("ow赛事", alias={'赛事'})
    async def ow_esports(self, event: AstrMessageEvent):
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🎮 正在从 Pandascore 获取实时赛事对阵...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-esports/image")
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "赛事信息获取失败。请检查后台是否正确配置了 `OW_ESPORTS_API_KEY`。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("获取段位分布")
    async def get_rank_distribution(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        # /获取段位分布        /获取段位分布 快速       /获取段位分布 快速 大师       /获取段位分布 大师
        _positional, kw = self._extract_keywords([arg1, arg2])
        game_mode = kw.get("game_mode", "competitive")
        mmr = kw.get("mmr", "all")
        mode_label = "快速" if game_mode == "quick" else "竞技"

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"📊 正在统计 {mode_label}（{mmr}）天梯全服大盘全英雄数据排行与环境分布...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"view": "ranking", "game_mode": game_mode, "mmr": mmr}

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "无法获取全服天梯分布排行。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("ow活动", alias={'活动'})
    async def ow_activities(self, event: AstrMessageEvent):
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🎉 正在拉取当前版本限时节日/赛季大活动公告卡片...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "big"})
            if not img_bytes:
                img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "暂无正在进行的版本活动公告。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("banpick", alias={'全英雄排行'})
    async def ban_pick_stats(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        # /banpick        /banpick 快速       /banpick 快速 黄金       /banpick 黄金
        _positional, kw = self._extract_keywords([arg1, arg2])
        game_mode = kw.get("game_mode", "competitive")
        mmr = kw.get("mmr", "all")
        mode_label = "快速" if game_mode == "quick" else "竞技"

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"🚫 正在获取 {mode_label}（{mmr}）本周天梯英雄大盘选禁用排行...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"view": "ranking", "game_mode": game_mode, "mmr": mmr}

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-hero-pick-rate/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "无法获取全英雄排行。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("mappick")
    async def map_pick_stats(self, event: AstrMessageEvent):
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, "🗺️ 正在从最新版本补丁中检索当前赛季地图池与轮换出场...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": "latest"})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "无法拉取最新地图池分布。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("皮肤搜索")
    async def skin_search(self, event: AstrMessageEvent, keyword: str = ""):
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🔍 正在检索包含关键词【{keyword or '最新'}】的精选上架皮肤商品卡片...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/ow-shop/image")
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "无法获取精选皮肤卡片。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("ow更新", alias={'版本更新'})
    async def ow_patch_notes(self, event: AstrMessageEvent, kind: str = "latest"):
        valid_kinds = ["latest", "small", "big"]
        if kind not in valid_kinds:
            yield self._plain_error_result(event, "❌ 参数错误。支持的日志类型：latest, small, big\n例如：/ow更新 small")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"📰 正在拉取外服 {kind} 更新日志卡片...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/patch-notes/image", {"patch_kind": kind})
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取更新日志失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

    @filter.command("省榜", alias={'排行'})
    async def ow_rank_leaderboard(self, event: AstrMessageEvent, province: str, role: str):
        if not province or not role:
            yield self._plain_error_result(event, "❌ 请输入省份名称 and 职责位置，例如：/省榜 北京 tank\n(支持的位置: tank / dps / healer / open)")
            return

        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(event, f"🏆 正在获取 {province} 地区 【{role}】 位置的大神天梯省榜...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"province": province, "role": role}

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-rank-leaderboard/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取天梯省榜失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)

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
    @filter.command("ow连接测试")
    async def connection_test_cmd(self, event: AstrMessageEvent):
        """测试 Overstats 后端连接（仅 AstrBot 管理员可用）"""
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

    # ==================== 一键部署指令（仅 auto 模式生效） ==================== #

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
        """查看后端持久化日志文件（仅 AstrBot 管理员可用，auto 模式生效）。

        从专用日志文件中提取最新的 10 条记录。与 /ow部署日志 不同，
        此指令读取持久化日志文件，日志跨后端重启保留。
        """
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端日志请到您部署后端的服务器查看。")
            return

        logs = self.deploy_manager.get_backend_logs(10)
        if not logs:
            yield event.plain_result(
                "📋 暂无后端持久化日志。\n"
                f"日志文件路径: {self.deploy_manager.log_file_path}\n"
                "后端启动并产生输出后日志将写入该文件。"
            )
            return

        lines = [
            f"📋 后端持久化日志（最新 {len(logs)} 条）：",
            f"📂 文件: {self.deploy_manager.log_file_path}",
            "",
        ]
        lines.extend(logs)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端")
    async def ow_uninstall_backend_cmd(self, event: AstrMessageEvent):
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
        lines.append("• 回复 /ow卸载后端 确认 — 删除全部（代码+venv+数据库）")
        lines.append("• 回复 /ow卸载后端 仅代码 — 仅删除后端代码（保留venv）")
        lines.append("• 回复 /ow卸载后端 仅venv — 仅删除虚拟环境（保留代码）")
        lines.append("• 回复 /ow卸载后端 强制 — 强制删除全部（进程无法停止时使用）")
        lines.append("")
        lines.append("❗ 卸载后如需重新使用，请重新执行 /ow部署")

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ow卸载后端执行")
    async def ow_uninstall_backend_exec_cmd(self, event: AstrMessageEvent, mode: str = "all"):
        """执行后端卸载（仅 AstrBot 管理员可用）。

        Args:
            mode: all=全部, code=仅代码, venv=仅venv, force=强制全部
        """
        if not self.deploy_manager.is_auto_mode:
            yield event.plain_result("⚠️ 当前为独立链接模式，后端由您自行部署管理。")
            return

        mode = (mode or "all").strip().lower()

        # 解析卸载选项
        if mode in ("确认", "all", "全部", "确认卸载"):
            delete_code, delete_venv, force = True, True, False
        elif mode in ("仅代码", "code", "代码"):
            delete_code, delete_venv, force = True, False, False
        elif mode in ("仅venv", "venv"):
            delete_code, delete_venv, force = False, True, False
        elif mode in ("强制", "force", "强制卸载"):
            delete_code, delete_venv, force = True, True, True
        else:
            yield event.plain_result("❌ 未识别的卸载模式。请使用：确认 / 仅代码 / 仅venv / 强制")
            return

        # 检查是否有东西可卸载
        preview = self.deploy_manager.get_uninstall_preview()
        if not preview["backend_exists"] and not preview["venv_exists"]:
            yield event.plain_result("ℹ️ 后端代码和虚拟环境均不存在，无需卸载。")
            return

        yield event.plain_result("⏳ 正在执行后端卸载（先停止进程 → 再删除文件）...")

        result = await self.deploy_manager.uninstall_backend(
            delete_code=delete_code,
            delete_venv=delete_venv,
            force=force,
        )

        status_icon = "✅" if result.success else "⚠️"
        lines = [
            f"{status_icon} 卸载{'完成' if result.success else '部分完成'}（释放 {result.freed_space_mb:.1f} MB）",
            "",
            result.message,
            "",
        ]
        lines.extend(result.details)
        yield event.plain_result("\n".join(lines))

    @filter.command("群设置")
    async def group_config_cmd(self, event: AstrMessageEvent, action: str = "", value: str = ""):
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
        """开启本群的全量消息适配（群管理员专属）。

        用法：/全量适配开 [机器人昵称]
        昵称可选，用于匹配"@昵称 指令"格式的纯文本消息。
        不填则优先自动探测（真实@机器人时获取），其次使用配置面板的「全量适配昵称兜底」。
        """
        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 此命令仅支持在群聊中使用")
            return
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 仅群管理员或群主可操作全量适配开关")
            return
        group_id = self._get_group_id(event)
        nickname = (nickname or "").strip()
        self._set_full_adapt(group_id, True, nickname if nickname else None)
        eff_nick = nickname or self._get_bot_nickname(event, self._get_full_adapt(group_id)) or "（未设置，将自动探测或使用全局兜底）"
        yield event.plain_result(
            f"✅ 本群（{group_id}）已开启全量消息适配。\n"
            f"📝 触发格式：@机器人昵称 指令（纯文本即可，无需真实@）。\n"
            f"🤖 当前机器人昵称：{eff_nick}\n"
            f"💡 可用 /全量适配开 昵称 重新指定昵称；/全量适配关 关闭。"
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

⚙️ **群组配置：**
• <qqbot-cmd-input text="/群设置 " show="/群设置" reference="false" /> 查看当前群组功能配置
• <qqbot-cmd-input text="/群设置 提示 开 " show="/群设置 提示 开" reference="false" /> / <qqbot-cmd-input text="/群设置 提示 关 " show="/群设置 提示 关" reference="false" /> 切换首次提示后不再提示
• <qqbot-cmd-input text="/群设置 追加提示 开 " show="/群设置 追加提示 开" reference="false" /> / <qqbot-cmd-input text="/群设置 追加提示 关 " show="/群设置 追加提示 关" reference="false" /> 切换追加交互提示
• <qqbot-cmd-input text="/全量适配开 " show="/全量适配开" reference="false" /> [昵称] / <qqbot-cmd-input text="/全量适配关 " show="/全量适配关" reference="false" /> 开关「@昵称 指令」纯文本触发（按群独立）

🔌 **系统诊断（所有模式通用）：**
• <qqbot-cmd-input text="/ow连接测试 " show="/ow连接测试" reference="false" /> 测试 Overstats 后端连接状态
• <qqbot-cmd-input text="/ow部署状态 " show="/ow部署状态" reference="false" /> 查看后端接入模式与运行状态

🚀 **一键部署（仅 auto 托管模式）：**
• <qqbot-cmd-input text="/ow部署 " show="/ow部署" reference="false" /> 一键部署后端（首次使用必执行）
• <qqbot-cmd-input text="/ow更新后端 " show="/ow更新后端" reference="false" /> 拉取最新代码并重启（跟进原项目更新）
• <qqbot-cmd-input text="/ow停止后端 " show="/ow停止后端" reference="false" /> 停止后端进程
• <qqbot-cmd-input text="/ow重启后端 " show="/ow重启后端" reference="false" /> 重启后端（修改配置后需执行）
• <qqbot-cmd-input text="/ow部署日志 " show="/ow部署日志" reference="false" /> 查看后端运行日志（内存，最近50行）
• <qqbot-cmd-input text="/ow后端日志 " show="/ow后端日志" reference="false" /> 查看后端持久化日志（文件，最新10条）

🗑️ **后端卸载（仅 auto 托管模式）：**
• <qqbot-cmd-input text="/ow卸载后端 " show="/ow卸载后端" reference="false" /> 预览卸载影响（空间/数据库/进程）
• <qqbot-cmd-input text="/ow卸载后端执行 确认 " show="/ow卸载后端执行 确认" reference="false" /> 删除全部后端资源（代码+venv+数据库）
• <qqbot-cmd-input text="/ow卸载后端执行 仅代码 " show="/ow卸载后端执行 仅代码" reference="false" /> 仅删除后端代码（保留venv）
• <qqbot-cmd-input text="/ow卸载后端执行 强制 " show="/ow卸载后端执行 强制" reference="false" /> 强制删除（进程无法停止时使用）

💡 维护模式开启后，所有指令将直接返回维护内容，不再执行业务逻辑。
💡 一键部署指令需在配置面板切换为 auto 模式后使用，配置修改后需重载插件生效。
💡 后端卸载与插件卸载相互独立，卸载后端不影响插件其他功能（manual 模式仍可用）。"""
        yield event.plain_result(self._format_markdown_by_platform(event, text))

    @filter.command("绝活榜", alias={'英雄省榜'})
    async def ow_hero_leaderboard(self, event: AstrMessageEvent, province: str, hero: str, arg3: str = ""):
        # /绝活榜 北京 猎空        /绝活榜 北京 猎空 开放（开放=开放队列模式）
        _positional, kw = self._extract_keywords([province, hero, arg3])
        # 关键词识别后还原省/英雄（关键词被剔除，剩下的前两个就是省份与英雄）
        province = _positional[0] if len(_positional) >= 1 else province
        hero = _positional[1] if len(_positional) >= 2 else hero
        if not province or not hero:
            yield self._plain_error_result(event, "❌ 请输入省份和英雄名称，例如：/绝活榜 北京 猎空\n可选附加 开放（开放队列模式），如 /绝活榜 北京 猎空 开放")
            return

        mode = kw.get("lb_mode", "preset")
        status_text, prompt_token, _maintenance_stop = await self._prepare_business_status_prompt(
            event, f"🎖️ 正在获取 {province} 地区 【{hero}】（{'开放队列' if mode == 'open' else '预设'}）的大神英雄专精绝活榜...")
        if status_text:
            yield event.plain_result(status_text)
        if _maintenance_stop:
            return

        payload = {"province": province, "hero": hero, "mode": mode}

        success = False
        try:
            img_bytes, error_data = await self._fetch_image("/dashen-hero-leaderboard/image", payload)
            if img_bytes:
                success = True
                yield self._send_image_result(event, img_bytes)
            else:
                err_msg = error_data.get("message") if error_data else "获取英雄绝活榜失败。"
                yield self._plain_error_result(event, f"❌ {err_msg}")
        finally:
            await self._finalize_business_status_prompt(prompt_token, success)
