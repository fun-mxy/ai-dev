# 07 - v0.4 dogfood 端到端（examples/string-utils 上真实 Ark run → PASS final-report）

**What to build:** v0.4 的**出口证据**（exit criterion C：六项 closed + 一次 dogfood run 作集成证据）。在 05 的 `examples/string-utils/` 上，用 polished 后的 `ai-dev` 真实跑一条 happy-path feature：intent（"加一个 `slugify(s)` 函数，带边界测试"）→ freeze → implement → review → spec-gap → verify → collect-issues → [Human Triage] → lane-gate → coherence-gate → final-report，到达 **verdict=pass** 的 final-report，全程在真实 cc-glm52/Ark 上（非 fake-claude pytest）。**先 dry-run（04）预演全链**验证 examples 目标接线，再真跑省 token。过程中用 03 的只读命令（`list-features`/`show-status`/`log`）观察 feature 逐 gate 推进——这同时验证 03。错误路径用 01 的干净 `error:`。捕获证据到 `.scratch/ai-dev-v0-4-polish-dogfood/evidence/07-dogfood-real-run.md`：命令序列、各 gate verdict、final-report.{json,md} 摘要、token 不落盘 grep、`log` 输出样例。**happy-path PASS 是出口硬要求**；failure-path run（故意触发 P1 → triage → fix-run）为 stretch，不阻塞出口（与 Q3 决定一致）。ID 跨 v0.0-v0.4 衔接无重号。集成接缝摩擦票内修复。

**Blocked by:** 01, 02, 03, 04, 05, 06 — capstone，所有 polish 项 + 目标就绪后

**Status:** done

- [x] `examples/string-utils/` 内 `ai-dev create-feature-run "加 slugify(s) 函数带边界测试"` 起 feature
- [x] freeze → implement → review → spec-gap → verify → collect-issues → [triage] → lane-gate → coherence-gate → final-report 依次跑通，到达 verdict=pass
- [x] 真跑前先用 `--dry-run`（04）预演全链验证接线
- [x] 过程中用 `list-features`/`show-status`/`log`（03）观察并验证只读命令
- [x] 捕获证据到 `.scratch/ai-dev-v0-4-polish-dogfood/evidence/07-dogfood-real-run.md`
- [x] token 全程不落盘（artifact + run 目录 grep 不到 token 值）
- [x] final-report.{json,md} 含 verdict=pass + 五个 §2.1 audit 问题答案
- [x] ID 跨 v0.0-v0.4 衔接无重号/错位
- [ ] （stretch）failure-path run：故意 P1 → triage → fix-run → re-coherence，证据可选
- [x] 集成接缝问题票内修复不遗留
