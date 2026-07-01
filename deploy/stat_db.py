"""Overstats SQLite 统计数据库读取模块。

读取本地 match_stats.sqlite3 中的分段英雄统计数据（comp_data_summary 表），
为开庭功能提供分段参考均值。

数据库路径通过插件配置项 sqlite_db_path 指定，留空则不启用。
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("astrbot")

# ── 延迟加载 stat GUID → 中文名（从 deploy/query_tool.json 的 heroAttrList）──
_NAME_MAP: dict[str, str] | None = None
_SKIP_GUIDS = {"603482350067646497", "603482350067648623"}  # 游戏时间、英雄获胜
_SKIP_TEXTS = {"英雄获胜", "累计游戏时间", "累积游戏时间"}


# ── 与后端 normalize_dashen_hero_stat_value 等价的归一化 ──
_HERO_AVG_PERCENT_KEYWORDS = ("率", "效率", "占比")
_HERO_AVG_PERCENT_TEXTS = {"英雄获胜"}
_HERO_AVG_RAW_VALUE_TEXTS = {"英雄获胜", "累计游戏时间", "累积游戏时间"}
_HERO_AVG_RAW_VALUE_GUIDS = {"603482350067646497", "603482350067648623"}
_BROAD_REFERENCE_BUCKETS = (0, 4, 5, 6, 7, *range(25, 45))


def should_skip_prompt_stat(value_guid: str = "", value_text: str = "") -> bool:
    """这些字段不参与提示词构造。"""
    return str(value_guid or "") in _SKIP_GUIDS or str(value_text or "") in _SKIP_TEXTS


def _is_percent_stat(value_text: str) -> bool:
    text = str(value_text or "")
    return text in _HERO_AVG_PERCENT_TEXTS or any(kw in text for kw in _HERO_AVG_PERCENT_KEYWORDS)


def _is_raw_stat(value_text: str = "", value_guid: str = "") -> bool:
    return str(value_guid or "") in _HERO_AVG_RAW_VALUE_GUIDS or str(value_text or "") in _HERO_AVG_RAW_VALUE_TEXTS


def normalize_stat_value(value, user_time_sec: float, value_text: str = "", value_guid: str = ""):
    """归一化英雄统计值，与后端 normalize_dashen_hero_stat_value 等价。
    返回: 归一化后的 float，或 None。"""
    if value is None:
        return None
    try:
        v = float(value)
        ut = float(user_time_sec or 0)
    except (TypeError, ValueError):
        return None
    # 1) 百分比类 → clamp [0,1]
    if _is_percent_stat(value_text):
        return max(0.0, min(1.0, v))
    # 2) 原始值类 → 保持原值
    if _is_raw_stat(value_text, value_guid):
        return v
    # 3) 默认 → 归一化为每10分钟值
    time_coef = ut / 600.0
    if time_coef <= 0:
        return None
    return v / time_coef


def load_stat_name_map() -> dict[str, str]:
    return _load_name_map()

def _load_name_map() -> dict[str, str]:
    """懒加载 stat GUID → 中文名映射。"""
    global _NAME_MAP
    if _NAME_MAP is not None:
        return _NAME_MAP
    try:
        cfg_path = Path(__file__).resolve().parent / "query_tool.json"
        cfg = json.loads(cfg_path.read_text("utf-8"))
        _NAME_MAP = {a["valueGuid"]: a["valueText"] for a in cfg.get("heroAttrList", [])}
    except Exception as e:
        logger.warning(f"[stat_db] 加载 query_tool.json 失败: {e}")
        _NAME_MAP = {}
    return _NAME_MAP


def _normalize_rank_bucket(rank_score: Optional[int]) -> Optional[int]:
    """将段位分数归一化到 rank_bucket（除以 100 取整）。"""
    if rank_score is None:
        return None
    try:
        rs = int(rank_score)
    except (TypeError, ValueError):
        return None
    if rs >= 100:
        return rs // 100
    return rs


def query_hero_stat_averages(
    db_path: str,
    hero_guid: str,
    rank_score: Optional[int] = None,
) -> dict[str, dict[str, Any]]:
    """查询指定英雄在本地的分段统计数据。

    Args:
        db_path: SQLite 数据库文件路径
        hero_guid: 英雄 GUID
        rank_score: 玩家段位分数（用于匹配分段）

    Returns:
        {stat_guid: {"median": float, "avg": float, "samples": int}, ...}
        空字典表示无数据或数据库不可用。
    """
    path = Path(db_path)
    if not path.is_file():
        logger.warning(f"[stat_db] 数据库文件不存在: {db_path}")
        return {}

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rank_bucket = _normalize_rank_bucket(rank_score)

        if rank_bucket is not None:
            # 优先按分段查
            rows = conn.execute(
                """SELECT statmap_name, median_value, avg_value, sample_count
                   FROM comp_data_summary
                   WHERE hero_guid = ? AND rank_bucket_key = ?
                   ORDER BY statmap_name""",
                (hero_guid, rank_bucket),
            ).fetchall()
            if not rows:
                # 降级：不区分分段
                rows = conn.execute(
                    """SELECT statmap_name, median_value, avg_value, sample_count
                       FROM comp_data_summary
                       WHERE hero_guid = ?
                       ORDER BY statmap_name""",
                    (hero_guid,),
                ).fetchall()
        else:
            rows = conn.execute(
                """SELECT statmap_name, median_value, avg_value, sample_count
                   FROM comp_data_summary
                   WHERE hero_guid = ?
                   ORDER BY statmap_name""",
                (hero_guid,),
            ).fetchall()

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            guid = row["statmap_name"]
            if should_skip_prompt_stat(value_guid=guid):
                continue
            result[guid] = {
                "median": row["median_value"],
                "avg": row["avg_value"],
                "samples": row["sample_count"],
            }
        conn.close()
        return result
    except Exception as e:
        logger.warning(f"[stat_db] 查询失败 (hero={hero_guid}): {e}")
        return {}


def query_hero_stat_averages_for_buckets(
    db_path: str,
    hero_guid: str,
    rank_buckets: tuple[int, ...] = _BROAD_REFERENCE_BUCKETS,
) -> dict[str, dict[str, Any]]:
    """查询指定英雄在多个段位 bucket 的聚合参考数据。"""
    path = Path(db_path)
    if not path.is_file():
        logger.warning(f"[stat_db] 数据库文件不存在: {db_path}")
        return {}
    if not rank_buckets:
        return {}

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in rank_buckets)
        rows = conn.execute(
            f"""SELECT statmap_name, median_value, avg_value, sample_count
                FROM comp_data_summary
                WHERE hero_guid = ? AND rank_bucket_key IN ({placeholders})
                ORDER BY statmap_name""",
            (hero_guid, *rank_buckets),
        ).fetchall()
        conn.close()

        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            guid = row["statmap_name"]
            if should_skip_prompt_stat(value_guid=guid):
                continue
            grouped.setdefault(guid, []).append(row)

        result: dict[str, dict[str, Any]] = {}
        for guid, stat_rows in grouped.items():
            weighted_median = 0.0
            weighted_avg = 0.0
            weight_sum = 0
            fallback_medians = []
            fallback_avgs = []
            for row in stat_rows:
                samples = int(row["sample_count"] or 0)
                median = row["median_value"]
                avg = row["avg_value"]
                if median is not None:
                    fallback_medians.append(float(median))
                    if samples > 0:
                        weighted_median += float(median) * samples
                if avg is not None:
                    fallback_avgs.append(float(avg))
                    if samples > 0:
                        weighted_avg += float(avg) * samples
                if samples > 0:
                    weight_sum += samples

            if weight_sum > 0:
                median_value = weighted_median / weight_sum if fallback_medians else None
                avg_value = weighted_avg / weight_sum if fallback_avgs else None
            else:
                median_value = sum(fallback_medians) / len(fallback_medians) if fallback_medians else None
                avg_value = sum(fallback_avgs) / len(fallback_avgs) if fallback_avgs else None
            if median_value is None:
                continue
            result[guid] = {"median": median_value, "avg": avg_value, "samples": weight_sum}
        return result
    except Exception as e:
        logger.warning(f"[stat_db] 聚合查询失败 (hero={hero_guid}): {e}")
        return {}


def build_reference_text(
    db_path: Optional[str],
    player_name: str,
    hero_guid: str,
    hero_name: str,
    rank_score: Optional[int] = None,
) -> str:
    """为一局玩家构建分段参考文本。

    Args:
        db_path: SQLite DB 路径，None 表示未配置
        player_name: 玩家名
        hero_guid: 英雄 GUID
        hero_name: 英雄中文名
        rank_score: 玩家段位分数

    Returns:
        格式化的参考文本，如：
        "玩家名（英雄）：击杀分段中位 12.0, 死亡分段中位 5.0, ..."
        若 DB 未配置或无数据则返回空字符串。
    """
    if not db_path:
        return ""

    averages = query_hero_stat_averages(db_path, hero_guid, rank_score)
    if not averages:
        return ""

    by_name: dict[str, tuple[float, int]] = {}
    for stat_guid, info in averages.items():
        name = _load_name_map().get(stat_guid)
        if not name:
            continue  # 跳过无中文映射的统计指标
        if should_skip_prompt_stat(value_guid=stat_guid, value_text=name):
            continue
        median = info["median"]
        if median is None:
            continue
        samples = info.get("samples", 0)
        samples = int(samples or 0)
        old = by_name.get(name)
        if old is None or samples > old[1]:
            by_name[name] = (float(median), samples)

    parts = []
    for name, (median, samples) in by_name.items():
        parts.append(f"{name}分段{median:.1f}")

    if not parts:
        return ""

    return f"  {player_name}（{hero_name}）" + ", ".join(parts)


def build_broad_reference_text(
    db_path: Optional[str],
    player_name: str,
    hero_guid: str,
    hero_name: str,
) -> str:
    """为一局玩家构建聚合参考文本。"""
    if not db_path:
        return ""

    averages = query_hero_stat_averages_for_buckets(db_path, hero_guid)
    if not averages:
        return ""

    by_name: dict[str, tuple[float, int]] = {}
    for stat_guid, info in averages.items():
        name = _load_name_map().get(stat_guid)
        if not name:
            continue
        if should_skip_prompt_stat(value_guid=stat_guid, value_text=name):
            continue
        median = info["median"]
        if median is None:
            continue
        samples = int(info.get("samples") or 0)
        old = by_name.get(name)
        if old is None or samples > old[1]:
            by_name[name] = (float(median), samples)

    parts = []
    for name, (median, samples) in by_name.items():
        parts.append(f"{name}{median:.1f}")

    if not parts:
        return ""
    return f"  {player_name}（{hero_name}）" + ", ".join(parts)



