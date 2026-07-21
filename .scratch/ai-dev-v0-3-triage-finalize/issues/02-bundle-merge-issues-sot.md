# 02 - bundle-merge：`issues/` 为 SoT，bundle 为投影（ADR-0002 D1）

**What to build:** 把 `issues/ISSUE-NNN.json` 立为 issue 的 source of truth，`issue-bundle.json` 退为它的投影。当前 collector（`issue_bundle.py` 的 `_normalize_issues`）每次 re-collect 都从 report 重建 issue dict 并覆写 `ISSUE-NNN.json`，抹掉已写的 triage/status--这是 happy-path bridge bug（即便没有 fix loop，re-collect 也会丢 triage）。改为 merge：保留 `ISSUE-NNN.json` 上已有的 `triage` / `status` 等非 report 派生字段，只更新 report 派生字段（severity/title/evidence/location 等）。bundle 从 `issues/` 投影生成。指纹匹配复用已有 `_existing_issue_ids_by_fingerprint`。本票只修 merge 语义 + 投影方向，不引入 triage/status 字段本身（status 在 03，triage 在 05）。

**Blocked by:** 无 - 可立即开始

**Status:** ready-for-agent

- [ ] re-collect 后，已写在 ISSUE-NNN.json 上的 `triage`/`status` 字段保留不丢（merge 而非覆写）
- [ ] report 派生字段（severity/title/evidence/location 等）按新 report 更新
- [ ] `issue-bundle.json` 从 `issues/` 投影生成（投影，非独立 SoT）
- [ ] 指纹复用：同一 issue 跨 re-collect 保持 ISSUE-NNN id
- [ ] 单测：首次 collect、re-collect 保留 triage、re-collect 更新 report 字段、指纹复用 id
- [ ] 缺前置 artifact fail-loud（§24.2）
