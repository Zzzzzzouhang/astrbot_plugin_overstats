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
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger("astrbot")

try:
    from .stat_db import build_reference_text, load_stat_name_map, normalize_stat_value
except ImportError:
    from stat_db import build_reference_text, load_stat_name_map, normalize_stat_value  # type: ignore[no-redef]

# 从 query_tool.json 加载游戏数据
_QTOOL = json.loads((Path(__file__).resolve().parent / "query_tool.json").read_text("utf-8"))
HERO_DICT = {h["heroGuid"]: {"name": h["name"], "role": h["roleType"]} for h in _QTOOL["heroList"]}
MAP_DICT = {m["guid"]: m["name"] for m in _QTOOL["mapList"]}

_MAX_CONCURRENT = 10
_QUEUE: deque = deque()
_ACTIVE: set = set()
_ACTIVE_META: dict[str, str] = {}  # uid → 当前步骤描述，用于重复触发时告知进度
_LAST_IMAGE: dict[str, str] = {}   # uid → 上次生成的图片文件路径
_QUEUE_LOCK = asyncio.Lock()

# ── AstrBot 文转图模板 ──────────────────────────────────────

_SHIQU_HTML_TMPL = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#12161e; color:#dce1eb; font-family:"PingFang SC","Microsoft YaHei",sans-serif; font-size:22px; line-height:1.85; padding:28px 24px; }
  h1 { font-size:30px; text-align:center; color:#f0b47c; margin-bottom:8px; padding-bottom:16px; border-bottom:2px solid #2a3040; }
  h2 { font-size:26px; color:#c9986a; margin:24px 0 10px; }
  h3 { font-size:24px; color:#e0c090; margin:18px 0 8px; }
  p { margin:6px 0; }
  p.gamen { margin:10px 0; line-height:1.85; }
  p.gamen b { color:#e8d5b7; }
  p.gamen span { color:#dce1eb; font-weight:normal; }
  p.mate { margin:10px 0; color:#b0bec5; }
  .score { font-size:48px; text-align:center; color:#ffd700; font-weight:bold; margin:12px 0; }
  .verdict { display:block; font-size:32px; text-align:center; font-weight:bold; margin:4px 0 32px; }
  .verdict.god,.iv.god { color:#e67e22; }
  .verdict.ok,.iv.ok { color:#4ecdc4; }
  .verdict.mid,.iv.mid { color:#f9ca24; }
  .verdict.bad,.iv.bad { color:#e17055; }
  .verdict.terrible,.iv.terrible { color:#d63031; }
  .iv { font-weight:bold; }
  .footer { margin-top:28px; padding-top:12px; border-top:1px solid #2a3040; color:#6e7681; font-size:13px; text-align:center; }
</style></head><body>
  <h1>{{ title }}</h1>
  <div class="body">{{ body|safe }}</div>
  <div class="footer">{{ footer }}</div>
</body></html>'''


class ShiquManager:
    def __init__(self, plugin):
        self._plugin = plugin

    # ── 权限 ──

    def _is_admin(self, event) -> bool:
        try:
            return self._plugin._is_astrbot_admin(event)
        except Exception:
            return False

    def check_access(self, event) -> bool:
        if self._is_admin(event):
            return True
        return self._plugin._is_whitelisted(event)

    # ── 排队 ──

    def _user_key(self, event) -> str:
        try:
            return f"{event.get_platform_name()}:{event.get_sender_id()}"
        except Exception:
            return str(event.get_sender_id())

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
            logger.warning(f"[是区吗] 拉取 index={index} 失败: {e}")
            return None

    async def _fetch_12_matches(self, bnet_id: str) -> list[dict]:
        tasks = [self._fetch_one_match(bnet_id, i) for i in range(12)]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    async def _fetch_teammate_details(self, detail_data: dict, target_id: str, index: int) -> None:
        """串行请求焦点玩家队伍每个成员的详细英雄数据，附加 _heroList 到玩家对象。"""
        tm_list = detail_data.get("teammateList", [])
        for p in tm_list:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if p.get("_heroList"):
                continue
            try:
                data = await self._fetch_one_match(name, index)
                if data:
                    pd = (data.get("detail", {}) or {}).get("data") or {}
                    hl = pd.get("heroList", [])
                    if hl:
                        p["_heroList"] = hl
            except Exception as e:
                logger.warning(f"[是区吗] 获取队友 {name} 详细失败: {e}")

    async def _serial_fetch_all_teammate_details(self, matches: list[dict], target_id: str) -> None:
        """对所有对局串行拉取队友详细数据。"""
        for idx, m in enumerate(matches):
            detail_data = (m.get("detail", {}) or {}).get("data") or {}
            if isinstance(detail_data, dict):
                await self._fetch_teammate_details(detail_data, target_id, idx)

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

        def _rate(v, t): return f"{v * 600 / max(t, 60):.1f}" if t > 0 else str(v)

        def _hero_detail_text(p) -> str:
            """将 _heroList 中的 statMap 转成可读文本（与 DB 归一化口径一致）。"""
            hl = p.get("_heroList")
            if not hl or not isinstance(hl, list):
                return ""
            name_map = load_stat_name_map()
            parts = []
            for entry in hl:
                if not isinstance(entry, dict):
                    continue
                sm = entry.get("statMap", {}) or {}
                ut = float(entry.get("userTimeSec", 600) or 600)
                for guid, raw_val in sm.items():
                    name = name_map.get(guid)
                    if not name:
                        continue
                    nv = normalize_stat_value(raw_val, ut, value_text=name, value_guid=guid)
                    if nv is None:
                        continue
                    if nv == int(nv):
                        parts.append(f"{name}: {int(nv)}")
                    else:
                        parts.append(f"{name}: {nv:.2f}")
            return ", ".join(parts) if parts else ""

        def _fmt_player(p, pos, game_sec, detail=""):
            name = str(p.get("name", "?"))
            hg = str(p.get("heroGuid", ""))
            hn = HERO_DICT.get(hg, {}).get("name", "?")
            display = f"*{name}" if name == target_id else name
            parts = [f"位置: {pos}", f"玩家: {display}", f"英雄: {hn}"]
            for k, cn in STAT_KEYS:
                v = int(p.get(k, 0) or 0)
                parts.append(f"{cn}/10min: {_rate(v, game_sec)}")
            if detail:
                parts.append(f"详细: {{ {detail} }}")
            return "{ " + ", ".join(parts) + " }"

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
            def _append_players(label, players):
                lines.append(f"  [{label}]")
                lines.append("  [")
                for p, pos in _sort_and_label(players):
                    hd = _hero_detail_text(p)
                    lines.append(f"    {_fmt_player(p, pos, game_sec, hd)},")
                    if db_path:
                        hg = str(p.get("heroGuid", ""))
                        hn = HERO_DICT.get(hg, {}).get("name", "?")
                        ref = build_reference_text(db_path, str(p.get("name", "?")), hg, hn)
                        if ref:
                            lines.append(f"    # 分段参考: {ref}")
                lines.append("  ],")

            if tm:
                _append_players("队友", tm)
            if en:
                _append_players("对手", en)
            lines.append("}")
            lines.append("")

        n = len(matches)

        # ── 队友汇总 ──
        tm_summary = {}
        for m in matches:
            detail_data = (m.get("detail", {}) or {}).get("data") or {}
            game_sec = float(detail_data.get("gameTimeSec", 600) or 600)
            ret = detail_data.get("matchRet")
            for p in detail_data.get("teammateList", []):
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name", ""))
                if name == target_id:
                    continue
                if name not in tm_summary:
                    tm_summary[name] = {"games": 0, "wins": 0, "total_sec": 0, "k": 0, "d": 0, "a": 0, "dmg": 0, "heal": 0, "heroes": {}}
                r = tm_summary[name]
                r["games"] += 1
                if ret == 1:
                    r["wins"] += 1
                r["total_sec"] += game_sec
                r["k"] += int(p.get("kill", 0) or 0)
                r["d"] += int(p.get("death", 0) or 0)
                r["a"] += int(p.get("assist", 0) or 0)
                r["dmg"] += int(p.get("heroDamage", 0) or 0)
                r["heal"] += int(p.get("cure", 0) or 0)
                hg = str(p.get("heroGuid", ""))
                hn = HERO_DICT.get(hg, {}).get("name", hg)
                r["heroes"][hn] = r["heroes"].get(hn, 0) + 1

        if tm_summary:
            lines.append("[焦点玩家的队友（共同游戏≥2局）]")
            lines.append("[")
            for name in sorted(tm_summary):
                r = tm_summary[name]
                if r["games"] < 2:
                    continue
                tg = max(r["total_sec"], 1)
                heroes = ", ".join(f"{h}x{c}" for h, c in r["heroes"].items())
                lines.append(f"  {{ 玩家: {name}, 共同局数: {r['games']}/{n}, 胜率: {r['wins']}/{r['games']}, "
                            f"击杀/10min: {r['k']*600/tg:.1f}, 阵亡/10min: {r['d']*600/tg:.1f}, "
                            f"伤害/10min: {int(r['dmg']*600/tg):,}, 治疗/10min: {int(r['heal']*600/tg):,}, "
                            f"英雄: {heroes} }},")
            lines.append("]")
            lines.append("")

        match_text = "\n".join(lines)

        return f"""你是守望先锋串子型数据分析师，圈内人称"数据带阴阳师"。

【角色定义】
1. 深耕守望先锋全英雄机制、版本环境、职业赛事与国服天梯生态，所有点评 100% 以游戏数据为唯一依据，拒绝空口黑屁、主观臆断。熟稔「我是区吗」全社区梗文化，对国服鱼塘到高分段的众生相了如指掌，鉴区准确率堪比官方外挂检测。
2. 人设底色：表面永远保持「我只是个念数据的中立人」的客观嘴脸，语气平淡像读财报，实则字字藏刀、句句带刺，精准戳中玩家最痛的操作痛点；核心立场是纯纯乐子人，看数据如同看乐子，毒舌但不恶毒，嘲讽只锁死游戏表现，绝不越界人身攻击。
3. 核心信条：数据不会说谎，但我可以用它扎心。每一句阴阳都必须有对应数据做支点，做到「字字有出处，句句能扎心」；永远不直接说「你菜」，只把数据摆出来，再贴心地帮你翻译翻译什么叫大区。
4. 说话习惯：擅长用反问、反讽、明褒暗贬、假装惋惜的语气输出暴击；常用「不是哥们？」「挺好的，就是没用」「不至于吧」「数据摆这了，你自己品」这类软刀子开场白；永远一副「我没骂你啊，我只是陈述事实」的无辜感。

【硬性约束】
- 仅针对游戏内数据、赛场表现、英雄操作效率点评，绝不涉及外貌、私生活、人品等人身攻击；不输出任何歧视、引战、恶意辱骂内容。
- 所有解读严格基于提供的原始数据，禁止编造数据、篡改数据含义、夸大数据结论；数据是刀，你只是持刀人，不能自己造刀。
- 严禁跨职责直接比较伤害/治疗等核心指标，每一句阴阳调侃必须对应明确的数据论据，做到"字字有出处，句句有支撑"。
- 不讨论外挂、代练等违规行为。
- 禁止进行反事实推演或假设性陈述（如"你本可以多拿3个击杀"），仅限描述已发生事件。

【评判规则】
1. 不同职责的核心指标优先级（所有数值均为每10分钟均值，与分段参考数据口径一致）：
   坦克位：(伤害-受疗) > 阵亡数 > 击杀参与率 > 助攻数
   输出位：单独消灭 > 最后一击 > 伤害 > 阵亡数 > 击杀参与率 > 助攻数
   辅助位：伤害量 ≈> 治疗量 > 阵亡数 > 击杀数 > 击杀参与率 >> 助攻数
2. 数据对比与评分：
   - 将焦点玩家数据与同英雄"# 分段参考行"对比。低于分段中位数应扣分。
   - 最后一击和单独消灭应额外加分，频繁阵亡且贡献低 → 加重扣分。
   - 百分制评分标准：
     * ≥85 = 你是职业吗？
     * >70 = 暴力炸！
     * 60~70 = 恭喜，你不是区
     * 55~59 = 不幸，你可能是区？
     * <55 = 别看了，你就是区！
     * <43 = 你个大区！！！
3. 综合判定：综合 {n} 场比赛中职责对应核心指标、KDA、胜负、英雄选择等因素进行评分。
4. 队友点评规则：
   - 共同少于6局的胜率无统计学意义，仅供参考。
   - 仅对共同≥2局的队友逐一评分。
   - 评分标准同焦点玩家（≥50夸/赞赏，<50串）。

【阴阳话术库（示例）】
数据显示：[指标]=[数值]，高于/低于分段参考[X%]——这数据，怕不是在给对面回蓝？
你的走位很有想象力，可惜伤害结算在了空气上。
恭喜啊，用实力证明了「辅助」和「被辅助」的区别。
这波操作，完美诠释了什么叫「无效阵亡」。
建议把「最后一击」截图珍藏，毕竟这种高光时刻不多见。

【输出格式】严格使用中文，纯文本（一行一句，不要 markdown，不要代码块，不要任何 ** 或 _ 标记）：

🔍 {target_id} 是区吗判定书

评分：XX/100
[判定文字，如：恭喜，你不是区]

数据概况：
一两句话概括 {n} 场整体表现，提及与分段均值的对比。

逐局点评：
第1局：胜/负/平，英雄名 —— 1~2句毒舌点评（好的夸差的串）
第2局：同上
（共 {n} 局）

综合评价：
约 300 字，串子风格阴阳总结，有数据支撑，可少量使用 emoji 增强表达力。

队友点评：
- 玩家名（共同X局，胜率Y%）：评分XX/100，判定文字 —— 1~2句毒舌点评
（≥50 夸/赞赏，<50 串；共同少于6局的胜率仅供参考，不作为核心评判依据）

【比赛数据】
{match_text}
"""

    # ── LLM 调用 ──

    async def _call_astrbot_llm(self, event, prompt: str) -> Optional[str]:
        """通过 AstrBot 内置 LLM 生成是区吗判定。"""
        try:
            umo = event.unified_msg_origin
            provider_id = None
            try:
                provider_id = await self._plugin.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                try:
                    provider_id = await self._plugin.context.get_current_chat_provider_id()
                except Exception:
                    pass

            if not provider_id:
                logger.error("[是区吗] LLM provider_id not found")
                return None

            resp = await self._plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            return (resp.completion_text if resp else None) or None
        except Exception as e:
            logger.error(f"[是区吗] 调用 AstrBot LLM 失败: {e}")
            return None

    # ── 纯文本 → HTML ──

    @staticmethod
    def _plain_to_html(text: str) -> str:
        """将 LLM 输出的纯文本转为 HTML 正文，与 tests/simulate_shiqu_render.py 渲染一致。"""
        # 转义 HTML 特殊字符
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # ── 评分着色（按档位）──
        def _score(m):
            s = int(m.group(1))
            if s >= 85:
                c = "#e67e22"
            elif s > 70:
                c = "#ff6b6b"
            elif s >= 60:
                c = "#4ecdc4"
            elif s >= 55:
                c = "#f9ca24"
            elif s >= 43:
                c = "#e17055"
            else:
                c = "#d63031"
            return f'<div class="score" style="color:{c}">{s}/100</div>'

        text = re.sub(r"评分：\s*(\d+)/100", _score, text)

        # ── 判定文字着色 ──
        _VERDICT_CLASS = {
            "你是职业吗？": "god",
            "暴力炸！": "god",
            "恭喜，你不是区": "ok",
            "不幸，你可能是区？": "mid",
            "别看了，你就是区！": "bad",
            "你个大区！！！": "terrible",
        }
        # 主判定行（独立一行）→ block div
        def _verdict(m):
            v = m.group(1).strip(",.;.")
            cls = _VERDICT_CLASS.get(v, "")
            return f'<div class="verdict {cls}">{v}</div>'

        verdict_pat = "|".join(re.escape(k) for k in _VERDICT_CLASS)
        text = re.sub(rf"^({verdict_pat})$", _verdict, text, flags=re.M)

        # 队友行 "判定：xxx" → 内联着色
        for v, cls in _VERDICT_CLASS.items():
            text = text.replace(f"判定：{v}", f'判定：<span class="iv {cls}">{v}</span>')

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
            # 已处理过的标签 → 直接保留
            if s.startswith("<h") or s.startswith("<div") or s.startswith("<span"):
                buf.append(s)
            # 单局条目："第N局：... " → 按 " —— " 分割为加粗头部 + 普通点评
            elif s.startswith("第") and "局" in s:
                if " —— " in s:
                    head, tail = s.split(" —— ", 1)
                    buf.append(f'<p class="gamen"><b>{head}</b> —— <span>{tail}</span></p>')
                else:
                    buf.append(f'<p class="gamen"><b>{s}</b></p>')
            # 队友条目："- 玩家名...":"
            elif s.startswith("- "):
                buf.append(f'<p class="mate">{s}</p>')
            # 标题行（已被转义但仍以 🔍 开头且含 是区吗判定书）
            elif "是区吗判定书" in s and s.startswith("🔍"):
                buf.append(f'<h1>{s.replace("🔍 ", "")}</h1>')
            else:
                buf.append(f"<p>{s}</p>")

        body = "\n".join(buf)

        # ── 去重：如果 build_html 返回结果中出现了裸标题 h1，移除一次 ──
        body = re.sub(r"<h1>是区吗判定书</h1>\s*", "", body, count=1)

        return body

    # ── 主流程 ──

    async def run(self, event, bnet_id_input: str = ""):
        uid = self._user_key(event)

        if not self.check_access(event):
            yield event.plain_result("🔒 是区吗功能处于测试阶段，仅对白名单/管理员开放。")
            return

        # 获取 bnet_id
        target_id = await self._plugin._get_bnet_id(event, bnet_id_input)
        if not target_id:
            yield event.plain_result("❌ 请提供 BattleTag，或先使用 /绑定 绑定。\n用法：是区吗 <battle_tag>")
            return

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

        try:
            # 仅一条进度消息
            yield event.plain_result(f"🔍 正在生成 {target_id} 是区吗判定书")

            t0 = time.time()
            _ACTIVE_META[uid] = "拉取对局数据"
            matches = await self._fetch_12_matches(target_id)
            t1 = time.time()
            logger.info(f"📊 已获取 {len(matches)} 场 ({(t1 - t0):.1f}s)")

            if len(matches) < 2:
                yield event.plain_result(f"❌ {target_id} 仅获取到 {len(matches)} 场对局，至少需要 2 场。")
                return

            _ACTIVE_META[uid] = "拉取队友详细数据"
            await self._serial_fetch_all_teammate_details(matches, target_id)
            t2 = time.time()
            logger.info(f"📊 队友数据拉取完成 ({(t2 - t1):.1f}s)")

            # 保存 JSON
            data_dir = self._plugin.plugin_data_dir / "shiqu"
            data_dir.mkdir(parents=True, exist_ok=True)
            json_path = data_dir / f"{target_id.replace('#','_')}_12matches.json"
            json_path.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")

            # 构建 prompt
            db_path = str(getattr(self._plugin, "config", {}).get("sqlite_db_path", "") or "").strip()
            prompt = self._build_prompt(matches, target_id, db_path=db_path)
            prompt_path = data_dir / f"{target_id.replace('#','_')}_prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            logger.info(f"✅ Prompt 已生成 ({len(prompt)} 字符)")

            # ── 调用 LLM ──
            _ACTIVE_META[uid] = "AI 生成判定"
            llm_text = await self._call_astrbot_llm(event, prompt)
            if not llm_text:
                yield event.plain_result("❌ AI 判定生成失败：LLM 调用异常，请稍后重试。")
                return
            t3 = time.time()
            logger.info(f"[是区吗] LLM done ({len(prompt)}→{len(llm_text)}ch, llm={t3 - t2:.1f}s)")

            # ── 渲染图片 ──
            _ACTIVE_META[uid] = "渲染图片"
            title = f"是区吗判定书 · {target_id}"
            footer = time.strftime("生成时间：%Y-%m-%d %H:%M") + "  ·  AI 数据带阴阳师 (Astrbot LLM)"
            body_html = self._plain_to_html(llm_text)
            url = await self._plugin.html_render(
                _SHIQU_HTML_TMPL,
                {"title": title, "body": body_html, "footer": footer},
                options={"type": "png"},
            )
            # 缓存图片路径，供「是区吗结果」复用
            _LAST_IMAGE[uid] = url
            yield event.image_result(url)
            logger.info(f"[是区吗] 总耗时={time.time() - t0:.1f}s (render={time.time() - t3:.1f}s)")

        except Exception as exc:
            logger.error(f"[是区吗] error: {exc}", exc_info=True)
            yield event.plain_result(f"❌ 生成判定书失败：{exc}")
        finally:
            await self._dequeue(uid)

    async def last_result(self, event):
        """返回用户上次生成的判定书图片。"""
        if not self.check_access(event):
            yield event.plain_result("🔒 是区吗功能处于测试阶段，仅对白名单/管理员开放。")
            return
        uid = self._user_key(event)
        path = _LAST_IMAGE.get(uid)
        if not path or not Path(path).exists():
            yield event.plain_result("❌ 没有找到上次的判定书结果，请先用「是区吗」生成一份。")
            return
        yield event.image_result(path)
