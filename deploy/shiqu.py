"""OW 是区吗功能模块（测试阶段）

调用 Overstats API 获取玩家最近 12 场对局，由 LLM 评估该玩家是否为"坑"。
输出渲染为 PNG 图片返回。

限流：全局限 10 并发，用户间排队，管理员不受限。
阶段：查数据 → LLM 生成 → 渲染成图
"""

import asyncio
import json
import re
import time
import logging
from logging.handlers import RotatingFileHandler
from collections import deque
from pathlib import Path
from typing import Optional

import aiohttp
from astrbot.api.message_components import Image

logger = logging.getLogger("astrbot")

try:
    from .stat_db import build_broad_reference_text, load_stat_name_map, normalize_stat_value, should_skip_prompt_stat
except ImportError:
    from stat_db import build_broad_reference_text, load_stat_name_map, normalize_stat_value, should_skip_prompt_stat  # type: ignore[no-redef]

# 从 query_tool.json 加载游戏数据
_QTOOL = json.loads((Path(__file__).resolve().parent / "query_tool.json").read_text("utf-8"))
HERO_DICT = {h["heroGuid"]: {"name": h["name"], "role": h["roleType"]} for h in _QTOOL["heroList"]}
MAP_DICT = {m["guid"]: m["name"] for m in _QTOOL["mapList"]}
_HERO_ATTR_GUIDS: dict[str, set[str]] = {}
_HERO_SPECIAL_ATTR_GUIDS: dict[str, set[str]] = {}
_GENERAL_ATTR_GUIDS: set[str] = set()
for _attr in _QTOOL.get("heroAttrList", []):
    _hg = str(_attr.get("heroGuid", ""))
    _vg = str(_attr.get("valueGuid", ""))
    if not _hg or not _vg:
        continue
    _HERO_ATTR_GUIDS.setdefault(_hg, set()).add(_vg)
    if _attr.get("valueType") == "通用数据":
        _GENERAL_ATTR_GUIDS.add(_vg)
    else:
        _HERO_SPECIAL_ATTR_GUIDS.setdefault(_hg, set()).add(_vg)


def _stat_allowed_for_hero(value_guid: str, hero_guid: str) -> bool:
    return value_guid in _GENERAL_ATTR_GUIDS or value_guid in _HERO_ATTR_GUIDS.get(hero_guid, set())


def _infer_hero_guid_from_stat_map(stat_map: dict, fallback_hero_guid: str = "", *, allow_fallback: bool = False) -> str:
    stat_guids = {str(g) for g in (stat_map or {}).keys()}
    scores = []
    for hero_guid, special_guids in _HERO_SPECIAL_ATTR_GUIDS.items():
        hits = len(stat_guids & special_guids)
        if hits > 0:
            scores.append((hits, hero_guid))
    if scores:
        best = max(hit for hit, _ in scores)
        winners = [hero_guid for hit, hero_guid in scores if hit == best]
        if fallback_hero_guid in winners:
            return fallback_hero_guid
        if len(winners) == 1:
            return winners[0]
        return ""
    return fallback_hero_guid if allow_fallback else ""

