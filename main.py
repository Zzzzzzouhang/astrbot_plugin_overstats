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
    from .deploy import DeployManager
except ImportError:
    # 兜底：插件作为顶层模块加载时相对导入可能失败
    from deploy import DeployManager  # type: ignore[no-redef]

# OW 开庭模块（独立封装，避免主文件臃肿）
try:
    from .deploy.ow.court import CourtManager
    from .deploy.ow.shiqu import ShiquManager
except ImportError:
    from deploy.ow.court import CourtManager  # type: ignore[no-redef]
    from deploy.ow.shiqu import ShiquManager  # type: ignore[no-redef]

# 监控模块
try:
    from .deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader
except ImportError:
    from deploy import MonitorCollector, MonitorLogHandler, MonitorSSEQueue, BackendMetricsReader  # type: ignore[no-redef]

# 是区吗调用日志读取
try:
    from .deploy.ow.shiqu_log import ShiquCallReader
except ImportError:
    from deploy.ow.shiqu_log import ShiquCallReader  # type: ignore[no-redef]

# Web API helpers
from astrbot.api.web import request, json_response, error_response, stream_response

# 拆分后的指令业务逻辑模块（handler 仅作薄封装，具体实现见 features/）
try:
    from .features import binding as features_binding
    from .features import summary as features_summary
    from .features import players as features_players
    from .features import charts as features_charts
    from .features import info as features_info
    from .features import court as features_court
    from .features import admin as features_admin
    from .features import config as features_config
except ImportError:  # 插件作为顶层模块加载时
    from features import binding as features_binding  # type: ignore[no-redef]
    from features import summary as features_summary  # type: ignore[no-redef]
    from features import players as features_players  # type: ignore[no-redef]
    from features import charts as features_charts  # type: ignore[no-redef]
    from features import info as features_info  # type: ignore[no-redef]
    from features import court as features_court  # type: ignore[no-redef]
    from features import admin as features_admin  # type: ignore[no-redef]
    from features import config as features_config  # type: ignore[no-redef]

logger = logging.getLogger("astrbot")

