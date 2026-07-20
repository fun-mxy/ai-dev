# 01 - Implementer leg（冻结 task -> implement run -> proposed_done -> implement-result）

**What to build:** 从一条已冻结 tasks + lane-graph 的 feature run 起，为 `LANE-001` 的一个 task 构建 Implementer input package（task 文本来自 `03-tasks.md`、allowed-files 来自 `04-lane-graph.yml` 的 expected/exclusive files），复用 v0.1 的 `prepare-run` / `run-headless` / `validate-run` 跑完一次 implement run；把 `result.json` 的 task 状态写回 canonical `task-status.yml`（`proposed_done`），由 deterministic runtime 写入（不交模型，§4.3）；产出 lane 级 `implement-result.{md,json}`（rollup 自 run 的 result + metadata）。§9.2 限制（不宣布 final done、不动 frozen、不改 canonical status、不越 allowed-files boundary）由现有 `validate-run` 与 runtime 保证。

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [ ] 从已冻结 tasks + lane-graph 的 feature run，为 `LANE-001` 的一个 task 构建 implementer input package（task 文本来自 `03-tasks.md`、allowed-files 来自 lane-graph）
- [ ] 复用 v0.1 `prepare-run` / `run-headless` / `validate-run` 跑通一次 implement run（不重建 run 机制）
- [ ] `result.json` 的 task 状态写回 canonical `task-status.yml` = `proposed_done`；由 deterministic runtime 写、不经模型（§4.3）
- [ ] 产出 lane 级 `implement-result.{md,json}`，字段齐全且与 run 的 result+metadata 一致（md+json 双产物 §4.4）
- [ ] §9.2 限制守住：不宣布 final done、不动 frozen、不越 allowed-files boundary（复用 `validate-run`）
- [ ] 单测：input-package 从冻结 artifact 组装、`proposed_done` 写回、`implement-result` rollup
