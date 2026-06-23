"""子进程管理：启动/停止 Overstats 后端进程、异步读取输出、健康检查轮询。

后端以子进程方式运行，stdout/stderr 异步读取并缓存最近 N 行，
通过 /healthz 端点轮询判断服务是否就绪。

日志同时写入内存缓冲区（用于实时查看）和专用日志文件（持久化），
文件容量上限由 max_file_log_lines 控制，超出时自动裁剪保留最新记录。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections import deque
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger("astrbot")

_CREATE_NO_WINDOW = 0x08000000


def _creationflags():
    if sys.platform == "win32":
        return _CREATE_NO_WINDOW
    return 0


class ProcessRunner:
    """Overstats 后端子进程管理器。

    负责启动、停止后端进程，异步读取输出并缓存日志，
    以及通过 HTTP 健康检查轮询判断服务就绪状态。

    日志双写机制：
    - 内存缓冲区（deque）：用于实时查看最近日志，重启后丢失
    - 专用日志文件（持久化）：跨重启保留，容量上限 max_file_log_lines
    """

    def __init__(
        self,
        max_log_lines: int = 200,
        log_file_path: str | Path | None = None,
        max_file_log_lines: int = 300,
    ):
        """
        Args:
            max_log_lines: 内存缓冲区最大行数
            log_file_path: 持久化日志文件路径。为 None 时不写文件。
            max_file_log_lines: 日志文件最大保留行数，超出时自动裁剪
        """
        self._process: Optional[asyncio.subprocess.Process] = None
        self._log_buffer: deque[str] = deque(maxlen=max_log_lines)
        self._reader_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._started = False

        # 持久化日志文件
        self._log_file: Optional[Path] = Path(log_file_path) if log_file_path else None
        self._max_file_log_lines = max_file_log_lines
        self._file_write_count = 0  # 写入计数，每 N 行触发一次裁剪
        if self._log_file:
            try:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"[OverstatsBackend] 创建日志目录失败: {e}")
                self._log_file = None

    @property
    def is_alive(self) -> bool:
        """后端进程是否存活。"""
        return self._process is not None and self._process.returncode is None

    @property
    def started(self) -> bool:
        """是否已执行过 start（无论进程当前是否存活）。"""
        return self._started

    def _append_log(self, line: str):
        if line:
            self._log_buffer.append(line)
            self._write_log_to_file(line)

    def _write_log_to_file(self, line: str):
        """将日志行追加写入持久化文件，并定期裁剪。"""
        if not self._log_file:
            return
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._file_write_count += 1
            # 每 50 行检查一次文件行数，超出上限则裁剪
            if self._file_write_count >= 50:
                self._file_write_count = 0
                self._trim_log_file()
        except Exception as e:
            logger.debug(f"[OverstatsBackend] 写入日志文件失败: {e}")

    def _trim_log_file(self):
        """裁剪日志文件，保留最新 max_file_log_lines 行。"""
        if not self._log_file or not self._log_file.exists():
            return
        try:
            lines = self._log_file.read_text(encoding="utf-8").splitlines()
            if len(lines) > self._max_file_log_lines:
                # 保留最新的 N 行
                trimmed = lines[-self._max_file_log_lines:]
                self._log_file.write_text(
                    "\n".join(trimmed) + "\n", encoding="utf-8"
                )
        except Exception as e:
            logger.debug(f"[OverstatsBackend] 裁剪日志文件失败: {e}")

    def get_file_logs(self, lines: int = 10) -> list[str]:
        """从持久化日志文件中读取最新的 N 行。

        Args:
            lines: 要读取的行数

        Returns:
            日志行列表，最新的在最后。文件不存在或为空时返回空列表。
        """
        if not self._log_file or not self._log_file.exists():
            return []
        try:
            all_lines = self._log_file.read_text(encoding="utf-8").splitlines()
            if lines <= 0:
                return all_lines
            return all_lines[-lines:] if all_lines else []
        except Exception as e:
            logger.debug(f"[OverstatsBackend] 读取日志文件失败: {e}")
            return []

    def get_recent_logs(self, lines: int = 50) -> list[str]:
        """获取最近的日志行。"""
        buf = list(self._log_buffer)
        if lines <= 0:
            return buf
        return buf[-lines:]

    def clear_logs(self):
        self._log_buffer.clear()

    async def start(self, cwd: str | Path, python_exe: str | Path, host: str, port: int) -> tuple[bool, str]:
        """启动后端进程。

        Args:
            cwd: 后端项目根目录（run.py 所在目录）
            python_exe: venv 内的 python 可执行文件
            host: 监听地址
            port: 监听端口

        Returns:
            (success, message)
        """
        async with self._lock:
            if self.is_alive:
                return False, "后端进程已在运行"

            cwd_path = Path(cwd)
            run_py = cwd_path / "run.py"
            if not run_py.exists():
                return False, f"未找到 run.py: {run_py}"

            # 通过环境变量覆盖 host/port（loader.py 支持 OVERSTATS_API_HOST/OVERSTATS_API_PORT）
            env = {}
            try:
                import os
                env = dict(os.environ)
            except Exception:
                env = {}
            env["OVERSTATS_API_HOST"] = host
            env["OVERSTATS_API_PORT"] = str(port)

            self._append_log(f"[启动] cwd={cwd_path}, python={python_exe}, 监听 {host}:{port}")

            try:
                self._process = await asyncio.create_subprocess_exec(
                    str(python_exe), "run.py",
                    cwd=str(cwd_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    creationflags=_creationflags(),
                )
            except FileNotFoundError:
                return False, f"python 可执行文件不存在: {python_exe}"
            except Exception as e:
                return False, f"启动子进程失败: {e}"

            self._started = True
            # 异步读取输出，写入日志缓冲区
            self._reader_task = asyncio.create_task(self._read_output_loop())

            return True, f"后端进程已启动 (PID={self._process.pid})"

    async def _read_output_loop(self):
        """持续读取子进程 stdout 并写入日志缓冲区，直到进程结束。"""
        if not self._process or not self._process.stdout:
            return
        try:
            while True:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    # EOF
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    self._append_log(line)
                    logger.debug(f"[OverstatsBackend] {line}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._append_log(f"[读取输出异常] {e}")
        finally:
            if self._process and self._process.returncode is not None:
                self._append_log(f"[进程已退出] returncode={self._process.returncode}")

    async def stop(self, timeout: float = 10.0) -> tuple[bool, str]:
        """停止后端进程。先尝试优雅终止，超时后强制杀死。

        Returns:
            (success, message)
        """
        async with self._lock:
            if not self._process:
                return True, "无运行中的后端进程"
            if self._process.returncode is not None:
                self._append_log(f"[停止] 进程已退出 (returncode={self._process.returncode})")
                self._process = None
                return True, "进程已退出"

            pid = self._process.pid
            self._append_log(f"[停止] 正在终止进程 PID={pid}")

            killed = False
            try:
                if sys.platform == "win32":
                    # Windows 下 terminate() 发送的信号无法被 Python 子进程优雅处理，
                    # 直接用 taskkill 终止整个进程树
                    kill_proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(pid),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=_creationflags(),
                    )
                    await asyncio.wait_for(kill_proc.communicate(), timeout=timeout)
                    killed = True
                else:
                    self._process.send_signal(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        self._process.send_signal(signal.SIGKILL)
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    killed = True
            except Exception as e:
                self._append_log(f"[停止异常] {e}")
                # 最后兜底
                try:
                    self._process.kill()
                except Exception:
                    pass

            # 取消读取任务
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._reader_task = None

            self._process = None
            return True, f"进程 PID={pid} 已停止"

    async def wait_for_health(self, host: str, port: int, timeout: float = 30.0, interval: float = 1.0) -> tuple[bool, str]:
        """轮询 /healthz 端点等待服务就绪。

        Args:
            host: 后端监听地址
            port: 后端监听端口
            timeout: 最大等待秒数
            interval: 轮询间隔秒数

        Returns:
            (ready, message)
        """
        url = f"http://{host}:{port}/healthz"
        deadline = asyncio.get_event_loop().time() + timeout
        last_err = ""

        # 复用独立的短超时 session，避免与插件主 session 冲突
        timeout_cfg = aiohttp.ClientTimeout(total=5)
        connector = aiohttp.TCPConnector(force_close=True)

        async with aiohttp.ClientSession(timeout=timeout_cfg, connector=connector) as session:
            attempt = 0
            while asyncio.get_event_loop().time() < deadline:
                attempt += 1
                # 如果进程已退出，无需继续等待
                if self._process is not None and self._process.returncode is not None:
                    return False, f"后端进程在启动后立即退出 (returncode={self._process.returncode})，请查看部署日志"
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            try:
                                data = await resp.json()
                                if data.get("ok") is True:
                                    return True, f"后端服务就绪 (第 {attempt} 次探测成功)"
                            except Exception:
                                body = await resp.text()
                                if "ok" in body:
                                    return True, f"后端服务就绪 (第 {attempt} 次探测成功)"
                        last_err = f"HTTP {resp.status}"
                except aiohttp.ClientConnectorError:
                    last_err = "连接被拒绝（服务可能尚未启动）"
                except asyncio.TimeoutError:
                    last_err = "请求超时"
                except Exception as e:
                    last_err = str(e)

                await asyncio.sleep(interval)

        return False, f"健康检查超时（{timeout}s），最后状态: {last_err}。请查看部署日志排查。"

    async def cleanup(self):
        """插件卸载时调用，确保进程被终止。

        先尝试异步优雅关闭，若事件循环异常或超时，则同步强制兜底，
        保证不会遗留僵尸后端进程占用端口。
        """
        # 1. 异步优雅关闭（正常路径）
        try:
            await self.stop(timeout=5.0)
        except Exception as e:
            logger.warning(f"[OverstatsBackend] 异步停止异常，尝试同步兜底: {e}")
            self._sync_kill_fallback()

    def _sync_kill_fallback(self):
        """同步强制杀进程兜底（事件循环不可用时使用）。"""
        if not self._process:
            return
        pid = self._process.pid
        if pid is None:
            return
        try:
            if sys.platform == "win32":
                import subprocess
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5,
                )
            else:
                import os
                os.kill(pid, signal.SIGKILL)
            logger.info(f"[OverstatsBackend] 同步兜底已强制终止进程 PID={pid}")
        except Exception as e:
            logger.error(f"[OverstatsBackend] 同步兜底杀进程失败 PID={pid}: {e}")
        finally:
            self._process = None
