# 多 Agent Profile 协作开发系统 — 正式设计 Spec

**版本**：v0 Walking Skeleton Spec  
**状态**：已确认设计基线  
**目标阶段**：MVP v0  
**核心定位**：基于 Git / 文件系统的轻量编排层，用多 Agent Profile 协作完成 Spec-Driven Development 的可审计开发闭环。

---

## 1. 背景与动机

当前用户已经在使用 speckit 做 SDD 驱动开发，但 speckit 的主要限制是：

1. 流程主要局限在 Claude Code 内部；
2. 从需求、设计、任务拆解、实现、审查到验证，通常由同一个模型贯穿完成；
3. 难以充分利用不同 Coding Agent / Model 的差异化优势；
4. 对单一模型或供应商的依赖较高；
5. 缺少跨 Agent / Model 的统一审计、状态、gate 和决策记录机制。

本系统的目标不是重新开发一个 Coding Agent，而是构建一层**薄编排器**：

> 把 Claude Code CLI、Codex CLI 等现有 Coding Agent 当作黑盒执行器，通过标准化输入、输出、状态、gate 和审计协议，协调多个 Agent Profile 在同一个 SDD 流程中协作。

---

## 2. 业务目标

### 2.1 第一目标：规格一致性与可审计性

系统必须保证：

- 需求、验收标准、设计、任务、实现、审查、验证之间有稳定 traceability；
- 人工确认后的规格 artifact 被冻结；
- Agent 不能擅自改写已冻结规格；
- 所有偏离、冲突、缺口、人工 override 都必须留下结构化记录；
- 最终能回答：
  - 某段代码对应哪个需求？
  - 某个需求是否实现？
  - 某个验收标准由什么验证？
  - 某个 issue 为什么被接受、拒绝或 override？
  - 哪个 Agent Profile 在什么时候做了什么？

### 2.2 第二目标：异构模型提升开发质量

通过不同 Agent / Model 承担不同角色：

- Planner
- Implementer
- Code Reviewer
- Spec Gap Analyst
- Verifier
- Merge Coordinator

利用模型差异形成互补，而不是让一个模型从头到尾自说自话。

### 2.3 第三目标：降低单一供应商依赖

系统应支持多个模型供应商和多个 CLI harness：

- Claude Code CLI + 第三方 Anthropic-compatible API
- Codex CLI
- 未来其他 Coding Agent CLI

通过 Agent Profile 抽象实现：

```text
(cli harness, base_url, backend model, auth_env, invocation, extra_env)
```

### 2.4 长期目标：工程决策数字分身

长期系统应积累用户在 gate、triage、override、review、merge decision 中的偏好，逐渐学习用户的工程判断方式，最终成为用户的软件开发决策代理。

MVP v0 不实现学习能力，但所有决策 artifact 都应为未来学习保留数据基础。

---

## 3. 系统定位

### 3.1 是什么

本系统是：

> 一组 Claude Code skills / slash commands + 确定性运行时脚本 + Agent Profile subprocess adapter + 文件系统状态协议。

它负责：

- 创建和维护 feature run；
- 生成和冻结 SDD artifacts；
- 调用不同 Agent Profile；
- 收集和验证 Agent 输出；
- 维护 canonical status；
- 执行 gate；
- 记录 issue、decision、audit log；
- 协调人工确认与 override。

### 3.2 不是什么

本系统不是：

- 新的 Coding Agent；
- 新的 IDE；
- 新的模型运行时；
- 替代 Claude Code / Codex 的完整开发环境；
- 自动语义合并器；
- 完全自动化无人值守开发平台。

---

## 4. 核心原则

### 4.1 Git / 文件系统是 Source of Truth

所有 canonical 状态、artifact、decision、issue、run result 均落在本地文件系统中，并由 Git 管理。

GitHub Issue、PR、Project Board 等只作为 projection / mirror，不是源状态。

### 4.2 人工确认后 artifact 冻结

一旦人工 gate 确认：

- requirements 冻结；
- design 冻结；
- tasks 冻结；
- lane graph 冻结。

Agent 不允许直接修改冻结 artifact。

任何修改必须通过 Change Proposal。

### 4.3 模型不写 canonical state

这是系统 cardinal rule：

> 模型永远不直接写 canonical state。  
> canonical IDs、status、gate verdict、decision state 只能由确定性脚本写入。

