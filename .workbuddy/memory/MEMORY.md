# 项目长期记忆：astrbot_plugin_overstats

## 架构约定
- `main.py` 仅作薄壳：`@register` + `OverstatsPlugin(Star, PluginCore, PluginBinds, PluginEvents, PluginCmdsQuery, PluginCmdsBrowse, PluginCmdsAdmin)`。
- 6 个 mixin 模块位于 **`deploy/plugin_modules/`**（含 `__init__.py`）：`plugin_core` / `plugin_binds` / `plugin_events` / `plugin_cmds_query` / `plugin_cmds_browse` / `plugin_cmds_admin`。
- 每个 mixin 复制同一导入块，用 `try/except` 同时支持相对导入与顶层导入。
- **相对导入点层数规则**：mixin 位于 `根/deploy/plugin_modules/`，比根级 `deploy` 包深 2 级，因此指向 deploy 包必须用 `from ...deploy`（三级点）；`main.py` 用 `from .deploy.plugin_modules.plugin_*`。

### deploy 包内部结构（已按职责分子包）
- `deploy/ops/`：运维/部署工具 —— `manager` / `config_writer` / `git_ops` / `process_runner` / `uninstaller` / `venv_ops`（彼此 `from .` 互引，均在 ops 内）。
- `deploy/ow/`：OW 业务领域 —— `court` / `shiqu` / `shiqu_log` / `stat_db` / `monitor` / `backend_metrics` / `render_utils` + 数据文件 `query_tool.json`（court/shiqu/stat_db 用 `Path(__file__).parent/"query_tool.json"` 加载，随文件同目录移动）。
- `deploy/plugin_modules/`：6 个 mixin（见上）。
- **引用规则**：OW 域经 `deploy.ow.court` / `deploy.ow.shiqu` / `deploy.ow.shiqu_log`（main.py 用 `from .deploy.ow.x`，mixin 用 `from ...deploy.ow.x`）；ops 域经 `deploy.ops.manager` / `deploy.ops.uninstaller`；`DeployManager`/`MonitorCollector` 等仍由 `deploy/__init__.py` 从 ops/ow 再导出，故 `from .deploy import X` 不变。
- 测试文件（`tests/test_*.py`、`simulate_prompt.py`）原 `from deploy.xxx` 已同步改为 `deploy.ow.xxx` / `deploy.ops.xxx`。

## 监控与 error_code
- `deploy/monitor.py` 的 `cmd_records` 含 `error_code` 列；`SOFT_ERRORS = {"summary_empty","bnet_not_found"}` 不计入成功率分母但出现在失败原因面板。
- 路由：`monitor/overview`、`monitor/commands`、`monitor/commands/failures`。

## 验证手段
- 无 astrbot 环境时，用桩模块（`astrbot`/`deploy`/`aiohttp` 占位，`filter.PlatformAdapterType`/`PermissionType` 用 `enum.Flag` 支持 `|`）`importlib` 加载 `main.py`，检查 MRO 与 `@filter.*` 收集数量。
- ⚠️ 拆分/移动脚本运行前先备份原文件，按方法边界切分而非裸行号。
