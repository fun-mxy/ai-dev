# 02 - Checking runs（Code Reviewer + Spec Gap Analyst；issues 契约 + review-report + spec-gap-report）

**What to build:** 一次 implement run 之后，构建 Reviewer input package（含 implement 的 `changed_files` / diff + task 上下文）与 Spec-Gap input package（含 requirements/design/tasks + implement diff），分别复用 `run-headless` 跑完、用新增的 **issues output-schema（§15）** 做 `validate-run`；产出 lane 级 `review-report.{md,json}` 与 `spec-gap-report.{md,json}`（各含 `issues[]`）。两角色共享同一 issues 契约，故合并一票。职责边界：reviewer 只查代码质量/bug/边界/安全、不判规格偏离；spec-gap 只对比 req/design/tasks 与 diff、不做风格审查（§9.3/§9.4）。

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] 新增 issues output-schema（§15：id/source/severity/title/description/related_tasks/related_requirements/related_acceptance_criteria/evidence/recommendation/requires_change_proposal），reviewer 与 gap 共用
- [ ] Reviewer input package 含 implement run 的 `changed_files`/diff + task 上下文；`run-headless` 跑完、`validate-run` 用 issues schema 通过
- [ ] Spec-Gap input package 含 requirements/design/tasks + implement diff；`run-headless` 跑完、`validate-run` 用 issues schema 通过
- [ ] 产出 lane 级 `review-report.{md,json}` 与 `spec-gap-report.{md,json}`，各含 `issues[]`（md+json 双产物）
- [ ] 职责边界守住：reviewer 不判规格偏离、gap 不做风格审查（§9.3/§9.4）
- [ ] 单测：issues schema 校验、两角色 input-package 组装、report rollup
