# 08 - v0.5 dogfood capstone (multi-profile + comparison + projection)

**What to build:** The v0.5 milestone capstone evidence. Run the **same intent** through BOTH
`cc-glm52` AND `codex-default` (two parallel feature-runs, full pipeline each, both reaching a
verdict). Then `compare-profiles` across the two (quality axis = requirement coverage, populated via
ticket 05 / ADR-0007). Then `project-github` the issues + `final-report` to a real GitHub issue/PR
(real `gh` push, `--pr` to a human-created PR). Capture evidence at
`.scratch/ai-dev-v0-5-second-profile/evidence/08-capstone-real-run.md`: both runs' verdicts, the
`profile-comparison` artifact, the GH `mapping.json` + pushed issue/PR-comment URLs, token-safety grep.
**Per the real-backend evidence discipline (E2E tickets need a real backend run, not just the
fake-claude test), this capstone stands as the genuine proof** that all four §27.1 items + Q2/Q3 work
end-to-end. Update `README.md` status with a v0.5 section.

**Blocked by:** 04, 05, 06, 07.

**Status:** pending

- [ ] same intent through `cc-glm52` AND `codex-default` -> two feature-runs, both verdict'd
- [ ] `compare-profiles` across the two (requirement-coverage quality axis populated)
- [ ] `project-github` to a real GH issue + PR comment (`--pr` to a human-created PR)
- [ ] `evidence/08-capstone-real-run.md`: both verdicts, comparison artifact, GH mapping + URLs, token grep
- [ ] real backend (codex/OpenAI) + real GitHub push - not just fake-claude / mocked-`gh` tests
- [ ] `README.md` v0.5 status section added
- [ ] milestone tickets 01-07 all done
