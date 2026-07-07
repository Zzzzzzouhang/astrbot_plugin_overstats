"""运维/部署工具子包。

封装 Overstats 后端的自动化部署、生命周期管理、配置生成与卸载逻辑：
manager / config_writer / git_ops / process_runner / uninstaller / venv_ops。
独立于 main.py，避免主插件文件臃肿。
"""