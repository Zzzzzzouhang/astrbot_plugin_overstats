"""是区吗调用日志读取器。

读取 shiqu_calls.log（JSONL 格式，20MB 轮转，最多 5 个备份），
支持筛选、分页和汇总统计。
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("astrbot.shiqu.reader")

# 轮转备份数量（与 shiqu.py 中 RotatingFileHandler 的 backupCount 保持一致）
MAX_BACKUPS = 5


class ShiquCallReader:
    """读取并解析 shiqu_calls.log JSONL 文件（含轮转备份）。"""

    def __init__(self, log_dir: Path = None):
        self._log_dir = log_dir

    # ── 文件发现 ──────────────────────────────────────

    @staticmethod
    def _log_files(log_dir: Path) -> list[Path]:
        """返回所有日志文件路径，按时间逆序（最新的在前）。

        顺序：.log → .log.1 → … → .log.5
        .log 是当前活跃文件，.log.1 是最新的轮转文件。
        """
        files = []
        main = log_dir / "shiqu_calls.log"
        if main.exists():
            files.append(main)
        for i in range(1, MAX_BACKUPS + 1):
            backup = log_dir / f"shiqu_calls.log.{i}"
            if backup.exists():
                files.append(backup)
        return files

    # ── 逐行解析 ──────────────────────────────────────

    def iter_records(self, log_dir: Path):
        """生成器，逐条产出调用记录（最新在前）。"""
        for fpath in self._log_files(log_dir):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            yield record
                        except json.JSONDecodeError:
                            # 跳过损坏的行
                            continue
            except OSError as e:
                logger.warning(f"[ShiquReader] 无法读取 {fpath}: {e}")
                continue

    # ── 查询 API ──────────────────────────────────────

    def query(
        self,
        log_dir: Path,
        limit: int = 30,
        offset: int = 0,
        openid: str = "",
        target_id: str = "",
        success: bool = None,
        verdict: str = "",
        search: str = "",
    ) -> dict:
        """查询调用记录，返回 {"summary": {...}, "records": [...], "total": int}。

        :param log_dir:   日志目录（通常是 plugin_data_dir / "shiqu"）
        :param limit:     返回条数上限
        :param offset:    分页偏移量
        :param openid:    按 openid 精确匹配（空字符串不筛选）
        :param target_id: 按目标战网 ID 精确匹配
        :param success:   按成功/失败筛选（None 不筛选）
        :param verdict:   按判定文本精确匹配
        :param search:    模糊搜索 target_id 或 verdict
        """
        if not log_dir or not log_dir.exists():
            return {"available": False, "summary": {}, "records": [], "total": 0}

        # 第一遍：收集所有匹配记录 + 统计
        all_matched: list[dict] = []
        total_attempts = 0
        total_success = 0
        total_duration = 0.0
        total_score = 0.0
        score_count = 0

        for rec in self.iter_records(log_dir):
            total_attempts += 1

            # ― 筛选逻辑 ―
            if openid and rec.get("openid", "") != openid:
                # 筛选模式下不纳入统计
                continue
            if target_id and rec.get("target_id", "") != target_id:
                continue
            if success is not None and rec.get("success") != success:
                continue
            if verdict and rec.get("verdict", "") != verdict:
                continue
            if search:
                    s = search.lower()
                    t = (rec.get("target_id") or "").lower()
                    v = (rec.get("verdict") or "").lower()
                    if s not in t and s not in v:
                        continue

            # 统计
            if rec.get("success"):
                total_success += 1
            dur = rec.get("duration_ms", 0) or 0
            total_duration += dur
            sc = rec.get("score")
            if isinstance(sc, (int, float)):
                total_score += sc
                score_count += 1

            all_matched.append(rec)

        matched_count = len(all_matched)

        # 按 ts 降序（最新在前）
        all_matched.sort(key=lambda r: r.get("ts", ""), reverse=True)

        # 分页
        page = all_matched[offset : offset + limit]

        # 构造摘要
        summary = {
            "total": total_attempts,
            "matched": matched_count,
            "success_rate": round(total_success / total_attempts, 4) if total_attempts else 0,
            "avg_duration_ms": round(total_duration / total_attempts) if total_attempts else 0,
            "avg_score": round(total_score / total_attempts, 1) if total_attempts else 0,
        }

        return {
            "available": True,
            "summary": summary,
            "records": page,
            "total": matched_count,
            "offset": offset,
            "limit": limit,
        }
