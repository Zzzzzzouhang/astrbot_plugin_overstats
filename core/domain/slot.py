"""并发槽位领域对象：请求、授予凭证与拒绝异常。"""

from dataclasses import dataclass
from typing import Optional

from .enums import CommandClass, RejectReason


@dataclass(frozen=True)
class SlotRequest:
    """一次槽位申请。

    user_key 为 None 表示不按用户计数（不限制、不占个人额度）。
    command_class 决定进入哪个全局池（轻 / 重独立）。
    """

    user_key: Optional[str] = None
    command_class: CommandClass = CommandClass.LIGHT
    privileged: bool = False

    @property
    def heavy(self) -> bool:
        return self.command_class is CommandClass.HEAVY

    @classmethod
    def of(cls, user_key: Optional[str], *, privileged: bool = False, heavy: bool = False) -> "SlotRequest":
        return cls(
            user_key=user_key,
            command_class=CommandClass.HEAVY if heavy else CommandClass.LIGHT,
            privileged=privileged,
        )


@dataclass(frozen=True)
class SlotTicket:
    """已获得的槽位凭证（slot_id 用于精确释放与超时回收）。"""

    user_key: Optional[str]
    slot_id: int
    acquired_at: float  # time.monotonic()


class SlotRejected(Exception):
    """指令被拒绝：个人并发已满 / 轻或重全局槽位已满（超限即拒，不排队）。"""

    def __init__(self, reason: "RejectReason | str"):
        self.reason = reason
        super().__init__(reason.value if isinstance(reason, RejectReason) else reason)
