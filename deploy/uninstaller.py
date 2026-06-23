"""Overstats 后端独立卸载模块。

提供细粒度的后端卸载能力，与 AstrBot 插件卸载逻辑隔离：
- 停止后端进程（优雅 → 强制兜底）
- 可选删除后端代码目录（含 SQLite 数据库）
- 可选删除虚拟环境
- 可选保留或清除生成的配置文件
- 不触碰插件其他数据（临时图片、群组配置、维护状态等）

安全设计：
1. 进程必须先停止，再删除文件，避免数据库文件被占用导致损坏
2. 删除前统计占用空间，给用户确认机会
3. 每步操作独立反馈，失败不影响后续步骤
4. 数据库文件（*.sqlite3）位于 overstats_backend/src/db/，删除代码目录会一并清除
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("astrbot")


@dataclass
class UninstallResult:
    """后端卸载操作的结果。"""
    success: bool
    message: str
    details: list[str] = field(default_factory=list)
    freed_space_mb: float = 0.0


class BackendUninstaller:
    """Overstats 后端独立卸载器。

    与 DeployManager 配合使用，提供安全、细粒度的后端资源清理。
    卸载流程严格遵循「先停进程 → 再删文件」顺序，确保数据库不会损坏。
    """

    def __init__(
        self,
        backend_dir: str | Path,
        venv_dir: str | Path,
        process_runner,
    ):
        """
        Args:
            backend_dir: 后端代码目录（overstats_backend/）
            venv_dir: 虚拟环境目录（overstats_backend_venv/）
            process_runner: ProcessRunner 实例（用于停止后端进程）
        """
        self._backend_dir = Path(backend_dir)
        self._venv_dir = Path(venv_dir)
        self._runner = process_runner

    # ------------------------------------------------------------------ #
    # 空间计算
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dir_size_mb(path: Path) -> float:
        """计算目录总大小（MB）。"""
        if not path.exists():
            return 0.0
        total = 0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except Exception:
            pass
        return round(total / (1024 * 1024), 2)

    def get_disk_usage(self) -> dict:
        """获取后端各部分磁盘占用。

        Returns:
            {"backend_code": float, "venv": float, "total": float}
            单位均为 MB
        """
        backend_size = self._dir_size_mb(self._backend_dir)
        venv_size = self._dir_size_mb(self._venv_dir)
        return {
            "backend_code": backend_size,
            "venv": venv_size,
            "total": round(backend_size + venv_size, 2),
        }

    # ------------------------------------------------------------------ #
    # 卸载预览（不实际删除，供用户确认）
    # ------------------------------------------------------------------ #

    def preview(self) -> dict:
        """预览卸载将影响的资源。

        Returns:
            {
                "process_running": bool,
                "backend_dir": str,
                "venv_dir": str,
                "backend_exists": bool,
                "venv_exists": bool,
                "disk_usage": {...},
                "db_files": [str],  # 将被删除的数据库文件列表
            }
        """
        db_files = []
        if self._backend_dir.exists():
            db_dir = self._backend_dir / "src" / "db"
            if db_dir.exists():
                db_files = [f.name for f in db_dir.glob("*.sqlite3")]

        return {
            "process_running": self._runner.is_alive,
            "backend_dir": str(self._backend_dir),
            "venv_dir": str(self._venv_dir),
            "backend_exists": self._backend_dir.exists(),
            "venv_exists": self._venv_dir.exists(),
            "disk_usage": self.get_disk_usage(),
            "db_files": db_files,
        }

    # ------------------------------------------------------------------ #
    # 核心卸载流程
    # ------------------------------------------------------------------ #

    async def uninstall(
        self,
        delete_code: bool = True,
        delete_venv: bool = True,
        force: bool = False,
    ) -> UninstallResult:
        """执行后端卸载。

        严格按照「停止进程 → 删除文件」顺序执行，确保安全。

        Args:
            delete_code: 是否删除后端代码目录（含数据库）。默认 True。
            delete_venv: 是否删除虚拟环境。默认 True。
            force: 是否跳过确认直接执行（指令层已确认后传 True）。

        Returns:
            UninstallResult
        """
        details: list[str] = []
        total_freed = 0.0
        errors: list[str] = []

        # 1. 停止后端进程（必须先停进程，避免数据库文件被占用）
        if self._runner.is_alive:
            details.append("[1/3] 正在停止后端进程...")
            try:
                ok, msg = await self._runner.stop(timeout=10.0)
                if ok:
                    details.append(f"[1/3] 进程已停止: {msg}")
                else:
                    details.append(f"[1/3] 停止进程失败: {msg}")
                    errors.append(f"进程停止失败: {msg}")
                    # 即使停止失败也继续删除文件（force 模式）
                    if not force:
                        return UninstallResult(
                            False,
                            f"后端进程停止失败: {msg}。可使用 force=True 强制卸载，或手动结束后端进程后重试。",
                            details,
                        )
            except Exception as e:
                details.append(f"[1/3] 停止进程异常: {e}")
                errors.append(str(e))
                if not force:
                    return UninstallResult(False, f"停止进程异常: {e}", details)
        else:
            details.append("[1/3] 后端进程未运行，跳过停止步骤")

        # 等待文件句柄释放（Windows 上进程刚停止时文件可能仍被占用）
        await asyncio.sleep(0.5)

        # 2. 删除后端代码目录
        if delete_code:
            if self._backend_dir.exists():
                size = self._dir_size_mb(self._backend_dir)
                details.append(f"[2/3] 正在删除后端代码目录（{size} MB）: {self._backend_dir}")
                try:
                    shutil.rmtree(self._backend_dir, ignore_errors=False)
                    total_freed += size
                    details.append(f"[2/3] 后端代码已删除（释放 {size} MB）")
                except Exception as e:
                    details.append(f"[2/3] 删除后端代码失败: {e}")
                    errors.append(f"删除后端代码失败: {e}")
                    # 尝试逐个删除（某些文件被占用时部分清理）
                    self._force_remove(self._backend_dir)
            else:
                details.append("[2/3] 后端代码目录不存在，跳过")
        else:
            details.append("[2/3] 保留后端代码（delete_code=False）")

        # 3. 删除虚拟环境
        if delete_venv:
            if self._venv_dir.exists():
                size = self._dir_size_mb(self._venv_dir)
                details.append(f"[3/3] 正在删除虚拟环境（{size} MB）: {self._venv_dir}")
                try:
                    shutil.rmtree(self._venv_dir, ignore_errors=False)
                    total_freed += size
                    details.append(f"[3/3] 虚拟环境已删除（释放 {size} MB）")
                except Exception as e:
                    details.append(f"[3/3] 删除虚拟环境失败: {e}")
                    errors.append(f"删除虚拟环境失败: {e}")
                    self._force_remove(self._venv_dir)
            else:
                details.append("[3/3] 虚拟环境目录不存在，跳过")
        else:
            details.append("[3/3] 保留虚拟环境（delete_venv=False）")

        # 汇总结果
        if errors:
            message = f"卸载部分完成（{len(errors)} 个错误），释放空间 {total_freed:.1f} MB"
            return UninstallResult(False, message, details, total_freed)
        else:
            message = f"后端卸载完成，释放空间 {total_freed:.1f} MB"
            return UninstallResult(True, message, details, total_freed)

    def _force_remove(self, path: Path):
        """强制删除目录（忽略错误，尽量清理）。"""
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 快捷方法
    # ------------------------------------------------------------------ #

    async def uninstall_all(self, force: bool = False) -> UninstallResult:
        """卸载全部后端资源（代码 + venv）。"""
        return await self.uninstall(delete_code=True, delete_venv=True, force=force)

    async def stop_only(self) -> UninstallResult:
        """仅停止后端进程，不删除任何文件。"""
        details: list[str] = []
        if not self._runner.is_alive:
            return UninstallResult(True, "后端进程未运行，无需停止", ["进程未运行"])
        try:
            ok, msg = await self._runner.stop(timeout=10.0)
            details.append(f"进程停止: {msg}")
            return UninstallResult(ok, msg, details)
        except Exception as e:
            return UninstallResult(False, f"停止进程异常: {e}", [f"异常: {e}"])