Agent 可以写：

- `result.md`
- `result.json`
- issue proposal
- change proposal
- review comments
- spec gap report

但不能直接改：

- `task-status.yml`
- `lane-status.yml`
- `feature-status.yml`
- final gate verdict
- canonical issue status
- canonical decision status

### 4.4 Markdown + JSON/YAML 双产物

所有重要 artifact 都应有：

- Markdown：给人读；
- JSON/YAML：给机器读。

例如：

```text
implement-result.md
implement-result.json

review-report.md
review-report.json

decision.md
decision.json
```

### 4.5 Role 与 Model 解耦

角色不绑定模型。

不是：

```text
Planner = Codex
Reviewer = Claude
```

而是：

```text
Role → Adapter Policy → Agent Profile
```

同一个角色未来可以切换到不同 profile，也可以多 profile 对比。

---

## 5. 关键概念

### 5.1 Feature Run

Feature Run 是顶层工作单元。

它对应一次从原始需求到最终可审计完成报告的完整流程。

Feature 是最接近原始用户需求的 artifact。

### 5.2 Stable IDs

系统使用稳定 ID 贯穿所有 artifact：

```text
REQ   Requirement
AC    Acceptance Criteria
DES   Design element
TASK  Development task
RUN   Agent profile run
REV   Review finding
GAP   Spec gap
VER   Verification item
ISSUE Issue
DEC   Decision
CP    Change Proposal
LANE  Implementation lane
```

所有 report、issue、decision 都必须引用相关 ID。

### 5.3 Implementation Lane

并行开发的最小单位不是 task，而是 lane。

一个 lane 可以包含多个 task。

Lane 表示：

- 一组可相对独立实现的任务；
- 一组 expected files；
- 一组 exclusive files；
- 一组 verification scope；
- 一组 dependency / provides / consumes 关系。

MVP v0 只使用单 lane，但数据结构从一开始保留 lane 概念。

### 5.4 Agent Profile

Agent Profile 描述某个可调用 Coding Agent 配置：

```text
profile_id
cli harness
backend provider
base_url
model
auth_env
invocation mode
extra_env
permission policy
```

Profile 是编排器调用外部 Agent 的最小配置单元。

---

## 6. 目录结构

Feature run 的建议目录结构：

```text
.ai-dev/
  agent-profiles.yml

  features/
    <feature-id>/
      00-intent.md

      01-requirements.md
      01-requirements.json

      02-design.md
      02-design.json

      03-tasks.md

      04-lane-graph.yml

      status/
        task-status.yml
        lane-status.yml
        feature-status.yml

      lanes/
        LANE-001/
          lane.md
          lane.json

          worktree.json

          implement-result.md
          implement-result.json

          diff.patch
          commits.log

          review/
            review-report.md
            review-report.json

          spec-gap/
            spec-gap-report.md
            spec-gap-report.json

          verification/
            verification-report.md
            verification-report.json

          issue-bundle.md
          issue-bundle.json

          lane-decision.md
          lane-decision.json

          merge-report.md
          merge-report.json

      runs/
        RUN-001/
          input/
            role.md
            system.md
            context/
            task-package.md
            output-schema.json
            allowed-files.txt

          run.sh

          output/
            stdout.log
            stderr.log
            result.md
            result.json
            diff.patch
            commits.log
            metadata.json

      issues/
        ISSUE-001.md
        ISSUE-001.json

      decisions/
        DEC-001.md
        DEC-001.json

      projections/
        github/

      final-report.md
      final-report.json

      audit.log.md
```

---

## 7. Artifact 说明

### 7.1 `00-intent.md`

记录原始用户意图。

内容包括：

- 用户原始需求；
- 背景；
- 业务目标；
- 非目标；
- 约束；
- 初始假设。

### 7.2 `01-requirements.md/json`

记录结构化需求。

应包含：

- REQ IDs；
- AC IDs；
- priority；
- scope；
- constraints；
- open questions；
- frozen state。

### 7.3 `02-design.md/json`

记录设计方案。

应包含：

- DES IDs；
- architecture decision；
- data model；
- API / CLI contract；
- file layout；
- invariants；
- risk；
- dependency；
- mapping to REQ / AC。

### 7.4 `03-tasks.md`

给人读的任务列表。

它可以包含 checkbox，但 checkbox 不是 canonical state。

Canonical task state 只在：

