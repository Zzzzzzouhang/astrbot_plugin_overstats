"""OW 开庭功能模块（薄代理）。

本模块不再在插件侧执行任何抓取 / Prompt / LLM / 渲染工作，
而是作为 Overstats 后端 `/dashen-court/image` 接口的薄代理：

1. 解析战网 ID（复用 `plugin._get_bnet_id`）；
2. 记录最近一次查询的 target_id（KV，供「结果」类指令复用）；
3. 调用后端接口取回判决书图片 bytes，经插件级发送助手发出
   （发送违规时由 `plugin._set_violation_ban` 自动封禁 12h，与原设计一致）；
4. 与是区吗共用分级 CD（shiqu_cd_map 配置）与白名单/管理员机制，CD 独立计时；
5. 跨功能并发守卫：同一用户 是区吗/开庭 最多一条在执行（共用 plugin._ow_executing）；
6. 保留「违规即封 12h」逻辑——由 features/court.py 在命令入口做
   `plugin._check_violation_ban(event, "开庭")` 检查，发送时由插件级助手自动封禁。
"""

import time
import json
import logging

logger = logging.getLogger("astrbot")

# 后端图片接口路径（base_url 已含 /api/v2，故此处不带前缀）
_COURT_IMAGE_ENDPOINT = "/dashen-court/image"

_COURT_COOLDOWN_KV_PREFIX = "ow_court_cooldown"
_COURT_LAST_TARGET_KV_PREFIX = "ow_court_last_target"

_COURT_BTN = '<qqbot-cmd-input text="ow开庭 " show="ow开庭" reference="false" />'
_COURT_RESULT_BTN = '<qqbot-cmd-input text="ow开庭结果" show="ow开庭结果" reference="false" />'


