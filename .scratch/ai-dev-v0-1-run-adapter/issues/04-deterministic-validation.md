# 04 - 确定性三道验证（schema + boundary + frozen seam）

**What to build:** §14 三道验证。§14.1 schema（result.json 存在/合法/符合 output-schema.json/必填字段/status enum；失败 retry once 再失败则 failed）。§14.2 file-boundary（changed_files 全在 allowed-files.txt 内；CC harness 态 out-of-band 不计）。§14.3 frozen seam（v0.1 MVP 的 allowed-files 不含 frozen artifact，校验路径存在但不误报）。提供 `ai-dev validate-run <FEATURE> <RUN-ID>` -> VALIDATE PASS/FAIL，退出码 0/1。prototype 的 `validate.py` 为种子。

**Blocked by:** 02, 03

**Status:** ready-for-agent

- [x] §14.1 schema 全检；失败 retry once 再失败标 failed
- [x] §14.2 changed_files 全在 allowed-files.txt 内，越界即 FAIL；out-of-band 不计
- [x] §14.3 frozen seam：触碰已冻结 artifact 即 FAIL（v0.1 不触发，但不误报）
- [x] validate-run 退出码 0=PASS / 1=FAIL，输出可读
- [x] 单测：合法 PASS、schema 破坏 FAIL、越界 FAIL、retry once 语义
