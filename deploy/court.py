"""OW 开庭功能模块（测试阶段）

将开庭核心逻辑从 main.py 抽离，独立封装以避免代码臃肿。
使用 AstrBot 内置 LLM 实现电竞法庭风格的对局分析。
输出渲染为 PNG 图片，保存到临时目录后发送。
测试阶段严格限制访问：仅白名单群组/用户及 AstrBot 管理员可用。
"""

import json
import re
import time
import logging
from pathlib import Path
from typing import Optional

# OW 游戏数据（从 query_tool.json 加载）
try:
    from .stat_db import build_reference_text, load_stat_name_map, normalize_stat_value
except ImportError:
    from stat_db import build_reference_text, load_stat_name_map, normalize_stat_value  # type: ignore[no-redef]

_QTOOL = json.loads((Path(__file__).resolve().parent / "query_tool.json").read_text("utf-8"))
HERO_DICT = {h["heroGuid"]: {"name": h["name"], "role": h["roleType"]} for h in _QTOOL["heroList"]}
MAP_DICT = {m["guid"]: m["name"] for m in _QTOOL["mapList"]}
_MODE_DICT = {
    "quick": "快速", "QuickPlay": "快速", "IT_QUICKPLAY": "快速",
    "sport": "竞技", "SportPreset": "竞技（预设职责）", "IT_RANKED": "竞技",
    "sportfight": "角斗竞技", "SportFight": "角斗竞技", "IT_STADIUM": "角斗竞技",
    "quickfight": "角斗快速", "LeisureFight": "角斗快速", "IT_FIGHT": "角斗快速",
}

logger = logging.getLogger("astrbot")

_COURT_COOLDOWN_SECONDS = 60
_COURT_COOLDOWN_KV_PREFIX = "ow_court_cooldown"

# 用于辅助 LLM 更友好地展示英雄名称
_EMOJI_MAP_TEXT = """
【常用英雄名称参考（请按数据匹配实际英雄名）】
• 坦克(Tank) > D.Va / 莱因哈特 / 温斯顿 / 查莉娅 / 路霸 / 奥丽莎 / 破坏球 / 末日铁拳 / 西格玛 / 渣客女王 / 拉玛刹 / 毛加 / 骇灾 / 金驭
• 输出(DPS) > 士兵76 / 死神 / 源氏 / 猎空 / 卡西迪 / 法老之鹰 / 黑百合 / 半藏 / 托比昂 / 狂鼠 / 堡垒 / 美 / 秩序之光 / 艾什 / 回声 / 黑影 / 索杰恩 / 探奇 / 弗蕾娅 / 安燃 / 埃姆雷 / 斩仇 / 西拉 / 死怨
• 辅助(Support) > 天使 / 卢西奥 / 禅雅塔 / 安娜 / 莫伊拉 / 布丽吉塔 / 巴蒂斯特 / 雾子 / 生命之梭 / 伊拉锐 / 朱诺 / 无漾 / 瑞稀 / 飞天猫
"""

# ── AstrBot 文转图模板 ──────────────────────────────────────

