# 08 - `ai-dev coherence-gate` 评估器（ADR-0003 D1/D2/D4）

**What to build:** v0.3 终局门控 `ai-dev coherence-gate`，确定性评估 3 条件：(1) final status 一致（current_gate/verdict/feature.status 互洽，04 的派生函数保证）；(2) 所有 P0/P1 已处理（resolved 或 disarmed，经 06 lane-gate 已 PASS）；(3) decisions 已记录（每个 disarmed blocking issue 有 DEC-NNN，ADR-0001 invariant #15）。同一次 mutation 原子写 `current_gate=feature_coherence_gate` + `verdict`（pass/fail）+ 派生 `feature.status`（done/blocked）。`verdict` 可变（re-coherence 覆写，如 fail->fix->re-coherence->pass）。§18.5 amend：删 "final report 是否完整" forward-ref（final report 是 coherence 的下游，step 21，非 coherence 验证对象；coherence 验 inputs）。纯 deterministic，不调模型。

**Blocked by:** 04, 06

**Status:** ready-for-agent

- [ ] `ai-dev coherence-gate` 评估 3 条件，产 pass/fail
- [ ] 原子写 current_gate=feature_coherence_gate + verdict + 派生 feature.status
- [ ] verdict 可变：re-coherence 覆写（fail->pass 测试）
- [ ] §18.5 删 "final report 是否完整" bullet
- [ ] P0/P1 未全处理 -> verdict=fail（status=blocked）
- [ ] 缺 DEC-NNN 的 disarmed issue -> verdict=fail
- [ ] 退出码 0=pass / 1=fail；审计 verdict 写入
- [ ] 单测：全 pass、P1 未处理 fail、缺 DEC fail、re-coherence 覆写
