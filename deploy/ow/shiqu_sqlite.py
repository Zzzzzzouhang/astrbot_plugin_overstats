"""是区吗调用日志读取器（SQLite 版）。

直接读取 Overstats 后端的 ``shiqu_llm.sqlite3``（位于
``sqlite_db_path`` 的父目录下），不再依赖插件本地 JSONL 日志。

表 ``shiqu_llm_result`` 字段：
    id, target_id, ok, prompt, raw_response, duration_ms, call_count, created_at

本读取器仅暴露监控页所需的列：id / target_id / ok / duration_ms /
call_count / created_at（移除原 JSONL 中的 verdict / score / openid）。
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("astrbot.shiqu.sqlite")

_TABLE = "shiqu_llm_result"


class ShiquSqliteReader:
    """读取后端 shiqu_llm.sqlite3 的是区吗调用记录。"""

    def __init__(self, sqlite_db_path: str = ""):
        # sqlite_db_path 通常指向后端的 match_stats.sqlite3，
        # shiqu_llm.sqlite3 与其同目录。
        self._base_path = sqlite_db_path or ""

    def _db_path(self) -> Path | None:
        if not self._base_path:
            return None
        try:
            return Path(self._base_path).resolve().parent / "shiqu_llm.sqlite3"
        except Exception:
            return None

    def query(
        self,
        limit: int = 30,
        offset: int = 0,
        target_id: str = "",
        success=None,
    ) -> dict:
        """查询调用记录，返回 {"available", "summary", "records", "total", "offset", "limit"}。

        :param success: None 不筛选；True 仅成功；False 仅失败
        """
        dbp = self._db_path()
        if not dbp or not dbp.exists():
            return {
                "available": False,
                "summary": {},
                "records": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
            }

        try:
            conn = sqlite3.connect(str(dbp), timeout=10)
        except Exception as e:
            logger.warning(f"[ShiquSqlite] 无法打开 {dbp}: {e}")
            return {
                "available": False,
                "summary": {},
                "records": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
            }

        try:
            where = []
            params: list = []
            if target_id:
                where.append("target_id = ?")
                params.append(target_id)
            if success is not None:
                where.append("ok = ?")
                params.append(1 if success else 0)
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""

            cur = conn.cursor()
            total = cur.execute(
                f"SELECT COUNT(*) FROM {_TABLE}{where_sql}", params
            ).fetchone()[0]
            srow = cur.execute(
                f"SELECT COUNT(*), SUM(ok), AVG(duration_ms) FROM {_TABLE}{where_sql}",
                params,
            ).fetchone()
            total_n = int(srow[0] or 0)
            success_n = int(srow[1] or 0)
            avg_dur = float(srow[2] or 0)

            rows = cur.execute(
                f"SELECT id, target_id, ok, duration_ms, call_count, created_at "
                f"FROM {_TABLE}{where_sql} "
                f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)],
            ).fetchall()

            records = [
                {
                    "id": r[0],
                    "target_id": r[1],
                    "ok": bool(r[2]),
                    "duration_ms": r[3],
                    "call_count": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]

            summary = {
                "total": total_n,
                "success": success_n,
                "failed": total_n - success_n,
                "avg_duration_ms": round(avg_dur) if avg_dur else 0,
            }
            return {
                "available": True,
                "summary": summary,
                "records": records,
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        except Exception as e:
            logger.warning(f"[ShiquSqlite] 查询失败: {e}")
            return {
                "available": False,
                "summary": {},
                "records": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
            }
        finally:
            conn.close()
