# 05 - `apply_triage` 确定性命令（ADR-0001）

**What to build:** v0.3 的 triage 写入 chokepoint--确定性命令 `ai-dev triage`，把 Human-Triage 的 disposition 写到 `ISSUE-NNN.json.triage`（不写到 bundle、不写到 Decision）。disposition 值域 `accept|reject|defer|override|request_fix|request_change_proposal`（§16）。按升格规则产生 DEC-NNN：DEC iff (override×P1) 或 (reject×{P0,P1})；其余 disposition 不升格。强制 disposition×severity 合法性矩阵（P0 不可 override、不可 defer/accept；P1 可 override/reject；P2/P3 仅 bookkeeping）。P0 两层防御：写层 refuse P0×override、gate 层（06）忽略 P0×override。reject-disarming（P0/P1 reject）须带 reason。`request_change_proposal` 走 clean deferral：记录 `request_cp` + 不产 CP-NNN（CP 生命周期 v0.4）。纯 deterministic，不调模型。triage 经 02 的 merge 在 re-collect 后保留。

**Blocked by:** 02, 03

**Status:** ready-for-agent

- [ ] `ai-dev triage --issue ISSUE-NNN --disposition <d> [--reason ...]` 写 `ISSUE-NNN.json.triage`
- [ ] 升格规则：override×P1 / reject×{P0,P1} 产 DEC-NNN；其余不升格
- [ ] disposition×severity 合法性矩阵全 cell 覆盖（合法通过 + 非法 fail-loud）
- [ ] P0×override 写层拒绝（两层防御第一层）
- [ ] reject-disarming（P0/P1 reject）缺 reason fail-loud
- [ ] `request_change_proposal` 记 `request_cp`，不产 CP-NNN（clean deferral）
- [ ] 纯 deterministic（不调模型），审计 triage 事件
- [ ] status 置 `triaged`（调用 03 helper）
