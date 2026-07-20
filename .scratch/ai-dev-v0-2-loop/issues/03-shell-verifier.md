# 03 - Shell Verifier（verification-report，§9.5 shell adapter）

**What to build:** 给定 implement run 的工作区改动，按声明的 verify 命令集（pytest/mypy/build，来源：feature 的 verifier 配置或 tasks 声明）逐条执行、捕获每条 pass/fail + stdout/stderr 摘要，产出 lane 级 `verification-report.{md,json}`。新的**非 agent** run kind（deterministic shell，不走 `claude -p`）。§9.5 MVP 优先 shell adapter。verifier 输出 verification report（pass/fail），不输出 `issues[]`（§9.5 vs §15：检查角色 = reviewer + gap）；verification pass/fail 是 §18.4 gate 的独立条件。

**Blocked by:** 01

**Status:** done

- [x] 按 declared verify 命令集（pytest/mypy/build）逐条执行，捕获每条 exit_code + stdout/stderr 摘要
- [x] 产出 lane 级 `verification-report.{md,json}`（md+json 双产物），含每条命令 pass/fail + 整体 verdict
- [x] 是 deterministic shell adapter（不走 `claude -p` / 不调模型，§9.5 MVP）
- [x] verifier 输出 report 不输出 `issues[]`；pass/fail 留给 gate 消费
- [x] 命令失败/缺失 fail loud（§24.2），退出码非 0
- [x] 单测：多命令 pass/fail 混合、report 字段、fail-loud
