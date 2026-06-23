"""Overstats 配置文件生成器。

Overstats 的 config/config.py 是纯 Python 文件，loader.py 仅对 host/port/stream 等少量字段
支持环境变量覆盖，DASHEN_ACCOUNTS 必须写入文件。因此采用直接生成完整 config.py 的策略：
从 AstrBot 配置读取用户填写的值，程序化生成合法的 Python 配置文件并覆盖写入。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot")

# Overstats 默认配置文件的模板（与原项目 config/config.py 保持一致，
# 仅占位值由本生成器动态填充）。
_CONFIG_TEMPLATE = '''from __future__ import annotations

# ======================= Core Service ====================== #
API_HOST = {host!r}
API_PORT = {port}
USE_STREAM_RESPONSE = True
ENABLE_DATABASE_WRITE = True

# ======================= Dashen Upstream ====================== #
# Configure at least one account.
DASHEN_ACCOUNTS = {accounts}

DASHEN_DTS = {dts}
DASHEN_SERVER = {server}
DASHEN_ACCOUNT_MAX_REQUESTS_PER_SECOND = 5
DASHEN_ACCOUNT_RATE_LIMIT_WINDOW_SECONDS = 1.0
DASHEN_CLIENT_TYPE = "60"
DASHEN_ORIGIN = "https://act.ds.163.com"
DASHEN_REFERER = "https://act.ds.163.com/"
DASHEN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36 "
    "app/df_client dfVersion/100111"
)
DASHEN_ACCOUNT_FAILURE_COOLDOWN_SECONDS = 60
DASHEN_MAX_CONCURRENT_REQUESTS = 2
DASHEN_MAX_ACCEPTED_REQUESTS = max(len(DASHEN_ACCOUNTS) * 4, 1)

# Optional proxy settings.
DASHEN_INTERNATIONAL_PROXY = {dashen_proxy!r}
DASHEN_NETEASE_PROXIES = [
    None,
]

# OW esports PandaScore API key.
OW_ESPORTS_API_KEY = {ow_esports_api_key!r}

# Optional external OW guess asset pack root.
OW_GUESS_ASSET_ROOT = "ow_guess_assets"

# ======================= Dashen Season ====================== #
DASHEN_CURRENT_SEASON = 23
DASHEN_HISTORY_START_SEASON = 15

# ======================= OW Hero Leaderboard ====================== #
OW_HERO_LEADERBOARD_CN_SEASON = 3

# ======================= Match Analysis ====================== #
ANALYSIS_BASE_URL = {analysis_base_url!r}
ANALYSIS_API_KEY = {analysis_api_key!r}
ANALYSIS_PROXY = ""

ANALYSIS_OPENAI_MODEL = {analysis_model!r}

# Optional external patch-note fetch proxy.
PATCH_NOTES_USE_INTERNATIONAL_PROXY = False
PATCH_NOTES_INTERNATIONAL_PROXY = ""

ANALYSIS_PERSONA_PROMPT = {persona_prompt!r}
'''


def _escape_string(s: Any) -> str:
    """将用户输入安全转为 Python 字符串字面量。

    使用 repr() 让 Python 自行处理转义，避免注入风险。None / 空值统一转为空串。
    """
    if s is None:
        return ""
    return str(s)


def _format_accounts(accounts: list[dict] | None) -> str:
    """将 template_list 形式的大神账号配置格式化为 Python 列表字面量。

    每个账号含 name/role_id/token。role_id 必须为正整数，token 必须非空。
    """
    if not accounts:
        return "[]"

    lines = ["["]
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        name = str(acc.get("name") or "account").strip() or "account"
        try:
            role_id = int(acc.get("role_id", 0))
        except (TypeError, ValueError):
            role_id = 0
        token = str(acc.get("token") or "").strip()
        if not token or role_id <= 0:
            # 跳过无效账号
            logger.warning(f"[OverstatsDeploy] 跳过无效账号配置: name={name}, role_id={role_id}")
            continue
        # 用 repr 确保安全转义
        lines.append(f"    {{'name': {name!r}, 'role_id': {role_id}, 'token': {token!r}}},")
    lines.append("]")
    return "\n".join(lines)


def generate_config(config: dict) -> str:
    """根据 AstrBot 配置生成完整的 config.py 文件内容。

    Args:
        config: AstrBot 插件配置字典

    Returns:
        合法的 Python 配置文件内容字符串
    """
    host = _escape_string(config.get("backend_host", "127.0.0.1"))
    port = int(config.get("backend_port", 18080) or 18080)
    accounts = config.get("dashen_accounts", []) or []
    dts = int(config.get("dashen_dts", 2026) or 2026)
    server = int(config.get("dashen_server", 1) or 1)
    dashen_proxy = _escape_string(config.get("dashen_international_proxy", ""))
    ow_esports_api_key = _escape_string(config.get("ow_esports_api_key", ""))
    analysis_base_url = _escape_string(config.get("analysis_base_url", ""))
    analysis_api_key = _escape_string(config.get("analysis_api_key", ""))
    analysis_model = _escape_string(config.get("analysis_model", ""))
    persona_prompt = _escape_string(config.get("analysis_persona_prompt", ""))

    return _CONFIG_TEMPLATE.format(
        host=host,
        port=port,
        accounts=_format_accounts(accounts),
        dts=dts,
        server=server,
        dashen_proxy=dashen_proxy,
        ow_esports_api_key=ow_esports_api_key,
        analysis_base_url=analysis_base_url,
        analysis_api_key=analysis_api_key,
        analysis_model=analysis_model,
        persona_prompt=persona_prompt,
    )


def write_config(backend_dir: str | Path, config: dict) -> tuple[bool, str]:
    """生成并写入 config/config.py 到后端目录。

    Args:
        backend_dir: Overstats 后端根目录
        config: AstrBot 插件配置字典

    Returns:
        (success, message)
    """
    backend = Path(backend_dir)
    config_dir = backend / "config"
    config_file = config_dir / "config.py"

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        content = generate_config(config)
        config_file.write_text(content, encoding="utf-8")
        # 校验生成的文件语法是否合法
        import py_compile
        try:
            py_compile.compile(str(config_file), doraise=True)
        except py_compile.PyCompileError as e:
            logger.error(f"[OverstatsDeploy] 生成的 config.py 语法错误: {e}")
            return False, f"生成的 config.py 语法错误: {e}"
    except Exception as e:
        logger.error(f"[OverstatsDeploy] 写入 config.py 失败: {e}")
        return False, f"写入配置文件失败: {e}"

    return True, f"配置文件已写入: {config_file}"


def validate_accounts(config: dict) -> tuple[bool, str]:
    """校验大神账号配置是否至少包含一个有效账号。

    Returns:
        (valid, message)
    """
    accounts = config.get("dashen_accounts", []) or []
    valid_count = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        try:
            role_id = int(acc.get("role_id", 0))
        except (TypeError, ValueError):
            role_id = 0
        token = str(acc.get("token") or "").strip()
        if role_id > 0 and token:
            valid_count += 1

    if valid_count == 0:
        return False, "未配置任何有效的大神账号，请先在 AstrBot 插件配置面板填写至少一个账号的 role_id 和 token"
    return True, f"已配置 {valid_count} 个有效账号"
