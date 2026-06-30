# Changelog

## v2.6.2 (2026-06-30)

### ✨ 新增
- **违规内容自动封禁**：所有图片查询指令发送时若被平台接口判定违规，自动封禁该用户对应指令 12h，提示联系管理员
- 新增 `/ow违禁封禁` / `/ow违禁解封` 管理员指令，支持按「用户ID+指令名」粒度手动管理封禁
- 封禁数据纯 JSON 文件存储（`violation_bans.json`），零 KV 依赖，过期自动清除

### 🔧 优化
- `_send_image_result` 重构为 async generator，25 处单图调用全部覆盖违规捕获

## v2.4.1 (2026-06-29)

### ✨ 新增
- **无空格指令识别**：支持 `/{命令}{战网ID}` 格式（如 `/今日总结Player#12345`），自动拆分并派发

### 🔧 优化
- **是区吗扩充 6v6 模式**：抓取范围从 `SportPreset/LeisurePreset` 扩展到 `Sport6v6/Leisure6v6`
- **队友英雄识别修复**：`_expand_player_segments` 改为优先使用 `_heroList.heroId`，避免从 statMap 推断错误
- **队友数据查询重构**：改用 `customer_token + match_id` 查同一局（对齐后端 `_build_all_player_details`），替代不可靠的 index 匹配
- **战网ID错误提示收敛**：`_bnet_err` 统一 10 处重复的错误文案

## v2.3.2 (2026-06-27)

### ✨ 新增
- **「ow是区吗」功能全量开放**：基于最近对局数据的 AI 阴阳怪气判定，支持分级 CD（管理员 0 / 白名单 30min / 普通 4h）
- **「ow是区吗结果」**：快速查看上次生成的判定书图片
- 新增 `shiqu_cd_map` 统一配置项（JSON）：`normal` / `whitelist` / `admin` CD 时长 + `match_count` 抓取场数 + `normal_enabled` 普通用户开关（默认关闭）
- 数据抓取优先 SportPreset / LeisurePreset 预设职责对局，不足时自动补充
- `owhelp` / README 新增是区吗功能入口

### 🔧 优化
- 是区吗判定分数区间重调：≥83 职业吗 → 82-75 暴力炸 → 74-68 化蛹成蝶 → 67-60 不是你区 → 59-52 可能是区 → 51-43 哦灭跌多 → <43 你个大区
- 移除提示词中所有 `(xxx样本)` 后缀
- 补全所有 `@filter.command` 方法的 docstring 描述
- `_ensure_full_adapt_map` 添加注释禁止高成本 AI 指令入表

## v2.3.1
- 历史版本（略）
