# 04 — Canonical status 写入器 + freeze 机制

**What to build:** 唯一可写 canonical status 的确定性写入器（§4.3 cardinal rule——模型不直接写 canonical state，只有确定性代码写）。按 §8 维护 `feature-status.yml`（frozen_artifacts、current_gate）及最小 `lane-status.yml`/`task-status.yml`；并实现 §4.2 的 freeze 操作（人工确认后翻转某 artifact 的 frozen 标志，引用 03 的稳定 ID）。

**Blocked by:** 01, 02, 03

**Status:** ready-for-agent

- [ ] 确定性写入器按 §8 schema 更新 `feature-status.yml`（frozen_artifacts、current_gate）
- [ ] 维护最小 schema-correct 的 `lane-status.yml`（单 LANE-001）与 `task-status.yml`
- [ ] freeze 操作：对某 artifact 执行后翻转其 frozen 标志并经 audit 记录
- [ ] 已 frozen 的 artifact 被再次写入时拒绝（守住 §4.2）
- [ ] 代码路径中不存在模型 / 非确定性写入 canonical status 的入口（§4.3 cardinal rule）
- [ ] 引用的 ID（如 LANE-001）来自 03 的分配器
