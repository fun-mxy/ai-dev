# 03 - issue `status` 字段 + 状态机（ADR-0002 D2）

**What to build:** 给 `ISSUE-NNN.json` 加 `status` 字段，值域 `raised | triaged | resolved | reappeared`，附转移规则与 helper。转移表：collector 首次发现 -> `raised`；apply_triage 写 disposition -> `triaged`；collector diff 发现 prior bundle 有、new bundle 无 -> `resolved`；collector diff 发现 fix 后重新出现 -> `reappeared`。gate **不读** status（读 severity + triage）--status 是 collector / fix-run 驱动 / apply_triage 的簿记。本票引入字段 + 转移 helper + 状态机单测；各 writer（02 collector、05 apply_triage、07 fix-run）在各自票里调用 helper。

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] `ISSUE-NNN.json` 含 `status` 字段，初值 `raised`
- [ ] 转移 helper 强制合法转移（非法转移 fail-loud）
- [ ] 状态机单测覆盖全部合法转移 + 拒绝非法转移
- [ ] gate（lane-gate）不读 status（读 severity + triage）--回归测试确认
- [ ] collector 写 raised/reappeared/resolved（02 的 merge 路径调用 helper）
