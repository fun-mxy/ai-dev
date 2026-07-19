# 02 - RUN 目录创建 + input package 构建器

**What to build:** 给定 feature_id + role + task，复用 v0.0 的 stable-ID 分配器分配 `RUN-NNN`，在 feature run 的 `runs/RUN-NNN/` 下创建 `input/ output/ workspace/`，并按 §12.2 写入 input package（role.md / system.md / task-package.md / output-schema.json / allowed-files.txt / context/）。提供 `ai-dev prepare-run <FEATURE> --role <role> --task <task>`，打印 RUN-NNN。input package 形态以 prototype 的 `runs/RUN-001/input/` 为种子。

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [x] 递增 RUN-NNN（复用 v0.0 分配器，重启不重复），落在 feature run 的 `runs/RUN-NNN/`
- [x] input package 含 §12.2 全部文件，output-schema.json 可解析、allowed-files 非空
- [x] system.md 含 §12.2 全局约束（不写 canonical status / 不改 frozen / 只写允许文件 / 必出 result.json）
- [x] 连续 prepare 两次得 RUN-001、RUN-002
- [x] 单测：目录结构 + input package 内容
