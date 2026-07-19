# 01 - agent-profiles.yml 加载器 + profile 解析

**What to build:** 加载 `.ai-dev/agent-profiles.yml`，按名解析单个 profile（如 cc-glm52）为已解析 profile（cli / base_url / model / auth_env / auth_env_fallback / auth_target / extra_env / env_strip_pattern）。token 按 §10.2 只用变量名：从 `${auth_env}`（未设则 `${auth_env_fallback}`）取值、注入目标 `${auth_target}`；值永不进配置、永不打印。提供 `ai-dev show-profile <name>` 打印已解析 profile（token 脱敏，仅显示来源变量名 + 是否已设置）。

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [ ] 解析指定 profile，含 §10.1 全部字段
- [ ] token 仅按名解析（auth_env -> fallback），配置无 token 值（不变量 #11）
- [ ] `show-profile` 输出 token 值脱敏；任何输出路径不泄露 token
- [ ] 缺 profile / 缺 token 来源 -> fail loud（§24.2），退出码非 0
- [ ] 单测：合法解析、fallback 触发、token 不泄露
