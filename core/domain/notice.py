"""提示决策领域：回复窗口与「是否发提示」的判定（纯函数）。

背景（来自线上日志统计）：25 分钟高峰期出现 105 次「回复消息 msg_id 已过期」、
11 次「被动回复时间或者次数超过限制」——绝大多数失败源于「用户已排队数分钟、
等结果时才被动回复，此时 msg_id 早已过期」。因此提示策略的核心是：
把告知动作前移到 msg_id 仍然有效的窗口内，并抑制注定失败的重复发送。
"""

from dataclasses import dataclass

from .queue_snapshot import QueueSnapshot


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
        """是否已到超时预警时间点（计时自「收到用户指令」起，含排队等待）。"""
        return elapsed >= self.warn_before

    def warn_delay(self, elapsed: float) -> float:
        """距预警时间点还需等待的秒数（已过点为 0）。"""
        return max(0.0, self.warn_before - elapsed)


@dataclass(frozen=True)
class NoticeDecision:
    """提示决策结果。"""

    should_send: bool
    waiting: int = 0


def decide_queue_notice(
    *,
    group_message: bool,
    privileged: bool,
    snapshot: QueueSnapshot,
    per_user_saturated: bool,
) -> NoticeDecision:
    """判定入队前是否推送「排队中」提示。

    - 仅群聊需要（私聊无 msg_id 过期问题，业务层提示已是即时反馈）；
    - 特权用户（白名单 / 管理员 / 限流关闭）豁免；
    - 个人并发已满时 acquire 会立即拒绝，再推「排队中…请稍后点 获取图片」
      会与「你已有指令正在执行」给出互相矛盾的两条指引，故跳过；
    - 指令无法立即获得槽位（等待队列非空 / 运行槽位已满）时才推送——
      用户在入队后的第一时间即得知排队人数与超时取回方式。
    """
    if not group_message or privileged or per_user_saturated:
        return NoticeDecision(False)
    if not snapshot.will_wait():
        return NoticeDecision(False)
    return NoticeDecision(True, snapshot.waiting)


def decide_timeout_warning(*, elapsed: float, window: ReplyWindow, already_notified: bool) -> bool:
    """判定是否推送 4:50 超时预警。

    已推送过「排队中」提示（同义，均含取图指引）时不再重复发送，避免同一用户收到两条。
    """
    if already_notified:
        return False
    return window.warn_due(elapsed)


def should_skip_status_prompt(*, elapsed: float, window: ReplyWindow, already_notified: bool) -> bool:
    """业务层「正在计算…」类状态提示是否应跳过。

    已推送过「排队中」提示、且窗口已过期时，该状态提示注定被动回复失败
    （日志中大量 msg_id 已过期失败即来自此类消息）。跳过它可：
    - 减少一次必然失败的平台调用与平台限流压力；
    - 避免用户在群内看到「发送失败」噪音；
    用户此时已持有取回方式（@机器人 获取图片）。
    """
    return already_notified and window.expired(elapsed)
