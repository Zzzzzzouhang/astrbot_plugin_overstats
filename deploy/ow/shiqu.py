"""OW 是区吗功能模块（薄代理）。

本模块不再在插件侧执行任何抓取 / Prompt / LLM / 渲染工作，
而是作为 Overstats 后端 `/dashen-shiqu/image` 接口的薄代理：

1. 解析战网 ID（复用 `plugin._get_bnet_id`）；
2. 记录最近一次查询的 target_id（KV，供「结果」类指令复用）；
3. 调用后端接口取回判定书图片 bytes，经插件级发送助手发出
   （发送违规时由 `plugin._set_violation_ban` 自动封禁 12h，与原设计一致）；
4. 保留按用户分级 CD、每日首发（凌晨 4 点重置）、pending 二次确认 UX；
5. 保留「违规即封 12h」逻辑——由 features/court.py 在命令入口做
   `plugin._check_violation_ban(event, "是区吗")` 检查，发送时由插件级助手自动封禁。
"""

import asyncio
import time
import json
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger("astrbot")

# ── KV 键前缀 ──
_SHIQU_COOLDOWN_KV_PREFIX = "ow_shiqu_cooldown"
_SHIQU_PENDING_KV_PREFIX = "ow_shiqu_pending"
_SHIQU_LAST_TARGET_KV_PREFIX = "ow_shiqu_last_target"
_SHIQU_LAST_QUERY_TS_KV_PREFIX = "ow_shiqu_last_query_ts"
_SHIQU_IMAGE_SENT_KV_PREFIX = "ow_shiqu_image_sent"
_SHIQU_PENDING_SECONDS = 300  # 5 分钟内再发确认
_OW_EXECUTING_TIMEOUT = 300  # 锁自动释放时间（秒）

# 后端图片接口路径（base_url 已含 /api/v2，故此处不带前缀）
_SHIQU_IMAGE_ENDPOINT = "/dashen-shiqu/image"

_SHIQU_BTN = '<qqbot-cmd-input text="是区吗" show="是区吗" reference="false" />'
_SHIQU_RESULT_BTN = '<qqbot-cmd-input text="ow是区吗结果" show="ow是区吗结果" reference="false" />'


