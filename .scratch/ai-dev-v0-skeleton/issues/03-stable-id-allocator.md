# 03 — Stable ID 分配器

**What to build:** 按 §5.2 为 REQ/AC/DES/TASK/RUN/REV/GAP/VER/ISSUE/DEC/CP/LANE 各类型确定性分配并持久化稳定 ID（同类型递增、跨 run 不重复），每次分配经 audit（用 02）记录。

**Blocked by:** 01, 02

**Status:** ready-for-agent

- [x] 支持为 §5.2 全部 12 种 ID 类型分配编号
- [x] 同类型连续分配递增（首个为 001）
- [x] 分配状态持久化在 feature run 内，进程重启后继续递增、不重复
- [x] 每次分配经 02 的 audit 组件记录
- [x] 连取两个 REQ 得 REQ-001、REQ-002；模拟重启后再取一个 REQ 得 REQ-003
