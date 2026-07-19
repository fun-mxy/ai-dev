# 05 — Artifact 模板（requirements / design / tasks / lane-graph）

**What to build:** §7 的 markdown + json/yaml 模板（`01-requirements`、`02-design`、`03-tasks`、`04-lane-graph`），带 stable-ID 占位与 frozen 标记；`04-lane-graph.yml` 内置 §5.3 / §7.5 的单 lane MVP 默认（LANE-001）。新建 feature run 时种子化这些模板，供 v0.2 的 Planner 填充。

**Blocked by:** 01, 03

**Status:** ready-for-agent

- [ ] 提供四个 artifact 的模板（requirements / design 各 md + json，tasks md，lane-graph yml）
- [ ] 模板含 stable-ID 占位与 frozen 标记字段
- [ ] `04-lane-graph.yml` 含单 lane MVP 默认（id: LANE-001，§7.5 形态）
- [ ] 新建 feature run（01）时种子化四个模板
- [ ] 模板产物 schema 合法（json/yaml 可解析、必填字段齐）
- [ ] lane-graph 中的 LANE-001 引用 03 分配的 ID