```text
status/task-status.yml
```

### 7.5 `04-lane-graph.yml`

机器可读 lane DAG。

Lane entry 形态：

```yaml
lanes:
  - id: LANE-001
    purpose: "Implement core adapter run contract"
    tasks:
      - TASK-001
      - TASK-002
    depends_on: []
    expected_files:
      - "src/adapter/**"
    exclusive_files:
      - "src/adapter/**"
    provides:
      - "adapter.run_contract"
    consumes: []
    verification_scope:
      - "tests/adapter/**"
    merge_policy:
      auto_merge: true
      allowed_mechanical_resolutions:
        - "format-only"
        - "lockfile-refresh"
      semantic_conflict_policy: "human_triage"
```

MVP v0 只有一个 lane：

```yaml
lanes:
  - id: LANE-001
```

但格式保持未来可扩展。

---

## 8. Canonical Status

### 8.1 `task-status.yml`

只能由 orchestrator 写。

示例：

```yaml
tasks:
  TASK-001:
    status: pending
    lane: LANE-001
    owner_run: null
    proposed_done_by: null
    accepted_done: false
    related_requirements:
      - REQ-001
    related_acceptance_criteria:
      - AC-001
```

Task 状态不是由 markdown checkbox 决定。

### 8.2 `lane-status.yml`

示例：

```yaml
lanes:
  LANE-001:
    status: pending
    current_phase: not_started
    worktree: null
    implement_run: null
    review_run: null
    spec_gap_run: null
    verification_run: null
    gate_verdict: null
```

### 8.3 `feature-status.yml`

示例：

```yaml
feature:
  id: FEATURE-001
  status: planning
  frozen_artifacts:
    requirements: false
    design: false
    tasks: false
    lane_graph: false
  current_gate: requirements_gate
  final_verdict: null
```

---

## 9. Agent Roles

### 9.1 Planner

职责：

- 将 intent 转为 requirements；
- 生成 acceptance criteria；
- 生成 design；
- 拆解 tasks；
- 生成 lane graph；
- 标注依赖关系；
- 标注 expected files / exclusive files。

限制：

- Planner 不能在冻结后直接修改 requirements/design/tasks；
- 修改必须通过 CP。

### 9.2 Implementer

职责：

- 根据 task package 实现代码；
- 只修改 allowed files；
- 生成 implementation result；
- 可标记任务为 `proposed_done`。

限制：

- 不能宣布 final done；
- 不能修改 frozen artifacts；
- 不能修改 canonical status；
- 不能越过 allowed-files boundary。

### 9.3 Code Reviewer

职责：

- 审查代码质量；
- 检查 bug、边界条件、可维护性、安全性；
- 输出 issues。

不负责：

- 判断是否偏离规格。

### 9.4 Spec Gap Analyst

职责：

- 对比 requirements/design/tasks 与实现 diff；
- 检查需求遗漏；
- 检查实现超范围；
- 检查设计偏离；
- 发现需要 CP 的地方。

不负责：

- 普通代码风格审查。

### 9.5 Verifier

职责：

- 执行测试、lint、typecheck、build；
- 收集命令结果；
- 输出 verification report。

Verifier 可以是：

- shell script adapter；
- Coding Agent Profile；
- 后续 CI adapter。

MVP v0 优先 shell adapter。

### 9.6 Merge Coordinator

职责：

- 多 lane 场景下做集成；
- 执行声明式 mechanical merge；
- 分类冲突；
- 生成 merge report。

限制：

- 不做语义冲突解决；
- 不擅自决定接口冲突；
- semantic conflict 必须进入 Human Triage。

MVP v0 暂不启用 Merge Coordinator。

---

## 10. Agent Profile 设计

### 10.1 Profile 文件

`.ai-dev/agent-profiles.yml`

示例：