@register("overstats_full", "YourName", "Overstats 全指令 QQ 机器人插件", "2.6.5")
class OverstatsPlugin(Star):
    """Overstats 全指令插件。

    本文件中仅保留：
    - 所有指令注册（@filter.command）与事件监听器（@filter.event_message_type 等）；
    - 通用辅助方法、类级常量与初始化逻辑。

    各指令的具体业务逻辑已拆分至 features/ 包下对应模块，
    handler 方法作为薄封装委托 features 中的函数执行（函数签名统一为
    ``func(self, event, *args)``，内部通过 plugin 访问本类辅助方法）。
    """

    # ===== 类级常量（从原 deploy/plugin_modules/plugin_core.py 恢复）=====
    _GROUP_CONFIG_KV_KEY = "group_feature_config"
    _MAINTENANCE_KV_KEY = "maintenance_state"
    _RATE_LIMIT_REJECT_MSG = "⏳ 同时执行的指令过多，请等待当前指令返回结果后再重试"
    _VIOLATION_BAN_SECONDS = 43200  # 12 小时
    _VIOLATION_BAN_FILE = "violation_bans.json"
    _VIOLATION_BAN_MSG = "⛔ 【极少数情况】您之前【{command}】查询返回的图片包含违规内容（来自qq官方接口返回提示，可能：违规id，等），该指令已被自动禁用{remain}。请勿再试，如有疑问请联系管理员：2338127903。"
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

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.file_lock = asyncio.Lock()
        self._cmd_concurrent_lock = asyncio.Lock()
        self._cmd_concurrent_count = 0
        self._load_rate_limit_config()
        self._semaphore_reset_task = asyncio.create_task(self._daily_semaphore_reset_loop())
        self.daily_group_prompt_suffix = '[今日已提示，群内后续指令不再提示将直接返回结果]'
        self.daily_group_prompt_notice = '群内使用请手动<qqbot-cmd-input text=" " show="@机器人" reference="false"/>或点击<qqbot-cmd-input text="快捷指令" show="快捷指令" reference="false"/>按钮，纯文本无法识别。'
        self.error_append_notice = '如需重新发起指令，请手动<qqbot-cmd-input text=" " show="@机器人" reference="false"/>或点击<qqbot-cmd-input text="快捷指令" show="快捷指令" reference="false"/>按钮，纯文本无法识别。'
        self.daily_prompt_pending_users: set[str] = set()
        self.id_resolve_error_hint = '未查询到id或者id错误，id严格区分大小写'
        self._save_image_locally = bool(self.config.get('save_image_locally', True))
        try:
            plugin_name = getattr(self, 'name', 'overstats_full')
            self.plugin_data_dir = Path(get_astrbot_data_path()) / 'plugin_data' / plugin_name
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            if self._save_image_locally:
                self.temp_image_dir = self.plugin_data_dir / 'temp'
                self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            else:
                self.temp_image_dir = None
        except Exception as e:
            logger.warning(f'初始化插件专属数据目录失败: {e}')
            self.plugin_data_dir = Path(tempfile.gettempdir())
            if self._save_image_locally:
                self.temp_image_dir = self.plugin_data_dir / 'temp'
                self.temp_image_dir.mkdir(parents=True, exist_ok=True)
            else:
                self.temp_image_dir = None
        self.deploy_manager = DeployManager(plugin_data_dir=self.plugin_data_dir, config=config, context=context)
        self.base_url = self.deploy_manager.resolve_base_url()
        logger.info(f'[Overstats] 接入模式: {self.deploy_manager.mode}, base_url={self.base_url}')
        self.daily_prompt_state_file = self.plugin_data_dir / 'daily_group_prompt_state.json'
        self.daily_prompt_state = self._load_daily_prompt_state()
        self.daily_prompt_reset_task = asyncio.create_task(self._daily_prompt_reset_loop())
        self._http_session: aiohttp.ClientSession | None = None
        self.overstats_semaphore = asyncio.Semaphore(3)
        self._overstats_inner_semaphore = asyncio.Semaphore(2)
        if self._save_image_locally:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())
        else:
            self._cleanup_task = None
        self.group_config: dict | None = None
        self._maintenance_state: dict | None = None
        self.full_adapt_config_file = self.plugin_data_dir / 'full_adaptation_config.json'
        self.full_adapt_config: dict | None = None
        self._bot_nickname_cache: str | None = None
        self._full_adapt_map: dict | None = None
        self._admin_cmd_set: set[str] = {'多图测试', '单图测试', 'ow开庭', '开庭', 'ow是区吗', '是区吗', 'ow是区吗结果', '是区吗结果', 'owAI检测', 'AI检测', '维护', 'ow违禁封禁', 'ow违禁解封', 'ow连接测试', 'ow部署', 'ow部署状态', 'ow更新后端', 'ow停止后端', 'ow重启后端', 'ow部署日志', 'ow后端日志', 'ow卸载后端', 'ow卸载后端执行确认', 'ow卸载后端执行仅代码', 'ow卸载后端执行仅venv', 'ow卸载后端执行强制', '群设置', '全量适配开', '全量适配开完全匹配', '全量适配关', '管理'}
        self.court_manager = CourtManager(self)
        self.shiqu_manager = ShiquManager(self)
        self._register_monitor_apis()
        try:
            _monitor_db = self.plugin_data_dir / 'monitor_stats.sqlite3'
            self.monitor = MonitorCollector(_monitor_db)
        except Exception as e:
            logger.warning(f'[Overstats] MonitorCollector 初始化失败: {e}')
            self.monitor = None
        try:
            self.monitor_sse = MonitorSSEQueue(maxsize=50)
            if self.monitor:
                _log_handler = MonitorLogHandler(self.monitor, self.monitor_sse)
                _log_handler.setLevel(logging.ERROR)
                logging.getLogger('astrbot').addHandler(_log_handler)
                self._monitor_log_handler = _log_handler
            else:
                self._monitor_log_handler = None
        except Exception as e:
            logger.warning(f'[Overstats] MonitorLogHandler 初始化失败: {e}')
            self._monitor_log_handler = None
        try:
            _sqlite_cfg = str(self.config.get('sqlite_db_path', '') or '')
            self._backend_metrics_reader = BackendMetricsReader()
            self._req_metrics_db_path = BackendMetricsReader.resolve_db_path(self.plugin_data_dir, self.deploy_manager.mode, _sqlite_cfg)
        except Exception as e:
            logger.warning(f'[Overstats] BackendMetricsReader 初始化失败: {e}')
            self._backend_metrics_reader = None
            self._req_metrics_db_path = None
        if self.monitor:
            logger.info(f'[Overstats] 监控模块初始化完成')
        else:
            logger.warning('[Overstats] 监控采集器未就绪，Web 面板将显示空数据')
        self._auto_start_task = asyncio.create_task(self.deploy_manager.maybe_auto_start())

    def _current_prompt_cycle_date(self, now: datetime | None=None) -> str:
        now = now or datetime.now()
        if now.time() < dt_time(hour=4):
            now -= timedelta(days=1)
        return now.date().isoformat()

    def _seconds_until_next_prompt_reset(self, now: datetime | None=None) -> float:
        now = now or datetime.now()
        next_reset = datetime.combine(now.date(), dt_time(hour=4))
        if now >= next_reset:
            next_reset += timedelta(days=1)
        return max((next_reset - now).total_seconds(), 1.0)

    def _build_empty_daily_prompt_state(self, cycle_date: str | None=None) -> dict:
        return {'cycle_date': cycle_date or self._current_prompt_cycle_date(), 'prompted_users': {}}

    def _load_daily_prompt_state(self) -> dict:
        default_state = self._build_empty_daily_prompt_state()
        try:
            if self.daily_prompt_state_file.exists():
                with open(self.daily_prompt_state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('prompted_users'), dict):
                    data.setdefault('cycle_date', default_state['cycle_date'])
                    return data
        except Exception as e:
            logger.error(f'读取每日首次提示状态失败: {e}')
        self._save_daily_prompt_state(default_state)
        return default_state

    def _save_daily_prompt_state(self, data: dict | None=None):
        payload = data or self.daily_prompt_state
        try:
            with open(self.daily_prompt_state_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f'保存每日首次提示状态失败: {e}')

    def _ensure_daily_prompt_state_current_unlocked(self):
        current_cycle_date = self._current_prompt_cycle_date()
        if self.daily_prompt_state.get('cycle_date') != current_cycle_date:
            self.daily_prompt_state = self._build_empty_daily_prompt_state(current_cycle_date)
            self._save_daily_prompt_state()
            self.daily_prompt_pending_users.clear()

    def _reset_daily_prompt_state_unlocked(self):
        self.daily_prompt_state = self._build_empty_daily_prompt_state()
        self._save_daily_prompt_state()
        self.daily_prompt_pending_users.clear()
        logger.info('已重置群聊每日首次提示状态')

    async def _daily_prompt_reset_loop(self):
        try:
            while True:
                await asyncio.sleep(self._seconds_until_next_prompt_reset())
                async with self.file_lock:
                    self._reset_daily_prompt_state_unlocked()
        except asyncio.CancelledError:
            logger.info('群聊每日首次提示重置任务已停止')
        except Exception as e:
            logger.error(f'群聊每日首次提示重置任务异常: {e}')

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
                logger.info(f'[Overstats] 每日 4 点已重置指令限流计数器（max={self._rate_limit_max}）')
        except asyncio.CancelledError:
            logger.info('指令限流每日重置任务已停止')
        except Exception as e:
            logger.error(f'指令限流每日重置任务异常: {e}')

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
            logger.error(f'从 KV 读取群组功能配置失败: {e}')
            self.group_config = {}

    async def _save_group_config(self):
        """将内存中的群组配置持久化到 KV 存储"""
        try:
            await self.put_kv_data(self._GROUP_CONFIG_KV_KEY, self.group_config)
        except Exception as e:
            logger.error(f'保存群组功能配置到 KV 失败: {e}')

    def _get_group_id(self, event: AstrMessageEvent) -> str | None:
        """从事件中获取群组ID"""
        try:
            if getattr(event, 'message_obj', None) and getattr(event.message_obj, 'group_id', ''):
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
            if hasattr(self.context, 'get_config'):
                cfg = self.context.get_config()
                admin_ids = [str(uid) for uid in getattr(cfg, 'admins_id', [])]
            if sender_id in admin_ids:
                return True
        except Exception:
            pass
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return False

    def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为群管理员/群主 或 AstrBot 管理员"""
        sender_id = ''
        try:
            sender_id = str(event.get_sender_id())
            admin_ids = []
            if hasattr(self.context, 'get_config'):
                cfg = self.context.get_config()
                admin_ids = [str(uid) for uid in getattr(cfg, 'admins_id', [])]
            if sender_id in admin_ids:
                return True
        except Exception:
            pass
        try:
            raw = getattr(getattr(event, 'message_obj', None), 'raw_message', None)
            if raw:
                if isinstance(raw, dict):
                    sender_data = raw.get('sender', {})
                    role = str(sender_data.get('role', '')).lower()
                    logger.debug(f'群管理员检查(OneBot dict): sender_id={sender_id}, role={role}')
                    if role in ('owner', 'admin'):
                        return True
                elif hasattr(raw, 'sender'):
                    role = str(getattr(raw.sender, 'role', '')).lower()
                    logger.debug(f'群管理员检查(raw.sender): role={role}')
                    if role in ('owner', 'admin', '群主', '管理员'):
                        return True
                else:
                    logger.debug(f'群管理员检查: raw_message 类型={type(raw).__name__}, 无法提取角色')
        except Exception as e:
            logger.debug(f'群管理员检查 raw_message 异常: {e}')
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return False

    def _get_group_feature_config(self, group_id: str) -> dict:
        """获取指定群组的功能配置（从内存缓存读取），未配置时返回默认值"""
        if self.group_config is None:
            return {'daily_prompt_skip': False, 'append_notice': True}
        return self.group_config.get(str(group_id), {'daily_prompt_skip': False, 'append_notice': True})

    async def _set_group_feature_config(self, group_id: str, feature: str, enabled: bool):
        """设置指定群组的某个功能开关并持久化"""
        await self._ensure_group_config_loaded()
        gid = str(group_id)
        if gid not in self.group_config:
            self.group_config[gid] = {'daily_prompt_skip': False, 'append_notice': True}
        self.group_config[gid][feature] = enabled
        await self._save_group_config()

    def _load_full_adapt_config(self) -> dict:
        """从 JSON 文件加载全量适配配置（惰性，带内存缓存）"""
        if self.full_adapt_config is not None:
            return self.full_adapt_config
        try:
            if self.full_adapt_config_file.exists():
                with open(self.full_adapt_config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.full_adapt_config = data if isinstance(data, dict) else {}
            else:
                self.full_adapt_config = {}
        except Exception as e:
            logger.error(f'读取全量适配配置失败: {e}')
            self.full_adapt_config = {}
        return self.full_adapt_config

    def _save_full_adapt_config(self):
        try:
            with open(self.full_adapt_config_file, 'w', encoding='utf-8') as f:
                json.dump(self.full_adapt_config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f'保存全量适配配置失败: {e}')

    def _get_full_adapt(self, group_id: str) -> dict:
        """获取指定群的全量适配配置"""
        cfg = self._load_full_adapt_config().get(str(group_id), {'enabled': False, 'nickname': '', 'mode': 'nickname'})
        cfg.setdefault('mode', 'nickname')
        return cfg

    def _set_full_adapt(self, group_id: str, enabled: bool, nickname: str | None=None, mode: str | None=None):
        """设置指定群的全量适配开关（可选更新昵称/模式）并持久化到 JSON"""
        cfg = self._load_full_adapt_config()
        gid = str(group_id)
        cur = cfg.get(gid, {'enabled': False, 'nickname': '', 'mode': 'nickname'})
        cur.setdefault('mode', 'nickname')
        cur['enabled'] = enabled
        if nickname is not None and nickname.strip():
            cur['nickname'] = nickname.strip()
        cur.setdefault('nickname', '')
        if mode is not None:
            cur['mode'] = mode
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
        if not self_id or self_id in ('qq_official', 'unknown_selfid'):
            return False
        try:
            for comp in event.get_messages():
                if isinstance(comp, Comp.At) and str(getattr(comp, 'qq', '')) == self_id:
                    return True
        except Exception:
            pass
        return False

    def _get_bot_nickname(self, event: AstrMessageEvent, adapt_cfg: dict | None=None) -> str:
        """解析机器人昵称：群独立配置 > 自动探测(真实@时的 At.name) > 全局配置兜底"""
        if adapt_cfg and adapt_cfg.get('nickname'):
            return adapt_cfg['nickname']
        try:
            self_id = str(event.get_self_id())
            if self_id and self_id not in ('qq_official', 'unknown_selfid'):
                for comp in event.get_messages():
                    if isinstance(comp, Comp.At) and str(getattr(comp, 'qq', '')) == self_id:
                        name = (getattr(comp, 'name', '') or '').strip()
                        if name:
                            self._bot_nickname_cache = name
                            return name
        except Exception:
            pass
        if self._bot_nickname_cache:
            return self._bot_nickname_cache
        return (self.config.get('full_adaptation_nickname', '') or '').strip()

    async def _ensure_maintenance_loaded(self):
        """确保维护模式状态已从 KV 加载到内存"""
        if self._maintenance_state is not None:
            return
        try:
            data = await self.get_kv_data(self._MAINTENANCE_KV_KEY, None)
            if isinstance(data, dict):
                self._maintenance_state = data
            else:
                self._maintenance_state = {'enabled': False, 'content': ''}
        except Exception as e:
            logger.error(f'读取维护模式状态失败: {e}')
            self._maintenance_state = {'enabled': False, 'content': ''}

    async def _set_maintenance(self, enabled: bool, content: str=''):
        """设置或取消维护模式并持久化"""
        await self._ensure_maintenance_loaded()
        self._maintenance_state['enabled'] = enabled
        self._maintenance_state['content'] = content if enabled else ''
        try:
            await self.put_kv_data(self._MAINTENANCE_KV_KEY, self._maintenance_state)
        except Exception as e:
            logger.error(f'保存维护模式状态失败: {e}')

    async def _check_maintenance(self, event: AstrMessageEvent) -> str | None:
        """集中维护模式检查，返回维护提示文本或 None（放行）。

            若维护模式启用且发送者非 AstrBot 管理员且不在白名单内，返回维护内容；
            否则返回 None，允许正常执行业务逻辑。
            调用方应在 yield 维护文本后 return，不再继续执行业务。
            """
        await self._ensure_maintenance_loaded()
        if self._maintenance_state and self._maintenance_state.get('enabled'):
            if not self._is_astrbot_admin(event) and (not self._is_whitelisted(event)):
                return self._maintenance_state.get('content', '系统维护中')
        return None

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        """检查当前消息的群聊或发送者是否在白名单中。"""
        group_whitelist = self.config.get('group_whitelist', []) or []
        user_whitelist = self.config.get('user_whitelist', []) or []
        if not group_whitelist and (not user_whitelist):
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
        raw = str(self.config.get('cmd_rate_limit', '{}') or '{}').strip()
        try:
            cfg = json.loads(raw) if raw else {}
        except Exception:
            cfg = {}
        self._rate_limit_enabled = bool(cfg.get('enabled', False))
        self._rate_limit_max = max(1, int(cfg.get('max_concurrent', 3) or 3))
        raw_llm = str(self.config.get('llm_rate_limit', '{}') or '{}').strip()
        try:
            llm_cfg = json.loads(raw_llm) if raw_llm else {}
        except Exception:
            llm_cfg = {}
        self._llm_rate_limit_enabled = bool(llm_cfg.get('enabled', False))
        self._llm_rate_limit_per_minute = max(1, int(llm_cfg.get('per_minute', 10) or 10))
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
                asyncio.ensure_future(self.monitor.record_rate_limit('llm'))
            return False
        self._llm_rate_limit_timestamps.append(now)
        return True

    def _is_privileged(self, event: AstrMessageEvent) -> bool:
        """白名单群/用户 或 AstrBot 管理员不受限流控制。限流关闭时所有人视为特权。"""
        return not self._rate_limit_enabled or self._is_whitelisted(event) or self._is_astrbot_admin(event)

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
            yield None
            return
        try:
            yield True
        finally:
            self._release_cmd_slot()

    def _violation_ban_path(self) -> Path:
        return self.plugin_data_dir / self._VIOLATION_BAN_FILE

    def _load_violation_bans(self) -> dict:
        """加载违规封禁记录 JSON，结构 { "平台:ID|指令名": 封禁时间戳 }。"""
        path = self._violation_ban_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_violation_bans(self, bans: dict) -> None:
        """原子写入 JSON。"""
        path = self._violation_ban_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'{path.name}.tmp')
        tmp.write_text(json.dumps(bans, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)

    def _user_key(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识（平台:ID）。"""
        try:
            return f'{event.get_platform_name()}:{event.get_sender_id()}'
        except Exception:
            return str(event.get_sender_id())

    @staticmethod
    def _violation_ban_key(user_key: str, command: str) -> str:
        """生成封禁 JSON key：用户|指令名。"""
        return f'{user_key}|{command}'

    async def _check_violation_ban(self, event: AstrMessageEvent, command: str) -> tuple[bool, int]:
        """检查 用户+指令 是否被封。返回 (is_banned, remaining_seconds)。过期自动清除。"""
        try:
            user_key = self._user_key(event)
            ban_key = self._violation_ban_key(user_key, command)
            bans = self._load_violation_bans()
            ban_ts = bans.get(ban_key, 0)
            if not ban_ts:
                return (False, 0)
            elapsed = int(time.time()) - int(ban_ts)
            if elapsed >= self._VIOLATION_BAN_SECONDS:
                bans.pop(ban_key, None)
                self._save_violation_bans(bans)
                return (False, 0)
            return (True, self._VIOLATION_BAN_SECONDS - elapsed)
        except Exception:
            return (False, 0)

    async def _set_violation_ban(self, event: AstrMessageEvent, command: str) -> None:
        """封禁 用户+指令 12小时。"""
        try:
            user_key = self._user_key(event)
            ban_key = self._violation_ban_key(user_key, command)
            bans = self._load_violation_bans()
            bans[ban_key] = int(time.time())
            self._save_violation_bans(bans)
            logger.warning(f'[ViolationBan] 封禁 {ban_key}（12小时）')
        except Exception as e:
            logger.error(f'[ViolationBan] 设置封禁失败: {e}')

    async def _clear_violation_ban(self, event: AstrMessageEvent, command: str) -> None:
        """解除 用户+指令 封禁。"""
        try:
            user_key = self._user_key(event)
            ban_key = self._violation_ban_key(user_key, command)
            bans = self._load_violation_bans()
            bans.pop(ban_key, None)
            self._save_violation_bans(bans)
            logger.info(f'[ViolationBan] 解封 {ban_key}')
        except Exception as e:
            logger.error(f'[ViolationBan] 解封失败: {e}')

    @staticmethod
    def _violation_ban_remain_str(seconds: int) -> str:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f'{int(h)}小时{int(m)}分钟'
        return f'{int(m)}分钟{int(s)}秒'

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        try:
            if getattr(event, 'message_obj', None) and getattr(event.message_obj, 'group_id', ''):
                return True
        except Exception:
            pass
        return 'GROUP_MESSAGE' in str(getattr(event, 'unified_msg_origin', '')).upper()

    def _is_qq_group_message(self, event: AstrMessageEvent) -> bool:
        if not self._is_group_message(event):
            return False
        try:
            platform_name = event.get_platform_name()
            if 'qq' in str(platform_name).lower():
                return True
        except Exception:
            pass
        return 'qq' in str(getattr(event, 'unified_msg_origin', '')).lower()

    def _append_group_interaction_notice(self, event: AstrMessageEvent, text: str) -> str:
        if not self._is_qq_group_message(event):
            return text
        group_id = self._get_group_id(event)
        if group_id and self.group_config is not None:
            cfg = self._get_group_feature_config(group_id)
            if not cfg.get('append_notice', True):
                return text
        notice = self._format_markdown_by_platform(event, self.error_append_notice)
        if notice in text:
            return text
        return f'{text}\n💡 {notice}'

    def _plain_error_result(self, event: AstrMessageEvent, text: str):
        return event.plain_result(self._append_group_interaction_notice(event, text))

    def _id_resolve_err(self, prefix: str) -> str:
        """统一的 ID 解析失败提示文案，修改 id_resolve_error_hint 即可全局生效"""
        return f'❌ {prefix}：{self.id_resolve_error_hint}'

    def _bnet_err(self, cmd: str) -> str:
        """ponytail: 10 处重复的战网ID必填错误提示，统一收敛到此"""
        return f'❌ 请输入战网ID，如：{cmd} Player#12345\n或先使用 绑定 战网ID，示例：绑定 Player#12345'

    async def _prepare_business_status_prompt(self, event: AstrMessageEvent, base_text: str) -> tuple[str | None, str | None, bool]:
        """准备业务状态提示。返回 (提示文本, 跟踪token, 是否应提前终止)。

            当 should_stop=True 时（维护模式），调用方应 yield 文本后 return，不再执行业务逻辑。
            AstrBot 管理员不受维护模式限制，可正常使用数据查询指令。
            非特权用户在此获取并发限流槽位（最多3条），由 _finalize_business_status_prompt 统一释放。
            """
        await self._ensure_maintenance_loaded()
        if self._maintenance_state and self._maintenance_state.get('enabled'):
            if self._is_astrbot_admin(event) or self._is_whitelisted(event):
                pass
            else:
                return (self._maintenance_state.get('content', '系统维护中'), None, True)
        need_release = False
        if not self._is_privileged(event):
            if not await self._try_acquire_cmd_slot():
                return (self._RATE_LIMIT_REJECT_MSG, None, True)
            need_release = True
        if not self._is_group_message(event):
            return (base_text, (None, need_release), False)
        await self._ensure_group_config_loaded()
        group_id = self._get_group_id(event)
        cfg = self._get_group_feature_config(group_id) if group_id else {'daily_prompt_skip': False, 'append_notice': True}
        daily_prompt_enabled = cfg.get('daily_prompt_skip', False)
        append_notice_enabled = cfg.get('append_notice', True)
        user_id = None
        if daily_prompt_enabled:
            user_id = str(event.get_sender_id())
            async with self.file_lock:
                self._ensure_daily_prompt_state_current_unlocked()
                prompted_users = self.daily_prompt_state.setdefault('prompted_users', {})
                if prompted_users.get(user_id) or user_id in self.daily_prompt_pending_users:
                    return (None, (None, need_release), False)
                else:
                    self.daily_prompt_pending_users.add(user_id)
        prompt_text = base_text
        if daily_prompt_enabled and user_id:
            prompt_text = f'{prompt_text}\n{self.daily_group_prompt_suffix}'
        if append_notice_enabled and self._is_qq_group_message(event):
            notice = self._format_markdown_by_platform(event, self.daily_group_prompt_notice)
            if notice not in prompt_text:
                prompt_text = f'{prompt_text}\n💡 {notice}'
        prompt_token = (user_id if daily_prompt_enabled and user_id else None, need_release)
        return (prompt_text, prompt_token, False)

    async def _finalize_business_status_prompt(self, reservation_user_id, success: bool, cmd_name: str='', error_code: str=''):
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
        if need_release:
            self._release_cmd_slot()
        if cmd_name and self.monitor:
            asyncio.ensure_future(self.monitor.record_command(cmd_name, success, error_code=error_code))
        if not daily_user_id:
            return
        async with self.file_lock:
            self._ensure_daily_prompt_state_current_unlocked()
            self.daily_prompt_pending_users.discard(daily_user_id)
            if success:
                prompted_users = self.daily_prompt_state.setdefault('prompted_users', {})
                prompted_users[daily_user_id] = True
                self._save_daily_prompt_state()

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """获取复用的 aiohttp.ClientSession（懒加载，自动重建已关闭的会话）"""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600), connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300))
        return self._http_session

    async def terminate(self):
        """插件卸载/停用时清理资源（AstrBot 生命周期方法）"""
        auto_task = getattr(self, '_auto_start_task', None)
        if auto_task and (not auto_task.done()):
            auto_task.cancel()
            try:
                await auto_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if hasattr(self, 'deploy_manager'):
                await self.deploy_manager.cleanup()
        except Exception as e:
            logger.warning(f'[Overstats] 停止后端进程异常: {e}')
        for task_attr in ('daily_prompt_reset_task', '_cleanup_task', '_semaphore_reset_task'):
            task = getattr(self, task_attr, None)
            if task and (not task.done()):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        _handler = getattr(self, '_monitor_log_handler', None)
        if _handler:
            try:
                logging.getLogger('astrbot').removeHandler(_handler)
            except Exception:
                pass
        if self._http_session and (not self._http_session.closed):
            await self._http_session.close()

    def _is_qq_official(self, event: AstrMessageEvent) -> bool:
        """判断当前消息是否来自 QQ 官方机器人平台"""
        try:
            pn = event.get_platform_name()
            if pn and 'qq_official' in str(pn).lower():
                return True
        except Exception:
            pass
        umo = event.unified_msg_origin
        if 'qq_official' in str(umo).lower() or 'qqofficial' in str(umo).lower():
            return True
        if hasattr(event, 'message_obj') and event.message_obj:
            s = str(event.message_obj).lower()
            if 'qq_official' in s or 'qqofficial' in s:
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
            text = re.sub('<qqbot-cmd-input\\s+text="([^"]+)"\\s+show="([^"]+)"\\s+reference="false"\\s*/>', replacer_input, text)
            text = re.sub('<qqbot-cmd-enter\\s+text="([^"]+)"\\s*/>', replacer_enter, text)
            return text
        else:

            def strip_replacer_input(match):
                text_val = match.group(1).strip()
                return f'{text_val}'

            def strip_replacer_enter(match):
                text_val = match.group(1).strip()
                return f'{text_val}'
            text = re.sub('<qqbot-cmd-input\\s+text="([^"]+)"\\s+show="([^"]+)"\\s+reference="false"\\s*/>', strip_replacer_input, text)
            text = re.sub('<qqbot-cmd-enter\\s+text="([^"]+)"\\s*/>', strip_replacer_enter, text)
            return text

    async def _fetch_image(self, endpoint: str, payload: dict=None, timeout: int=600) -> tuple[bytes | None, dict | None, str]:
        url = f'{self.base_url}{endpoint}'
        payload = payload or {}
        async with self.overstats_semaphore:
            async with self._overstats_inner_semaphore:
                for attempt in (1, 2):
                    start_ts = time.time()
                    try:
                        session = await self._get_http_session()
                        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout != 600 else None
                        async with session.post(url, json=payload, timeout=req_timeout) as resp:
                            if resp.status == 200:
                                if self.monitor:
                                    elapsed = int((time.time() - start_ts) * 1000)
                                    asyncio.ensure_future(self.monitor.record_api(endpoint, True, elapsed))
                                return (await resp.read(), None, '')
                            else:
                                try:
                                    error_data = await resp.json()
                                    _soft_errors = {'bnet_not_found', 'summary_empty'}
                                    _is_soft = isinstance(error_data, dict) and error_data.get('error') in _soft_errors
                                    _is_timeout = resp.status == 500 and isinstance(error_data, dict) and ('ConnectTimeout' in str(error_data))
                                    if _is_timeout and attempt == 1:
                                        if self.monitor:
                                            elapsed = int((time.time() - start_ts) * 1000)
                                            asyncio.ensure_future(self.monitor.record_api(endpoint, True, elapsed))
                                        logger.warning(f'Overstats API ConnectTimeout，1秒后重试: {endpoint}')
                                        await asyncio.sleep(1)
                                        continue
                                    if self.monitor:
                                        elapsed = int((time.time() - start_ts) * 1000)
                                        asyncio.ensure_future(self.monitor.record_api(endpoint, _is_soft, elapsed))
                                    logger.error(f'Overstats API 错误: {resp.status} - {error_data}')
                                    if isinstance(error_data, dict):
                                        if error_data.get('error') == 'internal_error':
                                            orig_msg = error_data.get('message', '')
                                            error_data['message'] = f'{orig_msg}，网络波动，请重试'
                                        if resp.status == 429 or 'too many requests' in str(error_data.get('message', '')).lower():
                                            orig_msg = error_data.get('message', '')
                                            error_data['message'] = f'{orig_msg}。使用人数过多，请稍后再试或尝试自部署Overstats或astrbot_plugin_overstats'
                                    return (None, error_data, error_data.get('error', '') if isinstance(error_data, dict) else '')
                                except:
                                    if self.monitor:
                                        elapsed = int((time.time() - start_ts) * 1000)
                                        asyncio.ensure_future(self.monitor.record_api(endpoint, False, elapsed))
                                    logger.error(f'Overstats API 返回了非 JSON 错误: {resp.status}')
                                    return (None, {'error': 'non_json_error', 'message': 'API返回非JSON格式错误'}, 'non_json_error')
                    except Exception as e:
                        if attempt == 1:
                            logger.warning(f'网络请求异常，1秒后重试: {e}')
                            await asyncio.sleep(1)
                            continue
                        if self.monitor:
                            elapsed = int((time.time() - start_ts) * 1000)
                            asyncio.ensure_future(self.monitor.record_api(endpoint, False, elapsed))
                        logger.error(f'网络请求异常: {e}')
                        return (None, {'error': 'network_error', 'message': str(e)}, 'network_error')
                return (None, {'error': 'retry_exhausted', 'message': '重试耗尽'}, 'retry_exhausted')

    async def _periodic_cleanup_loop(self):
        """定时清理临时图片目录中超过 7 天的文件（每天执行一次）"""
        try:
            while True:
                await asyncio.sleep(86400)
                try:
                    now = time.time()
                    count = 0
                    for f in self.temp_image_dir.iterdir():
                        if f.is_file() and now - f.stat().st_mtime > 7 * 86400:
                            try:
                                f.unlink()
                                count += 1
                            except Exception:
                                pass
                    if count:
                        logger.info(f'已清理 {count} 个过期临时图片文件')
                except Exception as e:
                    logger.error(f'临时文件清理异常: {e}')
        except asyncio.CancelledError:
            pass

    def _build_image_chain(self, event: AstrMessageEvent, img_bytes: bytes, fallback_text: str=''):
        """构建图片消息链（同步，不含违规检测）。"""
        if not img_bytes:
            return self._plain_error_result(event, fallback_text or '❌ 图片生成失败')
        try:
            user_id = event.get_sender_id()
            if self._save_image_locally:
                img_hash = abs(hash(img_bytes))
                img_path = self.temp_image_dir / f'{img_hash}.png'
                img_path.write_bytes(img_bytes)
                image_comp = Comp.Image.fromFileSystem(str(img_path))
            else:
                image_comp = Comp.Image.fromBytes(img_bytes)
            chain = [Comp.At(qq=user_id), Comp.Plain('\n' if not fallback_text else f'\n{fallback_text}\n'), image_comp]
            return event.chain_result(chain)
        except Exception as e:
            logger.error(f'构建图片消息链时发生错误: {e}')
            return self._plain_error_result(event, fallback_text or '❌ 机器人构建图片组件失败')

    async def _send_image_result(self, event: AstrMessageEvent, img_bytes: bytes, command: str, fallback_text: str=''):
        """发送单张图片，自动捕获违规异常并封禁该指令。"""
        result = self._build_image_chain(event, img_bytes, fallback_text)
        try:
            yield result
        except Exception as exc:
            err_msg = str(exc)
            logger.error(f'发送图片消息失败[{command}]: {err_msg}')
            if '违规' in err_msg or 'violation' in err_msg.lower():
                await self._set_violation_ban(event, command)
                yield event.plain_result(self._VIOLATION_BAN_MSG.format(command=command, remain='12小时'))
            else:
                yield event.plain_result('❌ 发送图片失败，请稍后重试。')

    async def _send_multiple_images_result(self, event: AstrMessageEvent, imgs_list: list[bytes], command: str):
        try:
            user_id = event.get_sender_id()
            valid_images = [img for img in imgs_list if img]
            if not valid_images:
                yield self._plain_error_result(event, '❌ 未能获取到有效的图片数据')
                return
            chain = [Comp.At(qq=user_id), Comp.Plain('\n')]
            for img_bytes in valid_images:
                if self._save_image_locally:
                    img_path = self.temp_image_dir / f'{abs(hash(img_bytes))}_{time.time_ns()}.png'
                    img_path.write_bytes(img_bytes)
                    chain.append(Comp.Image.fromFileSystem(str(img_path)))
                else:
                    chain.append(Comp.Image.fromBytes(img_bytes))
            try:
                yield event.chain_result(chain)
            except Exception as send_exc:
                err_msg = str(send_exc)
                logger.error(f'发送多图消息失败[{command}]: {err_msg}')
                if '违规' in err_msg or 'violation' in err_msg.lower():
                    await self._set_violation_ban(event, command)
                    yield event.plain_result(self._VIOLATION_BAN_MSG.format(command=command, remain='12小时'))
                else:
                    yield self._plain_error_result(event, '❌ 发送图片失败')
        except Exception as e:
            logger.error(f'多图发送逻辑错误: {e}')
            yield self._plain_error_result(event, '❌ 多图发送失败')

    def _ensure_full_adapt_map(self) -> dict:
        """懒构建全量适配指令分发表：指令名/别名 -> 方法引用。

            仅收录业务查询指令；管理/部署类指令需真实@机器人触发（避免权限绕过）。
            表与 @filter.command 声明保持同步，新增业务指令时在此追加一行即可。
            注意：「ow是区吗」/「ow开庭」等高成本 AI 指令不得加入此表，
            必须通过 @机器人显式触发。
            """
        if self._full_adapt_map is not None:
            return self._full_adapt_map
        table = [(['owhelp', 'ow菜单', 'ow帮助', 'OW帮助', 'help'], self.ow_help), (['所有指令', '别称'], self.show_aliases), (['快速指南', '快捷指令'], self.quick_guide_command), (['大神绑定', '绑定'], self.dashen_bind), (['今日总结', '今日', '今日数据'], self.dashen_today), (['昨日总结', '昨日', '昨日数据', '昨天数据'], self.dashen_yesterday), (['周度总结', '本周总结', '本周数据', '本周'], self.dashen_week), (['大神数据', '详情卡片', '战绩查询', '数据'], self.dashen_profile), (['大神对局', '最近对局', '战绩', '对局'], self.dashen_match), (['单局详细', '单局详情', '单局'], self.dashen_match_detail), (['历史段位', '历届段位'], self.dashen_rank_history), (['同玩查询', '开黑胜率'], self.dashen_sameplay), (['快速强度', '快速强度指数'], self.quick_strength), (['竞技强度', '竞技强度指数'], self.competitive_strength), (['快速英雄云图', '快速云图'], self.quick_hero_treemap), (['竞技英雄云图', '竞技云图'], self.competitive_hero_treemap), (['威能'], self.ow_hero_perk), (['ow英雄'], self.ow_hero_pick), (['商店', 'ow商店'], self.ow_shop), (['ow赛事', '赛事'], self.ow_esports), (['获取段位分布'], self.get_rank_distribution), (['ow活动', '活动'], self.ow_activities), (['banpick', '全英雄排行'], self.ban_pick_stats), (['mappick'], self.map_pick_stats), (['皮肤搜索'], self.skin_search), (['ow更新', '版本更新'], self.ow_patch_notes), (['省榜', '排行'], self.ow_rank_leaderboard), (['绝活榜', '英雄省榜'], self.ow_hero_leaderboard)]
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