class ShiquManager:
    def __init__(self, plugin):
        self._plugin = plugin
        # 并发守卫所用集合已上移到 plugin._ow_executing（与开庭共用，确保同一用户最多一条在执行）。


    # ── 权限 ──

    def _is_admin(self, event) -> bool:
        try:
            return self._plugin._is_astrbot_admin(event)
        except Exception:
            return False

    def _user_key(self, event) -> str:
        try:
            return f"{event.get_platform_name()}:{event.get_sender_id()}"
        except Exception:
            return str(event.get_sender_id())

    # ── 分级 CD ──

    def _get_config_map(self) -> dict:
        """解析 shiqu_cd_map JSON 配置，失败返回空 dict。

        AstrBot 将 type=text 的 shiqu_cd_map 以 JSON 字符串形式返回，
        需 json.loads；若配置直接是 dict（部分运行环境）则原样返回。
        """
        cd_raw = self._plugin.config.get("shiqu_cd_map", "{}") or "{}"
        try:
            if isinstance(cd_raw, dict):
                return cd_raw
            return json.loads(cd_raw)
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
        """将冷却时间戳置 0（失败兜底）：下次发送因 elapsed>=cd 且 cd_raw==0 而直接查询，跳过 pending 二次确认。"""
        key = f"{_SHIQU_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, 0)
        except Exception:
            pass

    async def _set_image_sent(self, event, ok: bool):
        """记录上次查询图片是否成功发送，供白名单跳过二次确认快路径使用。"""
        key = f"{_SHIQU_IMAGE_SENT_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, 1 if ok else 0)
        except Exception:
            pass

    async def _is_image_sent(self, event) -> bool:
        key = f"{_SHIQU_IMAGE_SENT_KV_PREFIX}:{self._user_key(event)}"
        try:
            return int(await self._plugin.get_kv_data(key, 0) or 0) == 1
        except Exception:
            return False


    # ── pending 二次确认 ──

    async def _set_pending(self, event, ts: int = None):
        """设置 pending 时间戳；ts=0 表示清除 pending。"""
        key = f"{_SHIQU_PENDING_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, int(ts if ts is not None else time.time()))
        except Exception:
            pass

    async def _is_pending(self, event) -> bool:
        key = f"{_SHIQU_PENDING_KV_PREFIX}:{self._user_key(event)}"
        try:
            pending_ts = await self._plugin.get_kv_data(key, 0)
        except Exception:
            return False
        pending_ts = int(pending_ts or 0)
        return pending_ts > 0 and (int(time.time()) - pending_ts) < _SHIQU_PENDING_SECONDS

    # ── 每日首发（凌晨 4 点重置）──

    @staticmethod
    def _get_today_4am_ts() -> int:
        """返回最近一次凌晨 4:00 的 Unix 时间戳（本地时间）。"""
        now = time.time()
        local = time.localtime(now)
        today_4am = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 4, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst))
        if now < today_4am:
            today_4am -= 86400
        return int(today_4am)

    async def _is_first_today(self, event) -> bool:
        """通过 KV 中记录的最后成功查询时间戳判断今天（凌晨4点重置）是否首次使用。"""
        key = f"{_SHIQU_LAST_QUERY_TS_KV_PREFIX}:{self._user_key(event)}"
        try:
            last_ts = await self._plugin.get_kv_data(key, 0)
        except Exception:
            return True
        last_ts = int(last_ts or 0)
        if last_ts <= 0:
            return True
        return last_ts < self._get_today_4am_ts()

    # ── 最近 target 记录 ──

    async def _get_last_target(self, event) -> str:
        key = f"{_SHIQU_LAST_TARGET_KV_PREFIX}:{self._user_key(event)}"
        try:
            return str(await self._plugin.get_kv_data(key, "") or "")
        except Exception:
            return ""

    async def _set_last_target(self, event, target_id: str):
        key = f"{_SHIQU_LAST_TARGET_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, target_id)
        except Exception:
            pass

    # ── 主流程 ──

    async def run(self, event, bnet_id_input: str = "", match_count: int = 0):
        uid = self._user_key(event)
        # 跨功能并发守卫：同一用户 是区吗 / 开庭 最多一条在执行（与开庭共用 plugin._ow_executing）。
        # 后端接口无法推送状态，故在进行中只返回固定提示。
        if uid in self._plugin._ow_executing:
            yield event.plain_result("⏳ 判定书正在生成中，当前步骤：AI 生成判定，请稍候…")
            return
        # 普通用户开关
        if not self._is_admin(event) and not self._plugin._is_whitelisted(event):
            cd_map = self._get_config_map()
            if not cd_map.get("normal_enabled", False):
                yield event.plain_result("🔒 是区吗功能暂未对普通用户开放。")
                return
        self._plugin._ow_executing.add(uid)
        # 自动超时释放锁（_OW_EXECUTING_TIMEOUT 秒后），避免后端卡死导致锁永远不释放
        _timeout_task: Optional[asyncio.Task] = None

        async def _auto_release():
            try:
                await asyncio.sleep(_OW_EXECUTING_TIMEOUT)
                self._plugin._ow_executing.discard(uid)
                logger.warning(f"[是区吗][uid={uid}] 锁超时自动释放 ({_OW_EXECUTING_TIMEOUT}s)")
            except asyncio.CancelledError:
                pass

        _timeout_task = asyncio.create_task(_auto_release())
        try:

            # 获取 bnet_id
            target_id = await self._plugin._get_bnet_id(event, bnet_id_input)
            if not target_id:
                yield event.plain_result(f"❌ 请提供 战网id，或先使用 /绑定 绑定。\n用法：{_SHIQU_BTN} &lt;battle_tag&gt;")
                return
            # 注意：last_target 不在此处提前写入，避免用未验证/后端无记录的新 ID
            # 覆盖掉可用的历史 target；改为在 _do_query 查询成功后再写入。

            # 分级 CD
            cd_ok, cd_remain = await self._check_cooldown(event)
            is_first_today = await self._is_first_today(event)
            is_pending = await self._is_pending(event)

            if not cd_ok:
                m, s = divmod(cd_remain, 60)
                h, m = divmod(m, 60)
                remain_str = f"{int(h)}时{int(m)}分{int(s)}秒" if h > 0 else f"{int(m)}分{int(s)}秒"
                yield event.plain_result(f"⏳ 冷却中，剩余 {remain_str}，届时再发 {_SHIQU_BTN} 开启新查询。")
                return

            if is_first_today or is_pending:
                # 直接开启新查询（每日首次 / pending 二次确认）
                if is_pending:
                    await self._set_pending(event, 0)
                async for r in self._do_query(event, uid, target_id, match_count=match_count):
                    yield r
                return

            # CD 已过、非首发、非 pending → 错误恢复优先，其次白名单快路径，否则展示上次结果+二次确认
            # ── 失败兜底：cooldown 被 _reset_cooldown 置零（上次查询失败）→ 直接查询，跳过确认（对齐原版错误恢复）──
            cd_key = f"{_SHIQU_COOLDOWN_KV_PREFIX}:{uid}"
            try:
                cd_raw = int(await self._plugin.get_kv_data(cd_key, -1) or -1)
            except Exception:
                cd_raw = -1
            if cd_raw == 0:
                async for r in self._do_query(event, uid, target_id, match_count=match_count):
                    yield r
                return

            # ── 白名单快路径：上次图片发送成功则跳过二次确认直接查询 ──
            if self._plugin._is_whitelisted(event) and await self._is_image_sent(event):
                async for r in self._do_query(event, uid, target_id, match_count=match_count):
                    yield r
                return

            # 普通路径：先发送上次结果图，再设 pending 等待二次确认
            last_target = await self._get_last_target(event)
            last_ok = False
            if last_target:
                img_bytes, _error_data, _err_code = await self._plugin._fetch_image(
                    _SHIQU_IMAGE_ENDPOINT, {"bnet_id": last_target, "use_db": True}, timeout=630
                )
                if img_bytes:
                    last_ok = True
                    async for r in self._plugin._send_image_result(event, img_bytes, "是区吗", ""):
                        yield r
            if last_ok:
                await self._set_pending(event)
                yield event.plain_result(f'👋 以下是上次的结果，如要开启新查询，请在 **5 分钟内**再次发送 {_SHIQU_BTN}。')
            else:
                # 取不到上次结果（后端无该 target 历史记录 / 从未成功查询过 / 取图失败）：
                # 设置 pending 并引导用户在 5 分钟内再发一次，下次即因 is_pending=True 直接开启新查询，
                # 避免在此处原地反复输出提示而不推进状态机导致的死锁。
                await self._set_pending(event)
                yield event.plain_result(f'ℹ️ 暂无可用的最近结果。请在 **5 分钟内**再次发送 {_SHIQU_BTN} 开启新查询。')
        finally:
            if _timeout_task is not None and not _timeout_task.done():
                _timeout_task.cancel()
            self._plugin._ow_executing.discard(uid)

    def _record_cmd(self, cmd_name: str, success: bool, error_code: str = ''):
        """记录指令真实执行结果到监控采集器（成功/失败均计入，修复成功率恒 100% 问题）。"""
        monitor = getattr(self._plugin, 'monitor', None)
        if monitor:
            asyncio.ensure_future(monitor.record_command(cmd_name, success, error_code=error_code))

    async def _do_query(self, event, uid: str, target_id: str, match_count: int = 0):

        """执行实际的新查询：调用后端接口取图并发送，成功后写 CD 与首日标记。"""
        t0 = time.time()
        yield event.plain_result(
            f'🔍 正在生成 {target_id} 是区吗判定书，5分钟未自动返回，请使用{_SHIQU_RESULT_BTN}'
        )
        payload = {"bnet_id": target_id, "use_db": False}
        if 0 < match_count <= 25:
            payload["match_count"] = match_count

        # 单次请求后端图片接口（违规时由 _send_image_result 自动封禁 12h）
        # 是区吗为 AI 生成类功能：失败不重试，避免重复高成本生成，直接提示用户重发。
        img_bytes, error_data, _err_code = await self._plugin._fetch_image(
            _SHIQU_IMAGE_ENDPOINT, payload, timeout=630, retry=False
        )
        if not img_bytes:
            msg = ""
            if isinstance(error_data, dict):
                msg = str(error_data.get("message") or error_data.get("error") or "")
            if 'Could not resolve customerToken' in msg:
                # ID 解析失败：复用插件统一文案（含"受大神接口维护影响"提示），避免透传英文技术错误
                yield self._plugin._plain_error_result(event, self._plugin._id_resolve_err(f'{target_id} 是区吗查询失败'))
            else:
                yield event.plain_result(f"❌ {target_id} 是区吗查询失败：{msg or '后端未返回图片'}。请重试 {_SHIQU_BTN}")
            # 失败兜底：重置冷却为 0，使下次发送因 cd_raw==0 直接查询，跳过 pending 二次确认（对齐原版错误恢复）。
            await self._reset_cooldown(event)
            await self._set_image_sent(event, False)
            self._record_cmd('ow是区吗', False, error_code=_err_code)
            return
        async for r in self._plugin._send_image_result(event, img_bytes, "是区吗", ""):
            yield r
        self._record_cmd('ow是区吗', True)

        # 成功：写入本次 target（供「结果」类指令与普通路径复用）、CD 与首日标记
        await self._set_last_target(event, target_id)
        await self._set_cooldown(event)
        await self._set_image_sent(event, True)
        key = f"{_SHIQU_LAST_QUERY_TS_KV_PREFIX}:{uid}"
        try:
            await self._plugin.put_kv_data(key, int(time.time()))
        except Exception:
            pass
        logger.info(f"[是区吗][uid={uid}] 查询完成 total={time.time() - t0:.1f}s")

    async def last_result(self, event):
        """读取用户上次查询的 target_id，调后端 use_db=true 取最近一次判定书图片发送。"""
        uid = self._user_key(event)
        target_id = await self._get_last_target(event)
        if not target_id:
            yield event.plain_result(f"❌ 没有找到上次的判定书结果，请先用 {_SHIQU_BTN} 生成一份。")
            return
        img_bytes, error_data, _err_code = await self._plugin._fetch_image(
            _SHIQU_IMAGE_ENDPOINT, {"bnet_id": target_id, "use_db": True}, timeout=900, retry=False
        )
        if not img_bytes:
            msg = ""
            if isinstance(error_data, dict):
                msg = str(error_data.get("message") or error_data.get("error") or "")
            yield event.plain_result(f"❌ 暂无 {target_id} 的最近判定书：{msg or '数据库记录缺失'}。")
            self._record_cmd('ow是区吗结果', False, error_code=_err_code)
            return
        async for r in self._plugin._send_image_result(event, img_bytes, "是区吗", ""):
            yield r
        self._record_cmd('ow是区吗结果', True)

    # ── 连通性检测（指向后端 /healthz）──

    async def test_connectivity(self):
        """测试后端可达性，返回 (ok, message)。"""
        base = str(self._plugin.base_url or "")
        health_url = base.rsplit("/api/v2", 1)[0].rstrip("/") + "/healthz"
        try:
            t0 = time.time()
            session = await self._plugin._get_http_session()
            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                elapsed = (time.time() - t0) * 1000
                text = await resp.text()
                if resp.status == 200:
                    return True, f"✅ 后端可达 ({elapsed:.0f}ms)\n{text[:200]}"
                return False, f"❌ 后端返回 HTTP {resp.status}\n{text[:300]}"
        except Exception as e:
            return False, f"❌ 后端连接异常: {e}"