```yaml
agent_profiles:
  cc-glm52:
    cli: claude
    backend: glm
    base_url: "https://api.z.ai/api/anthropic"
    auth_env: "CC_GLM52_TOKEN"                  # 来源变量名（不变量 #11）
    auth_env_fallback: "ANTHROPIC_AUTH_TOKEN"   # dev 回退（来源未设时）
    auth_target: "ANTHROPIC_AUTH_TOKEN"         # wrapper 注入目标（CC 实际读取）
    model: "glm-5.2"
    invocation: headless
    extra_env:
      ANTHROPIC_MODEL: "glm-5.2"

  cc-minimaxm3:
    cli: claude
    backend: minimax
    base_url: "https://<minimax-anthropic-compatible-endpoint>"
    auth_env: "CC_MINIMAXM3_TOKEN"
    model: "MiniMax-M3"
    invocation: headless
    extra_env:
      ANTHROPIC_MODEL: "MiniMax-M3"

  cc-deepseekv4pro:
    cli: claude
    backend: deepseek
    base_url: "https://<deepseek-anthropic-compatible-endpoint>"
    auth_env: "CC_DEEPSEEKV4PRO_TOKEN"
    model: "deepseek-v4-pro"
    invocation: headless
    extra_env:
      ANTHROPIC_MODEL: "deepseek-v4-pro"

  codex-default:
    cli: codex
    backend: openai
    base_url: null
    auth_env: "OPENAI_API_KEY"
    model: null
    invocation: headless
    extra_env: {}
```

> **认证字段语义**（补充 §10.2 的"只用变量名"规则）：
> - `auth_env`：token 的**来源**变量名（profile 自己的 secret 名）。
> - `auth_env_fallback`（可选）：来源未设置时的回退来源（dev 环境常为 `ANTHROPIC_AUTH_TOKEN`）。
> - `auth_target`：wrapper 实际**注入**的目标变量——即 CLI 真正读取的那个。`cli: claude` 的第三方 Anthropic-compatible 后端读的是 `ANTHROPIC_AUTH_TOKEN`（非 `ANTHROPIC_API_KEY`）。
>
> Wrapper 规则：从 `${auth_env}`（未设则 `${auth_env_fallback}`）取 token，export 为 `${auth_target}`；同时把 profile `base_url` export 为 `ANTHROPIC_BASE_URL`（详见 §10.3）。这样 profile 作者只声明"secret 叫什么、CLI 读什么"，token 永不进配置文件（不变量 #11），且消解了 `CC_GLM52_TOKEN` 与 `ANTHROPIC_AUTH_TOKEN` 的偏差。

### 10.2 Secret 规则

配置中只能写 env var 名称：

```yaml
auth_env: "CC_GLM52_TOKEN"
```

不能写 token 值。

Token 值来自：

- project-level `settings.local.json`
- `.env`
- shell environment
- secrets manager

### 10.3 Env 隔离

Wrapper 必须显式设置 profile env，并**剥离父进程的 Claude Code 身份变量**，避免子 `claude` 继承父会话或错误模型别名。

**注入**（export）：

```bash
ANTHROPIC_BASE_URL    # ← profile.base_url
ANTHROPIC_AUTH_TOKEN  # ← ${auth_env} 或 ${auth_env_fallback}（见 §10.1）
ANTHROPIC_MODEL       # ← profile.model / extra_env
```

**剥离**（unset）：编排器自身若也跑在 Claude Code 内，父 shell 会暴露下列身份/别名变量；子进程继承会导致串会话或回退到非 GLM 别名。注入前必须清掉：

```bash
CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_ENTRYPOINT \
  CLAUDE_CODE_EXECPATH CLAUDECODE AI_AGENT CLAUDE_EFFORT \
  ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL_NAME \
  ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME \
  ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL_NAME \
  ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL_NAME \
  ANTHROPIC_REASONING_MODEL IMG_BASE_URL
```

等价地，profile 可声明剥离正则，wrapper 据此 unset 所有匹配变量（替代或补充上面的显式列表）：

```yaml
env_strip_pattern: "^(CLAUDE_CODE_|CLAUDECODE$|AI_AGENT$|CLAUDE_EFFORT$|ANTHROPIC_DEFAULT_|ANTHROPIC_REASONING_MODEL$|IMG_BASE_URL$)"
```

> 实测（cc-glm52 prototype）：注入前 unset 上述变量、再 export 三个 ANTHROPIC 变量后，子进程 env 快照仅含这三个目标变量，`init` 事件的 `session_id` 为全新会话——证明无父会话继承。

---

## 11. CLI Invocation

### 11.1 Claude Code

MVP 采用 headless 模式：

```bash
claude -p "<prompt>" \
  --output-format stream-json \
  --verbose \
  --include-partial-messages \
  --permission-mode <mode> \
  --max-turns <n>
```

