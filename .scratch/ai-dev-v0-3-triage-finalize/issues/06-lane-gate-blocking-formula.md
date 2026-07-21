# 06 - lane-gate 阻塞公式更新（ADR-0001 D5）

**What to build:** 更新 lane-gate（`lane_gate.py`）的阻塞判定，从 v0.2 的 "raw issues 上 P0 必 fail、P1 默认 fail" 升级到读 triage 的修正公式。从 issues（经 bundle 投影）读每个 P0/P1 的 triage：P0×override 忽略（gate 层不认，即便写层漏过也挡）即仍 FAIL；P0×reject 合法逃生（有 DEC -> PASS，disarmed）；P1×override 有 DEC -> PASS；P1×reject 有 DEC -> PASS；P1 未 triaged 或 `request_fix` 未解决 -> FAIL；`request_cp` -> FAIL（recoverable，待 CP fulfillment v0.4）。v0.2 注释 "no triage override" 移除。lane-gate 仍不碰 current_gate（04 的 gate 推进只到 lane_gate，coherence 在 08）。

**Blocked by:** 05

**Status:** done

- [x] P0×override 仍 FAIL（gate 层忽略，第二层防御）
- [x] P0×reject + 有 DEC -> PASS（合法逃生）
- [x] P1×override/reject + 有 DEC -> PASS
- [x] P1 未 triaged / request_fix 未解决 -> FAIL
- [x] request_cp -> FAIL（recoverable，记录 resolution_path）
- [x] lane-gate 不写 current_gate（只 04/08 推进）
- [x] 单测：全 PASS、P0×override FAIL、P0×reject PASS、P1×override PASS、P1 未 triage FAIL、request_cp FAIL
- [x] 退出码 0=PASS / 1=FAIL；缺前置 artifact fail-loud（§24.2）
