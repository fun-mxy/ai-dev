# 01 - `final_verdict` -> `verdict` 字段重命名（ADR-0003 D4）

**What to build:** 把 `feature-status.yml` 的 `final_verdict` 字段重命名为 `verdict`，覆盖 status.py 初始写入（`_initial_feature_status`）+ 任何读取方 + 对应单测 + spec §8.3 示例。这是 v0.3 的基础预置：下游所有票（04 feature.status 派生、08 coherence 写 verdict、09 final-report 读 verdict）都引用 `verdict`。重命名对齐 ADR-0001 已立的 Verdict 词汇（"gate 确定性算出的 pass/fail"）--`lane-decision.json.verdict`（lane gate）与 `feature-status.yml.verdict`（coherence gate）同名、各住各 artifact，无字段碰撞。`final_` 前缀删除：它虚假暗示不可变（verdict 实为可变，08 re-coherence 覆写），且是冗余限定。本票只改名，不引入 writer（verdict 仍为 null，writer 在 08）。blast radius 小（字段当前无 writer，仅 init + 测试 + spec），单票机械完成，无需 expand-contract。

**Blocked by:** 无 - 可立即开始

**Status:** ready-for-agent

- [ ] `feature-status.yml` 字段 `final_verdict` -> `verdict`（init 值仍 null）
- [ ] status.py `_initial_feature_status` + 任何读取 `final_verdict` 的代码同步改名
- [ ] spec §8.3 示例 `final_verdict: null` -> `verdict: null`
- [ ] 单测更新并通过：init 文档含 `verdict: null`、不含 `final_verdict`
- [ ] mypy + 测试全绿；仓库内无残留 `final_verdict` 字符串（grep 干净）