> **`--verbose` 是硬性要求**（claude v2.1.207+）：当 `--output-format stream-json` 与 `-p` 组合时，缺它会在调用模型前直接报错退出（`Error: When using --print, --output-format=stream-json requires --verbose`）。该 flag 在原 spec 中遗漏，已由 cc-glm52 prototype 实跑确认（那次唯一重试就是补它）。

可选能力：

```text
--session-id
--resume
hooks
PreToolUse
PostToolUse
Stop
SubagentStop
```

### 11.2 Codex CLI

MVP 采用：

```bash
codex exec
```

具体参数由 adapter 层封装。

### 11.3 交互式模式

交互式模式暂不作为 MVP 主路径。

原因：

- 难以稳定收集结构化结果；
- 难以确定 run lifecycle；
- 难以统一审计；
- 难以作为自动编排步骤。

未来可作为：

- debugging mode；
- visibility mode；
- `human_takeover` escape hatch。

---

## 12. Agent Run Contract

### 12.1 Run 目录

每次调用 Agent Profile 创建一个 `RUN-ID`：

```text
runs/RUN-001/
  input/
  run.sh
  output/
```

### 12.2 Input Package

```text
input/
  role.md
  system.md
  context/
  task-package.md
  output-schema.json
  allowed-files.txt
```

#### `role.md`

定义本次 role：

```text
You are the Implementer for LANE-001.
```

#### `system.md`

定义全局约束：

- 不修改 frozen artifacts；
- 只写允许文件；
- 必须输出 `result.json`；
- 不写 canonical status；
- 不擅自关闭 issue；
- 不擅自 override gate。

#### `context/`

包含本次需要读取的 requirements、design、tasks、lane graph、prior decisions 等。

#### `task-package.md`

明确本次任务：

- lane id；
- task ids；
- expected outputs；
- verification commands；
- done criteria；
- forbidden behavior。

#### `output-schema.json`

本次 role 的结构化输出 schema。

#### `allowed-files.txt`

允许修改的文件边界。

---

## 13. Output Contract

### 13.1 Agent 写入

Agent 只写语义结果：

```text
output/result.md
output/result.json
```

`result.json` 示例：

```json
{
  "status": "proposed_done",
  "summary": "Implemented adapter run contract.",
  "tasks": [
    {
      "id": "TASK-001",
      "status": "proposed_done",
      "evidence": ["src/adapter/run.py"]
    }
  ],
  "related_requirements": ["REQ-001"],
  "related_acceptance_criteria": ["AC-001"],
  "known_issues": [],
  "change_proposals": []
}
```

### 13.2 Wrapper 写入

Wrapper 计算事实：

```text
output/diff.patch
output/commits.log
output/metadata.json
```

`metadata.json` 示例：

```json
{
  "run_id": "RUN-001",
  "profile": "cc-glm52",
  "cli": "claude",
  "backend": "glm",
  "model": "glm-5.2",
  "started_at": "2026-07-19T10:00:00Z",
  "ended_at": "2026-07-19T10:05:00Z",
  "exit_code": 0,
  "changed_files": [
    "src/adapter/run.py",
    "tests/adapter/test_run.py"
  ],
  "commits": [],
  "checks": [
    {
      "command": "pytest tests/adapter",
      "exit_code": 0
    }
  ]
}
```

### 13.3 为什么不用 native structured output

`result.json` 作为普通文件写入，而不是依赖某个 provider 的 native structured output。

原因：

- 不同 CLI / model provider 支持不一致；
- 第三方 Anthropic-compatible API 不一定完整支持；
- 文件契约最通用；
- wrapper 可以统一校验。

---

## 14. Deterministic Validation

每个 run 完成后，wrapper 必须执行三道验证。

### 14.1 Schema Validation

检查：

- `result.json` 是否存在；
- JSON 是否合法；
- 是否符合 `output-schema.json`；
- 必填字段是否存在；
- status 是否为允许值。

失败：retry once，再失败则 run `failed`。

### 14.2 File Boundary Validation

检查：

- changed files 是否都在 `allowed-files.txt` 内；
- 是否触碰 exclusive files；
- 是否修改 forbidden files。

失败：run `failed`，进入 Human Triage。

### 14.3 Frozen Artifact Validation

检查是否修改：

```text
01-requirements.md/json
02-design.md/json
03-tasks.md
04-lane-graph.yml
```

如果这些 artifact 已冻结，则任何直接修改都失败。

允许方式只有 CP。

