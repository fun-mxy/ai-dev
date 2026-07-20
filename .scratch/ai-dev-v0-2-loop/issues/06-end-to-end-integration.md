# 06 - v0.2 端到端集成（implement -> review/gap/verify -> bundle -> lane-gate，ark）

**What to build:** v0.2 walking-skeleton 证明。在真实 feature run 上把 01-05 串起来：冻结 tasks/lane-graph -> implement(01) -> review+gap(02)+verify(03) -> collect-issues(04) -> lane-gate(05)，产出完整 lane artifact 链 + 一个 `lane-decision`。捕获 PASS 与 FAIL 各一次作为证据。集成暴露的接缝摩擦（路径/ID/接口对齐）在本票内修复不遗留；token 全程不落盘。与 v0.1 ticket 05 同构。

**Blocked by:** 01, 02, 03, 04, 05

**Status:** ready-for-agent

- [ ] 从一条 intent 起步：冻结 tasks/lane-graph -> implement -> review+gap+verify -> collect-issues -> lane-gate 五段依次跑通，无手动干预
- [ ] 产出完整 lane artifact 链：`implement-result` / `review-report` / `spec-gap-report` / `verification-report` / `issue-bundle` / `lane-decision`（各 md+json）
- [ ] 捕获 PASS 场景（全绿 -> `lane-decision` PASS）与 FAIL 场景（注入一个 P0/P1 或 verification 失败 -> FAIL）各一次作证据
- [ ] `ISSUE-NNN` / `RUN-NNN` / `LANE-001` ID 在 v0.0/v0.1/v0.2 间正确衔接，无重号/错位
- [ ] token 全程不落盘（lane artifact 与 run 目录内 grep 不到 token 值）
- [ ] 集成暴露的接缝问题（路径/ID/接口对齐）本票内修复
