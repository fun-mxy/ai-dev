# 06 - 测试覆盖率：`pytest-cov` 接入 + README baseline + 补 surfaced 缺口

**What to build:** v0.4 polish 第六项（§26.5 test coverage），锁定方案 **(B)**：**测量 + 软目标**，**不**上 hard gate。现状仓库零覆盖率工具（pyproject 无 coverage/pytest-cov）。三件事：(1) 接入 `pytest-cov`（`uv add --group dev pytest-cov`，pyproject 配置 `--cov=ai_dev` + 分支覆盖 + 不含 tests/ 本身），`uv run pytest` 默认或 opt-in 出报告。(2) 跑一次得到**committed baseline 数字**，写进 README（与 v0.x status 段并列），作为本版本"test coverage"交付物的可指认数字。(3) 把 01-04 + dry-run/dogfood 新代码 **surfac**e 出的明显未测 seam 补测试（dry-run 路径、01 的新 error 分支、03 只读命令、02 新字段）——真补缺口，非凑数。**不**设硬阈值 gate（避免为打 gauge 写低价值测试；覆盖率数字 honest 即可）。文档一个软目标（如"维持/提升 baseline"）但不 fail build。

**Blocked by:** 01, 02, 03, 04 — baseline 须反映新代码，故靠后

**Status:** done

- [x] `uv add --group dev pytest-cov`；pyproject 配 `--cov=ai_dev` + 分支覆盖；更新 `uv.lock`
- [x] `uv run pytest` 出覆盖率报告（terminal + 可选 HTML）
- [x] README 写 committed baseline 数字（v0.4 status 段）
- [x] 补 01-04 surfaced 缺口测试（dry-run 路径、新 error 分支、只读命令、新审计字段）
- [x] 无 hard gate（确认 pyproject/CI 无 fail-on-coverage 阈值）
- [x] 文档软目标（README 一行）
- [x] mypy + 全测试绿；覆盖率数字 honest

Delivered baseline: **91%** (683 tests, line + branch). `pytest-cov` wired into the dev group;
`pyproject.toml`'s `[tool.pytest.ini_options]` adds `--cov=ai_dev --cov-branch --cov-report=term-missing`
by default (opt into HTML with `--cov-report=html`). No `--cov-fail-under` anywhere — coverage is a
soft target documented in README, not a build gate.

Gap tests filled (real seams, not gauge-padding):
- **dry-run (ticket 04):** the review + spec-gap checking-leg happy path through `_plan_checking`
  (previously untested) — which surfaced and fixed a real bug: `plan_spec_gap` called
  `_spec_gap_task_text(facts)` but that builder takes `(feature_root, facts)`, so every spec-gap
  dry-run crashed. Also added the disarming-without-reason + request_fix-budget-exhausted refusal
  branches, the token-source-not-set precondition, unknown disposition/severity, and parametrized
  feature-not-found guards across the planners.
- **error branches (ticket 01):** the shared `except ProfileError` guard in `implement` / `review` /
  `spec-gap` / `fix-run` (clean `error:` + exit 1 on a missing registry), parametrized across all four.
- Read-only commands (ticket 03) + audit fields (ticket 02) were already well covered; no padding added.

Verified: `uv run pytest` 683 passed, `uv run mypy src/ai_dev` clean.