---

## 15. Issue Contract

所有检查角色统一输出 `issues[]`。

示例：

```json
{
  "issues": [
    {
      "id": "ISSUE-001",
      "source": "code_review",
      "severity": "P1",
      "title": "Adapter does not handle invalid JSON result.",
      "description": "If result.json is malformed, the wrapper crashes instead of producing failed status.",
      "related_tasks": ["TASK-001"],
      "related_requirements": ["REQ-002"],
      "related_acceptance_criteria": ["AC-003"],
      "evidence": [
        {
          "file": "src/adapter/run.py",
          "line": 42
        }
      ],
      "recommendation": "Catch JSON parse errors and convert them to failed run result.",
      "requires_change_proposal": false
    }
  ]
}
```

### 15.1 Severity

```text
P0: Blocking, non-overridable
P1: Blocking by default, overridable with recorded reason
P2: Non-blocking issue
P3: Suggestion / minor improvement
```

### 15.2 Gate Rule

- P0 存在：gate 必须 fail；
- P1 存在：gate 默认 fail，但人可 override，必须记录 reason；
- P2/P3：不阻塞，但进入 final report。

---

## 16. Human Triage

任何自动 fix 前必须经过 Human Triage。

Triage 可做：

```text
accept_issue
reject_issue
defer_issue
override_issue
request_fix
request_change_proposal
```

P1 override 必须记录：

```json
{
  "decision": "override_issue",
  "issue_id": "ISSUE-001",
  "reason": "Known limitation acceptable for MVP v0 because input package is generated by trusted wrapper.",
  "approved_by": "human",
  "timestamp": "..."
}
```

P0 不允许 override。

---

## 17. Change Proposal

冻结 artifact 的修改只能通过 CP。

CP 示例：

```json
{
  "id": "CP-001",
  "title": "Add explicit timeout behavior to run contract",
  "source_issue": "ISSUE-002",
  "affected_artifacts": [
    "02-design.md",
    "02-design.json"
  ],
  "proposal": "Add timeout handling as a required wrapper behavior.",
  "reason": "Verifier found undefined behavior when Agent Profile subprocess hangs.",
  "status": "pending_human_approval"
}
```

CP 被人工接受后，orchestrator 才能生成新的 frozen artifact 版本。

---

## 18. Gate 模型

### 18.1 Requirements Gate

确认：

- intent 是否被完整表达；
- REQ / AC 是否清晰；
- scope / non-scope 是否明确。

通过后冻结 requirements。

### 18.2 Design Gate

确认：

- design 是否覆盖 REQ / AC；
- 关键技术决策是否明确；
- artifact schema 是否足够；
- non-goals 是否明确。

通过后冻结 design。

### 18.3 Task Gate

确认：

- tasks 是否可执行；
- task 与 REQ / AC / DES 是否可追踪；
- lane graph 是否合理。

通过后冻结 tasks 和 lane graph。

### 18.4 Lane Gate

确认：

- implementation proposed_done；
- review 无阻塞 issue；
- spec-gap 无阻塞 issue；
- verification 通过；
- issue triage 完成。

MVP v0 只有一个 lane，但仍保留 lane gate。

### 18.5 Feature Coherence Gate

多 lane 时检查整体一致性。

MVP v0 单 lane 下退化为最终复查：

- final status 是否一致；
- 所有 P0/P1 是否处理；
- final report 是否完整；
- decisions 是否记录。

---

## 19. Fix Loop

实现阶段最多一轮 bounded fix loop。

流程：

```text
Implement
→ Review / Gap / Verify
→ Issue Bundle
→ Human Triage
→ Fix Run
→ Re-review / Re-gap / Re-verify
→ Final Gate
```

如果 fix 后仍有 blocking issue：

- 不继续无限自动修；
- 进入人工决策；
- 可选择 fail、defer、override P1、创建 CP。

---

## 20. Worktree 设计

长期支持 worktree isolation。

参考已有实践：

```text
/Users/maxy1/Projects/playground/tsh_o1/scripts/shell/bootstrap-worktree.sh
```

设计原则：

### 20.1 Resource Classes

```text
independent
symlink-shared
forbidden-shared
```

### 20.2 Worktree Profile

未来每个 lane 可有 declarative worktree profile：

- 如何创建 worktree；
- 哪些文件复制；
- 哪些 secret symlink；
- 哪些资源禁止共享；
- 如何分配端口；
- 如何 bootstrap。

