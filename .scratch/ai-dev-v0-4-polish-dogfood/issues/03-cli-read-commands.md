# 03 - CLI 只读命令（`list-features` / `show-status` / `log`）+ 全局 `--json`

**What to build:** v0.4 polish 第三项（§26.5 CLI UX），锁定方案 **(A)**：把 CLI 从"只写"变成"可观测"。现状 13 条命令全是"做事"，零条让你"看状态"——要看 feature 进度得手 `cat` feature-status.yml / audit.log.md / lane-decision.json。三件事：(1) 新增 `ai-dev list-features`：列出 `.ai-dev/features/` 下所有 FEATURE-NNN 及派生 `feature.status`（planning/implementing/done/blocked）+ 当前 gate。(2) `ai-dev show-status <FEATURE>`：打印 current_gate + verdict + 派生 feature.status + 各 lane 的 lane-decision。(3) `ai-dev log <FEATURE>`：按时间序 pretty-print 审计时间线——**直接消费 02 的 `origin`/`elapsed_ms`**（这是 02 的回报点：没有 02，log 命令没有"谁驱动/跑了多久"可显示）。此外：(4) 全局 `--json` flag 让 `list-features`/`show-status`/`log` 输出机器可读 JSON（人读默认）。(5) `--repo-root` 重复声明抽到顶层 parser（dedup，减重复）。`log` 命令读 audit.log.json（机器产物）渲染，不读 .md。只读命令 exit 0=有数据、1=feature 不存在（§24.2 fail loud）。

**Blocked by:** 02 — `log` 依赖 02 写入的 `origin`/`elapsed_ms`

**Status:** pending

- [ ] `ai-dev list-features [--repo-root] [--json]`：列 FEATURE-NNN + 派生 status + current_gate
- [ ] `ai-dev show-status <FEATURE> [--repo-root] [--json]`：current_gate + verdict + feature.status + 各 lane lane-decision
- [ ] `ai-dev log <FEATURE> [--repo-root] [--json]`：按时间序渲染审计时间线（消费 02 的 `origin`/`elapsed_ms`）
- [ ] 全局 `--json` flag 接入三条只读命令（默认人读、JSON opt-in）
- [ ] `--repo-root` 抽到顶层 parser，各子命令 dedup（不破坏现有调用）
- [ ] 单测：三条命令在 seeded feature run 上输出正确；`--json` 输出可 parse
- [ ] feature 不存在时 exit 1 + 干净 `error:`（沿用 01 helper）
- [ ] mypy + 全测试绿
