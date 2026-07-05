"""后端 request_metrics.sqlite3 性能数据读取模块。

读取 Overstats 后端在运行时自动记录的请求性能指标，为监控面板提供
端点性能、上游 API 统计等功能。

路径自动探测：
- auto 模式：{plugin_data_dir}/overstats_backend/src/db/request_metrics.sqlite3
- manual 模式：{sqlite_db_path 的父目录}/request_metrics.sqlite3
- 都不可用时返回空数据，不影响监控系统核心功能。

参照 stat_db.py 的每次请求创建连接-查询-关闭只读模式。
"""

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("astrbot")


class BackendMetricsReader:
    """后端 request_metrics.sqlite3 只读查询器。

    所有方法均为静态方法，每次调用独立创建/关闭连接。
    """

    @staticmethod
    def resolve_db_path(
        plugin_data_dir: str | Path,
        deploy_mode: str,
        sqlite_db_path: str = "",
    ) -> Path | None:
        """自动探测 request_metrics.sqlite3 路径。

        Args:
            plugin_data_dir: 插件数据根目录
            deploy_mode: "auto" 或 "manual"
            sqlite_db_path: _conf_schema 中 sqlite_db_path 配置值（manual 模式用）

        Returns:
            数据库路径，不可用时返回 None
        """
        data_dir = Path(plugin_data_dir)

        if deploy_mode == "auto":
            # auto 模式：后端代码在 overstats_backend/src/db/ 下
            candidate = data_dir / "overstats_backend" / "src" / "db" / "request_metrics.sqlite3"
            if candidate.is_file():
                return candidate
            # 尝试不带 db 子目录
            candidate2 = data_dir / "overstats_backend" / "src" / "request_metrics.sqlite3"
            if candidate2.is_file():
                return candidate2
            logger.debug("[backend_metrics] auto 模式未找到 request_metrics.sqlite3")
            return None

        # manual 模式：从 sqlite_db_path 推导
        if sqlite_db_path:
            parent = Path(sqlite_db_path).parent
            candidate = parent / "request_metrics.sqlite3"
            if candidate.is_file():
                return candidate
            logger.debug(f"[backend_metrics] manual 模式未找到: {candidate}")
            return None

        logger.debug("[backend_metrics] sqlite_db_path 未配置，无法推导 request_metrics 路径")
        return None

    @staticmethod
    def get_endpoint_perf_stats(
        db_path: str,
        endpoint: str = "",
        time_range_hours: int = 24,
    ) -> list[dict]:
        """按端点聚合性能统计（avg/p95/max 耗时 + 成功率 + 队列等待 + 缓存命中）。

        Args:
            db_path: request_metrics.sqlite3 路径
            endpoint: 可选，单个端点精确匹配（为空则返回所有端点）
            time_range_hours: 时间范围（小时），0 表示不限

        Returns:
            [{url, avg_ms, max_ms, p95_ms, success_rate, avg_queue_ms,
              avg_upstream_ms, cache_hit_rate, count}, ...]
        """
        path = Path(db_path)
        if not path.is_file():
            return []

        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row

            where = ""
            params: list = []
            if endpoint:
                where += " AND url = ?"
                params.append(endpoint)
            if time_range_hours > 0:
                where += " AND recorded_at >= datetime('now', ? || ' hours')"
                params.append(f"-{time_range_hours}")

            # 聚合查询：avg/max/count
            rows = conn.execute(
                f"""SELECT url,
                           COUNT(*) AS count,
                           ROUND(AVG(total_ms), 0) AS avg_ms,
                           MAX(total_ms) AS max_ms,
                           ROUND(AVG(CASE WHEN success=1 THEN total_ms END), 0) AS avg_success_ms,
                           ROUND(AVG(CASE WHEN success=0 THEN total_ms END), 0) AS avg_fail_ms,
                           SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS success_count,
                           SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail_count,
                           ROUND(AVG(CASE WHEN queue_wait_ms>=0 THEN queue_wait_ms END), 0) AS avg_queue_ms,
                           ROUND(AVG(CASE WHEN upstream_ms>0 THEN upstream_ms END), 0) AS avg_upstream_ms,
                           ROUND(AVG(CASE WHEN memory_cache_hits>=0 THEN CAST(memory_cache_hits AS FLOAT) END), 1) AS avg_mem_cache,
                           ROUND(AVG(CASE WHEN db_read_hits>=0 THEN CAST(db_read_hits AS FLOAT) END), 1) AS avg_db_read,
                           ROUND(AVG(CASE WHEN upstream_count>=0 THEN CAST(upstream_count AS FLOAT) END), 1) AS avg_upstream_count
                    FROM endpoint_perf_stats
                    WHERE 1=1{where}
                    GROUP BY url
                    ORDER BY count DESC"""
            , params).fetchall()

            # 计算 p95（需要单独查询，SQLite 不支持 PERCENTILE_CONT）
            results = []
            for row in rows:
                d = dict(row)
                # p95
                p95_row = conn.execute(
                    f"""SELECT total_ms FROM endpoint_perf_stats
                        WHERE url = ?{where}
                        ORDER BY total_ms ASC
                        LIMIT 1 OFFSET CAST(0.95 * (SELECT COUNT(*) FROM endpoint_perf_stats WHERE url = ?{where}) AS INTEGER)""",
                    [d["url"]] + params + [d["url"]] + params,
                ).fetchone()
                d["p95_ms"] = round(p95_row["total_ms"], 0) if p95_row else None

                # 成功率
                total = d["success_count"] + d["fail_count"]
                d["success_rate"] = round(d["success_count"] / max(total, 1), 4)

                # 慢端点标记
                d["is_slow"] = (d["avg_ms"] or 0) > 10000

                results.append(d)

            conn.close()
            return results
        except Exception as e:
            logger.warning(f"[backend_metrics] 查询 endpoint_perf_stats 失败: {e}")
            return []

    @staticmethod
    def get_upstream_stats(db_path: str, limit: int = 30) -> list[dict]:
        """获取上游 API 调用聚合统计。

        Args:
            db_path: request_metrics.sqlite3 路径
            limit: 返回条数上限

        Returns:
            [{url, total, success, fail, success_rate}, ...]
        """
        path = Path(db_path)
        if not path.is_file():
            return []

        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT url, source_type,
                          total_requests, successful_requests, failed_requests,
                          ROUND(success_rate, 4) AS success_rate,
                          updated_at
                   FROM request_url_stats
                   ORDER BY total_requests DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[backend_metrics] 查询 request_url_stats 失败: {e}")
            return []

    @staticmethod
    def get_db_info(db_path: str) -> dict | None:
        """获取数据库元信息。

        Returns:
            {time_range_start, time_range_end, total_rows, unique_endpoints}
        """
        path = Path(db_path)
        if not path.is_file():
            return None

        try:
            conn = sqlite3.connect(str(path))
            r = conn.execute(
                """SELECT MIN(recorded_at) AS start, MAX(recorded_at) AS end,
                          COUNT(*) AS total,
                          COUNT(DISTINCT url) AS endpoints
                   FROM endpoint_perf_stats"""
            ).fetchone()
            conn.close()
            if r:
                return {
                    "time_range_start": r[0],
                    "time_range_end": r[1],
                    "total_rows": r[2],
                    "unique_endpoints": r[3],
                }
            return None
        except Exception as e:
            logger.warning(f"[backend_metrics] 查询 db_info 失败: {e}")
            return None

    @staticmethod
    def get_slow_endpoints(db_path: str, threshold_ms: int = 10000, time_range_hours: int = 24) -> list[dict]:
        """检测慢端点（avg 耗时超过阈值）。

        Returns:
            [{url, avg_ms, max_ms, count, success_rate}, ...]
        """
        path = Path(db_path)
        if not path.is_file():
            return []

        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            where = ""
            params: list = [threshold_ms]
            if time_range_hours > 0:
                where = " AND recorded_at >= datetime('now', ? || ' hours')"
                params.append(f"-{time_range_hours}")

            rows = conn.execute(
                f"""SELECT url,
                           COUNT(*) AS count,
                           ROUND(AVG(total_ms), 0) AS avg_ms,
                           MAX(total_ms) AS max_ms,
                           ROUND(CAST(SUM(success) AS FLOAT) / MAX(COUNT(*), 1), 4) AS success_rate
                    FROM endpoint_perf_stats
                    WHERE 1=1{where}
                    GROUP BY url
                    HAVING AVG(total_ms) > ?
                    ORDER BY avg_ms DESC""",
                params,
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[backend_metrics] 查询 slow_endpoints 失败: {e}")
            return []