class CourtManager:
    def __init__(self, plugin):
        self._plugin = plugin

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

    # ── 分级 CD（与是区吗共用同一配置，但独立计时）──

    def _get_config_map(self) -> dict:
        """解析 shiqu_cd_map JSON 配置，失败返回空 dict。"""
        cd_raw = self._plugin.config.get("shiqu_cd_map", "{}") or "{}"
        try:
            if isinstance(cd_raw, dict):
                return cd_raw
            return json.loads(cd_raw)
        except Exception:
            return {}

    def _get_cd_seconds(self, event) -> int:
        """分级冷却秒数：管理员0 / 白名单30min / 普通4h（与是区吗共用配置）。"""
        cd_map = self._get_config_map()
        if self._is_admin(event):
            return max(0, int(cd_map.get("admin", 0) or 0))
        if self._plugin._is_whitelisted(event):
            return max(0, int(cd_map.get("whitelist", 1800) or 1800))
        return max(0, int(cd_map.get("normal", 14400) or 14400))

    async def _check_cooldown(self, event) -> tuple:
        """返回 (ok: bool, remaining_seconds: int)。与是区吗独立计时（不同 KV 前缀）。"""
        cd = self._get_cd_seconds(event)
        if cd <= 0:
            return True, 0
        key = f"{_COURT_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            last = await self._plugin.get_kv_data(key, 0)
            elapsed = int(time.time()) - int(last)
            if elapsed >= cd:
                return True, 0
            return False, cd - elapsed
        except Exception:
            return True, 0

    async def _set_cooldown(self, event):
        key = f"{_COURT_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, int(time.time()))
        except Exception:
            pass

    # ── 最近 target 记录 ──

    async def _get_last_target(self, event) -> str:
        key = f"{_COURT_LAST_TARGET_KV_PREFIX}:{self._user_key(event)}"
        try:
            return str(await self._plugin.get_kv_data(key, "") or "")
        except Exception:
            return ""

    async def _set_last_target(self, event, target_id: str):
        key = f"{_COURT_LAST_TARGET_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, target_id)
        except Exception:
            pass

    # ── 参数解析 ──────────────────────────────────────────────

    @staticmethod
    def _parse_args(arg1: str, arg2: str) -> tuple:
        """解析 ow开庭 [玩家] [序号]，返回 (bnet_id: str|None, index: int|None, error: str|None)。

        index 返回 0-based（后端口径）；前端 1-based。
        """
        bnet_id = None
        index = None
        error = None

        args = [a for a in (arg1, arg2) if a and a.strip()]
        for a in args:
            a = a.strip()
            if a.isdigit():
                index = int(a)
            else:
                bnet_id = a

        if index is None:
            error = ("用法：ow开庭 <battle_tag 或 序号> [序号]\n"
                     "示例：ow开庭 1  或  ow开庭 Player#12345 1\n序号 1 表示最近一局。")
            return bnet_id, index, error

        if index < 1:
            error = "序号必须 >= 1（1 = 最近一局）。"
            return bnet_id, index, error

        index = index - 1  # 前端 1-based → 后端 0-based
        return bnet_id, index, None

    # ── 主流程 ────────────────────────────────────────────────

    async def run_court(self, event, arg1: str = "", arg2: str = ""):
        """开庭主流程（薄代理）。与是区吗共用白名单/分级 CD 机制，CD 独立计时；
        同一用户同一时间 是区吗/开庭 最多一条在执行。"""
        t_start = time.time()

        # 1. 访问权限（与是区吗一致：管理员/白名单可用，普通用户受 normal_enabled 控制）
        if not self._is_admin(event) and not self._plugin._is_whitelisted(event):
            cd_map = self._get_config_map()
            if not cd_map.get("normal_enabled", False):
                yield event.plain_result("🔒 开庭功能暂未对普通用户开放。")
                return

        # 2. 跨功能并发守卫：同一用户 是区吗/开庭 最多一条在执行
        uid = self._user_key(event)
        if uid in self._plugin._ow_executing:
            yield event.plain_result("⏳ 你已有一条 OW 分析（是区吗/开庭）正在生成中，请稍候…")
            return
        self._plugin._ow_executing.add(uid)
        try:
            # 3. 分级冷却检查（与是区吗独立计算）
            ok, remaining = await self._check_cooldown(event)
            if not ok:
                m, s = divmod(remaining, 60)
                h, m = divmod(m, 60)
                remain_str = f"{int(h)}时{int(m)}分{int(s)}秒" if h > 0 else f"{int(m)}分{int(s)}秒"
                yield event.plain_result(f"⏳ 开庭冷却中，剩余 {remain_str}，届时再发 {_COURT_BTN} 开启新审理。")
                return

            # 4. 参数解析
            bnet_id, index, error = self._parse_args(arg1, arg2)
            if error:
                yield event.plain_result(error)
                return

            # 5. 战网 ID 解析
            target_id = await self._plugin._get_bnet_id(event, bnet_id)
            if not target_id:
                yield event.plain_result(
                    "❌ 请提供 BattleTag 或先使用 /绑定 绑定。\n"
                    "用法：ow开庭 <序号>  或  ow开庭 <battle_tag> <序号>"
                )
                return
            await self._set_last_target(event, target_id)

            logger.info(f"[开庭] {target_id} #{index + 1}  start")
            yield event.plain_result(
                f'⚖️ 正在为 {target_id} 的第 {index + 1} 局开庭审理（{_COURT_BTN}），'
                f'请稍候，可用参数：序号 战网ID 查询别人。'
            )

            # 6. 调后端图片接口取图并发送（违规时由 _send_image_result 自动封禁 12h）
            payload = {"bnet_id": target_id, "index": index, "use_db": False}
            img_bytes, error_data, _err_code = await self._plugin._fetch_image(
                _COURT_IMAGE_ENDPOINT, payload, timeout=900
            )
            if not img_bytes:
                msg = ""
                if isinstance(error_data, dict):
                    msg = str(error_data.get("message") or error_data.get("error") or "")
                yield event.plain_result(f"❌ {target_id} 第 {index + 1} 局开庭失败：{msg or '后端未返回图片'}。")
                return
            async for r in self._plugin._send_image_result(event, img_bytes, "开庭", ""):
                yield r

            await self._set_cooldown(event)
            logger.info(f"[开庭] done total={time.time() - t_start:.1f}s")
        finally:
            self._plugin._ow_executing.discard(uid)

    async def last_result(self, event):
        """读取用户上次查询的 target_id，调后端 use_db=true 取最近一次判决书图片发送。"""
        target_id = await self._get_last_target(event)
        if not target_id:
            yield event.plain_result(
                f"❌ 没有找到上次的判决书结果，请先用 {_COURT_BTN} 开庭审理一份。"
            )
            return
        img_bytes, error_data, _err_code = await self._plugin._fetch_image(
            _COURT_IMAGE_ENDPOINT, {"bnet_id": target_id, "use_db": True}, timeout=900
        )
        if not img_bytes:
            msg = ""
            if isinstance(error_data, dict):
                msg = str(error_data.get("message") or error_data.get("error") or "")
            yield event.plain_result(f"❌ 暂无 {target_id} 的最近判决书：{msg or '数据库记录缺失'}。")
            return
        async for r in self._plugin._send_image_result(event, img_bytes, "开庭", ""):
            yield r
