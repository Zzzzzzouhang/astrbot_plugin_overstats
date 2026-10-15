"""队列快照：排队与提示决策共用的只读状态视图。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QueueSnapshot:
    """某一时刻的全局队列状态（不含等待者明细，供决策与监控使用）。"""

    capacity: int = 0
    per_user_max: int = 0
    running: int = 0
    waiting: int = 0
    privileged_waiting: int = 0
    normal_waiting: int = 0
    heavy_waiting: int = 0
    active_users: int = 0
    reclaimed_pending: int = 0
    privileged_jumped_total: int = 0

    # ── 决策辅助 ──────────────────────────────────────────────

    @property
    def has_free_slot(self) -> bool:
        """是否还有空闲运行槽位。

        capacity<=0 表示快照读取异常（未取到容量），按「无空闲」处理以避免误放行，
        但 `will_wait()` 另有保护，避免把异常快照误判为拥堵。
        """
        return self.capacity > 0 and self.running < self.capacity

    def will_wait(self) -> bool:
        """新指令是否必然需要等待：等待队列非空，或运行槽位已满。

        capacity<=0（stats 读取异常）时不得因 0>=0 误判为「槽位已满」，
        故此处显式要求 capacity > 0。
        """
        if self.waiting > 0:
            return True
        return self.capacity > 0 and self.running >= self.capacity

    def is_congested(self, threshold: int) -> bool:
        """等待数是否超过拥堵阈值（重指令降权判定用）。

        提示文案的「排队用户较多」判定为「>= 阈值」，见
        `core/service/reply_notice.py` 的 congestion_threshold。
        """
        return self.waiting > threshold

    # ── 兼容导出 ──────────────────────────────────────────────

    def to_stats_dict(self) -> dict:
        """转为旧 `stats()` 字段名的字典（global_running 等），供监控面板与旧调用点使用。"""
        return {
            "capacity": self.capacity,
            "per_user_max": self.per_user_max,
            "global_running": self.running,
            "waiting": self.waiting,
            "privileged_waiting": self.privileged_waiting,
            "normal_waiting": self.normal_waiting,
            "heavy_waiting": self.heavy_waiting,
            "active_users": self.active_users,
            "reclaimed_pending": self.reclaimed_pending,
            "privileged_jumped_total": self.privileged_jumped_total,
        }
