# 04 - `--dry-run` 模式（副作用命令，temp-dir，不 mint stable id）（ADR-0004）

**What to build:** v0.4 polish 第四项（§26.5 dry-run mode），v0.4 唯一的**全新能力**。锁定方案 **(B)**：给**副作用命令**加 `--dry-run` flag，执行命令的全部 *planning + §24.2 precondition + legality check*，但跳过"昂贵/不可逆步"——agent 命令（run-headless/implement/review/spec-gap/fix-run）跳过 claude 子进程；deterministic 命令（freeze/triage/coherence-gate/final-report/lane-gate）跳过 canonical state 写入，但**仍跑全部合法性检查**（所以 dry-run triage 能校验一个 disposition 合法性却不落记录）。跳过点之前算出的东西（input package、resolved profile、exact claude invocation + allowed-files boundary）打印成 plan，exit 0，**不写任何 canonical state、不 spawn 子进程**。**关键不变量（glossary pin `dry-run`）：dry-run 绝不 mint stable id**——不走 `prepare_run` 的真实 path（那会消耗 RUN-NNN 单调计数器、写 `runs/RUN-NNN/input/`），而是把 would-be package 渲染到 **temp dir**、打印摘要、mint 0 个 id、feature-run 树零改动。已纯/廉价命令（show-profile、validate-run、03 的只读命令）不加 flag（noise）。本票产 **ADR-0004**：为什么 dry-run 不复用 prepare 而走 temp-dir（trade-off：allocate-and-skip 更省工但烧 id+留孤儿目录，违反 monotonic allocation 与"dry"语义）。

**Blocked by:** 无 — 可立即开始（与 02/03 并行）

**Status:** done

- [x] `--dry-run` flag 接入副作用命令：agent 命令（run-headless/implement/review/spec-gap/fix-run）+ deterministic 命令（freeze/triage/coherence-gate/final-report/lane-gate）
- [x] agent 命令 dry-run：prepare input package 到 **temp dir**（非真实 runs/）、解析 profile、算 exact claude invocation + allowed-files boundary、跑 §24.2 precondition、打印 plan、exit 0、**不 spawn claude**、**不 mint RUN-NNN**、feature 树零改动
- [x] deterministic 命令 dry-run：跑全部 legality + precondition check、打印"将写入 X"、exit 0、**不写 canonical state**
- [x] **dry-run 不 mint stable id**：dry-run 前后 stable-id 计数器不变（单测断言）；`docs/glossary.md` pin `dry-run`
- [x] dry-run 事件审计 `origin=dry-run`（沿用 02）
- [x] ADR-0004 落 `docs/adr/0004-dry-run-mode.md`（trade-off + temp-dir 决策 + 跳过点表）
- [x] 单测：dry-run 打印 plan、不 spawn、不写状态、计数器不变
- [x] mypy + 全测试绿

Delivered by 74b1c9f (merged via 56a5af6). `--dry-run` planner lives at `src/ai_dev/dry_run.py`, wired through `cli.py` onto the side-effect agent + deterministic commands. The would-be input package renders to a temp dir (never `runs/RUN-NNN`), so `RUN`/`DEC` counters and the feature-run tree are untouched — the counter-invariance unit test asserts this. ADR-0004 (`docs/adr/0004-dry-run-mode.md`) records the temp-dir trade-off and the skip-point table; `docs/glossary.md` pins `dry-run`. Per ADR-0004 D4, the `origin=dry-run` audit *emission* remains deferred (dry-run still writes nothing); `origin` itself landed in ticket 02. Verified: `uv run pytest` 648 passed, `uv run mypy src/ai_dev` clean.
