# 02 — 结构化 audit log 追加器

**What to build:** 一个 append-only 的结构化审计写入组件，按 §4.4 产出 Markdown + JSON 双产物，记录 canonical-state 变更 / gate / decision / run 等事件（§2.1 traceability 骨干）。提供统一的 append 语义，被后续所有 ticket 复用，取代 01 里 inline 的极简写入。

**Blocked by:** 01

**Status:** ready-for-agent

- [x] 提供 append 语义：给定事件类型与结构化负载，追加一条带时间戳与事件类型的事件
- [x] 每次追加同时写入 Markdown（人读）与 JSON（机读）两个产物（§4.4）
- [x] append-only：既有条目不可被篡改或删除
- [x] 01 中 inline 的审计写入改由本组件承担
- [x] 连续追加多条不同类型事件，md 与 json 内容一致且完整