# ponytail: 移除 verdict 字段（顶层 + teammate_comments），由 score 经 _score_rule 反推。
# 原 schema 同时让 LLM 输出 score 和 verdict，但 _normalize_result 仍按 score 重算 verdict，
# 等于让模型多背一个 enum 约束却丢弃其输出，徒增 JSON 解析失败概率。
_SHIQU_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target_id", "score", "summary", "match_comments", "overall_comment", "teammate_comments"],
    "properties": {
        "target_id": {"type": "string"},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string", "minLength": 1},
        "match_comments": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "result", "hero", "comment"],
                "properties": {
                    "index": {"type": "integer", "minimum": 1},
                    "result": {"type": "string", "enum": ["胜", "负", "平", "未知"]},
                    "hero": {"type": "string"},
                    "comment": {"type": "string", "minLength": 1},
                },
            },
        },
        "overall_comment": {"type": "string", "minLength": 1},
        "teammate_comments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "games", "score", "comment"],
                "properties": {
                    "name": {"type": "string"},
                    "games": {"type": "integer", "minimum": 1},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "comment": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_MAX_CONCURRENT = 10
_QUEUE: deque = deque()
_ACTIVE: set = set()
_ACTIVE_META: dict[str, str] = {}  # uid → 当前步骤描述，用于重复触发时告知进度
_LAST_IMAGE: dict[str, str] = {}   # uid → 上次生成的图片文件路径（进程内加速用，持久记录以 JSON 为准）
_QUEUE_LOCK = asyncio.Lock()
_LLM_SEMAPHORE = asyncio.Semaphore(2)  # ponytail: LLM API 并发上限，防上游限流

_VERDICT_RULES = [
    {"score_min": 83, "labels": ("你是职业吗？",), "canonical": "你是职业吗？", "emoji": "😱", "class": "god"},
    {"score_min": 75, "labels": ("来了，暴力炸！",), "canonical": "来了，暴力炸！", "emoji": "🤤", "class": "boom"},
    {"score_min": 68, "labels": ("化蛹成蝶（？）",), "canonical": "化蛹成蝶（？）", "emoji": "🦋", "class": "butterfly"},
    {"score_min": 60, "labels": ("恭喜，你不是区！", "恭喜，你不是区"), "canonical": "恭喜，你不是区！", "emoji": "😂", "class": "ok"},
    {"score_min": 52, "labels": ("不幸，你可能是区？",), "canonical": "不幸，你可能是区？", "emoji": "🤔", "class": "mid"},
    {"score_min": 43, "labels": ("哦灭跌多，你就是区！", "哦灭跌多，你就是区"), "canonical": "哦灭跌多，你就是区！", "emoji": "🎉", "class": "bad"},
    {"score_min": 0, "labels": ("你个大区！！！",), "canonical": "你个大区！！！", "emoji": "😡", "class": "terrible"},
]

_VERDICT_BY_LABEL = {label: rule for rule in _VERDICT_RULES for label in rule["labels"]}

# ── CD ──
_SHIQU_COOLDOWN_KV_PREFIX = "ow_shiqu_cooldown"
_SHIQU_PENDING_KV_PREFIX = "ow_shiqu_pending"
_SHIQU_PENDING_SECONDS = 300  # 5 分钟内再发确认
_SHIQU_BTN = '<qqbot-cmd-input text="是区吗" show="是区吗" reference="false" />'

# ── 违规封禁 ──
_SHIQU_VIOLATION_BAN_SECONDS = 43200  # 12 小时
_SHIQU_VIOLATION_BAN_FILE = "violation_bans.json"


# ── AstrBot 文转图模板 ──────────────────────────────────────

_SHIQU_HTML_TMPL = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html { background:#12161e; }
  body { width:100%; min-width:0; background:#12161e; color:#dce1eb; font-family:"Noto Sans CJK SC","Microsoft YaHei","PingFang SC",Arial,sans-serif; font-size:56px; line-height:1.64; padding:48px 42px; overflow-wrap:break-word; word-break:normal; font-variant-numeric:normal; font-feature-settings:"tnum" 0; }
  h1 { font-size:72px; line-height:1.25; text-align:center; color:#f0b47c; margin-bottom:20px; padding-bottom:30px; border-bottom:3px solid #2a3040; }
  h2 { font-size:66px; color:#c9986a; margin:52px 0 24px; }
  h3 { font-size:62px; color:#e0c090; margin:40px 0 20px; }
  p { margin:16px 0; }
  p.gamen { margin:24px 0; line-height:1.64; }
  p.gamen b { color:#e8d5b7; }
  .score { font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif; font-size:120px; line-height:1.12; text-align:center; color:#ffd700; font-weight:bold; margin:32px 0 16px; letter-spacing:0; }
  .verdict { display:block; font-size:78px; line-height:1.25; text-align:center; font-weight:bold; margin:16px 0 48px; }
  .verdict.god,.iv.god { color:#e67e22; }
  .verdict.boom,.iv.boom { color:#ff6b6b; }
  .verdict.butterfly,.iv.butterfly { color:#a78bfa; }
  .verdict.ok,.iv.ok { color:#4ecdc4; }
  .verdict.mid,.iv.mid { color:#f9ca24; }
  .verdict.bad,.iv.bad { color:#e17055; }
  .verdict.terrible,.iv.terrible { color:#d63031; }
  .iv { font-weight:bold; }
  .result-win { color:#5cef34; font-weight:bold; }
  .result-loss { color:#ec6e72; font-weight:bold; }
  .result-draw { color:#faad14; font-weight:bold; }
  .result-unknown { color:#8c8c8c; font-weight:bold; }
  .hero-name { color:#e8d5b7; font-weight:bold; }
  .sep { color:#6e7681; }
  .gentime { font-size:36px; color:#6e7681; opacity:0.55; text-align:center; margin:6px 0 36px; }
  .disclaimer { font-size:36px; color:#6e7681; opacity:0.55; text-align:center; margin:32px 0; }
  .mate-entry { margin-bottom:36px; }
  .mate-entry:last-child { margin-bottom:0; }
  .mate-head { margin:0 0 8px; color:#dce1eb; }
  .mate-name { color:#e8d5b7; font-weight:bold; }
  .mate-games { color:#8c8c8c; }
  .mate-score { font-weight:bold; margin-left:3px; }
  .mate-comment { margin:0; color:#b0bec5; }
  .footer { margin-top:56px; padding-top:28px; border-top:2px solid #2a3040; color:#6e7681; font-size:32px; line-height:1.45; text-align:center; }
</style></head><body>
  <h1>{{ title }}</h1>
  <div class="body">{{ body|safe }}</div>
  <div class="footer">{{ footer }}</div>
</body></html>'''


class ShiquManager:
    def __init__(self, plugin):
        self._plugin = plugin
        self._setup_logger()

    def _setup_logger(self):
        """设置是区吗专用日志，JSONL 格式，20MB 自动轮转。"""
        log_dir = self._data_dir()
        log_path = log_dir / "shiqu_calls.log"
        self._call_logger = logging.getLogger("astrbot.shiqu.call")
        self._call_logger.propagate = False
        self._call_logger.setLevel(logging.INFO)
        if self._call_logger.handlers:
            return  # 已初始化
        handler = RotatingFileHandler(
            str(log_path), maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._call_logger.addHandler(handler)

    @staticmethod
    def _shiqu_log_entry(*, openid: str, uid: str, target_id: str, success: bool,
                         attempt: int = 0, verdict: str = "", score: int = 0,
                         llm_chars: int = 0, llm_response: str = "",
                         error: str = "", duration_ms: int = 0) -> str:
        return json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "openid": openid,
            "uid": uid,
            "target_id": target_id,
            "success": success,
            "attempt": attempt,
            "verdict": verdict,
            "score": score,
            "llm_chars": llm_chars,
            "llm_response": llm_response[:65536],  # 单行上限 64KB，防日志膨胀
            "error": error,
            "duration_ms": duration_ms,
        }, ensure_ascii=False)

    # ── 权限 ──

    def _is_admin(self, event) -> bool:
        try:
            return self._plugin._is_astrbot_admin(event)
        except Exception:
            return False

    # ── 违规封禁 ──

    def _violation_ban_path(self) -> Path:
        return self._data_dir() / _SHIQU_VIOLATION_BAN_FILE

    def _load_violation_bans(self) -> dict:
        """加载违规封禁记录 JSON 文件，返回 {user_key: ban_timestamp}。"""
        path = self._violation_ban_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_violation_bans(self, bans: dict) -> None:
        """保存违规封禁记录到 JSON 文件（原子写入）。"""
        self._atomic_write_json(self._violation_ban_path(), bans)

    async def _check_violation_ban(self, event) -> tuple[bool, int]:
        """检查用户是否处于违规封禁中。返回 (is_banned: bool, remaining_seconds: int)。"""
        try:
            user_key = self._user_key(event)
            bans = self._load_violation_bans()
            ban_ts = bans.get(user_key, 0)
            if not ban_ts:
                return False, 0
            elapsed = int(time.time()) - int(ban_ts)
            if elapsed >= _SHIQU_VIOLATION_BAN_SECONDS:
                # 封禁已过期，自动清除
                bans.pop(user_key, None)
                self._save_violation_bans(bans)
                return False, 0
            return True, _SHIQU_VIOLATION_BAN_SECONDS - elapsed
        except Exception:
            return False, 0

    async def _set_violation_ban(self, event) -> None:
        """设置用户违规封禁（12小时）。"""
        try:
            user_key = self._user_key(event)
            bans = self._load_violation_bans()
            bans[user_key] = int(time.time())
            self._save_violation_bans(bans)
            logger.warning(f"[是区吗] 已对用户 {user_key} 设置违规封禁（12小时）")
        except Exception as e:
            logger.error(f"[是区吗] 设置违规封禁失败: {e}")

    async def _clear_violation_ban(self, event) -> None:
        """清除用户违规封禁。"""
        try:
            user_key = self._user_key(event)
            bans = self._load_violation_bans()
            bans.pop(user_key, None)
            self._save_violation_bans(bans)
            logger.info(f"[是区吗] 已清除用户 {user_key} 的违规封禁")
        except Exception as e:
            logger.error(f"[是区吗] 清除违规封禁失败: {e}")

    # ── 分级 CD ──

    def _get_config_map(self) -> dict:
        """解析 shiqu_cd_map JSON 配置，失败返回空 dict。"""
        cd_raw = self._plugin.config.get("shiqu_cd_map", "{}") or "{}"
        try:
            return json.loads(cd_raw) if isinstance(cd_raw, str) else cd_raw
        except Exception:
            return {}

    def _get_cd_seconds(self, event) -> int:
        """根据角色返回冷却秒数：管理员0 / 白名单30min / 普通4h。"""
        cd_map = self._get_config_map()
        if self._is_admin(event):
            return max(0, int(cd_map.get("admin", 0) or 0))
        if self._plugin._is_whitelisted(event):
            return max(0, int(cd_map.get("whitelist", 1800) or 1800))
        return max(0, int(cd_map.get("normal", 14400) or 14400))

    async def _check_cooldown(self, event) -> tuple:
        """返回 (ok: bool, remaining_seconds: int)。"""
        cd = self._get_cd_seconds(event)
        if cd <= 0:
            return True, 0
        key = f"{_SHIQU_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            last = await self._plugin.get_kv_data(key, 0)
            elapsed = int(time.time()) - int(last)
            if elapsed >= cd:
                return True, 0
            return False, cd - elapsed
        except Exception:
            return True, 0

    async def _set_cooldown(self, event):
        key = f"{_SHIQU_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, int(time.time()))
        except Exception:
            pass

    async def _reset_cooldown(self, event):
        key = f"{_SHIQU_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, 0)
        except Exception:
            pass

    async def _set_pending(self, event):
        key = f"{_SHIQU_PENDING_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, int(time.time()))
        except Exception:
            pass

    @staticmethod
    def _get_today_4am_ts() -> int:
        """返回最近一次凌晨 4:00 的 Unix 时间戳（本地时间）。"""
        now = time.time()
        local = time.localtime(now)
        today_4am = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 4, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst))
        # 如果当前时间还没到凌晨4点，则"今天"从昨天凌晨4点算起
        if now < today_4am:
            today_4am -= 86400
        return int(today_4am)

    def _is_first_today(self, uid: str) -> bool:
        """通过用户记录中的 generated_at 判断今天（凌晨4点重置）是否首次使用。"""
        record = self._load_user_record(uid)
        if not record:
            return True
        gen_at = str(record.get("generated_at", ""))
        if not gen_at:
            return True
        try:
            gen_ts = int(time.mktime(time.strptime(gen_at, "%Y-%m-%d %H:%M:%S")))
            return gen_ts < self._get_today_4am_ts()
        except Exception:
            return True

    # ── 排队 ──

    def _user_key(self, event) -> str:
        try:
            return f"{event.get_platform_name()}:{event.get_sender_id()}"
        except Exception:
            return str(event.get_sender_id())

    def _data_dir(self) -> Path:
        data_dir = self._plugin.plugin_data_dir / "shiqu"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    @staticmethod
    def _safe_filename(value: str, fallback: str = "unknown") -> str:
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", str(value)).strip("._-")
        return safe[:80] or fallback

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def _atomic_write_json(cls, path: Path, data: dict | list) -> None:
        cls._atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))

    def _user_record_path(self, uid: str) -> Path:
        return self._data_dir() / "users" / f"{self._safe_filename(uid, 'user')}.json"

    def _new_artifact_paths(self, uid: str, target_id: str) -> tuple[Path, Path]:
        data_dir = self._data_dir() / "results"
        ts = time.strftime("%Y%m%d_%H%M%S")
        ms = int((time.time() * 1000) % 1000)
        base = f"{ts}_{ms:03d}_{self._safe_filename(uid, 'user')}_{self._safe_filename(target_id, 'target')}"
        return (
            data_dir / f"{base}_prompt.txt",
            data_dir / f"{base}_llm_raw.txt",
        )

    def _save_user_record(self, uid: str, record: dict) -> None:
        self._atomic_write_json(self._user_record_path(uid), record)

    def _load_user_record(self, uid: str) -> Optional[dict]:
        path = self._user_record_path(uid)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"[是区吗][uid={uid}] 读取用户结果记录失败: {e}")
            return None

    async def _render_result_image(self, result: dict, generated_at: str = "", uid: str = "") -> str:
        """渲染是区吗判定书并下载到本地持久化目录，返回本地文件路径。

        避免直接缓存 text2img 服务临时 URL（服务端可能随时清理导致 404）。
        """
        target_id = str(result.get("target_id") or "未知玩家")
        title = f"是区吗判定书 · {target_id}"
        footer_time = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
        footer = f"生成时间: {footer_time}  ·  AI 数据带阴阳师 (Astrbot LLM)"
        body_html = self._result_to_html(result, generated_at=generated_at)
        render_url = await self._plugin.html_render(
            _SHIQU_HTML_TMPL,
            {"title": title, "body": body_html, "footer": footer},
            options={"type": "png", "width": 520, "viewport": {"width": 520, "height": 900}},
        )

        # 从 text2img 服务下载图片字节，保存到本地持久化目录
        try:
            session = await self._plugin._get_http_session()
            async with session.get(render_url) as resp:
                if resp.status == 200:
                    img_bytes = await resp.read()
                else:
                    logger.error(f"[是区吗] 从 text2img 下载图片失败 HTTP {resp.status}")
                    return ""
        except Exception as e:
            logger.error(f"[是区吗] 从 text2img 下载图片异常: {e}")
            return ""

        # 保存到本地持久化目录
        img_dir = self._data_dir() / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        safe_uid = self._safe_filename(uid, "user")
        local_path = img_dir / f"shiqu_{safe_uid}_{ts}.png"
        local_path.write_bytes(img_bytes)
        logger.info(f"[是区吗] 判定书图片已保存到本地: {local_path}")
        return str(local_path)

    async def _enqueue(self, event) -> tuple[int, int]:
        """加入队列，返回 (排号, 总人数)。排号 0 表示立即执行。"""
        uid = self._user_key(event)
        is_admin = self._is_admin(event)
        async with _QUEUE_LOCK:
            # 用户自己是否已有排队/执行中的任务
            for i, (euid, _) in enumerate(_QUEUE):
                if euid == uid:
                    return i + 1 + len(_ACTIVE), len(_QUEUE) + len(_ACTIVE)
            if uid in _ACTIVE:
                return 0, len(_QUEUE) + len(_ACTIVE)  # 正在执行中

            if is_admin:
                # 管理员直接放行
                _ACTIVE.add(uid)
                return 0, 0
            if len(_ACTIVE) >= _MAX_CONCURRENT:
                _QUEUE.append((uid, event))
                return len(_QUEUE) + len(_ACTIVE), len(_QUEUE) + len(_ACTIVE)
            _ACTIVE.add(uid)
            return 0, 0

    async def _dequeue(self, uid: str):
        async with _QUEUE_LOCK:
            _ACTIVE.discard(uid)
            _ACTIVE_META.pop(uid, None)
            # 从队列中取出下一个
            while _QUEUE and len(_ACTIVE) < _MAX_CONCURRENT:
                next_uid, _ = _QUEUE.popleft()
                if next_uid not in _ACTIVE:
                    _ACTIVE.add(next_uid)
                    # 下一个排到的需要被通知——由外部事件循环处理
                    break

    async def _queue_status(self, event) -> str:
        uid = self._user_key(event)
        async with _QUEUE_LOCK:
            total = len(_QUEUE) + len(_ACTIVE)
            pos = 0
            for euid, _ in _QUEUE:
                if euid == uid:
                    pos = len(_ACTIVE) + list(_QUEUE).index((euid, _)) + 1
                    break
            if uid in _ACTIVE:
                return f"🔍 正在处理中，当前队列 {total} 人"
            if pos > 0:
                return f"⏳ 排队中：第 {pos}/{total} 位，请稍候…"
            return "⚡ 当前无任务，可直接查询。"

    # ── 数据获取 ──

    @staticmethod
    def _get_match_mode(match_data: dict) -> str:
        """从对局数据中提取 gameMode 字符串。"""
        detail = match_data.get("detail", {}) or {}
        detail_data = detail.get("data") or {} if isinstance(detail, dict) else {}
        source = match_data.get("source_match", {}) or {}
        return str(detail_data.get("gameMode") or source.get("gameMode")
                   or source.get("instanceType") or "")

    _PRESET_MODES = {"SportPreset", "LeisurePreset", "Sport6v6", "Leisure6v6"}

    async def _fetch_one_match(self, bnet_id: str, index: int) -> Optional[dict]:
        url = f"{self._plugin.base_url}/dashen-match/detail"
        try:
            session = await self._plugin._get_http_session()
            async with session.post(url, json={"bnet_id": bnet_id, "index": index}) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("detail"):
                    return data
                return None
        except Exception as e:
            logger.warning(f"[是区吗][bnet={bnet_id}] 拉取 index={index} 失败: {e}")
            return None

    async def _fetch_match_by_token_match_id(self, customer_token: str, match_id: str) -> Optional[dict]:
        """用队友的 customer_token + 比赛 match_id 直接查同一局详情，获取 heroList。"""
        url = f"{self._plugin.base_url}/dashen-match/detail"
        try:
            session = await self._plugin._get_http_session()
            async with session.post(url, json={"customer_token": customer_token, "match_id": match_id}) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("detail"):
                    return data
                return None
        except Exception as e:
            logger.warning(f"[是区吗][match={match_id[:12]}] 拉取队友详情失败: {e}")
            return None

    async def _fetch_matches(self, bnet_id: str, target: int = 12) -> list[dict]:
        """仅抓取 SportPreset/LeisurePreset/Sport6v6/Leisure6v6，不足则逐批往后拉，最多 100 场。"""
        if target <= 0:
            target = 12
        preset = []
        idx = 0
        while idx < 100 and len(preset) < target:
            end = min(idx + 20, 100)
            raw = await asyncio.gather(*[self._fetch_one_match(bnet_id, i) for i in range(idx, end)])
            preset.extend(m for m in raw if m is not None and self._get_match_mode(m) in self._PRESET_MODES)
            idx = end
        return preset[:target]

    async def _fetch_teammate_details(self, detail_data: dict, target_id: str, index: int,
                                       focus_match_id: str = "") -> None:
        """用队友自身的 customer_token + 比赛 match_id 查同一局详细数据。
        相比按 name+index 查询，这种方式不受各玩家比赛排序差异影响，100% 精确。"""
        tm_list = detail_data.get("teammateList", [])
        for p in tm_list:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if p.get("_heroList"):
                continue
            teammate_token = str(p.get("customerToken", ""))
            if not teammate_token or not focus_match_id:
                continue
            try:
                data = await self._fetch_match_by_token_match_id(teammate_token, focus_match_id)
                if data:
                    pd = (data.get("detail", {}) or {}).get("data") or {}
                    hl = pd.get("heroList", [])
                    if hl:
                        p["_heroList"] = hl
            except Exception as e:
                logger.warning(f"[是区吗][target={target_id}] 获取队友 {name} 详细失败: {e}")

    async def _serial_fetch_all_teammate_details(self, matches: list[dict], target_id: str) -> None:
        """对所有对局串行拉取队友详细数据。"""
        for idx, m in enumerate(matches):
            detail_data = (m.get("detail", {}) or {}).get("data") or {}
            if isinstance(detail_data, dict):
                await self._fetch_teammate_details(detail_data, target_id, idx,
                                                   focus_match_id=str(m.get("match_id", "")))

    # ── Prompt 构建 ──

    def _build_prompt(self, matches: list[dict], target_id: str, db_path: str = "") -> str:
        ROLE_ORDER = {"tank": 0, "dps": 1, "healer": 2}
        ROLE_LABEL = {"tank": "坦克", "dps": "输出", "healer": "辅助"}

        def _get_role(p):
            return HERO_DICT.get(str(p.get("heroGuid", "")), {}).get("role", "unknown")

        def _sort_and_label(players):
            indexed = [(ROLE_ORDER.get(_get_role(p), 9), i, p) for i, p in enumerate(players) if isinstance(p, dict)]
            indexed.sort(key=lambda x: (x[0], x[1]))
            role_ct = {}
            labeled = []
            for _, _, p in indexed:
                r = _get_role(p)
                role_ct[r] = role_ct.get(r, 0) + 1
                lb = ROLE_LABEL.get(r, r)
                ct = sum(1 for pp in players if isinstance(pp, dict) and _get_role(pp) == r)
                labeled.append((p, f"{lb}{role_ct[r]}" if ct > 1 else lb))
            return labeled

        STAT_KEYS = [("kill","击杀"),("assist","助攻"),("death","阵亡"),("finalHit","最后一击"),
                     ("heroDamage","伤害"),("damageTaken","承伤"),("cure","治疗"),
                     ("healingTaken","受疗"),("resistDamage","格挡")]
        ENTRY_STAT_GUIDS = [
            ("击杀", ("603482350067646495",)),
            ("助攻", ("603482350067648392",)),
            ("阵亡", ("603482350067646506",)),
            ("最后一击", ("603482350067646507",)),
            ("单独消灭", ("603482350067646509",)),
            ("伤害", ("603482350067647671",)),
            ("治疗", ("603482350067647479", "603482350067646913")),
        ]

        def _rate(v, t): return f"{v * 600 / max(t, 60):.1f}" if t > 0 else str(v)

        def _fmt_num(v) -> str:
            if v is None:
                return "?"
            if isinstance(v, (int, float)) and float(v) == int(v):
                return str(int(v))
            return f"{float(v):.2f}"

        def _fmt_minsec(seconds: float) -> str:
            seconds = max(0, int(seconds or 0))
            return f"{seconds // 60:02d}:{seconds % 60:02d}"

        def _normalized_stat(sm: dict, guids: tuple[str, ...], ut: float, name_map: dict[str, str], hero_guid: str):
            values = []
            for guid in guids:
                if guid not in sm or not _stat_allowed_for_hero(guid, hero_guid):
                    continue
                name = name_map.get(guid, "")
                if should_skip_prompt_stat(value_guid=guid, value_text=name):
                    continue
                nv = normalize_stat_value(sm.get(guid), ut, value_text=name, value_guid=guid)
                if nv is not None:
                    values.append(nv)
            if not values:
                return None
            return max(values)

        def _hero_detail_text(entry: dict, hero_guid: str, name_map: dict[str, str]) -> str:
            """将单个英雄 entry 的 statMap 转成可读文本（与 DB 归一化口径一致）。"""
            sm = entry.get("statMap", {}) or {}
            ut = float(entry.get("userTimeSec", 600) or 600)
            seen: dict[str, str] = {}
            for guid, raw_val in sm.items():
                guid = str(guid)
                if not _stat_allowed_for_hero(guid, hero_guid):
                    continue
                name = name_map.get(guid)
                if not name:
                    continue
                if should_skip_prompt_stat(value_guid=guid, value_text=name):
                    continue
                nv = normalize_stat_value(raw_val, ut, value_text=name, value_guid=guid)
                if nv is None:
                    continue
                seen.setdefault(name, _fmt_num(nv))
            return ", ".join(f"{name}: {value}" for name, value in seen.items())

        def _expand_player_segments(p) -> list[dict]:
            name_map = load_stat_name_map()
            hl = p.get("_heroList")
            fallback_hg = str(p.get("heroGuid", ""))
            if not hl or not isinstance(hl, list):
                return [{"player": p, "hero_guid": fallback_hg, "entry": None, "name_map": name_map}]

            segments = []
            long_entries = [entry for entry in hl if isinstance(entry, dict) and float(entry.get("userTimeSec", 0) or 0) >= 60]
            for entry in long_entries:
                if not isinstance(entry, dict):
                    continue
                # 优先使用 _heroList 中明确的 heroId，避免从 statMap 推断错误
                hg = str(entry.get("heroId", ""))
                if hg:
                    segments.append({"player": p, "hero_guid": hg, "entry": entry, "name_map": name_map})
                    continue
                sm = entry.get("statMap", {}) or {}
                hg = _infer_hero_guid_from_stat_map(sm, fallback_hg, allow_fallback=(len(long_entries) == 1))
                if not hg:
                    continue
                segments.append({"player": p, "hero_guid": hg, "entry": entry, "name_map": name_map})
            if segments:
                return segments
            return []

        def _get_segment_role(seg):
            return HERO_DICT.get(str(seg.get("hero_guid", "")), {}).get("role", "unknown")

        def _player_primary_role(player_segments: list[dict], fallback_player: dict) -> str:
            best_role = HERO_DICT.get(str(fallback_player.get("heroGuid", "")), {}).get("role", "unknown")
            best_time = -1.0
            for seg in player_segments:
                entry = seg.get("entry") or {}
                ut = float(entry.get("userTimeSec", 0) or 0)
                role = _get_segment_role(seg)
                if ut > best_time:
                    best_time = ut
                    best_role = role
            return best_role

        def _sort_and_label_players(players):
            indexed = []
            for pi, p in enumerate(players):
                if not isinstance(p, dict):
                    continue
                segments = _expand_player_segments(p)
                if not segments:
                    continue
                role = _player_primary_role(segments, p)
                indexed.append((ROLE_ORDER.get(role, 9), pi, role, p, segments))
            indexed.sort(key=lambda x: (x[0], x[1]))
            role_ct = {}
            role_total = {}
            for _, _, role, _, _ in indexed:
                r = role
                role_total[r] = role_total.get(r, 0) + 1
            labeled = []
            for _, _, role, p, segments in indexed:
                r = role
                role_ct[r] = role_ct.get(r, 0) + 1
                lb = ROLE_LABEL.get(r, r)
                labeled.append((p, segments, f"{lb}{role_ct[r]}" if role_total.get(r, 0) > 1 else lb))
            return labeled

        def _fmt_hero_segment(seg, include_detail: bool):
            p = seg["player"]
            hg = str(seg.get("hero_guid", ""))
            hn = HERO_DICT.get(hg, {}).get("name", "?")
            entry = seg.get("entry")
            parts = [f"英雄: {hn}"]
            if entry:
                ut = float(entry.get("userTimeSec", 0) or 0)
                sm = entry.get("statMap", {}) or {}
                parts.append(f"时长: {_fmt_minsec(ut)}")
                for cn, guids in ENTRY_STAT_GUIDS:
                    nv = _normalized_stat(sm, guids, ut, seg["name_map"], hg)
                    if nv is not None:
                        parts.append(f"{cn}: {_fmt_num(nv)}")
                detail = _hero_detail_text(entry, hg, seg["name_map"]) if include_detail else ""
            else:
                for k, cn in STAT_KEYS:
                    v = int(p.get(k, 0) or 0)
                    parts.append(f"{cn}: {_rate(v, game_sec)}")
                detail = ""
            if include_detail and detail:
                parts.append(f"详细: {{ {detail} }}")
            return "{ " + ", ".join(parts) + " }"

        def _fmt_player_block(p, segments: list[dict], pos, game_sec, include_detail: bool, enemy_total_deaths: int = 0):
            name = str(p.get("name", "?"))
            display = f"*{name}" if name == target_id else name
            # 击杀参与率（不分英雄合并计算）
            player_total_ka = 0.0
            for seg in segments:
                entry = seg.get("entry")
                hg = str(seg.get("hero_guid", ""))
                nm = seg["name_map"]
                if entry:
                    sm = entry.get("statMap", {}) or {}
                    ut = float(entry.get("userTimeSec", 600) or 600)
                    kv = _normalized_stat(sm, ("603482350067646495",), ut, nm, hg) or 0
                    av = _normalized_stat(sm, ("603482350067648392",), ut, nm, hg) or 0
                    player_total_ka += kv + av
                else:
                    player_total_ka += int(p.get("kill", 0) or 0) + int(p.get("assist", 0) or 0)
            kp_rate = player_total_ka / enemy_total_deaths if enemy_total_deaths > 0 else 0
            hero_text = ", ".join(_fmt_hero_segment(seg, include_detail) for seg in segments)
            return f"{{ 位置: {pos}, 玩家: {display}, 击杀参与率: {kp_rate:.3f}, 英雄片段: [ {hero_text} ] }}"

        def _player_ref_text(seg, player_name: str) -> str:
            if not db_path:
                return ""
            hg = str(seg.get("hero_guid", ""))
            hn = HERO_DICT.get(hg, {}).get("name", "?")
            return build_broad_reference_text(db_path, player_name, hg, hn)

        result_map = {1: "胜", 0: "平", -1: "负"}
        lines = []

        for idx, m in enumerate(matches):
            detail_data = (m.get("detail", {}) or {}).get("data") or {}
            source = m.get("source_match", {}) or {}
            game_sec = float(detail_data.get("gameTimeSec", 600) or 600)

            map_guid = str(detail_data.get("mapGuid") or source.get("mapGuid") or "")
            ret = detail_data.get("matchRet", source.get("matchRet"))
            dur = f"{int(game_sec // 60):02d}:{int(game_sec % 60):02d}"
            lines.append(f"[第{idx + 1}局] {result_map.get(ret, '?')} {MAP_DICT.get(map_guid, '?')} {dur} 焦点玩家: {target_id}")
            lines.append("{")
            lines.append(f"  比分: {detail_data.get('teamScore','?')}:{detail_data.get('opponentScore','?')},")

            tm = detail_data.get("teammateList", [])
            en = detail_data.get("enemyList", [])
            enemy_total_deaths = sum(int((p if isinstance(p, dict) else {}).get("death", 0) or 0) for p in en)
            def _append_players(label, players, *, include_detail: bool, include_reference: bool):
                lines.append(f"  [{label}]")
                lines.append("  [")
                for p, segments, pos in _sort_and_label_players(players):
                    player_name = str(p.get("name", "?"))
                    lines.append(f"    {_fmt_player_block(p, segments, pos, game_sec, include_detail, enemy_total_deaths)},")
                    if include_reference:
                        for seg in segments:
                            ref = _player_ref_text(seg, player_name)
                            if ref:
                                lines.append(f"    # 数据参考: {ref}")
                lines.append("  ],")

            if tm:
                _append_players("队友", tm, include_detail=True, include_reference=True)
            lines.append("}")
            lines.append("")

        n = len(matches)

        # ── 好友 ID 汇总（同玩≥3局才列入，只给模型识别可点评对象）──
        teammate_counts: dict[str, int] = {}
        for m in matches:
            detail_data = (m.get("detail", {}) or {}).get("data") or {}
            seen = set()
            for p in detail_data.get("teammateList", []):
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name", ""))
                if name == target_id or name in seen:
                    continue
                if name:
                    seen.add(name)
                    teammate_counts[name] = teammate_counts.get(name, 0) + 1

        friend_ids = sorted(name for name, cnt in teammate_counts.items() if cnt >= 3)
        friend_id_text = "\n".join(f"- {name}" for name in friend_ids) or "无"

        match_text = "\n".join(lines)

        return f"""你是守望先锋串子型数据分析师，圈内人称"数据带阴阳师"。

【角色定义】
1. 深耕守望先锋全英雄机制、版本环境、职业赛事与国服天梯生态，所有点评以游戏数据为唯一依据，拒绝空口黑屁、主观臆断。熟稔全社区梗文化，对国服鱼塘到高分段的众生相了如指掌，鉴区准确率堪比官方外挂检测。
2. 人设底色：表面永远保持「我只是个念数据的中立人」的客观嘴脸，语气平淡像读财报，实则字字藏刀、句句带刺，精准戳中玩家最痛的操作痛点；
3. 核心立场: 是纯纯乐子人，看数据如同看乐子，毒舌但不恶毒，嘲讽只锁死游戏表现，绝不越界人身攻击。
4. 说话习惯：擅长用反问、反讽、明褒暗贬、假装惋惜的语气输出暴击；

【硬性约束】
- 仅针对游戏内数据、赛场表现、英雄数据点评，绝不涉及外貌、私生活、人品等人身攻击；不输出任何歧视、引战、恶意辱骂内容。
- 所有解读严格基于提供的原始数据，禁止编造数据、篡改数据含义、夸大数据结论；
- 严禁跨职责直接比较伤害/治疗等核心指标，阴阳调侃必须对应明确的数据论据。
- 不讨论外挂、代练等违规行为。
- 禁止进行反事实推演或假设性陈述（如"你本可以多拿3个击杀"），仅限描述已发生事件。
- 守望先锋段位名称：青铜/白银/黄金/白金/钻石/大师/宗师/英杰
- 输出评价符合人设和说话习惯，整体为串子风格阴阳，对好的部分赞赏，差的部分指出，可少量使用 emoji。

【评判规则】
1. 不同职责的核心指标优先级参考，需要综合看（比赛数据中的数值均与数据参考口径一致）：
   坦克位：单独消灭 > 最后一击 > (伤害-受疗) > 阵亡数 > 击杀参与率 >> 其他数据
   输出位：单独消灭 > 最后一击 > 伤害 > 阵亡数 > 击杀参与率 >> 其他数据
   辅助位：最后一击 >  阵亡数 > 单独消灭 > 伤害 > 治疗量 > 击杀数 > 击杀参与率 >> 其他数据

2. 数据对比与评分：
   - 将焦点玩家数据与同英雄"# 数据参考行"对比，低于参考值应扣分。
   - 同一玩家同一局可能在"英雄片段"内出现多个英雄，时长<3分钟的片段为低权重。
   - 最后一击和单独消灭应额外加分，频繁阵亡且贡献低应加重扣分。
   - 击杀，助攻，阵亡是参考值正负230%，不具有参考价值，不参与评分。
   - 需要综合看英雄数据，比如有的输出英雄伤害低但是最后一击高，有的辅助英雄输出高但治疗会少一些，需要综合参考值考虑，不要跨英雄对比。
   - 不要把“助攻低”或“没有助攻”机械地当成负面结论。对于坦克位和输出位，助攻不是通用核心指标。很多英雄的技能机制、收割定位、爆发击杀方式或单点作战方式，本来就不容易稳定获得助攻。除非该英雄本身明显依赖团队增益、控制、挂状态或持续参与混战，且同场其他数据也能证明其协同性偏低，否则禁止写出无意义的“零助攻/助攻少所以表现差”评价
   - 比赛胜负不影响评分，只论数据。
   - 若某局数据异常（如焦点玩家和队友，英雄全部为空，全部字段为空），该局不参与评分，comment 写"数据缺失，无法评价"。
   - 百分制评分标准：
     * ≥83 = 你是职业吗？
     * 82~75 = 来了，暴力炸！
     * 74~68 = 化蛹成蝶（？）
     * 67~60 = 恭喜，你不是区！
     * 59~52 = 不幸，你可能是区？
     * 51~43 = 哦灭跌多，你就是区！
     * <43 = 你个大区！！！
3. 综合判定：综合 {n} 场比赛中英雄对应核心指标进行评分，不考虑比赛胜负，只论数据评价。
4. 好友点评规则：
   - 必须点评下方【焦点玩家的好友 ID】中出现的每一位好友，缺一不可。
   - 好友点评只能基于他们的【比赛数据】，比赛胜负不影响评价，可以对焦点玩家表现上下文进行轻量评价。
   - 评分标准同焦点玩家（≥50夸/赞赏，<50串），但没有数据时语气要保守。


【阴阳话术库（示例，用作参考，不要完全照搬）】
你的走位很有想象力，可惜伤害结算在了空气上。
恭喜啊，用实力证明了「辅助」和「被辅助」的区别。
这波操作，完美诠释了什么叫「无效阵亡」。
建议把「最后一击」截图珍藏，毕竟这种高光时刻不多见。

【输出格式】
严格输出符合下方 JSON Schema 的合法 JSON 对象，禁止输出 markdown 代码块、注释或任何 JSON 之外的文字。

字段规范（schema 未表达的部分）：
- 所有字符串使用中文，内容简练。字符串内禁止英文双引号，引用请用「」或『』，emoji 可正常使用。
- result 仅可取值：胜 / 负 / 平 / 未知。
- summary 是纯客观数据概览点评（约 100 字）
- match_comments 必须覆盖【比赛数据】全部 {n} 局，index 从 1 递增到 {n}，禁止跳号或重复（约50字），写出最突出/劣势的个人表现、关键数据差距。
- teammate_comments 必须为【焦点玩家的好友 ID】中的每一位好友都生成一条点评，缺一不可，禁止输出列表外的玩家。
- overall_comment 约 350 字，串子风格阴阳总结，可少量使用emoji。

JSON Schema：
{json.dumps(_SHIQU_JSON_SCHEMA, ensure_ascii=False, indent=2)}

【焦点玩家的好友 ID】
{friend_id_text}

【比赛数据】
{match_text}
"""

    # ── LLM 调用 ──

    async def _call_llm(self, prompt: str) -> Optional[str]:
        """直接调用 OpenAI 兼容 API（可配置），支持 SSE 流式传输。

        配置项：shiqu_llm_api_base / shiqu_llm_api_key / shiqu_llm_model / shiqu_llm_stream
        """
        api_base = str(self._plugin.config.get("shiqu_llm_api_base", "") or "").strip().rstrip("/")
        api_key = str(self._plugin.config.get("shiqu_llm_api_key", "") or "").strip()
        model = str(self._plugin.config.get("shiqu_llm_model", "") or "").strip()
        use_stream = bool(self._plugin.config.get("shiqu_llm_stream", True))

        if not api_base or not api_key or not model:
            logger.error("[是区吗] LLM 配置不完整，请在插件配置中填写 shiqu_llm_api_base / shiqu_llm_api_key / shiqu_llm_model")
            return None

        url = f"{api_base}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": use_stream,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "shiqu_result", "strict": True, "schema": _SHIQU_JSON_SCHEMA},
            },
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with _LLM_SEMAPHORE:
            try:
                session = await self._plugin._get_http_session()
                async with session.post(url, json=payload, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=600)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[是区吗] LLM API HTTP {resp.status}: {body[:500]}")
                        return None

                    if not use_stream:
                        data = await resp.json()
                        choices = data.get("choices") if isinstance(data, dict) else None
                        if not isinstance(choices, list) or not choices:
                            logger.error(f"[是区吗] LLM 非流式响应 choices 异常: {str(data)[:500]}")
                            return None
                        msg = choices[0] if isinstance(choices[0], dict) else {}
                        msg = msg.get("message") or {}
                        msg = msg if isinstance(msg, dict) else {}
                        return (str(msg.get("content") or "")).strip() or None

                    # SSE 流式累积
                    parts = []
                    async for line in resp.content:
                        line = line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        chunk = line[6:]
                        if chunk == "[DONE]":
                            break
                        # ponytail: 单个 chunk 结构异常不能毁掉整条流。
                        # 原 except 只接 JSONDecodeError，choices 为 dict/null/None 等
                        # 会抛 IndexError/AttributeError/TypeError 冒泡到外层 → 整条响应作废。
                        try:
                            obj = json.loads(chunk)
                            if not isinstance(obj, dict):
                                continue
                            choices = obj.get("choices")
                            if not isinstance(choices, list) or not choices:
                                continue
                            first = choices[0]
                            if not isinstance(first, dict):
                                continue
                            delta = first.get("delta") or {}
                            delta = delta if isinstance(delta, dict) else {}
                            if "content" in delta:
                                parts.append(delta["content"])
                        except Exception as chunk_err:
                            logger.debug(f"[是区吗] SSE chunk 跳过: {chunk_err} | {chunk[:200]}")
                            continue

                    return "".join(parts) or None
            except Exception as e:
                logger.error(f"[是区吗] LLM 调用异常: {e}", exc_info=True)
                return None

    async def test_connectivity(self):
        """测试 LLM 连通性，返回 (ok, message)。"""
        api_base = str(self._plugin.config.get("shiqu_llm_api_base", "") or "").strip().rstrip("/")
        api_key = str(self._plugin.config.get("shiqu_llm_api_key", "") or "").strip()
        model = str(self._plugin.config.get("shiqu_llm_model", "") or "").strip()

        if not api_base or not api_key or not model:
            return False, "❌ 是区吗 LLM 配置不完整，请先填写 shiqu_llm_api_base / shiqu_llm_api_key / shiqu_llm_model"

        url = f"{api_base}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        try:
            t0 = time.time()
            session = await self._plugin._get_http_session()
            async with session.post(url, json=payload, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=30)) as resp:
                elapsed = (time.time() - t0) * 1000
                if resp.status != 200:
                    body = await resp.text()
                    return False, f"❌ HTTP {resp.status} ({elapsed:.0f}ms)\n{body[:300]}"

                data = await resp.json()
                content = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
                usage = data.get("usage", {})
                return True, (
                    f"✅ 连通正常 ({elapsed:.0f}ms)\n"
                    f"模型: {model}\n"
                    f"响应: {content or '(空)'}\n"
                    f"Token: prompt={usage.get('prompt_tokens', '?')} "
                    f"completion={usage.get('completion_tokens', '?')} "
                    f"total={usage.get('total_tokens', '?')}"
                )
        except Exception as e:
            return False, f"❌ 连接异常: {e}"

    # ── 结构化结果 → 渲染文本 → HTML ──

    @staticmethod
    def _repair_json(text: str) -> str:
        """修复 LLM JSON 常见错误：字符串值内未转义的双引号。"""
        result = []
        i, n = 0, len(text)
        in_string = False
        while i < n:
            ch = text[i]
            if not in_string:
                result.append(ch)
                if ch == '"' and (i == 0 or text[i - 1] != '\\'):
                    in_string = True
            else:
                if ch == '\\' and i + 1 < n:
                    result.append(text[i:i + 2])
                    i += 1
                elif ch == '"':
                    # 闭合引号后面必须是 JSON 分隔符或空白
                    rest = text[i + 1:].lstrip()
                    if not rest or rest[0] in ',:}]':
                        in_string = False
                    else:
                        ch = '\\"'
                    result.append(ch)
                else:
                    result.append(ch)
            i += 1
        return ''.join(result)

    @staticmethod
    def _repair_json_structure(text: str) -> str:
        """修复 JSON 结构错误：孤立的 ] [ 和多余尾逗号。"""
        # 移除字符串值后孤立的 ]： "..." ] , "..." → "..." , "..."
        text = re.sub(r'"\s*\]\s*,(\s*")', r'",\1', text)
        # 移除字符串值后孤立的 [： "..." [ , "..." → "..." , "..."
        text = re.sub(r'"\s*\[\s*,(\s*")', r'",\1', text)
        # 移除 ] 或 } 前的多余尾逗号
        text = re.sub(r',\s*([}\]])', r'\1', text)
        return text

    @staticmethod
    def _repair_json_values(text: str) -> str:
        """修复 LLM JSON 值错误：非法值替换为默认值。

        旧正则 [^\\d\\s,\\}\\]\\]+ 遇到中文描述中的数字（如 "15局"）会截断，
        导致残留文本破坏 JSON。改为匹配到下一个 JSON 分隔符为止，
        并用负向前瞻保留已是合法数字的字段。
        """
        # index 字段 → 非数字替换为 1
        text = re.sub(r'"index"\s*:\s*(?!\s*\d+\s*[,}\]])[^,\}\]]+', '"index": 1', text)
        # score 字段 → 非数字替换为 50
        text = re.sub(r'"score"\s*:\s*(?!\s*\d+\s*[,}\]])[^,\}\]]+', '"score": 50', text)
        # games 字段 → 非数字替换为 1
        text = re.sub(r'"games"\s*:\s*(?!\s*\d+\s*[,}\]])[^,\}\]]+', '"games": 1', text)
        return text

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        attempts = [
            cleaned,
            ShiquManager._repair_json(cleaned),
            ShiquManager._repair_json_structure(cleaned),
            ShiquManager._repair_json_values(cleaned),
            ShiquManager._repair_json(ShiquManager._repair_json_structure(cleaned)),
            ShiquManager._repair_json(ShiquManager._repair_json_values(cleaned)),
            ShiquManager._repair_json_structure(ShiquManager._repair_json_values(cleaned)),
        ]
        for attempt in attempts:
            try:
                data = json.loads(attempt)
                return data if isinstance(data, dict) else None
            except Exception:
                pass

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            attempts = [
                cleaned[start:end + 1],
                ShiquManager._repair_json(cleaned[start:end + 1]),
                ShiquManager._repair_json_structure(cleaned[start:end + 1]),
                ShiquManager._repair_json_values(cleaned[start:end + 1]),
                ShiquManager._repair_json(ShiquManager._repair_json_structure(cleaned[start:end + 1])),
                ShiquManager._repair_json(ShiquManager._repair_json_values(cleaned[start:end + 1])),
                ShiquManager._repair_json_structure(ShiquManager._repair_json_values(cleaned[start:end + 1])),
            ]
            for attempt in attempts:
                try:
                    data = json.loads(attempt)
                    return data if isinstance(data, dict) else None
                except Exception:
                    pass
        return None

    @staticmethod
    def _clamp_score(value, default: int = 0) -> int:
        try:
            score = int(round(float(value)))
        except Exception:
            score = default
        return max(0, min(100, score))

    @classmethod
    def _normalize_result(cls, data: dict, target_id: str) -> dict:
        score = cls._clamp_score(data.get("score"), 0)
        result = {
            "target_id": str(data.get("target_id") or target_id),
            "score": score,
            "verdict": cls._score_rule(score)["canonical"],
            "summary": str(data.get("summary") or "暂无数据概况。").strip(),
            "match_comments": [],
            "overall_comment": str(data.get("overall_comment") or "暂无综合评价。").strip(),
            "teammate_comments": [],
        }

        for i, item in enumerate(data.get("match_comments") or [], start=1):
            if not isinstance(item, dict):
                continue
            result["match_comments"].append({
                "index": cls._clamp_score(item.get("index"), i),
                "result": str(item.get("result") or "未知"),
                "hero": str(item.get("hero") or "未知英雄"),
                "comment": str(item.get("comment") or "暂无点评。").strip(),
            })

        for item in data.get("teammate_comments") or []:
            if not isinstance(item, dict):
                continue
            tm_score = cls._clamp_score(item.get("score"), 0)
            teammate = {
                "name": str(item.get("name") or "未知队友"),
                "score": tm_score,
                "verdict": cls._score_rule(tm_score)["canonical"],
                "comment": str(item.get("comment") or "暂无点评。").strip(),
            }
            if item.get("games") is not None:
                teammate["games"] = max(1, cls._clamp_score(item.get("games"), 1))
            result["teammate_comments"].append(teammate)

        return result

    @classmethod
    def _parse_llm_json_result(cls, raw_text: str, target_id: str) -> Optional[dict]:
        data = cls._extract_json_object(raw_text)
        if data is None:
            return None
        return cls._normalize_result(data, target_id)

    @staticmethod
    def _result_to_plain_text(result: dict, generated_at: str = "") -> str:
        disclaimer = "* 功能仅限娱乐，切勿因为ai瞎编影响心情"
        gen_time = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"🔍 {result.get('target_id', '未知玩家')} 是区吗判定书",
            "",
            f"评分：{result.get('score', 0)}/100",
            str(result.get("verdict") or ""),
            "",
            disclaimer,
            f"生成时间：{gen_time}",
            "",
            "数据概况：",
            str(result.get("summary") or ""),
            "",
            "逐局点评：",
        ]

        for item in result.get("match_comments") or []:
            lines.append(
                f"第{item.get('index', '?')}局：{item.get('result', '未知')}，{item.get('hero', '未知英雄')}：{item.get('comment', '')}"
            )

        lines.extend(["", "综合评价：", str(result.get("overall_comment") or ""), "", disclaimer, "", "队友点评："])
        teammates = result.get("teammate_comments") or []
        if teammates:
            for item in teammates:
                games_text = f"（共同{item.get('games')}局）" if item.get("games") is not None else ""
                lines.append(
                    f"- {item.get('name', '未知队友')}{games_text}：评分{item.get('score', 0)}/100\n{item.get('verdict', '')}{item.get('comment', '')}"
                )
        else:
            lines.append("- 暂无共同游戏≥2局的队友。")

        return "\n".join(lines).strip()

    @staticmethod
    def _score_rule(score: int) -> dict:
        for rule in _VERDICT_RULES:
            if score >= int(rule["score_min"]):
                return rule
        return _VERDICT_RULES[-1]

    @staticmethod
    def _strip_verdict_emoji(value: str) -> str:
        return re.sub(r"^[😱🤤😂🤔🤡😡]\s*", "", value.strip())

    @classmethod
    def _verdict_rule_for_text(cls, value: str) -> Optional[dict]:
        label = cls._strip_verdict_emoji(value).strip("，,。.;； ")
        return _VERDICT_BY_LABEL.get(label)

    @classmethod
    def _decorate_verdict(cls, value: str, block: bool = False) -> str:
        rule = cls._verdict_rule_for_text(value)
        if not rule:
            return value
        text = f'{rule["emoji"]} {rule["canonical"]}'
        if block:
            return f'<div class="verdict {rule["class"]}">{text}</div>'
        return f'<span class="iv {rule["class"]}">{text}</span>'

    @classmethod
    def _decorate_inline_verdicts(cls, value: str) -> str:
        labels = sorted(_VERDICT_BY_LABEL, key=len, reverse=True)
        pattern = re.compile(rf"(?:[😱🤤😂🤔🤡😡]\s*)?({'|'.join(re.escape(label) for label in labels)})")

        def repl(m):
            rule = _VERDICT_BY_LABEL[m.group(1)]
            return f'<span class="iv {rule["class"]}">{rule["emoji"]} {rule["canonical"]}</span>'

        return pattern.sub(repl, value)

    @staticmethod
    def _plain_to_html(text: str) -> str:
        """将 LLM 输出的纯文本转为 HTML 正文，与 tests/simulate_shiqu_render.py 渲染一致。"""
        # 转义 HTML 特殊字符
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # ── 评分着色（按档位）──
        def _score(m):
            s = int(m.group(1))
            rule = ShiquManager._score_rule(s)
            c = {
                "god": "#e67e22",
                "boom": "#ff6b6b",
                "butterfly": "#a78bfa",
                "ok": "#4ecdc4",
                "mid": "#f9ca24",
                "bad": "#e17055",
                "terrible": "#d63031",
            }.get(str(rule["class"]), "#ffd700")
            return f'<div class="score" style="color:{c}">{s}/100</div>'

        text = re.sub(r"评分：\s*(\d+)/100", _score, text)

        # ── 章节标题 ──
        text = text.replace("逐局点评：", "\n<h2>逐局点评</h2>\n")
        text = text.replace("综合评价：", "\n<h2>综合评价</h2>\n")
        text = text.replace("队友点评：", "\n<h2>队友点评</h2>\n")
        text = text.replace("数据概况：", "\n<h3>数据概况</h3>\n")

        # ── 逐行处理 ──
        lines = text.split("\n")
        buf = []
        for s in lines:
            s = s.strip()
            if not s:
                buf.append("")
                continue
            verdict_rule = ShiquManager._verdict_rule_for_text(s)
            if verdict_rule:
                buf.append(ShiquManager._decorate_verdict(s, block=True))
                continue
            # LLM 第一行标题由外层模板渲染，正文里跳过，避免重复。
            if "是区吗判定书" in s and s.startswith("🔍"):
                continue
            # 已处理过的标签 → 直接保留
            if s.startswith("<h") or s.startswith("<div") or s.startswith("<span"):
                buf.append(s)
            # 单局条目："第N局：... " → 按最后一个"："分割为加粗头部 + 普通点评
            elif s.startswith("第") and "局" in s:
                if "：" in s:
                    head, tail = s.split("：", 1)
                    buf.append(f'<p class="gamen"><b>{head}：</b><span>{tail}</span></p>')
                else:
                    buf.append(f'<p class="gamen"><b>{s}</b></p>')
            # 队友条目："- 玩家名...":" → 名前部分正文字色，点评内容 mate 色
            elif s.startswith("- "):
                content = s[2:]
                if "：" in content:
                    prefix, rest = content.split("：", 1)
                    buf.append(f'<p>- {prefix}：<span class="mate">{ShiquManager._decorate_inline_verdicts(rest)}</span></p>')
                else:
                    buf.append(f'<p class="mate">{ShiquManager._decorate_inline_verdicts(s)}</p>')
            # 队友点评续行：以判定标签开头
            elif any(s.startswith(label) for label in _VERDICT_BY_LABEL):
                buf.append(f'<p class="mate">{ShiquManager._decorate_inline_verdicts(s)}</p>')
            # 免责声明 / 生成时间
            elif "功能仅限娱乐" in s or s.startswith("生成时间："):
                buf.append(f'<p class="disclaimer">{s}</p>')
            else:
                buf.append(f"<p>{s}</p>")

        body = "\n".join(buf)

        return body

    # ── 新渲染辅助 ──

    _SCORE_COLORS = {
        "god": "#e67e22", "boom": "#ff6b6b", "butterfly": "#a78bfa",
        "ok": "#4ecdc4", "mid": "#f9ca24", "bad": "#e17055", "terrible": "#d63031",
    }

    # 队友评分数字专用色板, 与评价文字(#b0bec5 冷灰)拉开色差
    _MATE_SCORE_COLORS = {
        "god": "#f6a863", "boom": "#f58989", "butterfly": "#bfacfa",
        "ok": "#7eddd6", "mid": "#f5d35a", "bad": "#f09c88", "terrible": "#ee6566",
    }

    @staticmethod
    def _escape_html(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _to_half_width_punct(text: str) -> str:
        """全角标点转半角:  ：→:  ，→,  （→(  ）→)  ；→;
        ！? 保留全角, 更符合中文阅读习惯。"""
        text = text.replace("：", ": ")
        text = text.replace("，", ", ")
        text = text.replace("（", "(").replace("）", ")")
        text = text.replace("；", ";")
        return text

    @staticmethod
    def _add_num_spacing(text: str) -> str:
        """中文字符与数字之间插入窄空格 (U+2009), 提升可读性。"""
        _THIN = "\u2009"
        text = re.sub(r"([\u4e00-\u9fff])(\d)", lambda m: m.group(1) + _THIN + m.group(2), text)
        text = re.sub(r"(\d)([\u4e00-\u9fff])", lambda m: m.group(1) + _THIN + m.group(2), text)
        return text

    @classmethod
    def _format_text(cls, text: str) -> str:
        """转义 HTML → 标注判定标签 → 半角标点 → 数字间隔。"""
        text = cls._escape_html(text)
        text = cls._decorate_inline_verdicts(text)
        text = cls._to_half_width_punct(text)
        text = cls._add_num_spacing(text)
        return text

    @classmethod
    def _result_to_html(cls, result: dict, generated_at: str = "") -> str:
        """直接从结构化结果生成 HTML 正文, 带颜色标注、半角标点、数字间隔。"""
        score = cls._clamp_score(result.get("score", 0), 0)
        verdict = str(result.get("verdict") or "")
        summary = str(result.get("summary") or "")
        overall = str(result.get("overall_comment") or "")
        gen_time = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
        disclaimer = "* 功能仅限娱乐, 切勿因为ai瞎编影响心情"

        rule = cls._score_rule(score)
        score_color = cls._SCORE_COLORS.get(str(rule["class"]), "#ffd700")

        parts: list[str] = []

        # 评分 + 判定
        parts.append(f'<div class="score" style="color:{score_color}">{score}/100</div>')
        verdict_block = cls._decorate_verdict(verdict, block=True)
        if verdict_block:
            parts.append(cls._to_half_width_punct(verdict_block))

        # 免责声明 + 生成时间 (间距缩小)
        parts.append(f'<p class="disclaimer" style="margin-bottom:4px">{cls._escape_html(disclaimer)}</p>')
        parts.append(f'<p class="gentime">生成时间: {cls._escape_html(gen_time)}</p>')

        # 数据概况
        parts.append("<h3>数据概况</h3>")
        parts.append(f"<p>{cls._format_text(summary)}</p>")

        # 逐局点评
        parts.append("<h2>逐局点评</h2>")
        result_classes = {"胜": "result-win", "负": "result-loss", "平": "result-draw"}
        for item in result.get("match_comments") or []:
            idx = cls._escape_html(str(item.get("index", "?")))
            res = str(item.get("result", "未知"))
            hero = cls._to_half_width_punct(cls._escape_html(str(item.get("hero", "未知英雄"))))
            comment = cls._format_text(str(item.get("comment", "")))
            res_cls = result_classes.get(res, "result-unknown")
            parts.append(
                f'<p class="gamen"><b>第{idx}局: </b>'
                f'<span class="{res_cls}">{cls._escape_html(res)}</span>'
                f'<span class="sep"> </span>'
                f'<span class="hero-name">{hero}</span>'
                f'<span class="sep">: </span>'
                f"<span>{comment}</span></p>"
            )

        # 综合评价
        parts.append("<h2>综合评价</h2>")
        parts.append(f"<p>{cls._format_text(overall)}</p>")
        parts.append(f'<p class="disclaimer">{cls._escape_html(disclaimer)}</p>')

        # 队友点评
        parts.append("<h2>队友点评</h2>")
        teammates = result.get("teammate_comments") or []
        if teammates:
            for item in teammates:
                name = cls._escape_html(str(item.get("name", "未知队友")))
                tm_score = cls._clamp_score(item.get("score", 0), 0)
                games = item.get("games")
                games_text = f"（共同{games}局）" if games is not None else ""
                games_text = cls._to_half_width_punct(games_text)
                tm_comment = cls._format_text(
                    str(item.get("verdict") or "") + str(item.get("comment") or "")
                )
                tm_rule = cls._score_rule(tm_score)
                tm_mate_color = cls._MATE_SCORE_COLORS.get(str(tm_rule["class"]), "#b0a080")
                parts.append(
                    f'<div class="mate-entry">'
                    f'<p class="mate-head">'
                    f'<span class="mate-name">{name}</span>'
                    f'<span class="mate-games">{cls._escape_html(games_text)}</span>'
                    f'<span class="sep">: </span>'
                    f'评分 <span class="mate-score" style="color:{tm_mate_color}">{tm_score}/100</span>'
                    f"</p>"
                    f'<p class="mate-comment">{tm_comment}</p>'
                    f"</div>"
                )
        else:
            parts.append('<p class="mate-comment">- 暂无共同游戏≥2局的队友.</p>')

        return "\n".join(parts)

    # ── 主流程 ──

    async def run(self, event, bnet_id_input: str = "", match_count: int = 0):
        uid = self._user_key(event)

        # ── 违规封禁检查 ──
        banned, ban_remain = await self._check_violation_ban(event)
        if banned:
            h, remainder = divmod(ban_remain, 3600)
            m, s = divmod(remainder, 60)
            remain_str = f"{int(h)}小时{int(m)}分钟" if h > 0 else f"{int(m)}分钟{int(s)}秒"
            yield event.plain_result(
                f"⛔ 您之前查询返回的图片内容包含违规信息，已被禁止使用是区吗指令 {remain_str}。\n"
                f"请勿再次尝试查询违规内容。"
            )
            return

        # 普通用户开关
        if not self._is_admin(event) and not self._plugin._is_whitelisted(event):
            cd_map = self._get_config_map()
            if not cd_map.get("normal_enabled", False):
                yield event.plain_result("🔒 是区吗功能暂未对普通用户开放。")
                return

        # ── 正在走流程 → 告知当前状态，不进入新流程 ──
        async with _QUEUE_LOCK:
            if uid in _ACTIVE_META:
                yield event.plain_result(f"⏳ 判定书正在生成中，当前步骤：{_ACTIVE_META[uid]}，请稍候…")
                return
            if uid in _ACTIVE:
                yield event.plain_result("⏳ 判定书正在生成中，请稍候…")
                return
            for euid, _ in _QUEUE:
                if euid == uid:
                    yield event.plain_result("⏳ 您的判定书正在排队中，请稍候…")
                    return

        # 获取 bnet_id
        target_id = await self._plugin._get_bnet_id(event, bnet_id_input)
        if not target_id:
            yield event.plain_result(f"❌ 请提供 战网id，或先使用 /绑定 绑定。\n用法：{_SHIQU_BTN} &lt;battle_tag&gt;")
            return

        # ── 每日首次使用（凌晨4点重置）→ 直接开启查询，跳过确认 ──
        cd_ok, cd_remain = await self._check_cooldown(event)
        is_first_today = self._is_first_today(uid)

        if is_first_today and cd_ok:
            async for r in self._do_query(event, uid, target_id, match_count=match_count):
                yield r
            return

        # ── 检查 pending 状态 ──
        pending_key = f"{_SHIQU_PENDING_KV_PREFIX}:{uid}"
        try:
            pending_ts = await self._plugin.get_kv_data(pending_key, 0)
        except Exception:
            pending_ts = 0
        is_pending = int(pending_ts or 0) > 0 and (int(time.time()) - int(pending_ts or 0)) < _SHIQU_PENDING_SECONDS

        # pending 有效且无 CD → 清除 pending，执行查询
        if is_pending and cd_ok:
            await self._plugin.put_kv_data(pending_key, 0)
            async for r in self._do_query(event, uid, target_id, match_count=match_count):
                yield r
            return

        # ── 非首次触发：文字先行，图片后发 ──
        record = self._load_user_record(uid)
        cached_image = str(record.get("image_path") or "") if record else ""

        # 1. 文字先发
        if not cd_ok:
            m, s = divmod(cd_remain, 60)
            h, m = divmod(m, 60)
            remain_str = f"{int(h)}时{int(m)}分{int(s)}秒" if h > 0 else f"{int(m)}分{int(s)}秒"
            yield event.plain_result(f"⏳ 冷却中，剩余 {remain_str}，届时再发 {_SHIQU_BTN} 开启新查询。")
        else:
            # ── 错误恢复：cooldown 被 _reset_cooldown 置零 → 跳过确认直接查 ──
            cd_key = f"{_SHIQU_COOLDOWN_KV_PREFIX}:{uid}"
            try:
                cd_raw = await self._plugin.get_kv_data(cd_key, -1)
            except Exception:
                cd_raw = -1
            if cd_raw == 0:
                async for r in self._do_query(event, uid, target_id, match_count=match_count):
                    yield r
                return

            # 白名单用户/群：仅当上次图片成功发送时跳过二次确认，否则走确认流程让用户先看到缓存图
            last_image_sent_ok = record.get("image_sent", False) if record else False
            if self._plugin._is_whitelisted(event) and last_image_sent_ok:
                async for r in self._do_query(event, uid, target_id, match_count=match_count):
                    yield r
                return

            await self._set_pending(event)
            yield event.plain_result(f'👋 以下是上次的结果，如要开启新查询，请在 **5 分钟内**再次发送 {_SHIQU_BTN}。')

        # 2. 图片直发（不重新渲染）
        if cached_image:
            # 兼容旧记录中可能是远程 URL 的情况，检查本地文件是否存在
            if cached_image.startswith("http://") or cached_image.startswith("https://"):
                logger.warning(f"[是区吗][uid={uid}] 缓存图片为远程 URL（旧格式），尝试重新渲染本地副本")
                result_data = record.get("result") if record else None
                if isinstance(result_data, dict):
                    try:
                        cached_image = await self._render_result_image(
                            result_data,
                            generated_at=str(record.get("generated_at") or ""),
                            uid=uid,
                        )
                        record["image_path"] = str(cached_image)
                        self._save_user_record(uid, record)
                    except Exception as e:
                        logger.error(f"[是区吗][uid={uid}] 旧 URL 迁移重新渲染失败: {e}")
                        cached_image = ""
                else:
                    cached_image = ""
            if cached_image:
                _LAST_IMAGE[uid] = cached_image
                try:
                    yield event.chain_result([Image.fromFileSystem(cached_image)])
                    # 缓存图发送成功 → 标记 image_sent，后续白名单可跳过二次确认
                    if record:
                        record["image_sent"] = True
                        self._save_user_record(uid, record)
                except Exception as exc:
                    err_msg = str(exc)
                    logger.error(f"[是区吗][uid={uid}] 发送缓存图片失败: {err_msg}")
                    if "违规" in err_msg or "violation" in err_msg.lower():
                        await self._set_violation_ban(event)
                        yield event.plain_result(
                            "⛔ 查询返回的图片内容包含违规信息，该指令已被禁用12小时，请勿再次尝试。"
                        )

    async def _do_query(self, event, uid: str, target_id: str, match_count: int = 0):
        """执行实际的是区吗查询流程。match_count>0 时覆盖配置的抓取场数。"""
        # ── 重复触发检查 ──
        async with _QUEUE_LOCK:
            if uid in _ACTIVE_META:
                step = _ACTIVE_META[uid]
                yield event.plain_result(f"⏳ 判定书正在生成中，当前步骤：{step}，请稍候…")
                return
            if uid in _ACTIVE:
                yield event.plain_result("⏳ 判定书正在生成中，请稍候…")
                return
            for euid, _ in _QUEUE:
                if euid == uid:
                    yield event.plain_result("⏳ 您的判定书正在排队中，请稍候…")
                    return

        # 排队
        pos, total = await self._enqueue(event)
        if pos > 0:
            yield event.plain_result(f"⏳ 排队中：第 {pos}/{total} 位，请稍候…")
            return

        last_attempt = 0
        t0 = time.time()
        try:
            # 仅一条进度消息
            yield event.plain_result(f'🔍 正在生成 {target_id} 是区吗判定书，未自动返回请使用<qqbot-cmd-input text="ow是区吗结果" show="ow是区吗结果" reference="false" />查询')
            _ACTIVE_META[uid] = "拉取对局数据"
            target_count = match_count if 0 < match_count <= 25 else int(self._get_config_map().get("match_count", 12) or 12)
            matches = await self._fetch_matches(target_id, target=target_count)
            t1 = time.time()
            logger.info(f"[是区吗][uid={uid}] 📊 已获取 {len(matches)} 场 ({(t1 - t0):.1f}s)")

            if len(matches) < 2:
                yield event.plain_result(f"❌ {target_id}{_SHIQU_BTN} 仅获取到 {len(matches)} 场对局，至少需要 2 场。")
                return

            _ACTIVE_META[uid] = "拉取队友详细数据"
            await self._serial_fetch_all_teammate_details(matches, target_id)
            t2 = time.time()
            logger.info(f"[是区吗][uid={uid}] 📊 队友数据拉取完成 ({(t2 - t1):.1f}s)")

            prompt_path, llm_raw_path = self._new_artifact_paths(uid, target_id)

            # 构建 prompt
            db_path = str(getattr(self._plugin, "config", {}).get("sqlite_db_path", "") or "").strip()
            prompt = self._build_prompt(matches, target_id, db_path=db_path)
            self._atomic_write_text(prompt_path, prompt)
            prompt_kb = len(prompt.encode("utf-8")) / 1024
            logger.info(f"[是区吗][uid={uid}] ✅ Prompt 已生成 ({len(prompt)} 字符, {prompt_kb:.1f}KB)")

            # 提示词过小 → 数据不足，放弃
            if len(prompt.encode("utf-8")) < 10240:
                await self._reset_cooldown(event)
                await self._set_pending(event)
                yield event.plain_result(f"❌ 数据抓取量异常，可能没有足够的预设/6v6比赛对局，[决斗领域]暂未适配，{_SHIQU_BTN} 已重置冷却。")
                return

            # ── 调用 LLM（含 NewAPI 自动切模型重试）──
            _ACTIVE_META[uid] = "AI 生成判定"
            result = None
            for attempt in range(1, 4):  # 首次 + 2 次重试
                last_attempt = attempt
                llm_text = await self._call_llm(prompt)
                result = self._parse_llm_json_result(llm_text or "", target_id) if llm_text else None
                if result:
                    break
                if attempt < 3:
                    logger.warning(f"[是区吗][uid={uid}] LLM 尝试 {attempt}/3 失败，等待 40 秒后重试...")
                    await asyncio.sleep(40)
            if not result:
                self._call_logger.info(self._shiqu_log_entry(
                    openid=str(event.get_sender_id()), uid=uid, target_id=target_id,
                    success=False, attempt=last_attempt,
                    llm_response=llm_text or "",
                    error="LLM 调用异常 / 返回内容不是合法 JSON",
                    duration_ms=int((time.time() - t0) * 1000),
                ))
                await self._reset_cooldown(event)
                await self._set_pending(event)
                yield event.plain_result(f"❌ AI 判定生成失败：大模型调用异常 / 返回内容不是合法 JSON，已重置冷却，请重试 {_SHIQU_BTN} 。")
                return
            self._atomic_write_text(llm_raw_path, llm_text)
            t3 = time.time()
            logger.info(f"[是区吗][uid={uid}] LLM done ({len(prompt)}→{len(llm_text)}ch, llm={t3 - t2:.1f}s)")

            # ── 渲染图片 ──
            _ACTIVE_META[uid] = "渲染图片"
            generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            image_path = await self._render_result_image(result, generated_at=generated_at, uid=uid)
            # 缓存图片路径，供「是区吗结果」复用
            _LAST_IMAGE[uid] = image_path
            self._save_user_record(uid, {
                "uid": uid,
                "target_id": target_id,
                "generated_at": generated_at,
                "prompt_path": str(prompt_path),
                "llm_raw_path": str(llm_raw_path),
                "image_path": str(image_path),
                "result": result,
            })
            if image_path:
                try:
                    yield event.chain_result([Image.fromFileSystem(image_path)])
                    # 图片发送成功 → 标记，供白名单二次确认判断用
                    record = self._load_user_record(uid)
                    if record:
                        record["image_sent"] = True
                        self._save_user_record(uid, record)
                except Exception as exc:
                    err_msg = str(exc)
                    logger.error(f"[是区吗][uid={uid}] 发送判定书图片失败: {err_msg}")
                    if "违规" in err_msg or "violation" in err_msg.lower():
                        await self._set_violation_ban(event)
                        yield event.plain_result(
                            "⛔ 查询返回的图片内容包含违规信息，该指令已被禁用12小时，请勿再次尝试。"
                        )
                    else:
                        yield event.plain_result(f"❌ 发送判定书图片失败，请稍后重试 {_SHIQU_BTN} 。")
                    return
            else:
                yield event.plain_result(f"❌ 判定书图片渲染失败，请稍后重试 {_SHIQU_BTN} 。")
            logger.info(f"[是区吗][uid={uid}] 总耗时={time.time() - t0:.1f}s (render={time.time() - t3:.1f}s)")

            # 设置 CD
            await self._set_cooldown(event)

            self._call_logger.info(self._shiqu_log_entry(
                openid=str(event.get_sender_id()), uid=uid, target_id=target_id,
                success=True, attempt=last_attempt,
                verdict=str(result.get("verdict", "")),
                score=int(result.get("score", 0)),
                llm_chars=len(llm_text),
                llm_response=llm_text,
                duration_ms=int((time.time() - t0) * 1000),
            ))

        except Exception as exc:
            logger.error(f"[是区吗][uid={uid}] error: {exc}", exc_info=True)
            self._call_logger.info(self._shiqu_log_entry(
                openid=str(event.get_sender_id()), uid=uid, target_id=target_id,
                success=False, attempt=last_attempt,
                llm_response=locals().get("llm_text", ""),
                error=str(exc),
                duration_ms=int((time.time() - t0) * 1000),
            ))
            yield event.plain_result(f"❌ 生成判定书失败 {_SHIQU_BTN} ：{exc}")
        finally:
            await self._dequeue(uid)

    async def last_result(self, event):
        """读取用户上次判定结果图片直接返回，不重新渲染。"""
        uid = self._user_key(event)

        # ── 违规封禁检查 ──
        banned, ban_remain = await self._check_violation_ban(event)
        if banned:
            h, remainder = divmod(ban_remain, 3600)
            m, s = divmod(remainder, 60)
            remain_str = f"{int(h)}小时{int(m)}分钟" if h > 0 else f"{int(m)}分钟{int(s)}秒"
            yield event.plain_result(
                f"⛔ 您之前查询返回的图片内容包含违规信息，已被禁止使用是区吗指令 {remain_str}。\n"
                f"请勿再次尝试查询违规内容。"
            )
            return

        # ── 正在走流程 → 告知当前状态，不展示上次结果 ──
        async with _QUEUE_LOCK:
            if uid in _ACTIVE_META:
                yield event.plain_result(f"⏳ 判定书正在生成中，当前步骤：{_ACTIVE_META[uid]}，请稍候…")
                return
            if uid in _ACTIVE:
                yield event.plain_result("⏳ 判定书正在生成中，请稍候…")
                return
            for euid, _ in _QUEUE:
                if euid == uid:
                    yield event.plain_result("⏳ 您的判定书正在排队中，请稍候…")
                    return

        record = self._load_user_record(uid)
        if not record:
            await self._set_pending(event)
            yield event.plain_result(f"❌ 没有找到上次的判定书结果，请先用 {_SHIQU_BTN} 生成一份。")
            return

        # 有缓存图片 → 直接发
        cached_image = str(record.get("image_path") or "")
        if cached_image:
            # 兼容旧记录中可能是远程 URL 的情况
            if cached_image.startswith("http://") or cached_image.startswith("https://"):
                logger.warning(f"[是区吗][uid={uid}] 缓存图片为远程 URL（旧格式），尝试重新渲染")
                result_data = record.get("result")
                if isinstance(result_data, dict):
                    try:
                        cached_image = await self._render_result_image(
                            result_data,
                            generated_at=str(record.get("generated_at") or ""),
                            uid=uid,
                        )
                        if cached_image:
                            record["image_path"] = str(cached_image)
                            self._save_user_record(uid, record)
                    except Exception as e:
                        logger.error(f"[是区吗][uid={uid}] 旧 URL 迁移重新渲染失败: {e}")
                        cached_image = ""
                else:
                    cached_image = ""
            if cached_image and Path(cached_image).is_file():
                _LAST_IMAGE[uid] = cached_image
                try:
                    yield event.chain_result([Image.fromFileSystem(cached_image)])
                except Exception as exc:
                    err_msg = str(exc)
                    logger.error(f"[是区吗][uid={uid}] 发送结果图片失败: {err_msg}")
                    if "违规" in err_msg or "violation" in err_msg.lower():
                        await self._set_violation_ban(event)
                        yield event.plain_result(
                            "⛔ 查询返回的图片内容包含违规信息，该指令已被禁用12小时，请勿再次尝试。"
                        )
                return
            else:
                logger.warning(f"[是区吗][uid={uid}] 缓存图片路径无效: {cached_image}")

        # 无缓存图片但 result 还在 → 重新渲染
        result = record.get("result")
        if isinstance(result, dict):
            try:
                image_path = await self._render_result_image(result, generated_at=str(record.get("generated_at") or ""), uid=uid)
                if image_path:
                    _LAST_IMAGE[uid] = image_path
                    record["image_path"] = str(image_path)
                    self._save_user_record(uid, record)
                    try:
                        yield event.chain_result([Image.fromFileSystem(image_path)])
                    except Exception as send_exc:
                        err_msg = str(send_exc)
                        logger.error(f"[是区吗][uid={uid}] 发送重渲染图片失败: {err_msg}")
                        if "违规" in err_msg or "violation" in err_msg.lower():
                            await self._set_violation_ban(event)
                            yield event.plain_result(
                                "⛔ 查询返回的图片内容包含违规信息，该指令已被禁用12小时，请勿再次尝试。"
                            )
                        else:
                            yield event.plain_result(f"❌ 重新渲染判定书发送失败，请稍后重试 {_SHIQU_BTN} 。")
                else:
                    yield event.plain_result(f"❌ 重新渲染判定书失败，请稍后重试 {_SHIQU_BTN} 。")
            except Exception as exc:
                logger.error(f"[是区吗][uid={uid}] 重新渲染结果失败: {exc}", exc_info=True)
                yield event.plain_result(f"❌ 重新渲染判定书失败：{exc}")
            return

        await self._set_pending(event)
        yield event.plain_result(f"❌ 上次的判定结果不存在，请用 {_SHIQU_BTN} 重新生成一份。")
