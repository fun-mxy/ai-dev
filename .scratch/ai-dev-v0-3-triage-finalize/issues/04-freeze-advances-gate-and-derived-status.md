# 04 - `set_current_gate` 接入 `freeze_artifact` + `feature.status` 派生投影（ADR-0003 D2/D3）

**What to build:** 把已存在的 `set_current_gate` 原语（status.py:222）原子接入 `freeze_artifact`：冻结 requirements/design/tasks 时，同一次 mutation 内推进 `current_gate` 到下一 stage（requirements_gate->design_gate->task_gate->lane_gate）。同时把 `feature.status` 立为 `(current_gate, verdict)` 的派生投影（非独立字段），每次写 current_gate 或 verdict 时原子重算：`verdict=pass`->`done` / `verdict=fail`->`blocked` / `null`+{req,design,task}gate->`planning` / `null`+lane_gate->`implementing`。`(null, feature_coherence_gate)` 是原子写不暴露的瞬态，不产生投影值（`verifying` 删除）。`freeze(lane_graph)` 不单独推进（推进已在 `freeze(tasks)` 完成）。本票只做人工门控推进 + 派生投影；lane gate 不碰 current_gate；coherence 推进在 08。

**Blocked by:** 01

**Status:** done

- [x] freeze(requirements/design/tasks) 原子推进 current_gate 到下一 stage，审计 advance_gate
- [x] `feature.status` 按 (current_gate, verdict) 派生表投影，永不独立写
- [x] 4 个 status 值（planning/implementing/done/blocked）cell-coverage 断言测试
- [x] 显式断言 `(null, feature_coherence_gate)` 不可达格永不被产生
- [x] `blocked` 严格指 coherence-fail（本票尚无 verdict writer，blocked 分支在 08 后可测；本票先钉派生函数）
- [x] freeze(lane_graph) 不重复推进 current_gate
