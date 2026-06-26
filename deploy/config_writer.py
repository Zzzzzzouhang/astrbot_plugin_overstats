"""Overstats 配置文件生成器。

Overstats 的 config/config.py 是纯 Python 文件，loader.py 仅对 host/port/stream 等少量字段
支持环境变量覆盖，DASHEN_ACCOUNTS 必须写入文件。因此采用直接生成完整 config.py 的策略：
从 AstrBot 配置读取用户填写的值，程序化生成合法的 Python 配置文件并覆盖写入。

支持批量账号配置：通过 dashen_accounts_bulk 文本配置项，每行一条账号
（格式：name,role_id,token），适合需要配置大量账号的场景。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("astrbot")

# Overstats 默认配置文件的模板（与原项目 config/config.py 保持一致，
# 仅占位值由本生成器动态填充）。
_CONFIG_TEMPLATE = '''from __future__ import annotations

# ======================= Core Service ====================== #
API_HOST = {host!r}
API_PORT = {port}
USE_STREAM_RESPONSE = {use_stream_response}
ENABLE_DATABASE_WRITE = {enable_database_write}

# ======================= Dashen Upstream ====================== #
# Configure at least one account.
DASHEN_ACCOUNTS = {accounts}

DASHEN_DTS = {dts}
DASHEN_SERVER = {server}
DASHEN_ACCOUNT_MAX_REQUESTS_PER_SECOND = {dashen_account_max_rps}
DASHEN_ACCOUNT_RATE_LIMIT_WINDOW_SECONDS = {dashen_rate_limit_window}
DASHEN_CLIENT_TYPE = "60"
DASHEN_ORIGIN = "https://act.ds.163.com"
DASHEN_REFERER = "https://act.ds.163.com/"
DASHEN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36 "
    "app/df_client dfVersion/100111"
)
DASHEN_ACCOUNT_FAILURE_COOLDOWN_SECONDS = {dashen_failure_cooldown}
DASHEN_MAX_CONCURRENT_REQUESTS = {dashen_max_concurrent}
DASHEN_MAX_ACCEPTED_REQUESTS = max(len(DASHEN_ACCOUNTS) * 4, 1)

# Optional proxy settings.
DASHEN_INTERNATIONAL_PROXY = {dashen_proxy!r}
DASHEN_NETEASE_PROXIES = [
    None,
]

# OW esports PandaScore API key.
OW_ESPORTS_API_KEY = {ow_esports_api_key!r}

# Optional external OW guess asset pack root.
OW_GUESS_ASSET_ROOT = {ow_guess_asset_root!r}

# ======================= Dashen Season ====================== #
DASHEN_CURRENT_SEASON = {dashen_current_season}
DASHEN_HISTORY_START_SEASON = {dashen_history_start_season}

# ======================= OW Hero Leaderboard ====================== #
OW_HERO_LEADERBOARD_CN_SEASON = {ow_hero_leaderboard_cn_season}

# ======================= Match Analysis ====================== #
ANALYSIS_BASE_URL = {analysis_base_url!r}
ANALYSIS_API_KEY = {analysis_api_key!r}
ANALYSIS_PROXY = {analysis_proxy!r}

ANALYSIS_OPENAI_MODEL = {analysis_model!r}

# Optional external patch-note fetch proxy.
PATCH_NOTES_USE_INTERNATIONAL_PROXY = {patch_notes_use_proxy}
PATCH_NOTES_INTERNATIONAL_PROXY = {patch_notes_proxy!r}

ANALYSIS_PERSONA_PROMPT = {persona_prompt!r}
'''


def _parse_bulk_accounts(bulk_text: str) -> list[dict]:
    """从文本批量解析大神账号。

    支持两种格式（自动检测）：

    **1. JSON 格式**（推荐，可直接粘贴 Overstats 原版 DASHEN_ACCOUNTS 数组）：
        [
            {"name": "account-1", "role_id": 123456789, "token": "xxx"},
            {"name": "account-2", "role_id": 987654321, "token": "yyy"}
        ]

    也支持不完整的数组片段（自动补全括号）：
        {
            "name": "account-1",
            "role_id": 123456789,
            "token": "xxx",
        },
        {
            "name": "account-2",
            "role_id": 987654321,
            "token": "yyy",
        }

    **2. 纯文本格式**（每行一条，逗号/制表符/竖线分隔）：
        account-1,123456789,xxx
        account-2,987654321,yyy

    Args:
        bulk_text: 多行文本

    Returns:
        账号字典列表
    """
    if not bulk_text or not bulk_text.strip():
        return []

    text = bulk_text.strip()

    # 检测 JSON 格式（含 { 或 [ 开头，或包含 "role_id" 关键词）
    is_json = (
        text[0] in "{[" 
        or '"role_id"' in text 
        or "'role_id'" in text
    )

    if is_json:
        return _parse_bulk_json(text)

    # 纯文本格式
    return _parse_bulk_lines(text)


def _parse_bulk_json(text: str) -> list[dict]:
    """解析 JSON 格式的批量账号。

    自动处理常见粘贴格式：
    - 完整数组 [...]
    - 数组片段 {...},{...}（自动补全外层 [ ]）
    - 单个对象 {...}
    - 带尾逗号的片段（自动清理）
    """
    import json
    import re

    accounts: list[dict] = []
    text = text.strip()

    # 清理常见粘贴问题：去掉首尾多余的逗号
    text = text.strip().strip(",").strip()

    # 尝试直接解析（完整数组或单对象）
    for candidate in [text, f"[{text}]"]:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        accounts.append(item)
                if accounts:
                    break
            elif isinstance(data, dict):
                accounts.append(data)
                break
        except (json.JSONDecodeError, ValueError):
            continue

    if not accounts:
        # 直接解析失败，逐个提取 {...} 对象（处理尾逗号、前导空格等）
        # 用正则匹配花括号内的内容（非贪婪，支持多行）
        obj_pattern = re.compile(r'\{[^{}]*\}', re.DOTALL)
        for match in obj_pattern.finditer(text):
            obj_str = match.group(0)
            # 清理对象内尾逗号（如 {"a": 1,} -> {"a": 1}）
            obj_str_clean = re.sub(r',\s*}', '}', obj_str)
            obj_str_clean = re.sub(r',\s*]', ']', obj_str_clean)
            try:
                item = json.loads(obj_str_clean)
                if isinstance(item, dict):
                    accounts.append(item)
            except (json.JSONDecodeError, ValueError):
                continue

    if not accounts:
        logger.warning("[OverstatsDeploy] JSON 格式账号解析失败，未提取到任何账号")

    return accounts


def _parse_bulk_lines(text: str) -> list[dict]:
    """解析纯文本格式的批量账号，每行一条。

    格式：名称,role_id,token（逗号/制表符/竖线分隔）
    """
    accounts: list[dict] = []
    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = None
        for sep in (",", "\t", "|"):
            if sep in line:
                parts = [p.strip() for p in line.split(sep, 2)]
                break
        if not parts or len(parts) < 2:
            logger.warning(f"[OverstatsDeploy] 批量账号第 {line_num} 行格式错误，跳过: {raw_line[:50]}")
            continue

        name = parts[0] or f"account_{line_num}"
        try:
            role_id = int(parts[1])
        except (TypeError, ValueError):
            logger.warning(f"[OverstatsDeploy] 批量账号第 {line_num} 行 role_id 非数字，跳过: {parts[1]}")
            continue
        token = parts[2] if len(parts) >= 3 else ""

        if role_id <= 0 or not token:
            logger.warning(f"[OverstatsDeploy] 批量账号第 {line_num} 行无效（role_id<=0 或 token 为空），跳过")
            continue

        accounts.append({"name": name, "role_id": role_id, "token": token})

    return accounts


def _merge_accounts(webui_accounts: list[dict] | None, bulk_accounts: list[dict]) -> list[dict]:
    """合并 WebUI 账号与批量文本账号，按 role_id 去重。

    优先级：批量文本账号 > WebUI 账号（role_id 相同时以批量账号为准）。

    Args:
        webui_accounts: AstrBot WebUI template_list 配置的账号
        bulk_accounts: 从文本批量配置解析的账号

    Returns:
        合并去重后的账号列表
    """
    merged: list[dict] = []
    seen_role_ids: set[int] = set()

    # 批量账号优先
    for acc in bulk_accounts:
        if not isinstance(acc, dict):
            continue
        try:
            role_id = int(acc.get("role_id", 0))
        except (TypeError, ValueError):
            role_id = 0
        if role_id > 0 and role_id not in seen_role_ids:
            seen_role_ids.add(role_id)
            merged.append(acc)

    # WebUI 账号补充（去重）
    for acc in (webui_accounts or []):
        if not isinstance(acc, dict):
            continue
        try:
            role_id = int(acc.get("role_id", 0))
        except (TypeError, ValueError):
            role_id = 0
        if role_id > 0 and role_id not in seen_role_ids:
            seen_role_ids.add(role_id)
            merged.append(acc)

    return merged


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
    host = str(config.get("backend_host", "127.0.0.1") or "")
    port = int(config.get("backend_port", 18081) or 18081)
    webui_accounts = config.get("dashen_accounts", []) or []

    # 批量账号文本：每行一条（name,role_id,token），合并到 WebUI 账号并去重
    bulk_text = str(config.get("dashen_accounts_bulk", "") or "")
    if bulk_text.strip():
        bulk_accounts = _parse_bulk_accounts(bulk_text)
        if bulk_accounts:
            accounts = _merge_accounts(webui_accounts, bulk_accounts)
            logger.info(
                f"[OverstatsDeploy] 账号合并: 批量文本 {len(bulk_accounts)} 条 + "
                f"WebUI {len(webui_accounts)} 条 → 去重后 {len(accounts)} 条"
            )
        else:
            accounts = webui_accounts
            logger.warning(f"[OverstatsDeploy] 批量账号文本解析无有效账号，仅使用 WebUI 账号")
    else:
        accounts = webui_accounts
    dts = int(config.get("dashen_dts", 2026) or 2026)
    server = int(config.get("dashen_server", 1) or 1)
    dashen_proxy = str(config.get("dashen_international_proxy", "") or "")
    ow_esports_api_key = str(config.get("ow_esports_api_key", "") or "")
    analysis_base_url = str(config.get("analysis_base_url", "") or "")
    analysis_api_key = str(config.get("analysis_api_key", "") or "")
    analysis_model = str(config.get("analysis_model", "") or "")
    persona_prompt = str(config.get("analysis_persona_prompt", "") or "")

    # 新增可调配置项（从 AstrBot 配置读取，回退到原项目默认值）
    use_stream_response = bool(config.get("use_stream_response", True))
    enable_database_write = bool(config.get("enable_database_write", True))
    dashen_account_max_rps = int(config.get("dashen_account_max_requests_per_second", 5) or 5)
    dashen_rate_limit_window = float(config.get("dashen_account_rate_limit_window_seconds", 1.0) or 1.0)
    dashen_failure_cooldown = int(config.get("dashen_account_failure_cooldown_seconds", 60) or 60)
    dashen_max_concurrent = int(config.get("dashen_max_concurrent_requests", 2) or 2)
    ow_guess_asset_root = str(config.get("ow_guess_asset_root", "ow_guess_assets") or "")
    dashen_current_season = int(config.get("dashen_current_season", 23) or 23)
    dashen_history_start_season = int(config.get("dashen_history_start_season", 15) or 15)
    ow_hero_leaderboard_cn_season = int(config.get("ow_hero_leaderboard_cn_season", 3) or 3)
    analysis_proxy = str(config.get("analysis_proxy", "") or "")
    patch_notes_use_proxy = bool(config.get("patch_notes_use_international_proxy", False))
    patch_notes_proxy = str(config.get("patch_notes_international_proxy", "") or "")

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
        use_stream_response=use_stream_response,
        enable_database_write=enable_database_write,
        dashen_account_max_rps=dashen_account_max_rps,
        dashen_rate_limit_window=dashen_rate_limit_window,
        dashen_failure_cooldown=dashen_failure_cooldown,
        dashen_max_concurrent=dashen_max_concurrent,
        ow_guess_asset_root=ow_guess_asset_root,
        dashen_current_season=dashen_current_season,
        dashen_history_start_season=dashen_history_start_season,
        ow_hero_leaderboard_cn_season=ow_hero_leaderboard_cn_season,
        analysis_proxy=analysis_proxy,
        patch_notes_use_proxy=patch_notes_use_proxy,
        patch_notes_proxy=patch_notes_proxy,
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

    同时检查 WebUI 账号和外部账号文件。

    Returns:
        (valid, message)
    """
    webui_accounts = config.get("dashen_accounts", []) or []

    # 合并批量文本账号
    bulk_text = str(config.get("dashen_accounts_bulk", "") or "")
    if bulk_text.strip():
        bulk_accounts = _parse_bulk_accounts(bulk_text)
        all_accounts = _merge_accounts(webui_accounts, bulk_accounts)
    else:
        all_accounts = webui_accounts

    valid_count = 0
    for acc in all_accounts:
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
        return False, "未配置任何有效的大神账号，请先在 AstrBot 插件配置面板填写至少一个账号的 role_id 和 token，或配置账号文件路径"
    return True, f"已配置 {valid_count} 个有效账号"