_COURT_HTML_TMPL = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#12161e; color:#dce1eb; font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC","WenQuanYi Micro Hei",sans-serif; font-size:17px; line-height:1.7; padding:36px 44px; }
  .title-bar { background:#1c202d; margin:-36px -44px 24px; padding:18px 44px; border-bottom:2px solid #f59e0b; }
  .title-bar h1 { color:#f59e0b; font-size:26px; font-weight:700; }
  .body p { margin:8px 0; }
  .body li { margin:3px 0; }
  .footer { margin-top:32px; padding-top:12px; border-top:1px solid #2a3040; color:#788296; font-size:13px; }
</style></head><body>
  <div class="title-bar"><h1>{{ title }}</h1></div>
  <div class="body">{{ body|safe }}</div>
  <div class="footer">{{ footer }}</div>
</body></html>'''


class CourtManager:

    def __init__(self, plugin):
        self._plugin = plugin

    # ── 权限检查 ──────────────────────────────────────────────

    def check_access(self, event) -> bool:
        """测试阶段权限：仅 AstrBot 管理员、白名单群组、白名单用户可触发。"""
        if self._plugin._is_astrbot_admin(event):
            return True
        return self._plugin._is_whitelisted(event)

    # ── 冷却检查 ──────────────────────────────────────────────

    def _user_key(self, event) -> str:
        try:
            return f"{event.get_platform_name()}:{event.get_sender_id()}"
        except Exception:
            return str(event.get_sender_id())

    async def _check_cooldown(self, event) -> tuple:
        """返回 (ok: bool, remaining_seconds: int)。"""
        key = f"{_COURT_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            last = await self._plugin.get_kv_data(key, 0)
            elapsed = int(time.time()) - int(last)
            if elapsed >= _COURT_COOLDOWN_SECONDS:
                return True, 0
            return False, _COURT_COOLDOWN_SECONDS - elapsed
        except Exception:
            return True, 0

    async def _set_cooldown(self, event):
        key = f"{_COURT_COOLDOWN_KV_PREFIX}:{self._user_key(event)}"
        try:
            await self._plugin.put_kv_data(key, int(time.time()))
        except Exception:
            pass

    # ── 参数解析 ──────────────────────────────────────────────

    @staticmethod
    def _parse_args(arg1: str, arg2: str) -> tuple:
        """解析 ow开庭 [玩家] [序号]，返回 (bnet_id: str|None, index: int|None, error: str|None)。"""
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
            error = "用法：ow开庭 <battle_tag 或 序号> [序号]\n示例：ow开庭 1  或  ow开庭 Player#12345 1\n序号 1 表示最近一局。"
            return bnet_id, index, error

        if index < 1:
            error = "序号必须 >= 1（1 = 最近一局）。"
            return bnet_id, index, error

        index = index - 1  # 前端 1-based → 后端 0-based
        return bnet_id, index, None

    # ── 获取对局原始数据 ──────────────────────────────────────

    async def _get_match_raw_data(self, bnet_id: str, index: int) -> Optional[dict]:
        """调用 Overstats API 获取单局原始详情 JSON。"""
        url = f"{self._plugin.base_url}/dashen-match/detail"
        payload = {"bnet_id": bnet_id, "index": index}
        try:
            session = await self._plugin._get_http_session()
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("detail"):
                    return data
                logger.warning(f"开庭-获取对局数据失败: {data.get('message', 'no detail')}")
                return None
        except Exception as e:
            logger.error(f"开庭-请求 Overstats API 异常: {e}")
            return None

    async def _fetch_teammate_details(self, detail_data: dict, target_id: str, index: int) -> None:
        """串行请求焦点玩家队伍每个成员的详细英雄数据，附加 _heroList 到玩家对象。"""
        tm_list = detail_data.get("teammateList", [])
        for p in tm_list:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if name == target_id or p.get("_heroList"):
                continue
            try:
                data = await self._get_match_raw_data(name, index)
                if data:
                    pd = (data.get("detail", {}) or {}).get("data") or {}
                    hl = pd.get("heroList", [])
                    if hl:
                        p["_heroList"] = hl
            except Exception as e:
                logger.warning(f"[开庭] 获取队友 {name} 详细失败: {e}")

    # ── 构建开庭 Prompt ──────────────────────────────────────

    @staticmethod
    def _build_court_prompt(raw_data: dict, target_id: str, db_path: str = "") -> str:
        """基于 API 返回的对局数据构建电竞法庭 LLM prompt。

        API 返回结构:
        {
          ok,
          detail: { code, success, data: { mapGuid, matchRet, teamScore,
            opponentScore, gameTimeSec, teammateList, enemyList, heroList,
            startTime, gameMode }, traceId },
          source_match: { mapGuid, matchRet, teamScore, opponentScore,
            instanceType, heroGuid, roleType, kill, death, ... },
          resolved: { bnet_id, full_id, ... },
          match_id, match_kind, customer_token
        }
        """
        detail = raw_data.get("detail", {}) or {}
        # 实际对局数据在 detail.data 下，而非 detail 顶层
        detail_data = detail.get("data") or {} if isinstance(detail, dict) else {}
        source = raw_data.get("source_match", {}) or {}

        # ── 地图 GUID → 中文名 ──
        map_guid = str(detail_data.get("mapGuid") or source.get("mapGuid") or "")
        map_name = MAP_DICT.get(map_guid, "未知")

        # ── 格式化比赛概览 ──
        result_map = {1: "胜利", 0: "平局", -1: "失败"}
        match_ret = detail_data.get("matchRet", source.get("matchRet"))
        result_text = result_map.get(match_ret, str(match_ret))
        team_score = detail_data.get("teamScore", source.get("teamScore", "?"))
        opp_score = detail_data.get("opponentScore", source.get("opponentScore", "?"))
        game_sec = detail_data.get("gameTimeSec", 0) or 0
        duration = f"{int(game_sec // 60):02d}:{int(game_sec % 60):02d}"

        # ── 游戏模式 ──
        raw_mode = str(detail_data.get("gameMode") or source.get("gameMode")
                       or source.get("instanceType") or "")
        game_mode = _MODE_DICT.get(raw_mode, raw_mode)

        # ── 四维属性分（移植自后端 calculate_match_scores）──
        tm_list_raw = detail_data.get("teammateList", [])
        en_list_raw = detail_data.get("enemyList", [])

        def _calc_scores() -> dict:
            """计算防碾压/团队/进攻/质量四项属性分 (0-100)。"""
            def _sum(players: list, key: str) -> float:
                return sum(float(p.get(key, 0) or 0) for p in players if isinstance(p, dict))

            try:
                t = tm_list_raw
                e = en_list_raw
                gt = float(detail_data.get("gameTimeSec", 1) or 1)

                # 抗压分 anti_pressure
                s_time = min(100.0, (gt / 1200.0) * 100.0)
                t_obj = _sum(t, "targetCompetingTime")
                e_obj = _sum(e, "targetCompetingTime")
                s_obj = (t_obj / (t_obj + e_obj) * 100.0) if (t_obj + e_obj) > 0 else 50.0
                t_block = _sum(t, "resistDamage")
                e_block = _sum(e, "resistDamage")
                s_block = (t_block / (t_block + e_block) * 100.0) if (t_block + e_block) > 0 else 50.0
                t_heal = _sum(t, "cure")
                t_death = _sum(t, "death")
                e_heal = _sum(e, "cure")
                e_death = _sum(e, "death")
                t_hd = t_heal / max(1.0, t_death)
                e_hd = e_heal / max(1.0, e_death)
                s_hd = (t_hd / (t_hd + e_hd) * 100.0) if (t_hd + e_hd) > 0 else 50.0
                total_death = t_death + e_death
                s_death = ((1.0 - (t_death / total_death)) * 100.0) if total_death > 0 else 50.0
                anti_pressure = s_time * 0.15 + s_obj * 0.2 + s_block * 0.2 + s_hd * 0.25 + s_death * 0.2

                # 团队分 teamwork
                t_elim = _sum(t, "kill")
                t_fb = _sum(t, "finalHit")
                t_assist = _sum(t, "assist")
                teamwork = min(100.0, (((t_elim - t_fb) + t_assist) / max(1.0, t_elim)) * 60.0)

                # 进攻分 aggressiveness
                t_dmg = _sum(t, "heroDamage")
                e_dmg = _sum(e, "heroDamage")
                e_elim = _sum(e, "kill")
                e_fb = _sum(e, "finalHit")
                s_dmg = (t_dmg / (t_dmg + e_dmg) * 100.0) if (t_dmg + e_dmg) > 0 else 50.0
                s_kill = (t_elim / (t_elim + e_elim) * 100.0) if (t_elim + e_elim) > 0 else 50.0
                s_fb = (t_fb / (t_fb + e_fb) * 100.0) if (t_fb + e_fb) > 0 else 50.0
                aggressiveness = s_dmg * 0.4 + s_kill * 0.3 + s_fb * 0.3

                # 质量分 match_quality
                def _balance(v1: float, v2: float) -> float:
                    return 1.0 - abs(v1 - v2) / max(1e-9, v1 + v2)
                balance_score = (_balance(t_dmg, e_dmg) + _balance(t_elim, e_elim) + _balance(t_heal, e_heal)) / 3.0 * 100.0
                match_quality = s_time * 0.4 + balance_score * 0.6

                return {
                    "anti_pressure": int(anti_pressure),
                    "teamwork": int(teamwork),
                    "aggressiveness": int(aggressiveness),
                    "match_quality": int(match_quality),
                }
            except Exception:
                return {"anti_pressure": 0, "teamwork": 0, "aggressiveness": 0, "match_quality": 0}

        scores = _calc_scores()

        lines = [
            "[对局概览]",
            "{",
            f"  地图: {map_name},",
            f"  模式: {game_mode},",
            f"  结果: {result_text} (比分 {team_score}:{opp_score}),",
            f"  时长: {duration},",
            f"  焦点玩家: {target_id},",
            f"  属性分: 抗压{scores['anti_pressure']} 团队{scores['teamwork']} 进攻{scores['aggressiveness']} 质量{scores['match_quality']}",
            "}",
            "",
        ]

        # ── 本地反推玩家位置 ──
        ROLE_ORDER = {"tank": 0, "dps": 1, "healer": 2}
        ROLE_LABEL_ZH = {"tank": "坦克", "dps": "输出", "healer": "辅助"}

        def _get_role(player: dict) -> str:
            """通过 heroGuid 反推玩家职责。"""
            hero_guid = str(player.get("heroGuid", ""))
            role = HERO_DICT.get(hero_guid, {}).get("role", "unknown")
            return role

        def _sort_and_label(players: list) -> list:
            """按职责排序（坦克→输出→辅助），并附加位置标签。

            返回: [(player_dict, position_label), ...]
            """
            if not players:
                return []
            # (sort_key, original_index, player)
            indexed = [
                (ROLE_ORDER.get(_get_role(p), 9), i, p)
                for i, p in enumerate(players) if isinstance(p, dict)
            ]
            indexed.sort(key=lambda x: (x[0], x[1]))  # 按角色优先级，同角色保持原始顺序
            # 分配位置标签
            role_counters = {}
            labeled = []
            for _, _, p in indexed:
                role = _get_role(p)
                cnt = role_counters.get(role, 0) + 1
                role_counters[role] = cnt
                label_zh = ROLE_LABEL_ZH.get(role, role)
                if sum(1 for pp in players if isinstance(pp, dict) and _get_role(pp) == role) > 1:
                    # 同职责多人在队伍中时加编号
                    position_label = f"{label_zh}{cnt}"
                else:
                    position_label = label_zh
                labeled.append((p, position_label))
            return labeled

        # ── 格式化对阵双方 ──
        # 玩家对象中对 LLM 有意义的数值型统计字段（排除 ID / 元数据 / 布尔标记 / 嵌套对象）
        _SKIP_FIELDS = {
            "name", "bnetId", "heroGuid", "heroIcon", "customerToken",
            "beginTs", "friendBnetIds", "endorserBnetIds", "perks",
            "killMax", "cureMax", "heroDamageMax", "resistDamageMax",
            "targetCompetingTime",  # 已从统计列表中移除
        }
        # 数值统计字段 → 中文名（按优先级排序）
        _STAT_FIELD_ORDER = [
            ("kill", "击杀"), ("assist", "助攻"), ("death", "阵亡"),
            ("finalHit", "最后一击"), ("heroDamage", "伤害"),
            ("damageTaken", "承伤"), ("cure", "治疗"),
            ("healingTaken", "受疗"), ("resistDamage", "格挡"),
        ]
        def _collect_all_stat_keys(players: list) -> list:
            """动态收集所有玩家中可解析的数值型统计字段名（排除已覆盖和无关字段）。"""
            covered = {k for k, _ in _STAT_FIELD_ORDER}
            covered.update(_SKIP_FIELDS)
            seen = set()
            extra = []
            for p in players:
                if not isinstance(p, dict):
                    continue
                for k, v in p.items():
                    if k in covered or k in seen:
                        continue
                    if isinstance(v, (int, float)):
                        seen.add(k)
                        extra.append((k, k))
            return extra

        _BIG_NUM_FIELDS = {"heroDamage", "damageTaken", "cure", "healingTaken", "resistDamage"}

        def _fmt_player(p: dict, pos_label: str, detail: str = "") -> str:
            """将单个玩家格式化为一行 JSON-like 对象字符串。"""
            name = str(p.get("name", "?"))
            hero_guid = str(p.get("heroGuid", ""))
            hero_name = HERO_DICT.get(hero_guid, {}).get("name", "?")
            display = f"*{name}" if name == target_id else name

            pairs = [f"位置: {pos_label}", f"玩家: {display}", f"英雄: {hero_name}"]
            for key, cn in _STAT_FIELD_ORDER:
                v = p.get(key, 0) or 0
                formatted = f"{int(v):,}" if key in _BIG_NUM_FIELDS else str(int(v))
                pairs.append(f"{cn}: {formatted}")
            # 动态额外字段
            for key, _ in extra_stat_keys:
                v = p.get(key, 0) or 0
                formatted = f"{int(v):,}" if key in _BIG_NUM_FIELDS else str(int(v))
                pairs.append(f"{key}: {formatted}")
            if detail:
                pairs.append(f"详细: {{ {detail} }}")
            return "{ " + ", ".join(pairs) + " }"

        def _hero_detail_text(p_dict: dict) -> str:
            """将 _heroList 中的 statMap 转成可读文本（与 DB 归一化口径一致）。"""
            hl = p_dict.get("_heroList")
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

        def fmt_team(label: str, players: list) -> None:
            if not players:
                return
            sorted_players = _sort_and_label(players)

            # 动态收集额外统计字段（队间共享）
            nonlocal extra_stat_keys
            if not extra_stat_keys:
                extra_stat_keys = _collect_all_stat_keys(players)

            lines.append(f"[{label}]")
            lines.append("[")
            for p, pos_label in sorted_players:
                hd = _hero_detail_text(p)
                lines.append(f"  {_fmt_player(p, pos_label, hd)},")
                if db_path:
                    hg = str(p.get("heroGuid", ""))
                    hn = HERO_DICT.get(hg, {}).get("name", "?")
                    ref = build_reference_text(db_path, str(p.get("name", "?")), hg, hn)
                    if ref:
                        lines.append(f"    # 分段参考: {ref}")
            lines.append("]")
            lines.append("")

        # 额外统计字段（跨队复用，只扫描一次）
        extra_stat_keys = []
        all_players = detail_data.get("teammateList", []) + detail_data.get("enemyList", [])
        extra_stat_keys = _collect_all_stat_keys(all_players)
        # all_players 仅用于动态字段扫描，DB 参考已在 fmt_team 内逐人插入

        fmt_team("队友", detail_data.get("teammateList", []))
        fmt_team("对手", detail_data.get("enemyList", []))

        # ── 降级：如果都没提取到，塞原始 JSON ──
        if not detail_data.get("teammateList") and not detail_data.get("enemyList"):
            lines.append("[注意：未能提取结构化数据，以下为原始 JSON，请自行解析字段含义]")
            lines.append(json.dumps(detail, ensure_ascii=False, indent=2))

        match_text = "\n".join(lines)

        return f"""你是电竞法庭的主审法官。本庭今日审理的是一场守望先锋对局。你需要以绝对中立的视角，基于数据证据，做出公正判决。

【输出要求】
1. 必须严格使用中文，只输出纯文本（不要 markdown 代码块），严格遵循输出格式，结构清晰。
2. 判决必须基于数据事实，不允许主观臆测或无端指责。
3. 好的表现必须肯定，差的表现必须严厉批判，不留情面。

【属性分说明】
对局概览中的四项属性分（0-100）由双方队伍汇总数据自动计算：
- 抗压分：基于目标时间、格挡、治疗/死亡比、死亡率，反映队伍的抗压与生存能力
- 团队分：基于助攻与最终击杀占比，反映队伍的配合与协同程度
- 进攻分：基于伤害占比、击杀占比、最终击杀占比，反映队伍的进攻火力
- 质量分：基于对局时长与双方数据均衡度，反映比赛的整体质量
判决时应结合属性分，高分项说明队伍在该维度表现优异，低分则反之。

【审判任务】
1. 从焦点玩家所在队伍的队友（不含对手）中找出本局 MVP（表现最佳者），给出判决理由。
2. 从焦点玩家所在队伍的队友（不含对手）中找出本局最差玩家（被告），给出判决理由和"原罪清单"（具体犯了哪些错误，用数据说话）。
3. 对焦点玩家做出判决：是功臣还是罪人，给出评分 S/A/B/C/D。
4. 对焦点玩家所在队伍的所有玩家逐一做出有功/有过/无功无过的判决，附一句话理由。
5. 分析三路对位差距：数据已按坦克→输出→辅助排序。对位规则：坦克位一对一比较；输出位整体比较（我方输出组 vs 对方输出组，不要拆分编号）；辅助位整体比较（我方辅助组 vs 对方辅助组，不要拆分编号）。

{_EMOJI_MAP_TEXT}

【输出格式】
⚖️ **电竞法庭判决书**

📋 **案件编号**：第 {{index_hint}} 局
🗺️ **案发地点**：（地图名）

🏆 **MVP（最佳表现者）**：（姓名）——（理由）

👎 **最差表现者（被告）**：（姓名）
**原罪清单**：
- ...

⚡ **焦点玩家判决**：（姓名）—— 评分：X
（详细理由）

📊 **全队审判**：
- {{target_player}}：...
- （队友名）：...
- ...

⚔️ **三路对位分析**：（输出位和辅助位均整体对比，无需拆编号一对一）
- 坦克位：我方（英雄名）vs 对方（英雄名）—— 对比分析
- 输出位：我方（英雄1 + 英雄2）vs 对方（英雄1 + 英雄2）—— 整体对比输出火力与击杀效率
- 辅助位：我方（英雄1 + 英雄2）vs 对方（英雄1 + 英雄2）—— 整体对比治疗量与生存/功能性

【比赛数据（数组格式，每个玩家一个对象）】
{match_text}
"""


    # ── 主流程 ────────────────────────────────────────────────

    async def run_court(self, event, arg1: str = "", arg2: str = ""):
        """开庭主流程。"""
        t_start = time.time()

        # 1-4. 权限 / 冷却 / 参数 / BattleTag
        if not self.check_access(event):
            yield event.plain_result(
                "🔒 OW 开庭功能处于测试阶段，仅对白名单群组/用户及 AstrBot 管理员开放。"
            )
            return
        ok, remaining = await self._check_cooldown(event)
        if not ok:
            yield event.plain_result(f"⏳ AI 开庭冷却中，请 {remaining} 秒后再试。")
            return
        bnet_id, index, error = self._parse_args(arg1, arg2)
        if error:
            yield event.plain_result(error)
            return
        target_id = await self._plugin._get_bnet_id(event, bnet_id)
        if not target_id:
            yield event.plain_result(
                "❌ 请提供 BattleTag 或先使用 /绑定 绑定。\n"
                "用法：ow开庭 <序号>  或  ow开庭 <battle_tag> <序号>"
            )
            return

        logger.info(f"[开庭] {target_id} #{index + 1}  start")
        yield event.plain_result(f"⚖️ 正在为 {target_id} 的第 {index + 1} 局开庭审理，请稍候...")

        try:
            # 5. 拉数据
            raw_data = await self._get_match_raw_data(target_id, index)
            if not raw_data:
                yield event.plain_result("❌ 获取对局数据失败，请确认对局记录存在。")
                return
            detail = raw_data.get("detail", {}) or {}
            detail_data = detail.get("data") or {} if isinstance(detail, dict) else {}
            t_fetch = time.time()
            logger.info(f"[开庭] data ok ({len(detail_data.get('teammateList', []))}v{len(detail_data.get('enemyList', []))} "
                        f"fetch={t_fetch - t_start:.1f}s)")

            # 5.5 串行拉取队友详细英雄数据
            await self._fetch_teammate_details(detail_data, target_id, index)
            t_teammates = time.time()
            logger.info(f"[开庭] teammates detail done ({t_teammates - t_fetch:.1f}s)")

            # 6. prompt + LLM
            db_path = str(getattr(self._plugin, "config", {}).get("sqlite_db_path", "") or "").strip()
            prompt = self._build_court_prompt(raw_data, target_id, db_path=db_path)
            prompt = prompt.replace("{index_hint}", str(index + 1)).replace("{target_player}", target_id)
            llm_text = await self._call_astrbot_llm(event, prompt)
            if not llm_text:
                yield event.plain_result("❌ AI 开庭审理失败：LLM 调用异常，请稍后重试。")
                return
            t_llm = time.time()
            logger.info(f"[开庭] LLM done ({len(prompt)}->{len(llm_text)}ch, "
                        f"llm={t_llm - t_fetch:.1f}s)")

            # 7. 渲染图片
            title = f"电竞法庭 · 第 {index + 1} 局判决"
            footer = time.strftime("生成时间：%Y-%m-%d %H:%M") + "  ·  AI 开庭 (Astrbot LLM)"
            body_html = _md_to_html(llm_text)
            url = await self._plugin.html_render(
                _COURT_HTML_TMPL,
                {"title": title, "body": body_html, "footer": footer},
                options={"type": "png"},
            )
            yield event.image_result(url)

            await self._set_cooldown(event)
            logger.info(f"[开庭] done total={time.time() - t_start:.1f}s "
                        f"(render={time.time() - t_llm:.1f}s)")

        except Exception as exc:
            logger.error(f"[开庭] error: {exc}", exc_info=True)
            yield event.plain_result(f"❌ 开庭过程发生错误：{exc}")

    # ── LLM 调用 ──────────────────────────────────────────────

    async def _call_astrbot_llm(self, event, prompt: str) -> Optional[str]:
        """通过 AstrBot 内置 LLM 生成开庭判决。"""
        try:
            umo = event.unified_msg_origin
            provider_id = None
            try:
                provider_id = await self._plugin.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                # 降级：尝试不传 umo
                try:
                    provider_id = await self._plugin.context.get_current_chat_provider_id()
                except Exception:
                    pass

            if not provider_id:
                logger.error("[开庭] LLM provider_id not found")
                return None

            resp = await self._plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            return (resp.completion_text if resp else None) or None
        except Exception as e:
            logger.error(f"开庭-调用 AstrBot LLM 失败: {e}")
            return None


# ── Markdown → HTML 转换 ────────────────────────────────────

def _md_to_html(text: str) -> str:
    """将 LLM 输出的简单 Markdown 转为 HTML，供 Jinja2 |safe 渲染。

    处理: **粗体**、- 无序列表、1. 有序列表、换行 → <br>。
    """
    # 先转义 HTML 特殊字符（防止 XSS，同时避免 <> 在 HTML 中被解析）
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = text.split("\n")
    out = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        # 空行 → 结束列表
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br>")
            continue

        # 无序列表
        if stripped.startswith(("- ", "* ", "+ ")):
            content = stripped[2:]
            if not in_list:
                out.append('<ul style="margin:4px 0;padding-left:24px;">')
                in_list = True
            out.append(f"<li>{_inline_md(content)}</li>")
            continue

        # 有序列表
        m = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
        if m:
            content = m.group(2)
            if not in_list:
                out.append('<ul style="margin:4px 0;padding-left:24px;">')
                in_list = True
            out.append(f"<li>{_inline_md(content)}</li>")
            continue

        # 普通段落
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline_md(stripped)}</p>")

    if in_list:
        out.append("</ul>")

    return "\n".join(out)


def _inline_md(text: str) -> str:
    """处理行内 Markdown：**粗体**、*斜体*、`代码`。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r'<code style="background:#2a3040;padding:1px 5px;border-radius:3px;">\1</code>', text)
    return text
