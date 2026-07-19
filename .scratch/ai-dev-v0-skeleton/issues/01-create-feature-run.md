# 01 — create-feature-run CLI（tracer bullet）

**What to build:** 一条命令把一段 intent 变成落盘的 feature run。在 `.ai-dev/features/<FEATURE-NNN>/` 下生成 §6 的完整目录骨架并分配 FEATURE 编号。这是 v0.0 的贯通薄切片，极简地触碰目录生成、ID、status、模板、audit 五件事（后续 ticket 把每件做实）。

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] 给定一段 intent 文本，命令在 `.ai-dev/features/` 下创建新目录并分配递增的 `FEATURE-NNN` 编号
- [ ] 目录结构符合 §6：含 `00-intent.md`（记录 intent）、`status/feature-status.yml`、空的 `lanes/ runs/ issues/ decisions/ projections/`、`final-report.md` 与 `final-report.json` 占位
- [ ] `status/feature-status.yml` 初始为 `status: planning`、`frozen_artifacts` 四项全 false、`current_gate: requirements_gate`（§8.3）
- [ ] `audit.log.md` 写入第一条 create 记录
- [ ] 连续创建两个 feature run，编号分别为 FEATURE-001、FEATURE-002
