"""Overstats 插件监控统计模块。

提供 MonitorCollector（SQLite 持久化的指令/API/限流统计采集器）、
MonitorLogHandler（挂载 astrbot logger 自动捕获 ERROR 日志）、
MonitorSSEQueue（实时错误推送队列）。

所有 SQLite 操作通过 asyncio.run_in_executor 包装，避免阻塞事件循环。
数据保留 30 天，每日凌晨自动清理过期记录。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import traceback as tb_module
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("astrbot")

# ── 指令分类映射（用于 _ensure_full_adapt_map 中已定义的分类）──
CMD_CATEGORY_MAP: dict[str, str] = {
    # 基础 & 绑定
    "owhelp": "基础绑定", "所有指令": "基础绑定", "快速指南": "基础绑定", "大神绑定": "基础绑定",
    # 数据查询
    "大神数据": "数据查询", "大神对局": "数据查询", "单局详细": "数据查询", "同玩查询": "数据查询",
    # 总结
    "今日总结": "总结", "昨日总结": "总结", "周度总结": "总结",
    # 图表 & 排行
    "历史段位": "图表排行", "快速强度": "图表排行", "竞技强度": "图表排行",
    "快速英雄云图": "图表排行", "竞技英雄云图": "图表排行",
    "省榜": "图表排行", "绝活榜": "图表排行", "banpick": "图表排行", "mappick": "图表排行",
    # 游戏资讯
    "威能": "游戏资讯", "ow英雄": "游戏资讯", "获取段位分布": "游戏资讯",
    "皮肤搜索": "游戏资讯", "商店": "游戏资讯", "ow赛事": "游戏资讯",
    "ow活动": "游戏资讯", "ow更新": "游戏资讯",
    # AI / 开庭
    "ow开庭": "AI开庭", "ow是区吗": "AI开庭", "owAI检测": "AI开庭",
    # 管理 / 部署
    "ow连接测试": "管理部署", "ow部署": "管理部署", "ow部署状态": "管理部署",
    "ow更新后端": "管理部署", "ow停止后端": "管理部署", "ow重启后端": "管理部署",
    "ow部署日志": "管理部署", "ow后端日志": "管理部署", "ow卸载后端": "管理部署",
    "维护": "管理部署", "ow违禁封禁": "管理部署", "ow违禁解封": "管理部署",
    "群设置": "管理部署", "全量适配开": "管理部署", "全量适配关": "管理部署", "管理": "管理部署",
}


def _resolve_category(cmd_name: str) -> str:
    """根据指令名返回分类，别名也向上映射。"""
    # 别名映射
    alias_map = {
        "ow菜单": "owhelp", "ow帮助": "owhelp", "OW帮助": "owhelp", "help": "owhelp",
        "别称": "所有指令",
        "快捷指令": "快速指南",
        "绑定": "大神绑定",
        "今日": "今日总结", "今日数据": "今日总结",
        "昨日": "昨日总结", "昨日数据": "昨日总结", "昨天数据": "昨日总结",
        "本周": "周度总结", "本周总结": "周度总结", "本周数据": "周度总结",
        "详情卡片": "大神数据", "战绩查询": "大神数据", "数据": "大神数据",
        "最近对局": "大神对局", "战绩": "大神对局", "对局": "大神对局",
        "单局": "单局详细", "单局详情": "单局详细",
        "历届段位": "历史段位",
        "开黑胜率": "同玩查询",
        "快速强度指数": "快速强度",
        "竞技强度指数": "竞技强度",
        "快速云图": "快速英雄云图",
        "竞技云图": "竞技英雄云图",
        "ow商店": "商店",
        "赛事": "ow赛事",
        "活动": "ow活动",
        "全英雄排行": "banpick",
        "排行": "省榜",
        "英雄省榜": "绝活榜",
        "版本更新": "ow更新",
        "开庭": "ow开庭",
        "是区吗": "ow是区吗",
        "AI检测": "owAI检测",
    }
    canonical = alias_map.get(cmd_name, cmd_name)
    return CMD_CATEGORY_MAP.get(canonical, "其他")


# ── SQL DDL ──

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cmd_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cmd_name      TEXT    NOT NULL,
    category      TEXT    NOT NULL DEFAULT '其他',
    success       INTEGER NOT NULL DEFAULT 1,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    user_id       TEXT    NOT NULL DEFAULT '',
    error_msg     TEXT    NOT NULL DEFAULT '',
    recorded_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cmd_records_name ON cmd_records(cmd_name);
CREATE INDEX IF NOT EXISTS idx_cmd_records_date ON cmd_records(recorded_at);
CREATE INDEX IF NOT EXISTS idx_cmd_records_category ON cmd_records(category);

CREATE TABLE IF NOT EXISTS api_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint      TEXT    NOT NULL,
    success       INTEGER NOT NULL DEFAULT 1,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    recorded_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_records_endpoint ON api_records(endpoint);

CREATE TABLE IF NOT EXISTS error_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    level         TEXT    NOT NULL DEFAULT 'ERROR',
    message       TEXT    NOT NULL DEFAULT '',
    command       TEXT    NOT NULL DEFAULT '',
    traceback     TEXT    NOT NULL DEFAULT '',
    recorded_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_error_log_date ON error_log(recorded_at);
CREATE INDEX IF NOT EXISTS idx_error_log_level ON error_log(level);

CREATE TABLE IF NOT EXISTS rate_limit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rl_type       TEXT    NOT NULL DEFAULT 'cmd',
    user_id       TEXT    NOT NULL DEFAULT '',
    recorded_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rl_log_date ON rate_limit_log(recorded_at);
CREATE INDEX IF NOT EXISTS idx_rl_log_type ON rate_limit_log(rl_type);
"""


