# 05 - v0.1 端到端集成（create -> prepare -> run -> validate，ark）

**What to build:** v0.1 的 walking-skeleton 证明。在真实 feature run 上把 v0.0 与 v0.1 串起来：`create-feature-run`（v0.0）-> `prepare-run`（02）-> `run`（03，cc-glm52 on ark）-> `validate-run`（04）-> VALIDATE PASS。配置真实 `.ai-dev/agent-profiles.yml`（cc-glm52，ark base_url，以验证过的 prototype profile 为种子）。捕获一次实跑的完整产物链作为证据。集成暴露的接缝摩擦（路径 / ID / 接口对齐）在本票内修复，不遗留。

**Blocked by:** 01, 02, 03, 04

**Status:** done

- [x] `.ai-dev/agent-profiles.yml` 配置 cc-glm52 profile（ark base_url，token 仅变量名）
- [x] 从一条 intent 起步：create -> prepare -> run -> validate 四步依次跑通，无手动干预
- [x] 实跑的 run：exit_code 0、result.json schema 合法、changed_files 全在 allowed-files 内、validate-run 输出 PASS（退出码 0）
- [x] RUN-NNN 正确分配在 feature run 的 `runs/` 下（v0.0 feature 骨架 ↔ v0.1 run 路径集成无误）
- [x] metadata.json 字段齐全，changed_files 与工作区实际改动一致
- [x] token 全程不落盘（在 run 目录内 grep 不到 token 值）
- [x] 集成暴露的接缝问题（路径 / ID / 接口对齐）在本票内修复