### 20.3 MVP v0

MVP v0 暂不实现完整 worktree profile 引擎。

可选择：

- 直接在当前 checkout 跑；
- 或使用一个简单 worktree。

但并行 worktree 生命周期留到 v0.2。

---

## 21. Merge Coordinator

长期多 lane 时启用。

### 21.1 职责

- 按 lane graph 顺序集成；
- 执行 mechanical merge；
- 生成 merge report；
- 分类 conflict。

### 21.2 允许自动处理

仅允许声明式 mechanical conflict，例如：

```text
format-only
lockfile-refresh
generated-index-refresh
```

### 21.3 不允许自动处理

```text
semantic conflict
API contract conflict
business logic conflict
design conflict
requirement ambiguity
```

这些必须进入 Human Triage / CP。

### 21.4 MVP v0

单 lane，不启用 Merge Coordinator。

---

## 22. GitHub Projection

GitHub Issue / PR / Project Board 是 projection，不是 source of truth。

本地 artifact 是 canonical。

未来可以把：

```text
issues/ISSUE-xxx.json
decisions/DEC-xxx.json
final-report.json
```

投影到 GitHub。

MVP v0 暂不实现 GitHub projection。

---

## 23. MVP v0 Walking Skeleton

### 23.1 v0 目标

v0 的目标不是完整并行系统，而是证明：

> 从 intent 到 final report 的最小可审计闭环成立。

### 23.2 v0 保留能力

v0 必须保留：

- Git / 文件系统 source of truth；
- Feature Run；
- stable IDs；
- requirements / design / tasks / lane graph；
- human gate；
- frozen artifact；
- CP 机制；
- single lane；
- single profile；
- Implementer；
- Code Reviewer；
- Spec Gap Analyst；
- Verifier；
- unified issues contract；
- severity；
- Human Triage；
- one bounded fix loop；
- final-report；
- audit log；
- deterministic status writer；
- Agent Run Contract；
- schema / boundary / frozen validation。

### 23.3 v0 暂不实现

v0 明确砍掉：

- 多 lane 并行；
- 多 profile fan-out；
- A/B model comparison；
- full worktree profile engine；
- Merge Coordinator；
- GitHub projection；
- interactive takeover；
- complex resume；
- orchestrator crash self-healing；
- long-term preference learning。

### 23.4 v0 推荐 profile

v0 优先使用：

```text
cc-glm52
```

原因：

- 第三方 Claude Code profile 是高不确定路径；
- 如果 `cc-glm52` 可以稳定完成 run contract，则其他 profile 接入是增量；
- 能尽早验证 Anthropic-compatible third-party backend 与 `claude -p` headless adapter 的兼容性。

### 23.5 v0 主流程

```text
1. Create Feature Run
2. Capture intent
3. Generate requirements
4. Human requirements gate
5. Generate design
6. Human design gate
7. Generate tasks
8. Generate single-lane lane graph
9. Human task/lane gate
10. Create RUN for Implementer using cc-glm52
11. Validate implement result
12. Create RUN for Code Reviewer
13. Create RUN for Spec Gap Analyst
14. Run Verifier
15. Normalize issues[]
16. Human Triage
17. If requested, run one bounded fix loop
18. Re-run checks
19. Lane Gate
20. Feature Coherence Gate
21. Write final-report.md/json
22. Append audit.log.md
```

---

## 24. v0 Failure / Resume Model

### 24.1 失败类型

v0 处理以下失败：

```text
profile subprocess exit non-zero
timeout
missing result.json
invalid JSON
schema validation failed
file boundary violation
frozen artifact violation
verification command failed
blocking issue found
```

### 24.2 失败处理

统一原则：

```text
Fail loud, preserve artifacts, require human triage.
```

失败时：

- 不自动静默恢复；
- 不自动跳过；
- 不自动改 canonical status 为 success；
- 保存 stdout/stderr/result/diff/metadata；
- 标记 run failed；
- 进入 Human Triage。

### 24.3 Retry

只有 schema / output format 类失败允许自动 retry 一次。

例如：

```text
missing result.json
malformed result.json
schema violation
```

Retry 仍失败则：

```text
run.status = failed
```

### 24.4 Resume

v0 resume 采用极简方式：

> 从 canonical artifact 和失败步骤重新创建 run。

