# 02 - 审计事件 `elapsed_ms` + `origin` 字段（ADR 待定，glossary pin `origin`）

**What to build:** v0.4 polish 第二项（§26.5 better audit log），锁定方案 **(B)**：给审计事件加两个字段，**不**做 correlation-key 全量重构（留作后续）。现状：~14 个 event 类型覆盖面够（create/allocate_id/prepare_run/run/validate/verify/lane_gate/collect_issues/triage/coherence_gate/fix_run/freeze/advance_gate/mark_task_proposed_done），但三个洞：(1) **无时长**——`started_at`/`ended_at` 在 `RunResult` 上，但 `run` 事件的 payload 只有 `exit_code`+`changed_files`，从日志答不出"implement leg 跑了多久"；(2) **无来源**——`run` 事件分不出是 `run-headless`、`implement` 还是 `fix-run` driver 触发的；(3) correlation-key 不统一（本票**不**修）。做两件事：(1) `append_audit_event` 的 payload 约定新增 `elapsed_ms`（run/verify/gate 等有时长语义的事件，值=ended-started 毫秒；无时长语义的事件不带）。(2) 新增 `origin` 字段为每个事件标注驱动者（如 `cli`、`implement-leg`、`review-leg`、`spec-gap-leg`、`fix-run-driver`、`dry-run`、`verifier`）——在调用方显式传入，不靠推断。canonical 字段名锁定为 **`origin`**（防止后续分裂成 `actor`/`source`/`trigger`），写入 `docs/glossary.md`。token 值绝不进 payload（与现有一致）。

**Blocked by:** 无 — 可立即开始（与 01 并行）

**Status:** pending

- [ ] `append_audit_event` 支持payload 带 `elapsed_ms`（number，毫秒）；文档说明"仅有时长语义的事件携带"
- [ ] run / verify / lane_gate / coherence_gate / fix_run 事件写入真实 `elapsed_ms`
- [ ] 每个事件携带 `origin`（调用方显式传入）；canonical 名 `origin` 写入 `docs/glossary.md`
- [ ] token 值仍绝不落 payload（grep 确认）
- [ ] 单测：典型事件含 `elapsed_ms` + `origin`；无时长语义事件不带 `elapsed_ms`
- [ ] 单测：audit.log.{md,json} 双产物同步含新字段
- [ ] mypy + 全测试绿
