"""并发状态快照：准入决策与监控展示共用的只读状态视图。

取代旧 queue_snapshot（v3 重构后无等待队列，快照只描述
「各池运行量与配置」，不再有 waiting 概念）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConcurrencySnapshot:
    """某一时刻的全局并发状态（不含槽位明细，供决策与监控使用）。

    容量字段 <=0 表示不限制（默认：轻指令池 0 不限、重指令池 10）。
    """

    enabled: bool = True
    per_user_max: int = 0
    light_max: int = 0
    heavy_max: int = 0
    light_running: int = 0
    heavy_running: int = 0
    active_users: int = 0
    reclaimed_pending: int = 0

    @property
    def total_running(self) -> int:
        """轻/重两池当前运行槽位合计（繁忙度信号，供超时预警文案使用）。"""
        return self.light_running + self.heavy_running

    def to_stats_dict(self) -> dict:
        """转为统计字典（兼容旧 stats() 字段名 global_running，供监控面板使用）。"""
        return {
            "enabled": self.enabled,
            "per_user_max": self.per_user_max,
            "light_max": self.light_max,
            "heavy_max": self.heavy_max,
            "light_running": self.light_running,
            "heavy_running": self.heavy_running,
            "global_running": self.total_running,
            "active_users": self.active_users,
            "reclaimed_pending": self.reclaimed_pending,
        }
