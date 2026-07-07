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

class PluginCore:

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.file_lock = asyncio.Lock()
        self._cmd_concurrent_lock = asyncio.Lock()  # 保护并发计数器的原子操作
        self._cmd_concurrent_count = 0
        self._load_rate_limit_config()
        self._semaphore_reset_task = asyncio.create_task(self._daily_semaphore_reset_loop())
        self.daily_group_prompt_suffix = "[今日已提示，群内后续指令不再提示将直接返回结果]"
        self.daily_group_prompt_notice = '群内使用请手动<qqbot-cmd-input text=" " show="@机器人" reference="false"/>或点击<qqbot-cmd-input text="快捷指令" show="快捷指令" reference="false"/>按钮，纯文本无法识别。'
        self.error_append_notice = '如需重新发起指令，请手动<qqbot-cmd-input text=" " show="@机器人" reference="false"/>或点击<qqbot-cmd-input text="快捷指令" show="快捷指令" reference="false"/>按钮，纯文本无法识别。'
        self.daily_prompt_pending_users: set[str] = set()
        self.id_resolve_error_hint = "未查询到id或者id错误，id严格区分大小写"
        
        # 读取图片本地保存开关（默认开启，保持向后兼容）
        self._save_image_locally = bool(self.config.get("save_image_locally", True))

        try:
            plugin_name = getattr(self, "name", "overstats_full")
            self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            if self._save_image_locally:
                self.temp_image_dir = self.plugin_data_dir / "temp"
                self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            else:
                self.temp_image_dir = None  # 不落盘模式下无需临时目录
        except Exception as e:
            logger.warning(f"初始化插件专属数据目录失败: {e}")
            self.plugin_data_dir = Path(tempfile.gettempdir())
            if self._save_image_locally:
                self.temp_image_dir = self.plugin_data_dir / "temp"
                self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            else:
                self.temp_image_dir = None

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

        # API 全局限流：所有请求最多同时 3 个
        self.overstats_semaphore = asyncio.Semaphore(3)
        # Overstats API 限流：Overstats 调用最多同时 2 个（包含在全局限流内）
        self._overstats_inner_semaphore = asyncio.Semaphore(2)

        # 定时清理临时图片（仅本地保存模式下启用，每天凌晨执行一次）
        if self._save_image_locally:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())
        else:
            self._cleanup_task = None

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

        # 管理/部署类指令集合（用于未匹配指令兜底时排除，避免管理指令误触发快速指南）
        self._admin_cmd_set: set[str] = {
            "多图测试", "单图测试",
            "ow开庭", "开庭", "ow是区吗", "是区吗",
            "ow是区吗结果", "是区吗结果", "owAI检测", "AI检测",
            "维护", "ow违禁封禁", "ow违禁解封",
            "ow连接测试",
            "ow部署", "ow部署状态", "ow更新后端", "ow停止后端",
            "ow重启后端", "ow部署日志", "ow后端日志",
            "ow卸载后端", "ow卸载后端执行确认", "ow卸载后端执行仅代码",
            "ow卸载后端执行仅venv", "ow卸载后端执行强制",
            "群设置", "全量适配开", "全量适配开完全匹配", "全量适配关", "管理",
        }

        # OW 开庭功能模块（测试阶段，独立封装）
        self.court_manager = CourtManager(self)
        self.shiqu_manager = ShiquManager(self)

        # ── 监控采集器（SQLite 持久化，trace 置为 False）──
        # 先注册 API 端点（即使采集器初始化失败也要注册，返回空数据而非 404）
        self._register_monitor_apis()

        try:
            _monitor_db = self.plugin_data_dir / "monitor_stats.sqlite3"
            self.monitor = MonitorCollector(_monitor_db)
        except Exception as e:
            logger.warning(f"[Overstats] MonitorCollector 初始化失败: {e}")
            self.monitor = None

        try:
            self.monitor_sse = MonitorSSEQueue(maxsize=50)
            if self.monitor:
                _log_handler = MonitorLogHandler(self.monitor, self.monitor_sse)
                _log_handler.setLevel(logging.ERROR)
                logging.getLogger("astrbot").addHandler(_log_handler)
                self._monitor_log_handler = _log_handler
            else:
                self._monitor_log_handler = None
        except Exception as e:
            logger.warning(f"[Overstats] MonitorLogHandler 初始化失败: {e}")
            self._monitor_log_handler = None

        try:
            _sqlite_cfg = str(self.config.get("sqlite_db_path", "") or "")
            self._backend_metrics_reader = BackendMetricsReader()
            self._req_metrics_db_path = BackendMetricsReader.resolve_db_path(
                self.plugin_data_dir, self.deploy_manager.mode, _sqlite_cfg,
            )
        except Exception as e:
            logger.warning(f"[Overstats] BackendMetricsReader 初始化失败: {e}")
            self._backend_metrics_reader = None
            self._req_metrics_db_path = None

        if self.monitor:
            logger.info(f"[Overstats] 监控模块初始化完成")
        else:
            logger.warning("[Overstats] 监控采集器未就绪，Web 面板将显示空数据")

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

    async def _daily_semaphore_reset_loop(self):
        """每日凌晨 4 点重置指令并发计数器，防止槽位泄漏累积。"""
        try:
            while True:
                now = datetime.now()
                next4 = now.replace(hour=4, minute=0, second=0, microsecond=0)
                if now >= next4:
                    next4 += timedelta(days=1)
                wait_sec = max(1, (next4 - now).total_seconds())
                await asyncio.sleep(wait_sec)
                async with self._cmd_concurrent_lock:
                    self._cmd_concurrent_count = 0
                logger.info(f"[Overstats] 每日 4 点已重置指令限流计数器（max={self._rate_limit_max}）")
        except asyncio.CancelledError:
            logger.info("指令限流每日重置任务已停止")
        except Exception as e:
            logger.error(f"指令限流每日重置任务异常: {e}")

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
        cfg = self._load_full_adapt_config().get(
            str(group_id), {"enabled": False, "nickname": "", "mode": "nickname"}
        )
        cfg.setdefault("mode", "nickname")
        return cfg

    def _set_full_adapt(self, group_id: str, enabled: bool, nickname: str | None = None, mode: str | None = None):
        """设置指定群的全量适配开关（可选更新昵称/模式）并持久化到 JSON"""
        cfg = self._load_full_adapt_config()
        gid = str(group_id)
        cur = cfg.get(gid, {"enabled": False, "nickname": "", "mode": "nickname"})
        cur.setdefault("mode", "nickname")
        cur["enabled"] = enabled
        if nickname is not None and nickname.strip():
            cur["nickname"] = nickname.strip()
        cur.setdefault("nickname", "")
        if mode is not None:
            cur["mode"] = mode
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

        若维护模式启用且发送者非 AstrBot 管理员且不在白名单内，返回维护内容；
        否则返回 None，允许正常执行业务逻辑。
        调用方应在 yield 维护文本后 return，不再继续执行业务。
        """
        await self._ensure_maintenance_loaded()
        if self._maintenance_state and self._maintenance_state.get("enabled"):
            if not self._is_astrbot_admin(event) and not self._is_whitelisted(event):
                return self._maintenance_state.get("content", "系统维护中")
        return None

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        """检查当前消息的群聊或发送者是否在白名单中。"""
        group_whitelist = self.config.get("group_whitelist", []) or []
        user_whitelist = self.config.get("user_whitelist", []) or []
        if not group_whitelist and not user_whitelist:
            return False
        if group_whitelist:
            group_id = self._get_group_id(event)
            if group_id and group_id in group_whitelist:
                return True
        if user_whitelist:
            try:
                sender_id = str(event.get_sender_id())
                if sender_id in user_whitelist:
                    return True
            except Exception:
                pass
        return False

    def _load_rate_limit_config(self):
        """读取指令并发限流 + LLM 频率限制配置。"""
        # ── 指令并发限流 ──
        raw = str(self.config.get("cmd_rate_limit", "{}") or "{}").strip()
        try:
            cfg = json.loads(raw) if raw else {}
        except Exception:
            cfg = {}
        self._rate_limit_enabled = bool(cfg.get("enabled", False))
        self._rate_limit_max = max(1, int(cfg.get("max_concurrent", 3) or 3))

        # ── LLM 频率限制 ──
        raw_llm = str(self.config.get("llm_rate_limit", "{}") or "{}").strip()
        try:
            llm_cfg = json.loads(raw_llm) if raw_llm else {}
        except Exception:
            llm_cfg = {}
        self._llm_rate_limit_enabled = bool(llm_cfg.get("enabled", False))
        self._llm_rate_limit_per_minute = max(1, int(llm_cfg.get("per_minute", 10) or 10))
        self._llm_rate_limit_timestamps: deque = deque()

    def _try_llm_rate_limit(self) -> bool:
        """LLM 调用频率限制检查并记录。返回 True=放行, False=超限。"""
        if not self._llm_rate_limit_enabled:
            return True
        now = time.time()
        cutoff = now - 60
        while self._llm_rate_limit_timestamps and self._llm_rate_limit_timestamps[0] < cutoff:
            self._llm_rate_limit_timestamps.popleft()
        if len(self._llm_rate_limit_timestamps) >= self._llm_rate_limit_per_minute:
            if self.monitor:
                asyncio.ensure_future(self.monitor.record_rate_limit("llm"))
            return False
        self._llm_rate_limit_timestamps.append(now)
        return True

    def _is_privileged(self, event: AstrMessageEvent) -> bool:
        """白名单群/用户 或 AstrBot 管理员不受限流控制。限流关闭时所有人视为特权。"""
        return not self._rate_limit_enabled or self._is_whitelisted(event) or self._is_astrbot_admin(event)

    _RATE_LIMIT_REJECT_MSG = "⏳ 同时执行的指令过多，请等待当前指令返回结果后再重试"

    async def _try_acquire_cmd_slot(self) -> bool:
        """非阻塞尝试获取指令并发槽位（异步锁保护原子性）。返回 True=获取成功。"""
        async with self._cmd_concurrent_lock:
            if self._cmd_concurrent_count >= self._rate_limit_max:
                return False
            self._cmd_concurrent_count += 1
            return True

    def _release_cmd_slot(self) -> None:
        """释放指令并发槽位（防止每日重置后 count 变为负值）。"""
        if self._cmd_concurrent_count > 0:
            self._cmd_concurrent_count -= 1

    @asynccontextmanager
    async def _rate_limit_slot(self, event: AstrMessageEvent):
        """非特权用户指令并发限流。槽位不足时 yield None 供调用方识别后立即拒绝；否则 yield True。"""
        if self._is_privileged(event):
            yield True
            return
        if not await self._try_acquire_cmd_slot():
            yield None  # 信号：并发已满，调用方应拒绝
            return
        try:
            yield True  # 信号：获取成功
        finally:
            self._release_cmd_slot()

    _VIOLATION_BAN_SECONDS = 43200  # 12 小时

    _VIOLATION_BAN_FILE = "violation_bans.json"

    def _violation_ban_path(self) -> Path:
        return self.plugin_data_dir / self._VIOLATION_BAN_FILE

    def _load_violation_bans(self) -> dict:
        """加载违规封禁记录 JSON，结构 { "平台:ID|指令名": 封禁时间戳 }。"""
        path = self._violation_ban_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_violation_bans(self, bans: dict) -> None:
        """原子写入 JSON。"""
        path = self._violation_ban_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(bans, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _user_key(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识（平台:ID）。"""
        try:
            return f"{event.get_platform_name()}:{event.get_sender_id()}"
        except Exception:
            return str(event.get_sender_id())

    @staticmethod
    def _violation_ban_key(user_key: str, command: str) -> str:
        """生成封禁 JSON key：用户|指令名。"""
        return f"{user_key}|{command}"

    async def _check_violation_ban(self, event: AstrMessageEvent, command: str) -> tuple[bool, int]:
        """检查 用户+指令 是否被封。返回 (is_banned, remaining_seconds)。过期自动清除。"""
        try:
            user_key = self._user_key(event)
            ban_key = self._violation_ban_key(user_key, command)
            bans = self._load_violation_bans()
            ban_ts = bans.get(ban_key, 0)
            if not ban_ts:
                return False, 0
            elapsed = int(time.time()) - int(ban_ts)
            if elapsed >= self._VIOLATION_BAN_SECONDS:
                bans.pop(ban_key, None)
                self._save_violation_bans(bans)
                return False, 0
            return True, self._VIOLATION_BAN_SECONDS - elapsed
        except Exception:
            return False, 0

    async def _set_violation_ban(self, event: AstrMessageEvent, command: str) -> None:
        """封禁 用户+指令 12小时。"""
        try:
            user_key = self._user_key(event)
            ban_key = self._violation_ban_key(user_key, command)
            bans = self._load_violation_bans()
            bans[ban_key] = int(time.time())
            self._save_violation_bans(bans)
            logger.warning(f"[ViolationBan] 封禁 {ban_key}（12小时）")
        except Exception as e:
            logger.error(f"[ViolationBan] 设置封禁失败: {e}")

    async def _clear_violation_ban(self, event: AstrMessageEvent, command: str) -> None:
        """解除 用户+指令 封禁。"""
        try:
            user_key = self._user_key(event)
            ban_key = self._violation_ban_key(user_key, command)
            bans = self._load_violation_bans()
            bans.pop(ban_key, None)
            self._save_violation_bans(bans)
            logger.info(f"[ViolationBan] 解封 {ban_key}")
        except Exception as e:
            logger.error(f"[ViolationBan] 解封失败: {e}")

    @staticmethod
    def _violation_ban_remain_str(seconds: int) -> str:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{int(h)}小时{int(m)}分钟"
        return f"{int(m)}分钟{int(s)}秒"

    _VIOLATION_BAN_MSG = "⛔ 【极少数情况】您之前【{command}】查询返回的图片包含违规内容（来自qq官方接口返回提示，可能：违规id，等），该指令已被自动禁用{remain}。请勿再试，如有疑问请联系管理员：2338127903。"

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

    def _bnet_err(self, cmd: str) -> str:
        """ponytail: 10 处重复的战网ID必填错误提示，统一收敛到此"""
        return f"❌ 请输入战网ID，如：/{cmd} Player#12345\n或先使用 /绑定 战网ID，示例：/绑定 Player#12345"

    async def _prepare_business_status_prompt(self, event: AstrMessageEvent, base_text: str) -> tuple[str | None, str | None, bool]:
        """准备业务状态提示。返回 (提示文本, 跟踪token, 是否应提前终止)。

        当 should_stop=True 时（维护模式），调用方应 yield 文本后 return，不再执行业务逻辑。
        AstrBot 管理员不受维护模式限制，可正常使用数据查询指令。
        非特权用户在此获取并发限流槽位（最多3条），由 _finalize_business_status_prompt 统一释放。
        """
        # 维护模式检查：激活时返回维护内容并标记提前终止（群聊/私聊均生效）
        # AstrBot 管理员 + 白名单群/用户绕过维护限制，可正常使用数据查询指令
        await self._ensure_maintenance_loaded()
        if self._maintenance_state and self._maintenance_state.get("enabled"):
            if self._is_astrbot_admin(event) or self._is_whitelisted(event):
                # 管理员/白名单不受维护模式限制，正常执行业务逻辑
                pass
            else:
                return self._maintenance_state.get("content", "系统维护中"), None, True

        # ── 指令并发限流：非特权用户非阻塞获取槽位，满则立即拒绝 ──
        need_release = False
        if not self._is_privileged(event):
            if not await self._try_acquire_cmd_slot():
                return self._RATE_LIMIT_REJECT_MSG, None, True
            need_release = True

        if not self._is_group_message(event):
            return base_text, (None, need_release), False

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
                    return None, (None, need_release), False
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

        # prompt_token 统一包含 (daily_user_id, need_release) 以便 finally 释放
        prompt_token = (user_id if (daily_prompt_enabled and user_id) else None, need_release)
        return prompt_text, prompt_token, False

    async def _finalize_business_status_prompt(self, reservation_user_id, success: bool, cmd_name: str = "", error_code: str = ""):
        """释放每日提示预留和限流槽位，并记录指令统计。

        reservation_user_id 可以是 str|None 或 (str|None, bool)。
        cmd_name 可选，传入后将自动记录到监控采集器。
        """
        daily_user_id: str | None = None
        need_release = False
        if isinstance(reservation_user_id, tuple):
            daily_user_id, need_release = reservation_user_id
        else:
            daily_user_id = reservation_user_id

        # 释放限流槽位
        if need_release:
            self._release_cmd_slot()

        # 记录指令统计
        if cmd_name and self.monitor:
            asyncio.ensure_future(self.monitor.record_command(cmd_name, success, error_code=error_code))

        if not daily_user_id:
            return

        async with self.file_lock:
            self._ensure_daily_prompt_state_current_unlocked()
            self.daily_prompt_pending_users.discard(daily_user_id)
            if success:
                prompted_users = self.daily_prompt_state.setdefault("prompted_users", {})
                prompted_users[daily_user_id] = True
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
        for task_attr in ("daily_prompt_reset_task", "_cleanup_task", "_semaphore_reset_task"):
            task = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # 卸载监控日志处理器
        _handler = getattr(self, "_monitor_log_handler", None)
        if _handler:
            try:
                logging.getLogger("astrbot").removeHandler(_handler)
            except Exception:
                pass
        # 关闭 HTTP 会话
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    def _is_qq_official(self, event: AstrMessageEvent) -> bool:
        """判断当前消息是否来自 QQ 官方机器人平台"""
        # 优先用 get_platform_name（可靠返回 "qq_official" / "qq_official_webhook"）
        try:
            pn = event.get_platform_name()
            if pn and "qq_official" in str(pn).lower():
                return True
        except Exception:
            pass
        # 兜底：unified_msg_origin / message_obj（兼容不带下划线 qqofficial 等写法）
        umo = event.unified_msg_origin
        if "qq_official" in str(umo).lower() or "qqofficial" in str(umo).lower():
            return True
        if hasattr(event, "message_obj") and event.message_obj:
            s = str(event.message_obj).lower()
            if "qq_official" in s or "qqofficial" in s:
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

    async def _fetch_image(self, endpoint: str, payload: dict = None, timeout: int = 600) -> tuple[bytes | None, dict | None, str]:
        url = f"{self.base_url}{endpoint}"
        payload = payload or {}
        async with self.overstats_semaphore:
            async with self._overstats_inner_semaphore:
                for attempt in (1, 2):
                    start_ts = time.time()
                    try:
                        session = await self._get_http_session()
                        # 若请求超时与默认不同，使用单次覆盖
                        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout != 600 else None
                        async with session.post(url, json=payload, timeout=req_timeout) as resp:
                            if resp.status == 200:
                                if self.monitor:
                                    elapsed = int((time.time() - start_ts) * 1000)
                                    asyncio.ensure_future(self.monitor.record_api(endpoint, True, elapsed))
                                return await resp.read(), None, ""
                            else:
                                try:
                                    error_data = await resp.json()
                                    # bnet_not_found / summary_empty 是正常业务结果，不算 API 失败
                                    _soft_errors = {"bnet_not_found", "summary_empty"}
                                    _is_soft = isinstance(error_data, dict) and error_data.get("error") in _soft_errors
                                    # 后端 ConnectTimeout → 自动重试 1 次（用字符串包含判断，兼容各种嵌套结构）
                                    _is_timeout = (resp.status == 500
                                                   and isinstance(error_data, dict)
                                                   and "ConnectTimeout" in str(error_data))
                                    if _is_timeout and attempt == 1:
                                        if self.monitor:
                                            elapsed = int((time.time() - start_ts) * 1000)
                                            asyncio.ensure_future(self.monitor.record_api(endpoint, True, elapsed))
                                        logger.warning(f"Overstats API ConnectTimeout，1秒后重试: {endpoint}")
                                        await asyncio.sleep(1)
                                        continue
                                    if self.monitor:
                                        elapsed = int((time.time() - start_ts) * 1000)
                                        asyncio.ensure_future(self.monitor.record_api(endpoint, _is_soft, elapsed))
                                    logger.error(f"Overstats API 错误: {resp.status} - {error_data}")
                                    # 后端超时/网络异常时追加重试提示
                                    if isinstance(error_data, dict):
                                        if error_data.get("error") == "internal_error":
                                            orig_msg = error_data.get("message", "")
                                            error_data["message"] = f"{orig_msg}，网络波动，请重试"
                                        # 请求频率限制时追加提示
                                        if resp.status == 429 or "too many requests" in str(error_data.get("message", "")).lower():
                                            orig_msg = error_data.get("message", "")
                                            error_data["message"] = f"{orig_msg}。使用人数过多，请稍后再试或尝试自部署Overstats或astrbot_plugin_overstats"
                                    return None, error_data, (error_data.get("error", "") if isinstance(error_data, dict) else "")
                                except:
                                    if self.monitor:
                                        elapsed = int((time.time() - start_ts) * 1000)
                                        asyncio.ensure_future(self.monitor.record_api(endpoint, False, elapsed))
                                    logger.error(f"Overstats API 返回了非 JSON 错误: {resp.status}")
                                    return None, {"error": "non_json_error", "message": "API返回非JSON格式错误"}, "non_json_error"
                    except Exception as e:
                        if attempt == 1:
                            logger.warning(f"网络请求异常，1秒后重试: {e}")
                            await asyncio.sleep(1)
                            continue
                        if self.monitor:
                            elapsed = int((time.time() - start_ts) * 1000)
                            asyncio.ensure_future(self.monitor.record_api(endpoint, False, elapsed))
                        logger.error(f"网络请求异常: {e}")
                        return None, {"error": "network_error", "message": str(e)}, "network_error"
                return None, {"error": "retry_exhausted", "message": "重试耗尽"}, "retry_exhausted"

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

    def _build_image_chain(self, event: AstrMessageEvent, img_bytes: bytes, fallback_text: str = ""):
        """构建图片消息链（同步，不含违规检测）。"""
        if not img_bytes:
            return self._plain_error_result(event, fallback_text or "❌ 图片生成失败")
        try:
            user_id = event.get_sender_id()
            if self._save_image_locally:
                img_hash = abs(hash(img_bytes))
                img_path = self.temp_image_dir / f"{img_hash}.png"
                img_path.write_bytes(img_bytes)
                image_comp = Comp.Image.fromFileSystem(str(img_path))
            else:
                image_comp = Comp.Image.fromBytes(img_bytes)
            chain = [Comp.At(qq=user_id), Comp.Plain("\n" if not fallback_text else f"\n{fallback_text}\n"), image_comp]
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f"构建图片消息链时发生错误: {e}")
            return self._plain_error_result(event, fallback_text or "❌ 机器人构建图片组件失败")

    async def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes, command: str, fallback_text: str = ""):
        """发送单张图片，自动捕获违规异常并封禁该指令。"""
        result = self._build_image_chain(event, img_bytes, fallback_text)
        try:
            yield result
        except Exception as exc:
            err_msg = str(exc)
            logger.error(f"发送图片消息失败[{command}]: {err_msg}")
            if "违规" in err_msg or "violation" in err_msg.lower():
                await self._set_violation_ban(event, command)
                yield event.plain_result(
                    self._VIOLATION_BAN_MSG.format(command=command, remain="12小时")
                )
            else:
                yield event.plain_result("❌ 发送图片失败，请稍后重试。")

    async def _send_multiple_images_result(self, event: AstrMessageEvent, imgs_list: list[bytes], command: str):
        try:
            user_id = event.get_sender_id()
            
            valid_images = [img for img in imgs_list if img]
            if not valid_images:
                yield self._plain_error_result(event, "❌ 未能获取到有效的图片数据")
                return

            chain = [Comp.At(qq=user_id), Comp.Plain("\n")]
            for img_bytes in valid_images:
                if self._save_image_locally:
                    # 本地保存模式：写临时文件再用 fromFileSystem 发送
                    img_path = self.temp_image_dir / f"{abs(hash(img_bytes))}_{time.time_ns()}.png"
                    img_path.write_bytes(img_bytes)
                    chain.append(Comp.Image.fromFileSystem(str(img_path)))
                else:
                    # 直接发送模式：bytes 转 base64，不落盘
                    chain.append(Comp.Image.fromBytes(img_bytes))
            try:
                yield event.chain_result(chain)
            except Exception as send_exc:
                err_msg = str(send_exc)
                logger.error(f"发送多图消息失败[{command}]: {err_msg}")
                if "违规" in err_msg or "violation" in err_msg.lower():
                    await self._set_violation_ban(event, command)
                    yield event.plain_result(
                        self._VIOLATION_BAN_MSG.format(command=command, remain="12小时")
                    )
                else:
                    yield self._plain_error_result(event, "❌ 发送图片失败")
                
        except Exception as e:
            logger.error(f"多图发送逻辑错误: {e}")
            yield self._plain_error_result(event, "❌ 多图发送失败")  

    def _ensure_full_adapt_map(self) -> dict:
        """懒构建全量适配指令分发表：指令名/别名 -> 方法引用。

        仅收录业务查询指令；管理/部署类指令需真实@机器人触发（避免权限绕过）。
        表与 @filter.command 声明保持同步，新增业务指令时在此追加一行即可。
        注意：「ow是区吗」/「ow开庭」等高成本 AI 指令不得加入此表，
        必须通过 @机器人显式触发。
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
            (["昨日总结", "昨日", "昨日数据", "昨天数据"], self.dashen_yesterday),
            (["周度总结", "本周总结", "本周数据", "本周"], self.dashen_week),
            (["大神数据", "详情卡片", "战绩查询", "数据"], self.dashen_profile),
            (["大神对局", "最近对局", "战绩", "对局"], self.dashen_match),
            (["单局详细", "单局详情", "单局"], self.dashen_match_detail),
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

    def _get_quick_guide(self, event: AstrMessageEvent, unmatched_cmd: str | None = None) -> str:
        """根据平台返回快速指南，可附带未匹配指令提示"""
        header = ""
        if unmatched_cmd:
            cmd_display = unmatched_cmd[:50] + ("..." if len(unmatched_cmd) > 50 else "")
            header = f"❌ `{cmd_display}` 暂时未匹配到指令\n\n"
        text = header + """📌 Overstats 快速指南

🔗 ➤ <qqbot-cmd-input text="/绑定 " show="绑定" reference="false" />示例：/绑定 Player#12345
📊 ➤ <qqbot-cmd-input text="/今日总结 " show="今日总结" reference="false" /> ➤ <qqbot-cmd-input text="/本周总结 " show="本周总结" reference="false" />
📈 ➤ <qqbot-cmd-input text="/大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="/大神对局 " show="对局" reference="false" />
💪 ➤ <qqbot-cmd-input text="/快速强度 " show="快速强度" reference="false" /> ➤ <qqbot-cmd-input text="/竞技强度 " show="竞技强度" reference="false" />
☁️ ➤ <qqbot-cmd-input text="/快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="/竞技英雄云图 " show="竞技云图" reference="false" />
📋 全部功能 <qqbot-cmd-input text="/owhelp " show="owhelp" reference="false" />
💡 必须<qqbot-cmd-input text=" " show="@机器人" reference="false"/>，不能复制纯文本识别不到
💡 大部分指令都可以带[战网id]参数，查询对应的数据，无需重新绑定"""
        return self._format_markdown_by_platform(event, text)
