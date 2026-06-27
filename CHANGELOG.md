# Changelog

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
