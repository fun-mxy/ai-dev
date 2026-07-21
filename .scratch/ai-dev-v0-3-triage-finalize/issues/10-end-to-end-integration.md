# 10 - v0.3 端到端集成（真实 Ark run）

**What to build:** v0.3 walking-skeleton 证明。在真实 feature run 上把 01-09 串起来：freeze（推进 current_gate）-> implement -> review+gap+verify -> collect-issues（02 merge）-> Human Triage（05 apply_triage）-> [若 request_fix] fix-run（07）-> re-collect + re-triage -> lane-gate（06）-> coherence-gate（08）-> final-report（09）。捕获 PASS 场景（全绿 -> verdict=pass -> final-report pass）与 FAIL 场景（override 一个 P1 / request_cp -> verdict=fail -> final-report failure_class=terminal/recoverable）各一次作证据。ISSUE/RUN/LANE/DEC ID 跨 v0.0-v0.3 正确衔接。token 全程不落盘。集成接缝摩擦票内修复。与 v0.1/v0.2 e2e 同构（真实 cc-glm52/Ark，非 fake-claude pytest）。

**Blocked by:** 07, 08, 09

**Status:** ready-for-agent

- [ ] 从一条 intent 起：freeze -> implement -> review/gap/verify -> collect -> triage -> [fix-run] -> re-collect/re-triage -> lane-gate -> coherence-gate -> final-report 依次跑通，无手动干预
- [ ] 产出完整 v0.3 artifact 链：triage 写入、DEC-NNN（若有）、fix-run（若有）、coherence verdict、final-report.{json,md}
- [ ] 捕获 PASS 场景（verdict=pass）与 FAIL 场景（P1 override / request_cp -> verdict=fail，failure_class 标注）各一次作证据
- [ ] current_gate 全程正确推进（requirements->...->feature_coherence_gate），feature.status 派生正确
- [ ] ISSUE/RUN/LANE/DEC ID 跨 v0.0-v0.3 衔接无重号/错位
- [ ] token 全程不落盘（artifact 与 run 目录 grep 不到 token 值）
- [ ] 集成接缝问题票内修复不遗留
- [ ] evidence/ 目录记录真实 Ark run 证据
