# 03 - Claude Code headless wrapper（env 隔离 + 调用 + 捕获 + metadata）

**What to build:** 给定已 prepare 的 run 目录 + 已解析 profile，构建自包含 prompt；按 §10.3 注入/剥离 env；按 §11.1 headless 调用 `claude -p`；捕获 stdout/stderr 落盘；按 §13.2 写 metadata.json。changed_files 由 before/after 快照 diff 计算、扣除 wrapper 自有产物；§14.2 卫生用 `--settings` 关闭 auto-memory（`--bare` 不用）。prototype 的 `run.sh` 为种子。

**Prototype 决策（内联）**：flags = `--output-format stream-json --verbose --include-partial-messages --permission-mode bypassPermissions --max-turns <n>`；`--verbose` 硬性要求（claude v2.1.207+）。env-strip 列表见 §10.3。不 pin 流式 model id（z.ai 报 `gpt-5.5`、ark 报 `glm-5.2`，跨后端会变）--用 profile 声明的 model；stream 解析容忍 `thinking_delta`。

**Blocked by:** 01, 02

**Status:** ready-for-agent

- [x] §10.3：注入 ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL，剥离父 CC 身份/别名变量；子进程 env 快照仅含目标变量（值脱敏）
- [x] §11.1：headless 调用含全部硬性 flag（含 `--verbose`）
- [x] §13.1：产出 output/result.{md,json}（agent 写）+ output/{stdout.log,stderr.log,metadata.json}（wrapper 写）
- [x] §13.2：metadata.json 字段齐全；changed_files 仅含 repo 工作区文件（扣除 wrapper 自有产物；CC harness 态 out-of-band 不计，§14.2）
- [x] §14.2 卫生：`--settings` 关闭 auto-memory
- [x] 实跑一次 cc-glm52（ark）run，exit_code 与 result.json 一致；token 不落盘
- [x] 单测：env 隔离、changed_files 计算、metadata 字段
