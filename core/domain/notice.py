"""提示决策领域：回复窗口与「是否发提示」的判定（纯函数）。

背景（来自线上日志统计）：25 分钟高峰期出现 105 次「回复消息 msg_id 已过期」、
11 次「被动回复时间或者次数超过限制」——绝大多数失败源于「用户已排队数分钟、
等结果时才被动回复，此时 msg_id 早已过期」。

v3 重构后取消等待队列（超限即拒），「排队中」前置提示随之移除；
本模块只保留 4:50 超时预警与业务层状态提示抑制两类判定。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyWindow:
    """QQ 群聊被动回复窗口。

    QQ 官方机器人以原消息 msg_id 作被动回复时，msg_id 自消息到达起约 5 分钟后过期；
    过期后只能走主动消息（需群主 / 管理员授权「允许主动发送消息」）。
    """

    ttl: float = 300.0        # msg_id 有效期（秒）
    warn_before: float = 290.0  # 距指令到达多久仍未返回即推送超时预警（4:50）

    def remaining(self, elapsed: float) -> float:
        """窗口剩余秒数（可为负，表示已过期）。"""
        return self.ttl - elapsed

    def expired(self, elapsed: float) -> bool:
        return elapsed >= self.ttl

    def warn_due(self, elapsed: float) -> bool:
        """是否已到超时预警时间点（计时自「收到用户指令」起）。"""
        return elapsed >= self.warn_before

    def warn_delay(self, elapsed: float) -> float:
        """距预警时间点还需等待的秒数（已过点为 0）。"""
        return max(0.0, self.warn_before - elapsed)


def decide_timeout_warning(*, elapsed: float, window: ReplyWindow, already_notified: bool) -> bool:
    """判定是否推送 4:50 超时预警。

    已推送过同义提示（含取图指引）时不再重复发送，避免同一用户收到两条。
    """
    if already_notified:
        return False
    return window.warn_due(elapsed)


def should_skip_status_prompt(*, elapsed: float, window: ReplyWindow, already_notified: bool) -> bool:
    """业务层「正在计算…」类状态提示是否应跳过。

    已推送过取图指引且窗口已过期时，该状态提示注定被动回复失败
    （日志中大量 msg_id 已过期失败即来自此类消息）。跳过它可：
    - 减少一次必然失败的平台调用与平台限流压力；
    - 避免用户在群内看到「发送失败」噪音；
    用户此时已持有取回方式（@机器人 获取图片）。
    """
    return already_notified and window.expired(elapsed)
