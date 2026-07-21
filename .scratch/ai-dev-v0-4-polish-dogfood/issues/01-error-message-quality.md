# 01 - 错误消息质量 + cli.main 顶层 except + `--debug`

**What to build:** v0.4 polish 第一项（§26.5 error messages），锁定方案 **(A)**：**不**改 exit code 语义（保持 0=成功/PASS、1=其它一切），把投入放在消息可操作性上。现状：每条命令都是 `print(f"error: {exc}", file=sys.stderr); return 1`，只 catch `ValueError`/`ProfileError`/`FrozenArtifactError`——任何其它异常裸抛 traceback；所有失败都 exit 1（precondition / validation-FAIL / usage 混在一起）；消息只陈述问题、不给恢复路径。三件事：(1) 在 `cli.main` 加顶层 `except`，把未捕获异常渲染成一行干净 `error:` 而非 stack dump；新增全局 `--debug` flag opt-in traceback（默认不吐栈）。(2) 把各 `_run_*` 的 `error:` 文案改成可操作：点名出错的 arg/artifact、给出"下一步"或"did you mean"提示（如 feature 不存在时提示现有 FEATURE-NNN；triage 非法 cell 时提示合法 disposition×severity）。(3) 统一一个错误渲染 helper（`_render_error`），让所有命令的 `error:` 行同构。本票是后续票的地基：02-07 都受益于干净错误。exit code 不变是硬约束——任何脚本化消费者不受影响。**不**引入 exit-code band（那是更难逆转的契约变更，留作后续）。

**Blocked by:** 无 — 可立即开始

**Status:** done

- [x] `cli.main` 加顶层 `except Exception`：渲染干净 `error:` 行（非 traceback），exit 1
- [x] 全局 `--debug` flag：设置后未捕获异常照常抛 traceback 供调试
- [x] 新增 `_render_error(exc, *, hint=None)` helper，所有 `_run_*` 复用，`error:` 文案同构
- [x] 关键命令文案可操作化：feature/run 不存在时提示现有候选；triage 非法 cell / 缺 reason 时点名合法值；profile token 未设时点名 source 变量名
- [x] exit code 保持 0/1（grep 确认无新增 return 2/3 等）
- [x] 单测：顶层 except 把未知异常转成 `error:` + exit 1；`--debug` 时抛原异常
- [x] 单测：可操作 hint 在典型错误路径上出现
- [x] mypy + 全测试绿