不做：

- subprocess 断点续传；
- partial tool-use replay；
- worktree 自愈；
- 自动冲突恢复。

---

## 25. Runtime 实现语言

### 25.1 Claude Code Skills / Slash Commands

编排器用户入口倾向于使用 Claude Code skill / slash command。

原因：

- 用户已经在 Claude Code 工作；
- skills 适合承载流程说明和模型驱动交互；
- slash command 适合触发 feature run；
- 能自然嵌入现有开发工作流。

### 25.2 Bash

Bash 用于：

- glue code；
- 调用 CLI；
- 管理环境变量；
- 启动 wrapper；
- 执行简单 shell verification。

### 25.3 Python

Python 用于 data plane：

- schema validation；
- YAML / JSON 读写；
- status 更新；
- issue normalization；
- lane graph 解析；
- artifact generation；
- GitHub projection；
- future PyGithub integration。

建议依赖：

```text
pydantic
pyyaml
jsonschema
pygithub
```

---

## 26. Build Sequencing

### 26.1 v0.0 — Local Artifact Skeleton

实现：

- feature directory generator；
- stable ID generator；
- status file writer；
- requirements/design/tasks/lane graph templates；
- audit log appender。

### 26.2 v0.1 — Single Profile Run Adapter

实现：

- `agent-profiles.yml` loader；
- `RUN-ID` directory creator；
- input package builder；
- Claude Code headless wrapper；
- env injection / cleanup；
- stdout/stderr capture；
- result schema validation；
- metadata computation。

### 26.3 v0.2 — Implement → Review → Gap → Verify Loop

实现：

- Implementer run；
- Code Reviewer run；
- Spec Gap Analyst run；
- shell Verifier；
- issue normalization；
- lane gate evaluator。

### 26.4 v0.3 — Human Triage + Fix Loop

实现：

- issue bundle；
- decision artifacts；
- P0/P1/P2/P3 gate rule；
- one bounded fix loop；
- final report。

### 26.5 v0.4 — Polish and Dogfood

实现：

- better audit log；
- CLI UX；
- example feature；
- dry-run mode；
- error messages；
- test coverage。

---

## 27. Future Roadmap

### 27.1 v0.1+

- Add second profile, e.g. `codex-default`;
- role → profile policy；
- compare profile performance and quality；
- basic GitHub projection。

### 27.2 v0.2+

- multi-lane support；
- worktree profile engine；
- lane dependency DAG execution；
- Merge Coordinator；
- lane-level merge gate。

### 27.3 v0.3+

- multi-profile reviewer panel；
- model voting；
- adversarial review；
- quality scoring；
- profile capability benchmark。

### 27.4 v1+

- preference learning；
- personalized gate recommendation；
- automatic P2/P3 handling；
- user decision digital twin；
- semi-autonomous feature execution。

---

## 28. Non-Goals for MVP v0

MVP v0 不追求：

- 高并发；
- 多模型自动投票；
- 自动语义冲突解决；
- 自动 PR 创建；
- 完整 GitHub integration；
- Web UI；
- 长期记忆学习；
- 复杂权限沙箱；
- 完整 CI/CD integration；
- provider benchmark dashboard。

---

## 29. 核心不变量

系统必须始终满足：

1. Frozen artifact 不能被 Agent 直接修改；
2. Canonical status 只能由 orchestrator deterministic code 写；
3. Agent result 必须经过 schema validation；
4. changed files 必须经过 allowed-files validation；
5. P0 issue 不可 override；
6. P1 override 必须有 reason；
7. Implementer 只能 `proposed_done`，不能 `accepted_done`；
8. Human Triage 发生在任何 automated fix 前；
9. Git / filesystem 是 source of truth；
10. GitHub projection 不能反向覆盖 canonical artifact；
11. Secrets 只引用 env var 名称，不能内联；
12. Model / vendor 不绑定 role；
13. Merge Coordinator 不做语义冲突解决；
14. 所有关键 decision 必须有 Markdown + JSON 双产物。

---

## 30. 一句话定义

> 这是一个以 Git / 文件系统为控制中心、以 SDD artifact 为审计骨架、以 Agent Profile 为执行后端、以 deterministic scripts 维护状态和 gate 的多 Coding Agent 协作编排层；MVP v0 先用单 lane、单 profile 跑通从 intent 到 final-report 的完整可审计闭环。