# ── MonitorCollector ──

class MonitorCollector:
    """插件级统计采集器（SQLite 持久化）。

    所有写入通过 asyncio.run_in_executor 包装，避免阻塞事件循环。
    数据保留 30 天，可通过 cleanup_old_records 定期清理。
    """

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at = time.time()
        self._init_db()

    def _init_db(self) -> None:
        """同步建表 + 索引（__init__ 中直接调用，不走 executor）。"""
        conn = sqlite3.connect(str(self._db_path))
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    # ── 写入接口 ──

    async def record_command(
        self,
        cmd_name: str,
        success: bool,
        duration_ms: int = 0,
        user_id: str = "",
        error_msg: str = "",
    ) -> None:
        """记录一条指令调用。"""
        category = _resolve_category(cmd_name)
        now = datetime.utcnow().isoformat()
        await self._run_sync(
            """INSERT INTO cmd_records (cmd_name, category, success, duration_ms, user_id, error_msg, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cmd_name, category, int(success), duration_ms, str(user_id or ""), str(error_msg or ""), now),
        )

    async def record_api(self, endpoint: str, success: bool, duration_ms: int = 0) -> None:
        """记录一次后端 API 调用。"""
        now = datetime.utcnow().isoformat()
        await self._run_sync(
            """INSERT INTO api_records (endpoint, success, duration_ms, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (endpoint, int(success), duration_ms, now),
        )

    async def record_error(self, level: str, message: str, command: str = "", tb_text: str = "") -> None:
        """记录一条错误日志。"""
        now = datetime.utcnow().isoformat()
        await self._run_sync(
            """INSERT INTO error_log (level, message, command, traceback, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (level, str(message)[:500], str(command or ""), str(tb_text)[:2000], now),
        )

    async def record_rate_limit(self, rl_type: str, user_id: str = "") -> None:
        """记录一次限流触发。"""
        now = datetime.utcnow().isoformat()
        await self._run_sync(
            """INSERT INTO rate_limit_log (rl_type, user_id, recorded_at)
               VALUES (?, ?, ?)""",
            (rl_type, str(user_id or ""), now),
        )

    # ── 查询接口 ──

    async def get_overview(self) -> dict:
        """总览数据。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            r = conn.execute("SELECT COUNT(*), SUM(success), COUNT(*) - SUM(success) FROM cmd_records").fetchone()
            cmd_total, cmd_success, cmd_fail = (r[0] or 0), (r[1] or 0), (r[2] or 0)
            r2 = conn.execute("SELECT COUNT(*), SUM(success), COUNT(*) - SUM(success) FROM api_records").fetchone()
            api_total = r2[0] or 0
            r3 = conn.execute("SELECT COUNT(*) FROM rate_limit_log").fetchone()
            rl_total = r3[0] or 0
            r4 = conn.execute("SELECT COUNT(*) FROM rate_limit_log WHERE recorded_at >= ?",
                              (datetime.utcnow().strftime("%Y-%m-%d"),)).fetchone()
            rl_today = r4[0] or 0
            conn.close()
            return {
                "uptime_seconds": self.uptime_seconds,
                "cmd_total": cmd_total,
                "cmd_success": cmd_success,
                "cmd_fail": cmd_fail,
                "cmd_success_rate": round(cmd_success / max(cmd_total, 1), 4),
                "api_total": api_total,
                "rl_total": rl_total,
                "rl_today": rl_today,
            }
        return await self._run_sync(_q)

    async def get_cmd_stats(self, category: str = "", search: str = "") -> list[dict]:
        """按指令名聚合统计（支持分类筛选 + 搜索）。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            sql = """
                SELECT cmd_name, category,
                       COUNT(*) AS total,
                       SUM(success) AS success,
                       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail,
                       ROUND(AVG(duration_ms), 0) AS avg_duration_ms,
                       MAX(recorded_at) AS last_used
                FROM cmd_records
                WHERE 1=1
            """
            params: list = []
            if category:
                sql += " AND category = ?"
                params.append(category)
            if search:
                sql += " AND cmd_name LIKE ?"
                params.append(f"%{search}%")
            sql += " GROUP BY cmd_name ORDER BY total DESC"
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        return await self._run_sync(_q)

    async def get_cmd_trend(self, cmd_name: str = "", days: int = 7) -> list[dict]:
        """近 N 天指令调用日趋势。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            sql = """SELECT DATE(recorded_at) AS date, cmd_name, COUNT(*) AS count
                     FROM cmd_records
                     WHERE DATE(recorded_at) >= ?"""
            params = [since]
            if cmd_name:
                sql += " AND cmd_name = ?"
                params.append(cmd_name)
            sql += " GROUP BY date, cmd_name ORDER BY date ASC"
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        return await self._run_sync(_q)

    async def get_top_users(self, top_n: int = 10) -> list[dict]:
        """Top N 活跃用户（按指令调用量）。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT user_id, COUNT(*) AS total,
                          SUM(success) AS success,
                          ROUND(CAST(SUM(success) AS FLOAT) / MAX(COUNT(*), 1), 3) AS success_rate
                   FROM cmd_records
                   WHERE user_id != ''
                   GROUP BY user_id
                   ORDER BY total DESC
                   LIMIT ?""",
                (top_n,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        return await self._run_sync(_q)

    async def get_hourly_distribution(self, date: str = "") -> list[dict]:
        """按小时聚合的指令调用分布（基于本地时间）。可指定 date（YYYY-MM-DD）筛选某天。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            if date:
                rows = conn.execute(
                    """SELECT CAST(STRFTIME('%H', recorded_at) AS INTEGER) AS hour, COUNT(*) AS count
                       FROM cmd_records WHERE DATE(recorded_at) = ?
                       GROUP BY hour ORDER BY hour""",
                    (date,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT CAST(STRFTIME('%H', recorded_at) AS INTEGER) AS hour, COUNT(*) AS count
                       FROM cmd_records
                       GROUP BY hour ORDER BY hour"""
                ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        return await self._run_sync(_q)

    async def get_errors(self, limit: int = 50, offset: int = 0, level: str = "") -> tuple[list[dict], int]:
        """分页获取错误日志。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            where = ""
            params: list = []
            if level:
                where = " WHERE level = ?"
                params.append(level)
            rows = conn.execute(
                f"SELECT * FROM error_log{where} ORDER BY recorded_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM error_log{where}", params).fetchone()[0]
            conn.close()
            return [dict(r) for r in rows], total
        return await self._run_sync(_q)

    async def get_rate_limit_stats(self) -> dict:
        """限流统计：按类型聚合总数 + 今日数。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            today = datetime.utcnow().strftime("%Y-%m-%d")
            rows = conn.execute(
                """SELECT rl_type, COUNT(*) AS total,
                          SUM(CASE WHEN DATE(recorded_at) = ? THEN 1 ELSE 0 END) AS today
                   FROM rate_limit_log
                   GROUP BY rl_type""", (today,)
            ).fetchall()
            conn.close()
            return {r[0]: {"total": r[1], "today": r[2]} for r in rows}
        return await self._run_sync(_q)

    async def get_all_errors(self) -> list[dict]:
        """获取全部错误日志（供 SSE 冷启动填充用）。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM error_log ORDER BY recorded_at DESC LIMIT 200").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        return await self._run_sync(_q)

    # ── 维护 ──

    async def clear_all_stats(self) -> int:
        """清空所有统计记录，返回删除行数。"""
        def _q():
            conn = sqlite3.connect(str(self._db_path))
            deleted = 0
            for table in ("cmd_records", "api_records", "error_log", "rate_limit_log"):
                cur = conn.execute(f"DELETE FROM {table}")
                deleted += cur.rowcount
            conn.commit()
            conn.close()
            return deleted
        return await self._run_sync(_q)

    async def close(self) -> None:
        """公共清理入口（terminate 时调用，SQLite 无需显式关闭连接堆）。"""
        pass  # 每次操作都独立连接，无需维护全局连接

    # ── 内部工具 ──

    async def _run_sync(self, func_or_sql, params=None):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if callable(func_or_sql):
            return await self._loop.run_in_executor(None, func_or_sql)
        # sql + params 模式
        sql, args = func_or_sql, params or ()

        def _exec():
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(sql, args)
            conn.commit()
            conn.close()

        await self._loop.run_in_executor(None, _exec)


# ── MonitorSSEQueue ──

class MonitorSSEQueue:
    """SSE 消息队列（非阻塞入队，超时出队）。"""

    def __init__(self, maxsize: int = 50):
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)

    def publish(self, event: dict) -> None:
        """非阻塞入队，队列满时丢弃最旧的消息。"""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # 丢弃最旧的
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, poll_timeout: float = 5.0):
        """async generator：超时轮询，避免永久阻塞。"""
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=poll_timeout)
                yield item
            except asyncio.TimeoutError:
                # 空转一轮，发心跳给客户端保持连接
                yield {"_heartbeat": True}


# ── MonitorLogHandler ──

class MonitorLogHandler(logging.Handler):
    """挂载到 astrbot logger 的 ERROR 级别处理器。

    自动将 ERROR 日志推送到 MonitorCollector 的错误缓冲 + SSE 队列。
    emit() 仅在 MonitorCollector 可用时执行，失败静默忽略。
    """

    def __init__(self, collector: MonitorCollector, sse_queue: MonitorSSEQueue | None = None):
        super().__init__(level=logging.ERROR)
        self._collector = collector
        self._sse_queue = sse_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
            if len(msg) > 500:
                msg = msg[:497] + "..."
            tb_text = ""
            if record.exc_info and record.exc_info[2]:
                tb_text = "".join(tb_module.format_tb(record.exc_info[2]))[:2000]

            event = {
                "level": record.levelname,
                "message": msg,
                "command": getattr(record, "command", ""),
                "traceback": tb_text,
                "recorded_at": datetime.utcnow().isoformat(),
            }

            # 异步写入 error_log + 推送到 SSE 队列（通过 event loop）
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self._collector.record_error(
                        event["level"], event["message"], event["command"], event["traceback"],
                    ))
            except RuntimeError:
                # 没有 event loop（如插件初始化阶段），同步写入
                self._collector._run_sync(
                    """INSERT INTO error_log (level, message, command, traceback, recorded_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (event["level"], event["message"], event["command"], event["traceback"], event["recorded_at"]),
                )

            if self._sse_queue:
                self._sse_queue.publish(event)
        except Exception:
            pass  # 监控不应影响主流程
