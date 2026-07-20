# 05 - Lane gate evaluator（§18.4 lane-decision）

**What to build:** 给定 implement `proposed_done`(01) + verification report(03) + issue bundle(04)，按 §18.4 评估 lane gate：`proposed_done` ✓、verification 通过、review/gap 无 P0/P1 阻塞 issue -> **PASS**；任一不满足 -> **FAIL** 并记录原因。产出 lane 级 `lane-decision.{md,json}`。v0.2 在 raw issues 上评估（P0 必 fail、P1 默认 fail），triage/override 留 v0.3（§16/§26.4）。纯 deterministic（读 artifact、判规则、写 decision），不调模型。§18.4 五条件中"issue triage 完成"在 v0.2 退化为"issue bundle 已生成"。

**Blocked by:** 01, 03, 04

**Status:** ready-for-agent

- [ ] §18.4 五条件评估：`proposed_done` ✓、verification 通过、review 无 P0/P1、gap 无 P0/P1、issue bundle 已生成
- [ ] P0 存在必 FAIL、P1 默认 FAIL（§15.2 gate rule）；v0.2 不做 override（留 v0.3）
- [ ] 产出 lane 级 `lane-decision.{md,json}`（md+json 双产物），PASS/FAIL + 逐条件原因
- [ ] 纯 deterministic（读 artifact、判规则、写 decision），不调模型
- [ ] 退出码 0=PASS / 1=FAIL；缺前置 artifact fail loud（§24.2）
- [ ] 单测：全 PASS、P0 FAIL、P1 FAIL、verification 失败 FAIL、缺 artifact fail-loud
