# 06 — (OPTIONAL / deferrable) Direct-edit refinement: deterministic `render` + `allocate-id` helper (ADR-0008)

**What to build:** The **optional** second refinement channel from ADR-0008 D4 — defer this ticket
unless you want to bypass a model round-trip for surgical fixes. Alongside the model-mediated
feedback loop (the primary path, tickets 02-04), allow a human to **directly edit the canonical
**unfrozen** `.json`** of a planning artifact (e.g. fix a typo in `01-requirements.json`, reword a
DES). Two deterministic helpers support it: (1) **`render`** — re-renders the `.md` mirror from the
edited `.json` so the single-source-of-truth invariant holds; (2) **`allocate-id`** — allocates the
next id from the counter for any *new* item the human adds, so ids stay in the counter and out of the
human's hands (§4.3 reserves ids for scripts). Allowed because §4.3 reserves only
ids/status/gate-verdict for deterministic scripts — unfrozen **content** is editable; the artifact
must still be **unfrozen** (frozen ⇒ Change Proposal, which is out of scope for v0.6 per ADR-0008 D4).
On accept→freeze, the existing reference-integrity + coverage checks still run.

**Blocked by:** 04 (the full planning flow should exist first; this is an add-on channel).

**Status:** done

- [x] human can edit an **unfrozen** canonical `.json`; `render` re-renders the `.md` mirror
- [x] `allocate-id` helper assigns the next counter id for human-added items
- [x] edits rejected on **frozen** artifacts (frozen ⇒ CP, out of scope for v0.6)
- [x] reference-integrity + coverage checks still run at the subsequent freeze
- [x] `uv run mypy` + `uv run pytest` green
