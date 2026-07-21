# 09 - `ai-dev final-report` 生成器（ADR-0003 D5/D6/D7）

**What to build:** `ai-dev final-report` 生成器，读 coherence verdict（08），产出 `final-report.json`（canonical）+ `final-report.md`（骨架）。JSON 顶层以 §2.1 五问为骨架：`code_to_requirement` / `requirement_coverage` / `acceptance_verification` / `issue_dispositions` / `agent_timeline` + `meta` + failure-shape（`verdict` + `failure_class: recoverable|terminal` + `blocking_reasons[]`，每条 issue_id/kind/resolution_path）。四条生成纪律：(1) failure-shape 是 D6 分类的可审计载体；(2) 多值按稳定 key 排序枚举，键永在、值可空（区分"因空而缺"vs"因损坏而缺"）；(3) code->requirement 追溯索引必须存在，v0.3 runs/ 不携带 changed-files 则显式留空标 known_gap，不静默省略；(4) 各 section 内字段枚举留 ticket。MD 为确定性骨架（从 JSON 渲染），v0.3 无 narrative hook，spec/model 内容分 section 隔离。`verdict==null`（coherence 未运行）时 fail-loud 拒绝（§24.2）。final-report 非冻结 artifact（可重算）。§23.5 step 20/21 标注 producer/consumer。

**Blocked by:** 08

**Status:** done

- [x] `ai-dev final-report` 产 final-report.json + final-report.md
- [x] JSON 顶层五键 + meta + failure-shape 俱在（"五键俱在"机械可校验）
- [x] verdict==null 时 fail-loud 拒绝
- [x] failure_class: recoverable|terminal；blocking_reasons[] 带 issue_id/kind/resolution_path
- [x] 键永在、值可空；多值稳定 key 排序
- [x] code->requirement 索引存在，无 changed-files 时显式空 + known_gap 标记
- [x] verdict=pass 与 verdict=fail 各生成一次（FAIL 报告仍答五问）
- [x] MD 骨架从 JSON 渲染，无 narrative，spec/model 分 section
- [x] 缺 optional artifact（无 decisions/、无 fix run）不 crash；缺 required artifact fail-loud
- [x] final-report 可重算（同 artifact -> 同 JSON）；非冻结
