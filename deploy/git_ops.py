"""Git 操作封装：检测可用性、clone、pull、stash、获取 commit hash。

使用 asyncio.create_subprocess_exec 异步执行 git 命令，不阻塞事件循环。

支持 GitHub 加速代理：当 clone 出现 RPC 失败、HTTP/2 流错误、early EOF 等
网络错误时，自动通过加速代理重试。代理地址优先级：自定义 > 主 > 备。
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("astrbot")

# Windows 下 subprocess 需要避免控制台窗口弹出
_CREATIONFLAGS = 0x08000000 if sys.platform == "win32" else 0

# 默认 GitHub 加速代理地址（按优先级排列）
DEFAULT_PROXY_PRIMARY = "https://gh.llkk.cc/"
DEFAULT_PROXY_FALLBACK = "https://gh-proxy.com/"

# 网络错误关键词：出现这些关键词时判定为需要使用代理重试
_NETWORK_ERROR_PATTERNS = [
    r"RPC failed",
    r"HTTP/2 stream",
    r"early EOF",
    r"fatal: the remote end hung up unexpectedly",
    r"fetch-pack: unexpected disconnect",
    r"fatal: early EOF",
    r"fatal: index-pack failed",
    r"Connection timed out",
    r"Connection reset by peer",
    r"Could not resolve host",
    r"SSL_ERROR",
    r"gnutls_handshake",
    r"Connection was reset",
    r"Protocol error",
    r"transfer closed with outstanding read data remaining",
]


def _is_network_error(stderr: str) -> bool:
    """判断 git stderr 是否包含网络相关错误。

    RPC 失败、HTTP/2 流错误、early EOF 等通常由网络不稳定或 GFW 干扰导致，
    通过加速代理重试可以解决。
    """
    if not stderr:
        return False
    stderr_lower = stderr.lower()
    for pattern in _NETWORK_ERROR_PATTERNS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return True
    # 额外检查：退出码非零且 stderr 包含 "fatal" + 网络相关词
    if "fatal" in stderr_lower and any(
        kw in stderr_lower for kw in ("network", "connection", "timeout", "reset", "eof", "hung up")
    ):
        return True
    return False


def _apply_proxy(repo_url: str, proxy_base: str) -> str:
    """将 GitHub URL 转换为加速代理 URL。

    Args:
        repo_url: 原始仓库 URL，如 https://github.com/user/repo.git
        proxy_base: 代理基础地址，如 https://gh.llkk.cc/

    Returns:
        代理 URL，如 https://gh.llkk.cc/https://github.com/user/repo.git
    """
    proxy_base = proxy_base.rstrip("/") + "/"
    # 避免重复代理
    if repo_url.startswith(proxy_base) or "/gh-proxy" in repo_url or "gh.llkk.cc" in repo_url:
        return repo_url
    return f"{proxy_base}{repo_url}"


def _get_proxy_list(config: dict | None) -> list[str]:
    """从配置获取代理地址列表（按优先级排列）。

    优先级：自定义代理 > 主代理 > 备代理
    """
    if not config:
        return []

    proxies: list[str] = []

    # 自定义代理（最高优先级）
    custom = str(config.get("github_proxy_custom", "") or "").strip()
    if custom:
        proxies.append(custom)

    # 主代理
    primary = str(config.get("github_proxy_primary", DEFAULT_PROXY_PRIMARY) or DEFAULT_PROXY_PRIMARY).strip()
    if primary and primary not in proxies:
        proxies.append(primary)

    # 备代理
    fallback = str(config.get("github_proxy_fallback", DEFAULT_PROXY_FALLBACK) or DEFAULT_PROXY_FALLBACK).strip()
    if fallback and fallback not in proxies:
        proxies.append(fallback)

    return proxies


async def _run_git(args: list[str], cwd: str | Path | None = None, timeout: float = 120.0) -> tuple[int, str, str]:
    """执行 git 命令并返回 (returncode, stdout, stderr)。

    Args:
        args: git 子命令参数列表，如 ["--version"] 或 ["clone", url, target]
        cwd: 工作目录
        timeout: 超时秒数
    """
    cmd = ["git", *args]
    logger.debug(f"[OverstatsDeploy] git: {' '.join(cmd)} (cwd={cwd})")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATIONFLAGS,
        )
    except FileNotFoundError:
        # git 未安装
        return 127, "", "git command not found"
    except Exception as e:
        return 1, "", str(e)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "", "git command timed out"

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    return proc.returncode if proc.returncode is not None else 1, stdout, stderr


async def check_git_available() -> bool:
    """检测系统是否安装了 git。"""
    rc, _, _ = await _run_git(["--version"], timeout=10.0)
    return rc == 0


async def clone(
    repo_url: str,
    target_dir: str | Path,
    proxy_config: dict | None = None,
) -> tuple[bool, str]:
    """克隆仓库到目标目录。

    支持自动加速代理：当首次 clone 出现网络错误时，自动通过加速代理重试。

    Args:
        repo_url: 原始仓库 URL
        target_dir: 目标目录
        proxy_config: 代理配置字典，包含 github_proxy_enabled / github_proxy_custom /
                      github_proxy_primary / github_proxy_fallback

    Returns:
        (success, message) — message 中包含代理启用信息（若使用了代理）
    """
    target = Path(target_dir)
    if target.exists() and any(target.iterdir()):
        return False, f"目标目录已存在且非空: {target}"

    target.parent.mkdir(parents=True, exist_ok=True)

    # 第一次尝试：直接克隆（不走代理）
    rc, stdout, stderr = await _run_git(
        ["clone", "--depth", "1", repo_url, str(target)],
        timeout=600.0,
    )
    if rc == 0:
        return True, f"克隆成功: {repo_url}"

    # 直接克隆失败，检查是否是网络错误 + 代理是否启用
    proxy_enabled = bool(proxy_config and proxy_config.get("github_proxy_enabled", False))

    # 即使代理默认关闭，网络错误时也自动启用（用户需求：RPC 失败时自动启用加速）
    if not proxy_enabled and _is_network_error(stderr or stdout):
        logger.info(f"[OverstatsDeploy] 检测到网络错误，自动启用 GitHub 加速代理重试")
        proxy_enabled = True

    if rc == 127:
        return False, "未检测到 git 命令，请先安装 git 并确保在 PATH 中可用。"

    if not proxy_enabled or not _is_network_error(stderr or stdout):
        # 非网络错误，或代理未启用且不是网络错误，直接返回原始错误
        msg = stderr or stdout or f"git clone 失败，退出码 {rc}"
        return False, msg

    # === 网络错误 + 代理启用：通过加速代理重试 ===
    # 清理可能残留的不完整目录
    import shutil
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    proxies = _get_proxy_list(proxy_config)
    if not proxies:
        proxies = [DEFAULT_PROXY_PRIMARY, DEFAULT_PROXY_FALLBACK]

    proxy_messages: list[str] = []
    proxy_messages.append(f"⚠️ 直接克隆失败（网络错误），已自动启用 GitHub 加速代理重试。")
    proxy_messages.append(f"   原始错误: {(stderr or stdout)[:200]}")

    for i, proxy_base in enumerate(proxies):
        proxy_url = _apply_proxy(repo_url, proxy_base)
        proxy_label = proxy_base.rstrip("/")
        proxy_messages.append(f"   [{i+1}/{len(proxies)}] 尝试代理: {proxy_label}")

        # 清理上次失败残留
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

        rc, stdout, stderr = await _run_git(
            ["clone", "--depth", "1", proxy_url, str(target)],
            timeout=600.0,
        )
        if rc == 0:
            proxy_messages.append(f"   ✅ 代理克隆成功: {proxy_label}")
            return True, "\n".join(proxy_messages)
        else:
            err = stderr or stdout or f"退出码 {rc}"
            proxy_messages.append(f"   ❌ 代理 {proxy_label} 失败: {err[:150]}")

    # 所有代理都失败
    proxy_messages.append(f"   所有代理均失败，请检查网络或手动配置代理地址。")
    return False, "\n".join(proxy_messages)


async def pull(repo_dir: str | Path, proxy_config: dict | None = None) -> tuple[bool, str]:
    """拉取远程仓库最新更新。

    支持自动加速代理：网络错误时自动重试。

    Args:
        repo_dir: 仓库目录
        proxy_config: 代理配置字典

    Returns:
        (success, message)
    """
    rc, stdout, stderr = await _run_git(
        ["pull", "--ff-only"],
        cwd=repo_dir,
        timeout=300.0,
    )
    if rc == 0:
        return True, stdout or "已是最新"

    # 网络错误检测 + 代理重试
    proxy_enabled = bool(proxy_config and proxy_config.get("github_proxy_enabled", False))
    if not proxy_enabled and _is_network_error(stderr or stdout):
        proxy_enabled = True

    if rc == 127:
        return False, "未检测到 git 命令。"

    if not proxy_enabled or not _is_network_error(stderr or stdout):
        msg = stderr or stdout or f"git pull 失败，退出码 {rc}"
        return False, msg

    # 通过代理重试：临时修改 remote URL
    remote_url = await get_remote_url(repo_dir)
    if not remote_url or "github.com" not in remote_url:
        return False, stderr or stdout or f"git pull 失败，退出码 {rc}"

    proxies = _get_proxy_list(proxy_config)
    if not proxies:
        proxies = [DEFAULT_PROXY_PRIMARY, DEFAULT_PROXY_FALLBACK]

    proxy_messages: list[str] = [f"⚠️ 直接拉取失败（网络错误），已自动启用 GitHub 加速代理重试。"]

    for i, proxy_base in enumerate(proxies):
        proxy_url = _apply_proxy(remote_url, proxy_base)
        proxy_label = proxy_base.rstrip("/")

        # 临时设置代理 remote
        await _run_git(["remote", "set-url", "origin", proxy_url], cwd=repo_dir, timeout=15.0)

        rc, stdout, stderr = await _run_git(
            ["pull", "--ff-only"],
            cwd=repo_dir,
            timeout=300.0,
        )

        # 恢复原始 remote URL
        await _run_git(["remote", "set-url", "origin", remote_url], cwd=repo_dir, timeout=15.0)

        if rc == 0:
            proxy_messages.append(f"   ✅ 代理拉取成功: {proxy_label}")
            return True, "\n".join(proxy_messages) + f"\n{stdout or '已是最新'}"
        else:
            err = stderr or stdout or f"退出码 {rc}"
            proxy_messages.append(f"   ❌ 代理 {proxy_label} 失败: {err[:150]}")

    return False, "\n".join(proxy_messages)


async def fetch_and_reset(
    repo_dir: str | Path,
    remote: str = "origin",
    branch: str = "main",
    proxy_config: dict | None = None,
) -> tuple[bool, str]:
    """强制拉取并重置到远程分支（用于更新流程，丢弃本地提交但保留未跟踪文件）。

    Args:
        repo_dir: 仓库目录
        remote: 远程名
        branch: 分支名
        proxy_config: 代理配置字典

    Returns:
        (success, message)
    """
    rc, _, stderr = await _run_git(["fetch", remote, branch], cwd=repo_dir, timeout=300.0)
    if rc != 0:
        # 网络错误时尝试代理
        proxy_enabled = bool(proxy_config and proxy_config.get("github_proxy_enabled", False))
        if not proxy_enabled and _is_network_error(stderr):
            proxy_enabled = True

        if proxy_enabled and _is_network_error(stderr):
            remote_url = await get_remote_url(repo_dir)
            if remote_url and "github.com" in remote_url:
                proxies = _get_proxy_list(proxy_config) or [DEFAULT_PROXY_PRIMARY, DEFAULT_PROXY_FALLBACK]
                for proxy_base in proxies:
                    proxy_url = _apply_proxy(remote_url, proxy_base)
                    await _run_git(["remote", "set-url", "origin", proxy_url], cwd=repo_dir, timeout=15.0)
                    rc, _, stderr = await _run_git(["fetch", remote, branch], cwd=repo_dir, timeout=300.0)
                    await _run_git(["remote", "set-url", "origin", remote_url], cwd=repo_dir, timeout=15.0)
                    if rc == 0:
                        break

        if rc != 0:
            return False, stderr or "git fetch 失败"

    rc, stdout, stderr = await _run_git(["reset", "--hard", f"{remote}/{branch}"], cwd=repo_dir, timeout=60.0)
    if rc != 0:
        return False, stderr or "git reset 失败"
    return True, stdout or "已重置到远程最新"


async def get_commit_hash(repo_dir: str | Path) -> str:
    """获取当前 HEAD 的 commit hash（短）。失败返回 "unknown"。"""
    rc, stdout, _ = await _run_git(["rev-parse", "--short", "HEAD"], cwd=repo_dir, timeout=15.0)
    if rc == 0 and stdout:
        return stdout
    return "unknown"


async def is_inside_repo(repo_dir: str | Path) -> bool:
    """判断目录是否是一个 git 仓库。"""
    rc, _, _ = await _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_dir, timeout=10.0)
    return rc == 0


async def get_remote_url(repo_dir: str | Path) -> str:
    """获取 origin 远程地址。失败返回空字符串。"""
    rc, stdout, _ = await _run_git(["config", "--get", "remote.origin.url"], cwd=repo_dir, timeout=10.0)
    if rc == 0:
        return stdout.strip()
    return ""
