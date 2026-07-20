# 04 - Issue normalization + bundle（ISSUE-NNN + issue-bundle）

**What to build:** 收集 `review-report` + `spec-gap-report` 的 `issues[]`，复用 v0.0 stable-ID 分配器分配 `ISSUE-NNN`（重启不重复）、去重、附 severity（§15.1 P0-P3），写 feature 级 `issues/ISSUE-NNN.{md,json}` + lane 级 `issue-bundle.{md,json}`（md+json 双产物）。只聚合 reviewer+gap 的 issues；verifier 的 pass/fail 由 gate 直接消费、不进 bundle。severity 由角色给出，normalizer 只做编号/去重/聚合，不改判 severity。

**Blocked by:** 02

**Status:** done

- [x] 收集 `review-report` + `spec-gap-report` 的 `issues[]`，复用 v0.0 分配器分配 `ISSUE-NNN`（重启不重复）
- [x] 去重（同 source/title/evidence 合并）、保留 severity（§15.1 P0-P3），normalizer 不改判 severity
- [x] 写 feature 级 `issues/ISSUE-NNN.{md,json}`（每 issue 一对双产物）+ lane 级 `issue-bundle.{md,json}`
- [x] 只聚合 reviewer+gap 的 issues；verifier pass/fail 不进 bundle
- [x] 连续两次 collect 得稳定 `ISSUE-NNN`（不重号）
- [x] 单测：去重、编号稳定、severity 保留、双产物
