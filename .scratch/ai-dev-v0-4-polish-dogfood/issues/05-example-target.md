# 05 - 示例目标仓库 `examples/string-utils/`

**What to build:** v0.4 polish 第五项（§26.5 example feature），锁定方案 **(B)** 的目标侧：一个**提交进仓库**的微型 Python 目标项目，既作"example feature"交付物，又作 07 dogfood run 的运行靶子。位置 `examples/string-utils/`。形态：一个极小 Python 包（一两个模块 + pytest 测试套），让 verifier 能直接跑 `pytest`/`mypy` 通过——verify leg 零新工具链。预置一个已存在的函数 + 测试（如 `snake_case(s)`），dogfood intent（07）再加一个新函数（如 `slugify(s)`，带边界测试：空串/unicode/首尾连字符）。目标项目自带 `pyproject.toml`（或最小可跑配置），使 `ai-dev` 在其仓库内 `create-feature-run` 后，implement→verify 全链可在 Ark 上真实跑通。**本票只交付目标项目本体**（源码 + 测试 + 可运行配置 + 一份 README 说明如何作为 dogfood 靶子用）；真实 dogfood run 在 07。目标项目的 `.ai-dev/` 是 throwaway 运行态、gitignore（与本仓库同构原则）。

**Blocked by:** 无 — 可立即开始（纯内容，与 01-04 并行）

**Status:** pending

- [ ] `examples/string-utils/` 微型 Python 包：一两个模块 + 预置函数（如 `snake_case`）+ pytest 测试
- [ ] 目标项目可独立 `pytest`/`mypy` 通过（verify leg 命令可跑）
- [ ] 目标项目最小可运行配置（`pyproject.toml` 或等价），使 `ai-dev` 能在其内 `create-feature-run`
- [ ] 目标项目 README：说明它是 ai-dev dogfood 靶子、如何起一个 feature run、验证命令是什么
- [ ] 目标项目的 `.ai-dev/` gitignore（throwaway 运行态）
- [ ] 手动验证：在 `examples/string-utils/` 内 `ai-dev create-feature-run` + freeze + prepare 可起（不要求真跑 claude，那是 07）
