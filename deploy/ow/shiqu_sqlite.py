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
                f"SELECT id, target_id, ok, raw_response, duration_ms, call_count, created_at "
                f"FROM {_TABLE}{where_sql} "
                f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)],
            ).fetchall()

            def _parse_ok(raw: str) -> bool:
                """raw_response 能否解析为有效 JSON（决定「是否解析成功」）。"""
                if not raw:
                    return False
                try:
                    json.loads(raw)
                    return True
                except Exception:
                    return False

            records = []
            for r in rows:
                ok = bool(r[2])
                raw = r[3] or ""
                # 仅对失败 / 未解析成功的记录附带 raw_response 原文，
                # 避免成功记录的大字段常驻内存与重复传输。
                raw_for_list = raw if (not ok or not _parse_ok(raw)) else ""
                records.append({
                    "id": r[0],
                    "target_id": r[1],
                    "ok": ok,
                    "raw_response": raw_for_list,
                    "duration_ms": r[4],
                    "call_count": r[5],
                    "created_at": r[6],
                })

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

    def get_by_id(self, record_id: int) -> dict | None:
        """按需读取单条完整记录（含 prompt / raw_response），用于详情面板。

        仅点击表格时调用，不在列表接口返回，避免大字段常驻内存与重复传输。
        返回 None 表示未找到 / 不可用。
        """
        dbp = self._db_path()
        if not dbp or not dbp.exists() or not record_id:
            return None
        try:
            conn = sqlite3.connect(str(dbp), timeout=10)
        except Exception as e:
            logger.warning(f"[ShiquSqlite] 无法打开 {dbp}: {e}")
            return None
        try:
            row = conn.execute(
                f"SELECT id, target_id, ok, prompt, raw_response, duration_ms, call_count, created_at "
                f"FROM {_TABLE} WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "target_id": row[1],
                "ok": bool(row[2]),
                "prompt": row[3] or "",
                "raw_response": row[4] or "",
                "duration_ms": row[5],
                "call_count": row[6],
                "created_at": row[7],
            }
        except Exception as e:
            logger.warning(f"[ShiquSqlite] 单条查询失败: {e}")
            return None
        finally:
            conn.close()


class CourtSqliteReader:
    """读取后端 shiqu_llm.sqlite3 的 ``court_llm_result`` 表（开庭调用记录）。

    与 :class:`ShiquSqliteReader` 共用同一个 sqlite 文件，但表结构不同：
    除 ``prompt`` / ``raw_response`` 外，还额外落库 ``match_index`` / ``map_name`` /
    ``game_mode`` 等渲染元数据；``raw_response`` 为纯文本判决书（非 JSON）。
    """

    _TABLE = "court_llm_result"

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
        """查询开庭调用记录，返回 {"available", "summary", "records", "total", "offset", "limit"}。

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
            logger.warning(f"[CourtSqlite] 无法打开 {dbp}: {e}")
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
                f"SELECT COUNT(*) FROM {self._TABLE}{where_sql}", params
            ).fetchone()[0]
            srow = cur.execute(
                f"SELECT COUNT(*), SUM(ok), AVG(duration_ms) FROM {self._TABLE}{where_sql}",
                params,
            ).fetchone()
            total_n = int(srow[0] or 0)
            success_n = int(srow[1] or 0)
            avg_dur = float(srow[2] or 0)

            rows = cur.execute(
                f"SELECT id, target_id, match_index, map_name, game_mode, "
                f"ok, duration_ms, call_count, created_at "
                f"FROM {self._TABLE}{where_sql} "
                f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)],
            ).fetchall()

            records = [
                {
                    "id": r[0],
                    "target_id": r[1],
                    "match_index": r[2],
                    "map_name": r[3],
                    "game_mode": r[4],
                    "ok": bool(r[5]),
                    "duration_ms": r[6],
                    "call_count": r[7],
                    "created_at": r[8],
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
            logger.warning(f"[CourtSqlite] 查询失败: {e}")
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

    def get_by_id(self, record_id: int) -> dict | None:
        """按需读取单条完整记录（含 prompt / raw_response），用于详情面板。

        仅点击表格时调用，不在列表接口返回，避免大字段常驻内存与重复传输。
        返回 None 表示未找到 / 不可用。
        """
        dbp = self._db_path()
        if not dbp or not dbp.exists() or not record_id:
            return None
        try:
            conn = sqlite3.connect(str(dbp), timeout=10)
        except Exception as e:
            logger.warning(f"[CourtSqlite] 无法打开 {dbp}: {e}")
            return None
        try:
            row = conn.execute(
                f"SELECT id, target_id, match_index, map_name, game_mode, "
                f"prompt, raw_response, ok, duration_ms, call_count, created_at "
                f"FROM {self._TABLE} WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "target_id": row[1],
                "match_index": row[2],
                "map_name": row[3],
                "game_mode": row[4],
                "prompt": row[5] or "",
                "raw_response": row[6] or "",
                "ok": bool(row[7]),
                "duration_ms": row[8],
                "call_count": row[9],
                "created_at": row[10],
            }
        except Exception as e:
            logger.warning(f"[CourtSqlite] 单条查询失败: {e}")
            return None
        finally:
            conn.close()

