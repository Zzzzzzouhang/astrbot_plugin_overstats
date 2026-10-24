"""插件侧图片静态服务：把内存里的图片暴露成公网可 GET 的短链。

存在意义：QQ 开放接口 `POST /v2/groups/{group_openid}/files` 支持以 `url` 上传
（由平台自行下载转存），但 AstrBot 的 QQ 官方适配器在图片分支写死了
`file_data`(base64)，无法透传 url。本服务负责提供「QQ 服务器可访问的图片直链」，
配合 `qq_url_sender` 直接调官方接口完成 URL 直传。

设计约束：
- 只服务白名单文件名（uuid hex + 图片后缀），杜绝路径穿越与目录遍历；
- 图片以原子写落盘（tmp + os.replace），避免 QQ 拉到半截文件；
- 链接按 TTL 过期，`publish` 时顺带清理，无需常驻清理任务；
- 公网地址优先取配置，其次自动探测出口 IP 兜底。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

logger = logging.getLogger("astrbot")

# 只放行本服务自己生成的文件名：32 位 uuid hex + 图片后缀
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|jpeg)$")

# 公网出口 IP 探测端点：依次尝试，任一成功即返回
_PUBLIC_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ip.3322.net",
)


def _write_atomic(path: Path, data: bytes) -> None:
    """先写临时文件再 os.replace 原子替换（同 main.py 的图片落盘策略）。"""
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _lan_ip() -> str:
    """取本机出口内网地址（UDP connect 不实际发包，仅用于选路）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return ""


class MediaURLServer:
    """极简静态图片服务：GET /media/{name} -> image/png。"""

    def __init__(
        self,
        root: Path | str,
        *,
        host: str = "0.0.0.0",
        port: int = 18357,
        public_base: str = "",
        ttl: int = 900,
        auto_detect_ip: bool = True,
    ) -> None:
        self._root = Path(root)
        self._host = host or "0.0.0.0"
        self._port = int(port)
        self._ttl = max(60, int(ttl))
        self._auto_detect_ip = bool(auto_detect_ip)
        self.public_base = (public_base or "").strip().rstrip("/")
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lock = asyncio.Lock()
        self._last_prune = 0.0

    # ── 生命周期 ──────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> str:
        """启动服务并返回公网基地址；返回空串表示公网地址不可得。"""
        async with self._lock:
            if self._runner is not None:
                if not self.public_base:
                    self.public_base = await self._detect_base()
                return self.public_base
            await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
            app = web.Application()
            app.router.add_get("/media/{name}", self._handle_get)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, self._host, self._port, reuse_address=True)
            try:
                await site.start()
            except OSError as exc:
                await runner.cleanup()
                raise RuntimeError(
                    f"监听 {self._host}:{self._port} 失败（端口被占用或无权限）: {exc}"
                ) from exc
            self._runner, self._site = runner, site
            if not self.public_base:
                self.public_base = await self._detect_base()
            logger.info(
                "[Overstats] 图片直传服务已启动: %s -> %s (public_base=%s)",
                f"{self._host}:{self._port}",
                self._root,
                self.public_base or "<未探测到公网地址>",
            )
            return self.public_base

    async def stop(self) -> None:
        async with self._lock:
            runner = self._runner
            self._runner = None
            self._site = None
        if runner is not None:
            await runner.cleanup()
            logger.info("[Overstats] 图片直传服务已停止")

    # ── 发布 ──────────────────────────────────────────────────

    async def publish(self, data: bytes, suffix: str = ".png") -> str:
        """落盘并返回公网 URL；空串表示不可用（无数据 / 无公网地址）。"""
        if not data:
            return ""
        base = await self.start()
        if not base:
            return ""
        name = f"{uuid.uuid4().hex}{suffix}"
        await asyncio.to_thread(_write_atomic, self._root / name, data)
        now = time.monotonic()
        if now - self._last_prune > 60:
            self._last_prune = now
            await asyncio.to_thread(self._prune_sync)
        return f"{base}/media/{name}"

    # ── 内部 ──────────────────────────────────────────────────

    async def _handle_get(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name", "")
        if not _NAME_RE.match(name):
            raise web.HTTPNotFound()
        path = self._root / name
        if not path.is_file():
            raise web.HTTPNotFound()
        resp = web.FileResponse(path)
        resp.headers["Cache-Control"] = f"public, max-age={self._ttl}"
        return resp

    async def _detect_base(self) -> str:
        ip = ""
        if self._auto_detect_ip:
            ip = await self._detect_public_ip() or _lan_ip()
        if not ip:
            return ""
        return f"http://{ip}:{self._port}"

    async def _detect_public_ip(self) -> str:
        timeout = aiohttp.ClientTimeout(total=4)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for endpoint in _PUBLIC_IP_ENDPOINTS:
                    try:
                        async with session.get(endpoint) as resp:
                            if resp.status != 200:
                                continue
                            text = (await resp.text()).strip()
                            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
                                return text
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug("[Overstats] 公网 IP 探测失败: %s", exc)
        return ""

    def _prune_sync(self) -> None:
        """清理过期图片文件。

        保留时长取 max(ttl, 300s)：TTL 配得过小时，QQ 可能还在下载就被删掉，
        表现为「链接 404 / 上传超时」。300s 下限覆盖 QQ 侧拉取与重试的窗口。
        """
        keep_seconds = max(self._ttl, 300)
        now = time.time()
        try:
            for entry in self._root.iterdir():
                try:
                    if entry.is_file() and (now - entry.stat().st_mtime) > keep_seconds:
                        entry.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass
