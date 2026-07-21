# 07 - `fix_loop_budget` + `ai-dev fix-run`（有界循环 + reappear 重 triage）（ADR-0002 D3-D9）

**What to build:** v0.3 的 fix loop 驱动。feature 级 `fix_loop_budget: {used, max: 1}` 作为 §19 "至多一轮 fix" 的唯一 enforcer--`apply_triage` 的 `request_fix` 在 `used >= max` 时拒绝。`ai-dev fix-run` 驱动命令：跑一次 fix implement run。budget 时点：仅在 §14-validated implement-result 时自增（launch 不计、re-collect-done 不计、issue 解决不计）；crash/boundary/frozen 违规不消耗（failed run）；§24.3 schema auto-retry 是一次 run 内部、不消耗；脏 worktree（boundary/frozen）须人工 revert（§24.4）。`request_fix` reappear-after-own-fix：fix-run 后 re-collect 发现 issue 重新出现 -> `status=reappeared` -> triage 失效 -> 强制 re-triage（amend §19 flow，原 §19 未画此步）。CP 不消耗 budget（per implement-cycle；v0.3 因 clean deferral 无 open-CP 可 cap）。

**Blocked by:** 05

**Status:** done

- [x] `fix_loop_budget: {used, max: 1}` 写入 feature-status，初 used=0
- [x] apply_triage `request_fix` 在 used>=max 时 fail-loud 拒绝
- [x] `ai-dev fix-run` 驱动一次 fix implement run
- [x] budget 仅在 §14-validated implement-result 自增（boundary/frozen/crash 不消耗）
- [x] §24.3 schema auto-retry 不消耗 budget（run 内部）
- [x] request_fix reappear-after-own-fix -> status=reappeared -> triage 失效 -> 强制 re-triage
- [x] §19 flow amend：re-triage 步骤显式画出
- [x] 脏 worktree（boundary/frozen）提示人工 revert（§24.4），不自动重 launch
- [x] 单测：budget 0->1、超限拒绝、crash 不消耗、reappear 触发 re-triage
