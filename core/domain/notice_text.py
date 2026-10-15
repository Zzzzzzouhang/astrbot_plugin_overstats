"""提示文案构造（纯字符串处理，不做格式化/不触碰平台）。

文案与线上版本逐字保持一致；平台相关的 markdown 标签转换由 platform 层负责。
"""

from __future__ import annotations

import re

GET_IMAGE_TAG = '<qqbot-cmd-input text="获取图片 " show="获取图片" reference="false" />'

# 战网 ID：Name#12345（兼容全角 ＃）
_BNET_RE = re.compile(r"[A-Za-z0-9_\-]{2,16}[#＃]\d{4,8}")


def extract_bnet_id(text: str) -> str:
    """从指令文本中提取战网 ID（无 IO，可在提示发送前同步调用）。"""
    if not text:
        return ""
    m = _BNET_RE.search(text)
    return m.group(0) if m else ""


def command_label(command_text: str) -> str:
    """取指令首个词作为标签文本；空文本回落为「指令」。"""
    parts = (command_text or "").strip().split()
    return parts[0] if parts else "指令"


def build_cmd_tag(command_text: str) -> str:
    """把指令文本转为 QQ 官方可点击指令标签（非官方平台由 platform 层降级为纯文本）。"""
    label = command_label(command_text)
    return f'<qqbot-cmd-input text="{label} " show="{label}" reference="false" />'


def build_queue_waiting_text(*, bnet_id: str, cmd_tag: str, waiting: int, get_tag: str = GET_IMAGE_TAG) -> str:
    """「排队中」前置提示（入队即发）。

    waiting=0 表示运行槽位已满但等待队列暂空（即将成为第一位等待者），
    此时不能显示「约 0 人」，改述为并发已满；两种情况均保留超时取回指引。
    """
    if bnet_id:
        head = f"⏳ {bnet_id} 发送的「{cmd_tag}」正在排队中，"
    else:
        head = f"⏳ 你发送的「{cmd_tag}」正在排队中，"
    if waiting > 0:
        tail = f"当前排队用户较多（约 {waiting} 人），回复即将超时，请稍后 @机器人 点 {get_tag}。"
    else:
        tail = f"当前并发指令已满、即将排队，若回复超时，请稍后 @机器人 点 {get_tag}。"
    return head + tail + "\n⚠️ 推荐**私聊**（60分钟）使用或**群主** 点击机器人头像-右上角设置-机器人主动在群内发言。"


def build_timeout_warning_text(
    *,
    bnet_id: str,
    cmd_tag: str,
    waiting: int,
    congested: bool,
    get_tag: str = GET_IMAGE_TAG,
) -> str:
    """4:50 超时预警（「仍在生成中」）。"""
    if bnet_id:
        head = f"⏳ {bnet_id} 发送的「{cmd_tag}」仍在生成中，"
    else:
        head = f"⏳ 你发送的「{cmd_tag}」仍在生成中，"
    if congested:
        tail = f"当前排队用户较多（约 {waiting} 人），回复即将超时，请稍后 @机器人 点 {get_tag}。"
    else:
        tail = f"若稍后未收到图片，请 @机器人 发送 {get_tag} 取回（图片保留 24 小时）。"
    return head + tail + "\n⚠️ 推荐**私聊**（60分钟）使用或**群主/管理员** 点击机器人头像-右上角设置-机器人权限设置-**允许主动发送消息**。"
