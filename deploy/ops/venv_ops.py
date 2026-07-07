"""虚拟环境管理：创建 venv、pip install 依赖、跨平台路径处理。

使用 AstrBot 主进程的 Python 解释器创建虚拟环境，确保版本一致；
pip 安装复用 AstrBot 配置的 PyPI 镜像源加速国内安装。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger("astrbot")

_CREATIONFLAGS = 0x08000000 if sys.platform == "win32" else 0


def get_venv_python(venv_dir: str | Path) -> Path:
    """跨平台获取 venv 内的 python 可执行文件路径。"""
    venv = Path(venv_dir)
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def exists(venv_dir: str | Path) -> bool:
    """检测 venv 是否已创建（通过 python 可执行文件存在性判断）。"""
    return get_venv_python(venv_dir).exists()


async def _run_subprocess(cmd: list[str], cwd: str | Path | None = None, timeout: float = 600.0) -> tuple[int, str, str]:
    """通用异步子进程执行，返回 (returncode, stdout, stderr)。"""
    logger.debug(f"[OverstatsDeploy] exec: {' '.join(cmd)} (cwd={cwd})")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATIONFLAGS,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "", "command timed out"

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    return proc.returncode if proc.returncode is not None else 1, stdout, stderr


async def create(venv_dir: str | Path, base_python: str | None = None) -> tuple[bool, str]:
    """创建虚拟环境。

    Args:
        venv_dir: venv 目标目录
        base_python: 基础 Python 解释器路径，默认使用当前进程的 sys.executable

    Returns:
        (success, message)
    """
    if exists(venv_dir):
        return True, "虚拟环境已存在，跳过创建"

    python_exe = base_python or sys.executable
    venv_path = Path(venv_dir)
    venv_path.parent.mkdir(parents=True, exist_ok=True)

    rc, stdout, stderr = await _run_subprocess(
        [python_exe, "-m", "venv", str(venv_path)],
        timeout=120.0,
    )
    if rc != 0:
        return False, stderr or stdout or f"venv 创建失败，退出码 {rc}"

    if not exists(venv_path):
        return False, "venv 创建后未找到 python 可执行文件"

    return True, "虚拟环境创建成功"


async def upgrade_pip(venv_dir: str | Path) -> tuple[bool, str]:
    """升级 venv 内的 pip 到最新版本（避免老版本 pip 对某些包解析失败）。"""
    venv_python = get_venv_python(venv_dir)
    rc, stdout, stderr = await _run_subprocess(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
        timeout=120.0,
    )
    if rc != 0:
        # 升级失败不致命，继续后续安装
        logger.warning(f"[OverstatsDeploy] pip 升级失败（忽略）: {stderr or stdout}")
        return True, "pip 升级失败，已忽略"
    return True, "pip 已升级"


async def install_requirements(
    venv_dir: str | Path,
    requirements_path: str | Path,
    index_url: str | None = None,
    extra_args: str | None = None,
) -> tuple[bool, str]:
    """使用 venv 内的 pip 安装 requirements.txt 依赖。

    Args:
        venv_dir: venv 目录
        requirements_path: requirements.txt 路径
        index_url: PyPI 镜像源地址，默认使用 AstrBot 配置
        extra_args: 额外的 pip 参数

    Returns:
        (success, message)
    """
    if not Path(requirements_path).exists():
        return False, f"requirements.txt 不存在: {requirements_path}"

    venv_python = get_venv_python(venv_dir)
    cmd = [str(venv_python), "-m", "pip", "install", "-r", str(requirements_path)]
    if index_url:
        cmd.extend(["-i", index_url])
    if extra_args:
        cmd.extend(extra_args.split())

    rc, stdout, stderr = await _run_subprocess(cmd, timeout=900.0)
    if rc != 0:
        tail = (stderr or stdout)[-800:] if (stderr or stdout) else f"pip install 失败，退出码 {rc}"
        return False, tail
    return True, "依赖安装完成"


async def pip_show(venv_dir: str | Path, package: str) -> bool:
    """检测 venv 内是否已安装某个包。"""
    venv_python = get_venv_python(venv_dir)
    rc, _, _ = await _run_subprocess(
        [str(venv_python), "-c", f"import importlib; importlib.import_module('{package}')"],
        timeout=15.0,
    )
    return rc == 0