🔗 ➤ <qqbot-cmd-input text="绑定 " show="绑定" reference="false" />示例：/绑定 Player#12345
📊 ➤ <qqbot-cmd-input text="今日总结 " show="今日总结" reference="false" /> ➤ <qqbot-cmd-input text="本周总结 " show="本周总结" reference="false" />
📈 ➤ <qqbot-cmd-input text="大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="大神对局 " show="对局" reference="false" />
💪 ➤ <qqbot-cmd-input text="快速强度 " show="快速强度" reference="false" /> ➤ <qqbot-cmd-input text="竞技强度 " show="竞技强度" reference="false" />
☁️ ➤ <qqbot-cmd-input text="快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="竞技英雄云图 " show="竞技云图" reference="false" />
📋 全部功能 <qqbot-cmd-input text="owhelp " show="owhelp" reference="false" />
💡 必须<qqbot-cmd-input text=" " show="@机器人" reference="false"/>，不能复制纯文本识别不到
💡 大部分指令都可以带 战网id 参数，查询对应的数据，无需重新绑定"""
        return self._format_markdown_by_platform(event, text)


    def _get_binds_file_path(self) -> Path:
        bot_id = 'default'
        try:
            if hasattr(self.context, 'get_config'):
                bot_identity = self.context.get_config().active_bot_identity
                if bot_identity:
                    bot_id = str(bot_identity)
        except Exception:
            pass
        file_path = self.plugin_data_dir / f'binds_{bot_id}.json'
        if not file_path.exists():
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump({}, f, ensure_ascii=False, indent=4)
            except Exception as e:
                logger.error(f'创建机器人 [{bot_id}] 的绑定文件失败: {e}')
        return file_path

    def _load_binds(self) -> dict:
        try:
            file_path = self._get_binds_file_path()
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f'读取绑定明文文件失败: {e}')
        return {}

    def _save_binds(self, data: dict):
        try:
            file_path = self._get_binds_file_path()
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f'保存绑定明文文件失败: {e}')

    async def _get_user_bind_id(self, user_id: str) -> str | None:
        async with self.file_lock:
            binds = self._load_binds()
            user_key = str(user_id)
            if user_key in binds:
                return binds[user_key]
            try:
                old_kv_key = f'bind_{user_id}'
                old_bnet_id = await self.get_kv_data(old_kv_key, None)
                if old_bnet_id:
                    binds[user_key] = str(old_bnet_id)
                    self._save_binds(binds)
                    return old_bnet_id
            except Exception as e:
                logger.error(f'迁移数据失败: {e}')
            return None

    async def _set_user_bind_id(self, user_id: str, bnet_id: str):
        async with self.file_lock:
            binds = self._load_binds()
            binds[str(user_id)] = bnet_id
            self._save_binds(binds)
            try:
                old_kv_key = f'bind_{user_id}'
                await self.delete_kv_data(old_kv_key)
            except Exception:
                pass

    async def _get_bnet_id(self, event: AstrMessageEvent, input_id: str='') -> str:
        if input_id and input_id.strip():
            clean_id = input_id.strip().lstrip('@')
            nickname = self._get_bot_nickname(event)
            if nickname and clean_id.lower().startswith(nickname.lower()):
                clean_id = clean_id[len(nickname):]
            if '#' not in clean_id and '＃' not in clean_id:
                return None
            return clean_id
        user_id = event.get_sender_id()
        bind_id = await self._get_user_bind_id(user_id)
        return bind_id

    def _parse_treemap_args(self, arg1: str='', arg2: str='') -> tuple[str | None, str | None]:
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
        return (bnet_id, season)

    def _parse_profile_args(self, arg1: str='', arg2: str='') -> tuple[str | None, str]:
        bnet_id = None
        mode = 'quick'
        for arg in [arg1, arg2]:
            if not arg:
                continue
            if arg in ['快速', 'quick']:
                mode = 'quick'
            elif arg in ['竞技', 'competitive']:
                mode = 'competitive'
            else:
                bnet_id = arg
        return (bnet_id, mode)

    def _extract_keywords(self, args) -> tuple[list[str], dict]:
        """从位置参数中识别中文关键词，返回 (剩下的位置参数, 命中的字段字典)。
            关键词可无序、可省略、可混用；非关键词一律保留为位置参数。"""
        positional, fields = ([], {})
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
        return (positional, fields)

    # ===================== 事件监听器（必须保留在 main.py） =====================

    @filter.command('大神绑定', alias={'绑定'})
    async def dashen_bind(self, event: AstrMessageEvent, bnet_id: str):
        """绑定战网账号，格式：/绑定 Player#12345。"""
        async for r in features_binding.dashen_bind(self, event, bnet_id):
            yield r

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE)
    async def handle_direct_text_events(self, event: AstrMessageEvent):
        """处理直接发送的战网 ID、单局数字、绑定指令，以及纯@时返回快速指南。

            兼容 QQ 官方机器人（qq_official / qq_official_webhook）与普通 QQ 机器人（aiocqhttp）。
            群聊中必须真实@机器人才会响应（排除 QQ 官方占位@[At:qq_official]，防止误触发），私聊放行。
            需在配置面板开启「是否开启直接消息处理」开关后生效。
            """
        if not bool(self.config.get('direct_message_handling_enabled', False)):
            return
        platform_name = ''
        try:
            platform_name = event.get_platform_name() or ''
        except Exception:
            pass
        if platform_name not in ('qq_official', 'qq_official_webhook', 'aiocqhttp'):
            return
        msg = event.message_str.strip() if event.message_str else ''
        is_group = self._is_group_message(event)
        if is_group and (not self._is_real_at_bot(event)):
            if not (hasattr(event, 'is_at_or_wake_command') and event.is_at_or_wake_command):
                return
        if is_group:
            await self._ensure_group_config_loaded()
        if hasattr(event, 'is_at_or_wake_command') and event.is_at_or_wake_command:
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
        adapt_map = self._ensure_full_adapt_map()
        for cmd_name in sorted(adapt_map.keys(), key=len, reverse=True):
            if cmd_name in ('大神绑定', '绑定'):
                continue
            method = adapt_map[cmd_name]
            for prefix in (f'/{cmd_name}', cmd_name):
                if not msg.startswith(prefix) or len(msg) <= len(prefix):
                    continue
                rest = msg[len(prefix):]
                bnet_m = re.match('^([^#＃\\s]+[#＃]\\d+)$', rest)
                if not bnet_m:
                    continue
                bnet_id = bnet_m.group(1)
                try:
                    params = [p for p in inspect.signature(method).parameters.values() if p.name not in ('self', 'event')]
                except Exception:
                    params = []
                if not params:
                    continue
                args = [bnet_id] + [''] * (len(params) - 1)
                try:
                    async for r in method(event, *args[:len(params)]):
                        yield r
                    event.stop_event()
                    return
                except Exception as e:
                    logger.debug(f'无空格指令派发失败（{cmd_name}）: {e}')
                    return
        bind_match = re.match('^(?:/?(?:大神)?绑定\\s+)?(?:/?(?:大神)?绑定)?\\s*([^#＃\\s]+[#＃]\\d+)$', msg)
        if bind_match:
            clean_bnet_id = bind_match.group(1)
            async for result in self.dashen_bind(event, bnet_id=clean_bnet_id):
                yield result
            event.stop_event()
            return
        if hasattr(event, 'is_at_or_wake_command') and event.is_at_or_wake_command and self.config.get('unmatched_cmd_guide_enabled', False):
            if not is_group or self._is_real_at_bot(event):
                cmd_first_token = msg.split()[0].lstrip('/')
                if cmd_first_token not in self._admin_cmd_set and cmd_first_token not in self._ensure_full_adapt_map():
                    yield event.plain_result(self._get_quick_guide(event, unmatched_cmd=msg))
                    event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def unmatched_command_guide(self, event: AstrMessageEvent):
        """未匹配指令兜底：非 QQ 平台群聊中 @机器人 发送了未识别的用户指令时，返回快速指南。

            QQ 平台已由 handle_direct_text_events / full_adaptation_interceptor 专门处理，
            此处仅覆盖其他平台（如 Telegram、Discord 等）的 @唤醒未匹配场景。
            """
        if not self.config.get('unmatched_cmd_guide_enabled', False):
            return
        if not hasattr(event, 'is_at_or_wake_command') or not event.is_at_or_wake_command:
            return
        platform_name = ''
        try:
            platform_name = event.get_platform_name() or ''
        except Exception:
            pass
        if platform_name in ('qq_official', 'qq_official_webhook', 'aiocqhttp'):
            return
        msg = event.message_str.strip() if event.message_str else ''
        if not msg:
            return
        cmd_first_token = msg.split()[0].lstrip('/')
        if cmd_first_token in self._admin_cmd_set or cmd_first_token in self._ensure_full_adapt_map():
            return
        if cmd_first_token.isdigit() and 0 < int(cmd_first_token) <= 20:
            return
        yield event.plain_result(self._get_quick_guide(event, unmatched_cmd=msg))
        event.stop_event()

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def full_adaptation_interceptor(self, event: AstrMessageEvent):
        """全量消息适配拦截器：群聊中直接发送 table 指令或"@机器人昵称 指令"格式触发对应命令。

            默认关闭，需群管理员通过 /全量适配开（@昵称模式）或 /全量适配开完全匹配（直接匹配模式）开启。
            按群独立配置，持久化到 JSON。
            支持两种模式：
            - 直接匹配模式：消息与 table 中收录的指令名完全匹配即触发，无需 @机器人昵称
            - @昵称模式：需以 "@机器人昵称 指令" 格式发送纯文本消息

            仅处理纯文本（非真实@提及）；真实@仍走框架原生指令派发，避免重复响应。
            兼容 QQ 官方（含 [At:qq_official] 占位与 [At:<openid>] 真实@两种格式）与普通机器人。
            """
        if not self._is_group_message(event):
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        adapt_cfg = self._get_full_adapt(group_id)
        if not adapt_cfg.get('enabled', False):
            return
        if self._is_real_at_bot(event):
            return
        msg = (event.message_str or '').strip()
        direct_mode = adapt_cfg.get('mode', 'nickname') == 'direct'
        if direct_mode:
            if not msg or msg.isdigit():
                return
            cmd_text = msg
            nickname = self._get_bot_nickname(event, adapt_cfg)
            if nickname:
                prefix = f'@{nickname}'
                if cmd_text.startswith(prefix + ' ') or cmd_text.startswith(prefix + '\u3000'):
                    cmd_text = cmd_text[len(prefix):].strip()
                elif cmd_text.startswith(prefix):
                    cmd_text = cmd_text[len(prefix):]
        else:
            nickname = self._get_bot_nickname(event, adapt_cfg)
            if not nickname:
                return
            prefix = f'@{nickname}'
            if not (msg == prefix or msg.startswith(prefix + ' ') or msg.startswith(prefix + '\u3000')):
                return
            cmd_text = msg[len(prefix):].strip()
            if not cmd_text:
                yield event.plain_result(self._get_quick_guide(event))
                event.stop_event()
                return
        if cmd_text.isdigit() and 0 < int(cmd_text) <= 20:
            async for r in self.dashen_match_detail(event, arg1=str(int(cmd_text))):
                yield r
            event.stop_event()
            return
        adapt_map = self._ensure_full_adapt_map()
        for cmd_name in sorted(adapt_map.keys(), key=len, reverse=True):
            if cmd_name in ('大神绑定', '绑定'):
                continue
            method = adapt_map[cmd_name]
            for prefix in (cmd_name,):
                if not cmd_text.startswith(prefix) or len(cmd_text) <= len(prefix):
                    continue
                rest = cmd_text[len(prefix):]
                bnet_m = re.match('^([^#＃\\s]+[#＃]\\d+)$', rest)
                if not bnet_m:
                    continue
                bnet_id = bnet_m.group(1)
                try:
                    params = [p for p in inspect.signature(method).parameters.values() if p.name not in ('self', 'event')]
                except Exception:
                    params = []
                if not params:
                    continue
                args = [bnet_id] + [''] * (len(params) - 1)
                try:
                    async for r in method(event, *args[:len(params)]):
                        yield r
                    event.stop_event()
                    return
                except Exception as e:
                    logger.debug(f'全量适配无空格派发失败（{cmd_name}）: {e}')
                    return
        bind_m = re.match('^(?:/?(?:大神)?绑定\\s+)?(?:/?(?:大神)?绑定)?\\s*([^#＃\\s]+[#＃]\\d+)$', cmd_text)
        if bind_m:
            async for r in self.dashen_bind(event, bnet_id=bind_m.group(1)):
                yield r
            event.stop_event()
            return
        tokens = cmd_text.split()
        cmd = tokens[0].lstrip('/')
        rest = tokens[1:]
        method = self._ensure_full_adapt_map().get(cmd)
        if not method:
            if not direct_mode and self.config.get('unmatched_cmd_guide_enabled', False):
                yield event.plain_result(self._get_quick_guide(event, unmatched_cmd=cmd_text))
                event.stop_event()
            return
        try:
            params = [p for p in inspect.signature(method).parameters.values() if p.name not in ('self', 'event')]
        except Exception:
            params = []
        n_max = len(params)
        args = rest[:n_max]
        args += [''] * (n_max - len(args))
        try:
            async for r in method(event, *args):
                yield r
            event.stop_event()
        except TypeError as e:
            yield self._plain_error_result(event, f'❌ 指令参数不匹配：{e}\n请检查指令用法，可发送 @机器人昵称 获取快速指南。')
            event.stop_event()
        except Exception as e:
            logger.error(f'全量适配派发异常（{cmd}）: {e}')
            yield self._plain_error_result(event, f'❌ 指令执行异常：{e}')
            event.stop_event()

    # ===================== 纯文本 / 轻量指令（保留在 main.py） =====================

    @filter.command('快速指南', alias={'快捷指令'})
    async def quick_guide_command(self, event: AstrMessageEvent):
        """独立快速指南指令"""
        quick_guide = self._get_quick_guide(event)
        yield event.plain_result(quick_guide)

    @filter.command('所有指令', alias={'别称'})
    async def show_aliases(self, event: AstrMessageEvent):
        """展示所有插件指令及其别称"""
        text = """📋 **【Overstats 指令及别称大全】**

🔹 **基础与绑定类：**
• <qqbot-cmd-input text="owhelp " show="owhelp" reference="false" /> (别称：<qqbot-cmd-input text="ow菜单 " show="ow菜单" reference="false" />, <qqbot-cmd-input text="ow帮助 " show="ow帮助" reference="false" />, <qqbot-cmd-input text="OW帮助 " show="OW帮助" reference="false" />, <qqbot-cmd-input text="help " show="help" reference="false" />)
• <qqbot-cmd-input text="所有指令 " show="所有指令" reference="false" /> (别称：<qqbot-cmd-input text="别称 " show="别称" reference="false" />)
• <qqbot-cmd-input text="大神绑定 " show="大神绑定" reference="false" /> (别称：<qqbot-cmd-input text="绑定 " show="绑定" reference="false" />)

🔹 **数据查询类：**
• <qqbot-cmd-input text="大神数据 " show="大神数据" reference="false" /> (别称：<qqbot-cmd-input text="详情卡片 " show="详情卡片" reference="false" />, <qqbot-cmd-input text="战绩查询 " show="战绩查询" reference="false" />, <qqbot-cmd-input text="数据 " show="数据" reference="false" />)
• <qqbot-cmd-input text="大神对局 " show="大神对局" reference="false" /> (别称：<qqbot-cmd-input text="最近对局 " show="最近对局" reference="false" />, <qqbot-cmd-input text="战绩 " show="战绩" reference="false" />, <qqbot-cmd-input text="对局 " show="对局" reference="false" />)
• <qqbot-cmd-input text="单局详细 " show="单局详细" reference="false" /> (别称：<qqbot-cmd-input text="单局详情 " show="单局详情" reference="false" />, <qqbot-cmd-input text="单局 " show="单局" reference="false" />)
• <qqbot-cmd-input text="同玩查询 " show="同玩查询" reference="false" /> (别称：<qqbot-cmd-input text="开黑胜率 " show="开黑胜率" reference="false" />)

🔹 **总结类：**
• <qqbot-cmd-input text="今日总结 " show="今日总结" reference="false" /> (别称：<qqbot-cmd-input text="今日 " show="今日" reference="false" />, <qqbot-cmd-input text="今日数据 " show="今日数据" reference="false" />)
• <qqbot-cmd-input text="昨日总结 " show="昨日总结" reference="false" /> (别称：<qqbot-cmd-input text="昨日 " show="昨日" reference="false" />, <qqbot-cmd-input text="昨日数据 " show="昨日数据" reference="false" />, <qqbot-cmd-input text="昨天数据 " show="昨天数据" reference="false" />)
• <qqbot-cmd-input text="周度总结 " show="周度总结" reference="false" /> (别称：<qqbot-cmd-input text="本周总结 " show="本周总结" reference="false" />, <qqbot-cmd-input text="本周数据 " show="本周数据" reference="false" />, <qqbot-cmd-input text="本周 " show="本周" reference="false" />)

🔹 **图表与排行类：**
• <qqbot-cmd-input text="历史段位 " show="历史段位" reference="false" /> (别称：<qqbot-cmd-input text="历届段位 " show="历届段位" reference="false" />)
• <qqbot-cmd-input text="快速强度 " show="快速强度" reference="false" /> (别称：<qqbot-cmd-input text="快速强度指数 " show="快速强度指数" reference="false" />)
• <qqbot-cmd-input text="竞技强度 " show="竞技强度" reference="false" /> (别称：<qqbot-cmd-input text="竞技强度指数 " show="竞技强度指数" reference="false" />)
• <qqbot-cmd-input text="快速英雄云图 " show="快速英雄云图" reference="false" /> (别称：<qqbot-cmd-input text="快速云图 " show="快速云图" reference="false" />)
• <qqbot-cmd-input text="竞技英雄云图 " show="竞技英雄云图" reference="false" /> (别称：<qqbot-cmd-input text="竞技云图 " show="竞技云图" reference="false" />)
• <qqbot-cmd-input text="省榜 " show="省榜" reference="false" /> (别称：<qqbot-cmd-input text="排行 " show="排行" reference="false" />)
• <qqbot-cmd-input text="绝活榜 " show="绝活榜" reference="false" /> (别称：<qqbot-cmd-input text="英雄省榜 " show="英雄省榜" reference="false" />)
• <qqbot-cmd-input text="banpick " show="banpick" reference="false" /> (别称：<qqbot-cmd-input text="全英雄排行 " show="全英雄排行" reference="false" />)

🔹 **游戏资讯类：**
• <qqbot-cmd-input text="威能 " show="威能" reference="false" />, <qqbot-cmd-input text="ow英雄 " show="ow英雄" reference="false" />, <qqbot-cmd-input text="获取段位分布 " show="获取段位分布" reference="false" />, <qqbot-cmd-input text="mappick " show="mappick" reference="false" />, <qqbot-cmd-input text="皮肤搜索 " show="皮肤搜索" reference="false" />
• <qqbot-cmd-input text="商店 " show="商店" reference="false" /> (别称：<qqbot-cmd-input text="ow商店 " show="ow商店" reference="false" />)
• <qqbot-cmd-input text="ow赛事 " show="ow赛事" reference="false" /> (别称：<qqbot-cmd-input text="赛事 " show="赛事" reference="false" />)
• <qqbot-cmd-input text="ow活动 " show="ow活动" reference="false" /> (别称：<qqbot-cmd-input text="活动 " show="活动" reference="false" />)
• <qqbot-cmd-input text="ow更新 " show="ow更新" reference="false" /> (别称：<qqbot-cmd-input text="版本更新 " show="版本更新" reference="false" />)"""

        yield event.plain_result(self._format_markdown_by_platform(event, text))


    @filter.command('多图测试')
    async def multi_image_test(self, event: AstrMessageEvent):
        """多图测试：发送多张测试图片。"""
        imgs_list = []
        for img_name in ['test1.png', 'test2.png', 'test3.png']:
            img_path = self.plugin_data_dir / img_name
            if img_path.exists():
                try:
                    with open(img_path, 'rb') as f:
                        imgs_list.append(f.read())
                except Exception as e:
                    logger.error(f'读取测试图片 {img_name} 失败: {e}')
        if not imgs_list:
            yield self._plain_error_result(event, '❌ 未能读取到测试图片。')
            return
        async for r in self._send_multiple_images_result(event, imgs_list, '多图测试'):
            yield r

    @filter.command('单图测试')
    async def single_image_test(self, event: AstrMessageEvent):
        """单图测试：发送单张 test1.png 测试图片。"""
        img_path = self.plugin_data_dir / 'test1.png'
        if not img_path.exists():
            yield self._plain_error_result(event, '❌ 未能读取到测试图片 test1.png。')
            return
        try:
            with open(img_path, 'rb') as f:
                img_bytes = f.read()
        except Exception as e:
            logger.error(f'读取测试图片 test1.png 失败: {e}')
            yield self._plain_error_result(event, '❌ 读取测试图片失败。')
            return
        async for r in self._send_image_result(event, img_bytes, '单图测试'):
            yield r

    @filter.command('owhelp', alias={'ow菜单', 'ow帮助', 'OW帮助', 'help'})
    async def ow_help(self, event: AstrMessageEvent):
        """显示 Overstats 查询菜单，列出所有常用指令入口。"""
        help_text = """📌 Overstats 查询菜单
🔗 ➤ <qqbot-cmd-input text="绑定 " show="绑定" reference="false" />示例：/绑定 Player#12345
📋 ➤ <qqbot-cmd-input text="今日总结 " show="今日" reference="false" /> ➤ <qqbot-cmd-input text="昨日总结 " show="昨日" reference="false" /> ➤ <qqbot-cmd-input text="周度总结 " show="本周" reference="false" />
📊 ➤ <qqbot-cmd-input text="大神数据 " show="大神数据" reference="false" /> ➤ <qqbot-cmd-input text="大神对局 " show="大神对局" reference="false" /> ➤ <qqbot-cmd-input text="单局详细 " show="单局详细" reference="false" />数字 锐评关/全员关
📈 ➤ <qqbot-cmd-input text="快速强度 " show="快速强度" reference="false" />可选对局数 ➤ <qqbot-cmd-input text="竞技强度 " show="竞技强度" reference="false" />可选对局数 ➤ <qqbot-cmd-input text="获取段位分布 " show="获取段位分布" reference="false" />可选 快速/竞技 段位
🗺️ ➤ <qqbot-cmd-input text="快速英雄云图 " show="快速云图" reference="false" /> ➤ <qqbot-cmd-input text="竞技英雄云图 " show="竞技云图" reference="false" /> ➤ <qqbot-cmd-input text="历史段位 " show="历史段位" reference="false" />
🏆 ➤ <qqbot-cmd-input text="省榜 " show="省榜" reference="false" />省份 位置 ➤ <qqbot-cmd-input text="绝活榜 " show="绝活榜" reference="false" />省份 英雄 开放
⚔️ ➤ <qqbot-cmd-input text="威能 " show="威能" reference="false" />英雄名 ➤ <qqbot-cmd-input text="ow英雄 " show="ow 英雄" reference="false" />英雄名 快速/竞技 段位 ➤ <qqbot-cmd-input text="banpick " show="banpick" reference="false" />可选 快速/竞技 段位 ➤ <qqbot-cmd-input text="mappick " show="mappick" reference="false" />
🌍 ➤ <qqbot-cmd-input text="同玩查询 " show="同玩查询" reference="false" />ID1 ID2 ➤ <qqbot-cmd-input text="商店 " show="商店" reference="false" /> ➤ <qqbot-cmd-input text="皮肤搜索 " show="皮肤搜索" reference="false" /> ➤ <qqbot-cmd-input text="ow赛事 " show="ow 赛事" reference="false" />
📰 ➤ <qqbot-cmd-input text="ow更新 " show="ow更新" reference="false" />latest/small/big
🧪 ➤ <qqbot-cmd-input text="ow是区吗 " show="ow是区吗" reference="false" />战网ID ➤ <qqbot-cmd-input text="ow是区吗结果 " show="ow是区吗结果" reference="false" />
🎁 ➤ 段位关键词：青铜/白银/黄金/白金/钻石/大师/宗师/英杰
💡 大部分指令都可以带 战网id 参数，查询对应的数据，无需重新绑定
💡 发送 <qqbot-cmd-input text="别称 " show="别称" reference="false" /> 可查看所有指令对应别称列表。"""

        yield event.plain_result(self._format_markdown_by_platform(event, help_text))


    @filter.command('今日总结', alias={'今日', '今日数据'})
    async def dashen_today(self, event: AstrMessageEvent, bnet_id: str = ''):
        """生成过去 24 小时内的对局大数据总结卡片。"""
        async for r in features_summary.dashen_today(self, event, bnet_id):
            yield r

    @filter.command('昨日总结', alias={'昨日', '昨日数据', '昨天数据'})
    async def dashen_yesterday(self, event: AstrMessageEvent, bnet_id: str = '', _skip_status_prompt: bool = False):
        """统计并生成昨日战绩数据卡片。"""
        async for r in features_summary.dashen_yesterday(self, event, bnet_id, _skip_status_prompt):
            yield r

    @filter.command('周度总结', alias={'本周总结', '本周数据', '本周'})
    async def dashen_week(self, event: AstrMessageEvent, bnet_id: str = ''):
        """统计本周战绩大数据总结，耗时较长（约 30-60 秒）。"""
        async for r in features_summary.dashen_week(self, event, bnet_id):
            yield r

    @filter.command('大神数据', alias={'详情卡片', '战绩查询', '数据'})
    async def dashen_profile(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """查看玩家详情卡片（支持 快速/竞技 模式）。"""
        async for r in features_players.dashen_profile(self, event, arg1, arg2):
            yield r

    @filter.command('大神对局', alias={'最近对局', '战绩', '对局'})
    async def dashen_match(self, event: AstrMessageEvent, bnet_id: str = ''):
        """拉取最近 20 局的对局列表。"""
        async for r in features_players.dashen_match(self, event, bnet_id):
            yield r

    @filter.command('单局详细', alias={'单局', '单局详情'})
    async def dashen_match_detail(self, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
        """查看指定序号的单局多图详细战绩（可加 锐评关/全员关 控制开关）。"""
        async for r in features_players.dashen_match_detail(self, event, arg1, arg2, arg3):
            yield r

    @filter.command('历史段位', alias={'历届段位'})
    async def dashen_rank_history(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """追溯玩家的历史天梯段位记录（可选赛季范围）。"""
        async for r in features_players.dashen_rank_history(self, event, arg1, arg2):
            yield r

    @filter.command('同玩查询', alias={'开黑胜率'})
    async def dashen_sameplay(self, event: AstrMessageEvent, p1: str = '', p2: str = ''):
        """深度分析两位玩家一同游玩开黑时的战绩与胜率。"""
        async for r in features_players.dashen_sameplay(self, event, p1, p2):
            yield r

    @filter.command('快速强度', alias={'快速强度指数'})
    async def quick_strength(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """评估玩家快速模式下的强度指数（可选对局数 3-12）。"""
        async for r in features_charts.quick_strength(self, event, arg1, arg2):
            yield r

    @filter.command('竞技强度', alias={'竞技强度指数'})
    async def competitive_strength(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """评估玩家竞技天梯模式下的强度指数（可选对局数 3-12）。"""
        async for r in features_charts.competitive_strength(self, event, arg1, arg2):
            yield r

    @filter.command('快速英雄云图', alias={'快速云图'})
    async def quick_hero_treemap(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """获取快速模式英雄使用率矩形树图（可选赛季）。"""
        async for r in features_charts.quick_hero_treemap(self, event, arg1, arg2):
            yield r

    @filter.command('竞技英雄云图', alias={'竞技云图'})
    async def competitive_hero_treemap(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """获取竞技模式英雄使用率矩形树图（可选赛季）。"""
        async for r in features_charts.competitive_hero_treemap(self, event, arg1, arg2):
            yield r

    @filter.command('威能')
    async def ow_hero_perk(self, event: AstrMessageEvent, hero_name: str):
        """提取指定英雄的核心威能、机制数据图。"""
        async for r in features_charts.ow_hero_perk(self, event, hero_name):
            yield r

    @filter.command('ow英雄')
    async def ow_hero_pick(self, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
        """读取指定英雄在当前天梯的 Pick 率历史走势图（可选模式/段位）。"""
        async for r in features_charts.ow_hero_pick(self, event, arg1, arg2, arg3):
            yield r

    @filter.command('省榜', alias={'排行'})
    async def ow_rank_leaderboard(self, event: AstrMessageEvent, province: str, role: str):
        """获取指定地区的大神天梯省榜（位置：tank / dps / healer / open）。"""
        async for r in features_info.ow_rank_leaderboard(self, event, province, role):
            yield r

    @filter.command('绝活榜', alias={'英雄省榜'})
    async def ow_hero_leaderboard(self, event: AstrMessageEvent, province: str, hero: str, arg3: str = ''):
        """获取指定地区特定英雄的大神专精绝活榜（可选开放队列模式）。"""
        async for r in features_info.ow_hero_leaderboard(self, event, province, hero, arg3):
            yield r

    @filter.command('商店', alias={'ow商店'})
    async def ow_shop(self, event: AstrMessageEvent):
        """拉取今日精选商店在售皮肤商品。"""
        async for r in features_info.ow_shop(self, event):
            yield r

    @filter.command('ow赛事', alias={'赛事'})
    async def ow_esports(self, event: AstrMessageEvent):
        """获取实时职业赛事对阵及赛程信息。"""
        async for r in features_info.ow_esports(self, event):
            yield r

    @filter.command('获取段位分布')
    async def get_rank_distribution(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """统计天梯全服大盘全英雄数据排行与天梯环境分布（可选模式/段位）。"""
        async for r in features_charts.get_rank_distribution(self, event, arg1, arg2):
            yield r

    @filter.command('ow活动', alias={'活动'})
    async def ow_activities(self, event: AstrMessageEvent):
        """拉取当前版本限时节日或赛季大活动公告卡片。"""
        async for r in features_info.ow_activities(self, event):
            yield r

    @filter.command('banpick', alias={'全英雄排行'})
    async def ban_pick_stats(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """获取本周天梯英雄大盘的选禁用排行（可选模式/段位）。"""
        async for r in features_info.ban_pick_stats(self, event, arg1, arg2):
            yield r

    @filter.command('mappick')
    async def map_pick_stats(self, event: AstrMessageEvent):
        """从最新补丁中检索当前赛季地图池与轮换出场情况。"""
        async for r in features_info.map_pick_stats(self, event):
            yield r

    @filter.command('皮肤搜索')
    async def skin_search(self, event: AstrMessageEvent, keyword: str = ''):
        """检索包含指定关键词的精选上架皮肤商品卡片。"""
        async for r in features_info.skin_search(self, event, keyword):
            yield r

    @filter.command('ow更新', alias={'版本更新'})
    async def ow_patch_notes(self, event: AstrMessageEvent, kind: str = 'latest'):
        """拉取外服更新日志卡片（参数：latest / small / big）。"""
        async for r in features_info.ow_patch_notes(self, event, kind):
            yield r

    @filter.command('ow开庭', alias={'开庭'})
    async def ow_court(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """OW 开庭：AI 对单局数据进行电竞法庭风格分析（测试阶段，仅白名单/管理员可用）。"""
        async for r in features_court.ow_court(self, event, arg1, arg2):
            yield r

    @filter.command('ow是区吗', alias={'是区吗'})
    async def ow_shiqu(self, event: AstrMessageEvent, arg1: str = '', arg2: str = '', arg3: str = ''):
        """OW 是区吗：展示上次判定结果。5 分钟内再次发送确认后开启新查询（分级 CD）。可加局数 1~25。"""
        async for r in features_court.ow_shiqu(self, event, arg1, arg2, arg3):
            yield r

    @filter.command('ow是区吗结果', alias={'是区吗结果'})
    async def ow_shiqu_result(self, event: AstrMessageEvent):
        """OW 是区吗结果：返回上次生成的判定书图片。"""
        async for r in features_court.ow_shiqu_result(self, event):
            yield r

    @filter.command('owAI检测', alias={'AI检测'})
    async def ow_ai_test(self, event: AstrMessageEvent):
        """测试是区吗 LLM API 连通性。"""
        async for r in features_court.ow_ai_test(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('维护')
    async def maintenance_cmd(self, event: AstrMessageEvent, action: str = ''):
        """设置或取消维护模式（仅 AstrBot 管理员可用）"""
        async for r in features_admin.maintenance_cmd(self, event, action):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow违禁封禁')
    async def violation_ban_cmd(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """管理员封禁 用户+指令（12小时）。用法：/ow违禁封禁 <用户ID> <指令名>"""
        async for r in features_admin.violation_ban_cmd(self, event, arg1, arg2):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow违禁解封')
    async def violation_unban_cmd(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        """管理员解除 用户+指令 封禁。用法：/ow违禁解封 <用户ID> <指令名>"""
        async for r in features_admin.violation_unban_cmd(self, event, arg1, arg2):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow连接测试')
    async def connection_test_cmd(self, event: AstrMessageEvent):
        """测试 Overstats 后端连接（仅 AstrBot 管理员可用）"""
        async for r in features_admin.connection_test_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow部署')
    async def ow_deploy_cmd(self, event: AstrMessageEvent):
        """一键部署 Overstats 后端（仅 AstrBot 管理员可用，auto 模式生效）"""
        async for r in features_admin.ow_deploy_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow部署状态')
    async def ow_deploy_status_cmd(self, event: AstrMessageEvent):
        """查看后端部署状态（仅 AstrBot 管理员可用）"""
        async for r in features_admin.ow_deploy_status_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow更新后端')
    async def ow_update_backend_cmd(self, event: AstrMessageEvent):
        """更新后端代码并重启（仅 AstrBot 管理员可用，auto 模式生效）"""
        async for r in features_admin.ow_update_backend_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow停止后端')
    async def ow_stop_backend_cmd(self, event: AstrMessageEvent):
        """停止后端进程（仅 AstrBot 管理员可用，auto 模式生效）"""
        async for r in features_admin.ow_stop_backend_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow重启后端')
    async def ow_restart_backend_cmd(self, event: AstrMessageEvent):
        """重启后端进程（仅 AstrBot 管理员可用，auto 模式生效）"""
        async for r in features_admin.ow_restart_backend_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow部署日志')
    async def ow_deploy_logs_cmd(self, event: AstrMessageEvent):
        """查看后端运行日志（仅 AstrBot 管理员可用，auto 模式生效）"""
        async for r in features_admin.ow_deploy_logs_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow后端日志')
    async def ow_backend_logs_cmd(self, event: AstrMessageEvent):
        """查看 Overstats 后端持久化日志文件（仅 AstrBot 管理员可用，auto 模式生效）。"""
        async for r in features_admin.ow_backend_logs_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow卸载后端')
    async def ow_uninstall_backend_cmd(self, event: AstrMessageEvent):
        """卸载 Overstats 后端（仅 AstrBot 管理员可用，auto 模式生效）。"""
        async for r in features_admin.ow_uninstall_backend_cmd(self, event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow卸载后端执行确认')
    async def ow_uninstall_backend_confirm(self, event: AstrMessageEvent):
        """删除全部后端资源（代码+venv+数据库）。"""
        async for r in features_admin.uninstall_exec(self, event, delete_code=True, delete_venv=True, force=False):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow卸载后端执行仅代码')
    async def ow_uninstall_backend_code_only(self, event: AstrMessageEvent):
        """仅删除后端代码目录（含数据库），保留虚拟环境。"""
        async for r in features_admin.uninstall_exec(self, event, delete_code=True, delete_venv=False, force=False):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow卸载后端执行仅venv')
    async def ow_uninstall_backend_venv_only(self, event: AstrMessageEvent):
        """仅删除虚拟环境，保留后端代码。"""
        async for r in features_admin.uninstall_exec(self, event, delete_code=False, delete_venv=True, force=False):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ow卸载后端执行强制')
    async def ow_uninstall_backend_force(self, event: AstrMessageEvent):
        """强制删除全部后端资源（进程无法正常停止时使用）。"""
        async for r in features_admin.uninstall_exec(self, event, delete_code=True, delete_venv=True, force=True):
            yield r

    @filter.command('群设置')
    async def group_config_cmd(self, event: AstrMessageEvent, action: str = '', value: str = ''):
        """查看/切换群组功能配置。"""
        async for r in features_config.group_config_cmd(self, event, action, value):
            yield r

    @filter.command('全量适配开')
    async def full_adapt_enable(self, event: AstrMessageEvent, nickname: str = ''):
        """开启本群的全量消息适配 — @昵称模式（群管理员专属）。"""
        async for r in features_config.full_adapt_enable(self, event, nickname):
            yield r

    @filter.command('全量适配开完全匹配')
    async def full_adapt_enable_direct(self, event: AstrMessageEvent):
        """开启本群的全量消息适配 — 直接匹配模式（群管理员专属）。"""
        async for r in features_config.full_adapt_enable_direct(self, event):
            yield r

    @filter.command('全量适配关')
    async def full_adapt_disable(self, event: AstrMessageEvent):
        """关闭本群的全量消息适配（群管理员专属）。"""
        async for r in features_config.full_adapt_disable(self, event):
            yield r

    @filter.command('管理')
    async def admin_menu(self, event: AstrMessageEvent):
        """管理员维护指令菜单（仅 Bot 管理员或群主/群管理员可用）"""
        if not self._is_group_admin(event):
            yield event.plain_result("⚠️ 此命令仅限 Bot 管理员或群主/群管理员使用")
            return

        text = """🛠️ 管理员维护指令菜单

🔧 **维护管理：**
• <qqbot-cmd-input text="维护 " show="维护 内容" reference="false" /> 开启维护模式（如：维护 服务升级中，暂停服务）
• <qqbot-cmd-input text="维护 取消 " show="维护 取消" reference="false" /> 关闭维护模式

⛔ **违规封禁管理：**
• <qqbot-cmd-input text="ow违禁封禁 " show="ow违禁封禁 用户ID 指令名" reference="false" /> 封禁用户指定指令12h（如：ow违禁封禁 qqofficial:1170599013 单局详细）
• <qqbot-cmd-input text="ow违禁解封 " show="ow违禁解封 用户ID 指令名" reference="false" /> 解除用户指定指令封禁

⚙️ **群组配置：**
• <qqbot-cmd-input text="群设置 " show="群设置" reference="false" /> 查看当前群组功能配置
• <qqbot-cmd-input text="群设置 提示 开 " show="群设置 提示 开" reference="false" /> / <qqbot-cmd-input text="群设置 提示 关 " show="群设置 提示 关" reference="false" /> 切换首次提示后不再提示
• <qqbot-cmd-input text="群设置 追加提示 开 " show="群设置 追加提示 开" reference="false" /> / <qqbot-cmd-input text="群设置 追加提示 关 " show="群设置 追加提示 关" reference="false" /> 切换追加交互提示
• <qqbot-cmd-input text="全量适配开 " show="全量适配开" reference="false" /> [昵称] / <qqbot-cmd-input text="全量适配开完全匹配 " show="全量适配开完全匹配" reference="false" /> / <qqbot-cmd-input text="全量适配关 " show="全量适配关" reference="false" /> 全量适配（@昵称/直接匹配/关闭，按群独立）

🔌 **系统诊断（所有模式通用）：**
• <qqbot-cmd-input text="ow连接测试 " show="ow连接测试" reference="false" /> 测试 Overstats 后端连接状态
• <qqbot-cmd-input text="ow部署状态 " show="ow部署状态" reference="false" /> 查看后端接入模式与运行状态

🚀 **一键部署（仅 auto 托管模式）：**
• <qqbot-cmd-input text="ow部署 " show="ow部署" reference="false" /> 一键部署后端（首次使用必执行）
• <qqbot-cmd-input text="ow更新后端 " show="ow更新后端" reference="false" /> 拉取最新代码并重启（跟进原项目更新）
• <qqbot-cmd-input text="ow停止后端 " show="ow停止后端" reference="false" /> 停止后端进程
• <qqbot-cmd-input text="ow重启后端 " show="ow重启后端" reference="false" /> 重启后端（修改配置后需执行）
• <qqbot-cmd-input text="ow部署日志 " show="ow部署日志" reference="false" /> 查看后端运行日志（内存，最近50行）
• <qqbot-cmd-input text="ow后端日志 " show="ow后端日志" reference="false" /> 查看 Overstats 后端持久化日志（文件，最新30条）

🗑️ **后端卸载（仅 auto 托管模式）：**
• <qqbot-cmd-input text="ow卸载后端 " show="ow卸载后端" reference="false" /> 预览卸载影响（空间/数据库/进程）
• <qqbot-cmd-input text="ow卸载后端执行确认 " show="ow卸载后端执行确认" reference="false" /> 删除全部后端资源（代码+venv+数据库）
• <qqbot-cmd-input text="ow卸载后端执行仅代码 " show="ow卸载后端执行仅代码" reference="false" /> 仅删除后端代码（保留venv）
• <qqbot-cmd-input text="ow卸载后端执行仅venv " show="ow卸载后端执行仅venv" reference="false" /> 仅删除虚拟环境（保留代码）
• <qqbot-cmd-input text="ow卸载后端执行强制 " show="ow卸载后端执行强制" reference="false" /> 强制删除（进程无法停止时使用）

💡 维护模式开启后，所有指令将直接返回维护内容，不再执行业务逻辑。
💡 一键部署指令需在配置面板切换为 auto 模式后使用，配置修改后需重载插件生效。
💡 后端卸载与插件卸载相互独立，卸载后端不影响插件其他功能（manual 模式仍可用）。"""
        yield event.plain_result(self._format_markdown_by_platform(event, text))


    def _register_monitor_apis(self):
        """注册监控面板的 10 个 Web API 端点。"""
        P = 'astrbot_plugin_overstats'
        ctx = self.context
        ctx.register_web_api(f'/{P}/monitor/overview', self._api_monitor_overview, ['GET'], '监控总览')
        ctx.register_web_api(f'/{P}/monitor/commands', self._api_monitor_commands, ['GET'], '指令统计')
        ctx.register_web_api(f'/{P}/monitor/commands/failures', self._api_monitor_cmd_failures, ['GET'], '指令失败原因')
        ctx.register_web_api(f'/{P}/monitor/trend', self._api_monitor_trend, ['GET'], '日趋势')
        ctx.register_web_api(f'/{P}/monitor/hourly', self._api_monitor_hourly, ['GET'], '时段分布')
        ctx.register_web_api(f'/{P}/monitor/errors', self._api_monitor_errors, ['GET'], '错误日志')
        ctx.register_web_api(f'/{P}/monitor/deploy', self._api_monitor_deploy, ['GET'], '部署状态')
        ctx.register_web_api(f'/{P}/monitor/errors/stream', self._api_monitor_errors_stream, ['GET'], 'SSE 错误流')
        ctx.register_web_api(f'/{P}/monitor/backend/perf', self._api_monitor_backend_perf, ['GET'], '后端性能')
        ctx.register_web_api(f'/{P}/monitor/backend/upstream', self._api_monitor_backend_upstream, ['GET'], '上游统计')
        ctx.register_web_api(f'/{P}/monitor/shiqu/calls', self._api_monitor_shiqu_calls, ['GET'], '是区吗调用日志')
        ctx.register_web_api(f'/{P}/monitor/rate_limit', self._api_monitor_rate_limit, ['GET'], '限流统计')
        ctx.register_web_api(f'/{P}/monitor/clear', self._api_monitor_clear, ['POST'], '清空统计')

    async def _api_monitor_overview(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        start = request.query.get('start', '', type=str)
        end = request.query.get('end', '', type=str)
        data = await self.monitor.get_overview(start=start or None, end=end or None)
        dm = self.deploy_manager
        try:
            status = dm.status()
        except Exception:
            status = None
        data['deploy'] = {'mode': dm.mode, 'state': status.state if status else 'unknown', 'process_alive': status.process_alive if status else False, 'backend_port': status.backend_port if status else 0, 'pid': status.process_pid if status else None, 'git_commit': status.git_commit if status else 'unknown', 'last_deploy_time': status.last_deploy_time if status else 0, 'last_error': status.last_error if status else ''} if status else {'mode': dm.mode, 'state': 'unknown'}
        rl_stats = await self.monitor.get_rate_limit_stats()
        data['rate_limit'] = rl_stats
        data['rate_limit_config'] = {'cmd_enabled': getattr(self, '_rate_limit_enabled', False), 'cmd_max': getattr(self, '_rate_limit_max', 3), 'llm_enabled': getattr(self, '_llm_rate_limit_enabled', False), 'llm_per_minute': getattr(self, '_llm_rate_limit_per_minute', 10)}
        return json_response(data)

    async def _api_monitor_commands(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        category = request.query.get('category', '', type=str)
        search = request.query.get('search', '', type=str)
        start = request.query.get('start', '', type=str)
        end = request.query.get('end', '', type=str)
        data = await self.monitor.get_cmd_stats(category=category, search=search, start=start or None, end=end or None)
        return json_response(data)

    async def _api_monitor_cmd_failures(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        cmd = request.query.get('cmd', '', type=str)
        start = request.query.get('start', '', type=str)
        end = request.query.get('end', '', type=str)
        if not cmd:
            return json_response({'error': '缺少 cmd 参数'})
        reasons = await self.monitor.get_cmd_failure_reasons(cmd, start=start or None, end=end or None)
        return json_response({'cmd': cmd, 'reasons': reasons})

    async def _api_monitor_trend(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        cmd = request.query.get('cmd', '', type=str)
        days = request.query.get('days', 7, type=int)
        data = await self.monitor.get_cmd_trend(cmd_name=cmd, days=max(1, min(90, days)))
        return json_response(data)

    async def _api_monitor_hourly(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        date = request.query.get('date', '', type=str)
        data = await self.monitor.get_hourly_distribution(date=date)
        return json_response(data)

    async def _api_monitor_errors(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        limit = request.query.get('limit', 50, type=int)
        offset = request.query.get('offset', 0, type=int)
        level = request.query.get('level', '', type=str)
        rows, total = await self.monitor.get_errors(limit=min(limit, 200), offset=offset, level=level)
        return json_response({'rows': rows, 'total': total})

    async def _api_monitor_deploy(self):
        dm = self.deploy_manager
        try:
            status = dm.status()
        except Exception:
            return json_response({'error': '获取部署状态失败'})
        return json_response({'mode': dm.mode, 'state': status.state, 'backend_dir': str(status.backend_dir) if status.backend_dir else '', 'backend_port': status.backend_port, 'backend_host': status.backend_host, 'git_commit': status.git_commit, 'process_alive': status.process_alive, 'process_pid': status.process_pid, 'last_deploy_time': status.last_deploy_time, 'last_error': status.last_error})

    async def _api_monitor_errors_stream(self):
        if not self.monitor_sse:
            return error_response('监控未初始化', status_code=503)

        async def stream():
            if self.monitor:
                existing = await self.monitor.get_all_errors()
                import json as _json
                yield f'event: init\ndata: {_json.dumps(existing[-100:], ensure_ascii=False)}\n\n'
            async for event in self.monitor_sse.subscribe(poll_timeout=5.0):
                import json as _json
                if event.get('_heartbeat'):
                    yield ': heartbeat\n\n'
                else:
                    yield f'data: {_json.dumps(event, ensure_ascii=False)}\n\n'
        return stream_response(stream())

    async def _api_monitor_backend_perf(self):
        db_path = getattr(self, '_req_metrics_db_path', None)
        if not db_path or not str(db_path):
            hint = '未配置 sqlite_db_path' if self.deploy_manager.mode == 'manual' else 'auto 模式未找到 overstats_backend'
            expected = str(self.plugin_data_dir / 'overstats_backend' / 'src' / 'db' / 'request_metrics.sqlite3')
            return json_response({'available': False, 'data': [], 'hint': hint, 'expected_path': expected})
        endpoint = request.query.get('endpoint', '', type=str)
        hours = request.query.get('hours', 24, type=int)
        reader = getattr(self, '_backend_metrics_reader', None) or BackendMetricsReader()
        perf = reader.get_endpoint_perf_stats(str(db_path), endpoint=endpoint, time_range_hours=hours)
        slow = reader.get_slow_endpoints(str(db_path), time_range_hours=hours)
        info = reader.get_db_info(str(db_path))
        return json_response({'available': True, 'perf': perf, 'slow': slow, 'db_info': info})

    async def _api_monitor_backend_upstream(self):
        db_path = getattr(self, '_req_metrics_db_path', None)
        if not db_path or not str(db_path):
            return json_response({'available': False, 'data': [], 'table_exists': False})
        limit = request.query.get('limit', 30, type=int)
        reader = getattr(self, '_backend_metrics_reader', None) or BackendMetricsReader()
        result = reader.get_upstream_stats(str(db_path), limit=limit)
        return json_response({'available': True, **result})

    async def _api_monitor_rate_limit(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        stats = await self.monitor.get_rate_limit_stats()
        return json_response({'stats': stats, 'config': {'cmd_enabled': getattr(self, '_rate_limit_enabled', False), 'cmd_max': getattr(self, '_rate_limit_max', 3), 'llm_enabled': getattr(self, '_llm_rate_limit_enabled', False), 'llm_per_minute': getattr(self, '_llm_rate_limit_per_minute', 10)}})

    async def _api_monitor_clear(self):
        if not self.monitor:
            return json_response({'error': '监控未初始化'})
        deleted = await self.monitor.clear_all_stats()
        if self.monitor_sse:
            pass
        return json_response({'deleted': deleted, 'message': f'已清空 {deleted} 条记录'})

    async def _api_monitor_shiqu_calls(self):
        """GET /monitor/shiqu/calls?limit=30&offset=0&openid=&target_id=&success=&verdict=&search="""
        log_dir = self.plugin_data_dir / 'shiqu'
        limit = request.query.get('limit', 30, type=int)
        offset = request.query.get('offset', 0, type=int)
        openid = request.query.get('openid', '', type=str)
        target_id = request.query.get('target_id', '', type=str)
        success_str = request.query.get('success', '')
        verdict = request.query.get('verdict', '', type=str)
        search = request.query.get('search', '', type=str)
        success = None
        if success_str.lower() in ('true', '1', 'yes'):
            success = True
        elif success_str.lower() in ('false', '0', 'no'):
            success = False
        reader = ShiquCallReader()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, reader.query, log_dir, limit, offset, openid, target_id, success, verdict, search)
        return json_response(result)
