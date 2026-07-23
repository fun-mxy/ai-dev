"""The deterministic ``promote`` primitive — v0.6 spine (ADR-0008 D1/D2/D3).

``promote`` is the planning-leg analogue of the implement leg's result rollup:
it is the *sole* deterministic writer that turns a Planner run's id-free
structured-JSON **proposal** (living in the run's ``output/``) into a canonical
unfrozen artifact (``01-requirements.json``). ADR-0008 D2 gives it three jobs:

1. **allocate** canonical stable IDs from the existing per-type counter
   (``feature_ids.allocate_id``) — the model never assigns ids;
2. **stitch** cross-references by resolving the proposal's *local* refs against
   real allocated ids — via the generic :class:`RefResolver`;
3. **write** the canonical ``.json`` **and render** the ``.md`` mirror — promote
   is the *sole* md renderer (single source of truth: markdown is always a
   rendered mirror of canonical JSON).

Plus the reference-integrity check (D3): every local ref must resolve to a real
allocated id; an unresolvable ref is a malformed proposal, so promote **fails
loud** (§24.2) rather than silently dropping or guessing. (Coverage
*completeness* — every upstream item referenced at least once — is a separate
freeze-gate precheck, not promote's job: a proposal is expected to be incomplete
while being refined.)

This ticket wires the **requirements** stage (the root — no upstream artifacts).
The :class:`RefResolver` and the write/render spine are built generically so
design (ticket 03) and tasks (ticket 04) extend them with no rework: they add an
upstream id-set (frozen REQ/DES ids) and reuse the same resolve + render path.

Scope: ``promote_requirements`` takes the proposal as a parsed mapping (pure at
the seam) and the feature-run root (for id allocation + the frozen guard). It is
unit-tested with synthetic proposals; **no real model run** is involved here —
ticket 02's ``generate-requirements`` reads the run's ``result.json`` and calls
this.

The **design** stage (ticket 03) is wired here too: ``promote_design`` is the
first stage with a real frozen upstream - its ``requirement_mapping`` REQ refs
resolve against the frozen ``01-requirements.json`` via ``add_upstream`` (the
root never calls it), and its ``design_elements`` local refs resolve via
``register_local`` as DES ids are allocated. ``generate-design`` (ticket 03's
leg) reads the run's ``result.json`` and calls ``promote_design``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.feature_ids import allocate_id
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.status import frozen_artifacts_status
from ai_dev.templates import (
    DESIGN_JSON,
    DESIGN_MD,
    REQUIREMENTS_JSON,
    REQUIREMENTS_MD,
)

# The audit event for a promote (the planning-leg analogue of ``implement-result``
# / ``mark_task_proposed_done``). One record per promote, carrying the stage, the
# artifact, and the ids it allocated — so the canonical write is traceable.
_PROMOTE_EVENT = "promote"

# The §4.2 artifact name this stage writes (mirrors ``FROZEN_ARTIFACTS``). Public
# so ticket 02 can reference the artifact promote targets from one source.
REQUIREMENTS_ARTIFACT = "requirements"

# The §4.2 artifact name the design stage writes (ticket 03). Public so the
# design leg / coverage precheck reference it from one source.
DESIGN_ARTIFACT = "design"


class UnresolvedRefError(ValueError):
    """A local ref in the proposal does not resolve to a real allocated id.

    ADR-0008 D3 (reference-integrity, promote-time): every local ref must resolve
    to a real allocated upstream id. An unresolvable ref means the proposal is
    malformed (it points at something the model never defined) -> promote fails
    loud (§24.2) -> Human Triage. Subclasses ``ValueError`` so callers may catch
    either; ``promote_requirements`` lets it propagate.
    """


class FrozenArtifactWriteError(ValueError):
    """promote attempted to overwrite an already-frozen artifact (§4.2).

    A frozen artifact is immutable (§4.2); the only sanctioned change path is a
    Change Proposal (§17), not a second promote. promote targets the
    *canonical-unfrozen* state (D1) — if the artifact is already frozen, refuse
    rather than silently overwriting frozen content.
    """


# The optional §7.2 prose facets a requirements proposal may carry alongside its
# requirements/ACs. Listed once and shared by the build core (carry-through) and
# the renderer, so adding a facet is a one-place edit rather than Shotgun Surgery
# across the module. ``priority``/``scope`` are scalars (rendered inline);
# ``constraints``/``open_questions`` are lists (rendered as bullets) — the
# renderer splits on that, not on this constant's order.
_SCALAR_FACETS: tuple[str, ...] = ("priority", "scope")
_LIST_FACETS: tuple[str, ...] = ("constraints", "open_questions")
_PROSE_FACETS: tuple[str, ...] = _SCALAR_FACETS + _LIST_FACETS
# Markdown heading for each prose facet (renderer mirrors the canonical JSON).
_FACET_HEADINGS: Mapping[str, str] = {
    "priority": "Priority",
    "scope": "Scope",
    "constraints": "Constraints",
    "open_questions": "Open questions",
}


# ---------------------------------------------------------------------------
# Generic local-ref resolver (reused unchanged by design/tasks promote).
# ---------------------------------------------------------------------------


@dataclass
class RefResolver:
    """Resolve a proposal's local refs to canonical stable ids (ADR-0008 D2/D3).

    A ref is one of two things, and the resolver checks both in order:

    * a **local key** — a handle *within this proposal* (e.g. an AC's
      ``requirement: "r1"`` pointing at the requirement whose ``key`` is
      ``"r1"``). Registered via :meth:`register_local` as ids are allocated, so
      same-proposal refs (AC -> REQ in requirements) resolve as promote walks the
      proposal;
    * a **canonical id** from a *frozen upstream* artifact (e.g. a design
      element's ``requirement: "REQ-001"`` pointing at a requirement already
      frozen in ``01-requirements.json``). Registered via :meth:`add_upstream`
      from the frozen id-set, so cross-stage refs resolve without re-allocation.

    :meth:`resolve` returns the canonical id for a ref, checking the local map
    first then the upstream set; an id present in neither is unresolvable and
    raises :class:`UnresolvedRefError` (D3 reference-integrity, fail loud).

    Built generically so the design (ticket 03) and tasks (ticket 04) stages
    reuse it with no rework: requirements uses only ``register_local`` (it is the
    root); design/tasks add an ``add_upstream`` id-set and keep the same resolve
    path. Per-type namespaces (``"REQ"`` / ``"DES"`` / …) keep a REQ key and a
    DES key from colliding.
    """

    # type -> {local_key: canonical_id}  (same-proposal refs)
    _local: dict[str, dict[str, str]] = field(default_factory=dict)
    # type -> set of canonical ids known from frozen upstream artifacts
    _upstream: dict[str, set[str]] = field(default_factory=dict)

    def register_local(self, id_type: str, local_key: str, canonical_id: str) -> None:
        """Record that proposal-local ``local_key`` maps to allocated ``canonical_id``.

        A second registration of the same ``local_key`` for the same type fails
        loud (§24.2): a duplicate local key means the proposal is ambiguous about
        which item a ref points at, and silently overwriting would hide that.
        """
        if not local_key:
            raise ValueError("local_key must be a non-empty string")
        if not canonical_id:
            raise ValueError("canonical_id must be a non-empty string")
        scope = self._local.setdefault(id_type, {})
        if local_key in scope and scope[local_key] != canonical_id:
            raise ValueError(
                f"duplicate local key {local_key!r} for type {id_type!r}: "
                f"already maps to {scope[local_key]!r}, now told {canonical_id!r}"
            )
        scope[local_key] = canonical_id

    def add_upstream(self, id_type: str, ids: Any) -> None:
        """Register frozen-upstream canonical ``ids`` (an iterable of id strings).

        Cross-stage refs resolve against this set: a design element referencing
        ``REQ-001`` resolves directly because ``REQ-001`` is in the upstream REQ
        set (read from frozen ``01-requirements.json``). Requirements (the root)
        has no upstream, so it never calls this.
        """
        scope = self._upstream.setdefault(id_type, set())
        for value in ids:
            if isinstance(value, str) and value:
                scope.add(value)

    def resolve(self, id_type: str, ref: str) -> str:
        """Return the canonical id for ``ref`` under ``id_type`` (fail loud, D3).

        Local map first (a local key maps to its allocated id), then the upstream
        set (a canonical id resolves to itself). A ref in neither is an
        unresolvable local ref -> :class:`UnresolvedRefError` (§24.2).
        """
        if not isinstance(ref, str) or not ref:
            raise UnresolvedRefError(
                f"ref for type {id_type!r} is not a non-empty string: {ref!r}"
            )
        local = self._local.get(id_type, {})
        if ref in local:
            return local[ref]
        if ref in self._upstream.get(id_type, ()):
            return ref
        raise UnresolvedRefError(
            f"local ref {ref!r} (type {id_type!r}) does not resolve to any "
            f"allocated or upstream id; the proposal is malformed (ADR-0008 D3)"
        )

    def can_resolve(self, id_type: str, ref: str) -> bool:
        """Whether ``ref`` resolves under ``id_type`` (:meth:`resolve` without raising)."""
        local = self._local.get(id_type, {})
        return ref in local or ref in self._upstream.get(id_type, set())

    def resolve_list(self, id_type: str, refs: Any) -> list[str]:
        """Resolve a list of refs, preserving order and deduping.

        A task commonly references several REQs/DESs; this resolves each via
        :meth:`resolve` (so an unresolvable member still fails loud) and returns
        the de-duplicated canonical-id list. A non-list ``refs`` (or a non-string
        member) fails loud rather than being silently coerced (§24.2).
        """
        if not isinstance(refs, list):
            raise UnresolvedRefError(
                f"expected a list of refs for type {id_type!r}, got "
                f"{type(refs).__name__}"
            )
        resolved: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            canonical = self.resolve(id_type, ref)
            if canonical not in seen:
                seen.add(canonical)
                resolved.append(canonical)
        return resolved


# ---------------------------------------------------------------------------
# Requirements proposal -> canonical artifact (id-allocate + stitch + render).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromoteResult:
    """Outcome of one promote: the files written and the ids allocated.

    ``allocated`` maps each §5.2 type promote minted (``REQ`` / ``AC`` for the
    requirements stage) to the list of canonical ids, in allocation order — so a
    caller (or test) can assert exactly which ids a proposal produced without
    re-reading the artifact.
    """

    stage: str
    artifact: str
    json_path: Path
    md_path: Path
    allocated: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _entries(raw: Any, field: str) -> list[Mapping[str, Any]]:
    """Coerce a proposal array field to a list of mappings (fail loud, §24.2).

    The §14.1 schema check (validate-run) already enforces this shape before
    promote runs, but promote is defensive: a non-list field or a non-mapping
    entry is a malformed proposal, not a silent skip. Shared by the
    ``requirements`` and ``acceptance_criteria`` arrays so the two cannot drift.
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"proposal {field!r} must be a list (§24.2); got {type(raw).__name__}"
        )
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"proposal {field}[{i}] must be an object (§24.2); "
                f"got {type(entry).__name__}"
            )
    return [entry for entry in raw]


def _required_str(
    entry: Mapping[str, Any], field: str, index: int, *, what: str
) -> str:
    """Fetch a required non-empty string ``field`` from ``entry`` (fail loud, §24.2).

    The shared shape behind ``key`` / ``statement`` / ``requirement`` / ``criterion``
    extraction: each is a non-empty string the proposal cannot omit. ``what`` is
    the array label (``"requirement"`` / ``"acceptance_criteria"``) for the message.
    """
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"proposal {what}[{index}] needs a non-empty string {field!r} (§24.2)"
        )
    return value


def build_canonical_requirements(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    origin: str | None,
    timestamp: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Allocate ids + stitch refs -> the canonical requirements document.

    The pure allocation/stitch core of ``promote_requirements``, split out so the
    render + write + audit shell stays thin and the id-allocation logic is
    unit-testable on its own. Returns ``(canonical_doc, allocated)`` where
    ``allocated`` is ``{"REQ": [...], "AC": [...]}`` in allocation order.

    Walks requirements first (allocating ``REQ-NNN`` and registering each
    ``key`` -> id on the resolver), then acceptance criteria (resolving each AC's
    ``requirement`` local ref to its allocated REQ id via the resolver, then
    allocating ``AC-NNN``). Reference-integrity (D3) is enforced by
    :meth:`RefResolver.resolve`: an AC pointing at a non-existent REQ key raises
    :class:`UnresolvedRefError` and the whole promote fails loud — no partial
    canonical artifact is written.

    ``timestamp`` is threaded into every ``allocate_id`` call so all id-allocation
    audit records inside one promote share the promote's timestamp (deterministic
    in tests), matching the promote audit record itself.

    The non-id §7.2 facets (:data:`_PROSE_FACETS`) are carried through verbatim
    from the proposal; promote allocates ids and stitches refs, it does not
    editorialise content.
    """
    resolver = RefResolver()
    allocated: dict[str, list[str]] = {"REQ": [], "AC": []}

    requirements: list[dict[str, Any]] = []
    for i, entry in enumerate(_entries(proposal.get("requirements"), "requirements")):
        key = _required_str(entry, "key", i, what="requirement")
        req_id = allocate_id(
            feature_root, "REQ", origin=origin, timestamp=timestamp
        )
        resolver.register_local("REQ", key, req_id)
        allocated["REQ"].append(req_id)
        requirement: dict[str, Any] = {"id": req_id, "key": key}
        # Carry the model's content fields through in a stable order; only
        # ``statement`` is required, the rest are optional prose.
        requirement["statement"] = _required_str(entry, "statement", i, what="requirement")
        if "priority" in entry and entry.get("priority") is not None:
            requirement["priority"] = entry["priority"]
        if "rationale" in entry and entry.get("rationale") is not None:
            requirement["rationale"] = entry["rationale"]
        requirements.append(requirement)

    acceptance_criteria: list[dict[str, Any]] = []
    for i, entry in enumerate(
        _entries(proposal.get("acceptance_criteria"), "acceptance_criteria")
    ):
        key = _required_str(entry, "key", i, what="acceptance_criteria")
        ref = _required_str(entry, "requirement", i, what="acceptance_criteria")
        # D3 reference-integrity: resolve raises UnresolvedRefError if the AC's
        # REQ local ref was never allocated — fail loud, write nothing.
        req_id = resolver.resolve("REQ", ref)
        ac_id = allocate_id(
            feature_root, "AC", origin=origin, timestamp=timestamp
        )
        allocated["AC"].append(ac_id)
        acceptance_criteria.append(
            {
                "id": ac_id,
                "key": key,
                # The stitched canonical ref — the local key resolved to REQ-NNN.
                "requirement": req_id,
                "criterion": _required_str(
                    entry, "criterion", i, what="acceptance_criteria"
                ),
            }
        )

    doc: dict[str, Any] = {
        "feature": feature_id,
        # D1: promote writes the canonical-unfrozen artifact. The frozen flag is
        # the human gate's to flip (status.freeze_artifact); promote never sets
        # it true. The frozen guard in ``promote_requirements`` refuses a frozen
        # artifact outright, so this is always false here.
        "frozen": False,
        "requirements": requirements,
        "acceptance_criteria": acceptance_criteria,
    }
    # Optional §7.2 prose facets carried through verbatim when present.
    for facet in _PROSE_FACETS:
        if facet in proposal and proposal.get(facet) is not None:
            doc[facet] = proposal[facet]

    return doc, allocated


# ---------------------------------------------------------------------------
# The sole requirements markdown renderer (ADR-0008 D2).
# ---------------------------------------------------------------------------


def _render_inline(value: Any) -> str:
    """Render a scalar facet for markdown: bare text, or compact JSON for compound."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _render_list_facets(
    lines: list[str],
    facets: tuple[str, ...],
    headings: Mapping[str, str],
    doc: Mapping[str, Any],
) -> None:
    """Append the optional list-prose facets to ``lines`` (shared by both stages).

    Each facet renders as a ``## <heading>`` section: a bullet list when the
    proposal supplied a list (``_None._`` when empty), or inline prose when it
    supplied a scalar. Facets absent from the doc (``None``) are skipped. The
    requirements and design renderers differ only in which facets + headings they
    pass, so the loop body is shared (it was byte-identical across the two before
    extraction).
    """
    for facet in facets:
        value = doc.get(facet)
        if value is None:
            continue
        lines.append(f"## {headings[facet]}")
        lines.append("")
        if isinstance(value, list):
            if not value:
                lines.append("_None._\n")
            else:
                for item in value:
                    lines.append(f"- {_render_inline(item)}")
                lines.append("")
        else:
            lines.append(_render_inline(value))
            lines.append("")


def render_requirements_md(feature_id: str, doc: Mapping[str, Any]) -> str:
    """Render the human ``01-requirements.md`` mirror from the canonical doc.

    promote is the *sole* md renderer (ADR-0008 D2): markdown is always a
    rendered mirror of canonical JSON, never authored independently. Deterministic
    given the doc — two promotes of equal content produce byte-identical markdown
    (modulo allocated ids). The header carries the frozen state and a pointer to
    the canonical JSON so a reader knows the mirror's source of truth.
    """
    frozen = bool(doc.get("frozen", False))
    lines: list[str] = [
        f"# Requirements — {feature_id}",
        "",
        f"Frozen: {str(frozen).lower()}",
        "",
        "> Stable IDs (REQ-NNN / AC-NNN) are allocated by `promote` from the",
        "> per-type id counter and recorded in `01-requirements.json`. This",
        "> markdown is a rendered mirror; the JSON is canonical (§4.3,",
        "> ADR-0008 D2). Acceptance-criteria `requirement` refs are stitched",
        "> canonical ids, resolved from the proposal's local keys.",
        "",
        "## Requirements (REQ-NNN)",
        "",
    ]

    requirements = doc.get("requirements") or []
    if not requirements:
        lines.append("_None yet._\n")
    for req in requirements:
        rid = req.get("id", "?")
        statement = req.get("statement", "")
        lines.append(f"### {rid} — {statement}")
        if req.get("priority") is not None:
            lines.append(f"- priority: {_render_inline(req['priority'])}")
        if req.get("rationale") is not None:
            lines.append(f"- rationale: {_render_inline(req['rationale'])}")
        lines.append("")

    lines.append("## Acceptance criteria (AC-NNN)")
    lines.append("")
    acs = doc.get("acceptance_criteria") or []
    if not acs:
        lines.append("_None yet._\n")
    for ac in acs:
        aid = ac.get("id", "?")
        req_ref = ac.get("requirement", "?")
        criterion = ac.get("criterion", "")
        lines.append(f"- **{aid}** ({req_ref}): {criterion}")
    if acs:
        lines.append("")

    # Optional §7.2 prose facets, rendered only when the proposal carried them.
    # ``_SCALAR_FACETS`` render inline; ``_LIST_FACETS`` render as bullets (or
    # inline if the proposal supplied a non-list value).
    for facet in _SCALAR_FACETS:
        if doc.get(facet) is not None:
            lines.append(f"## {_FACET_HEADINGS[facet]}")
            lines.append("")
            lines.append(_render_inline(doc[facet]))
            lines.append("")
    _render_list_facets(lines, _LIST_FACETS, _FACET_HEADINGS, doc)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# promote_requirements: the write/render/audit shell over the pure core.
# ---------------------------------------------------------------------------


def promote_requirements(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> PromoteResult:
    """Promote an id-free requirements proposal to the canonical artifact.

    The deterministic stitcher/renderer (ADR-0008 D2): allocates REQ/AC ids from
    the counter, stitches each AC's ``requirement`` local ref to its allocated
    REQ id (reference-integrity, D3 — fails loud on an unresolvable ref), writes
    ``01-requirements.json``, and renders ``01-requirements.md`` (the sole md
    renderer). Refuses to overwrite a frozen requirements artifact (§4.2) and
    appends one ``promote`` audit record carrying the allocated ids.

    ``proposal`` is the parsed Planner output (the run's ``result.json``);
    ticket 02's ``generate-requirements`` reads that file and calls here. Pure at
    the seam apart from the deterministic writes (id counter, canonical json/md,
    audit) — no subprocess, no model.
    """
    if not feature_id:
        raise ValueError("feature_id must be a non-empty string")

    # §4.2 guard: promote targets the canonical-*unfrozen* artifact (D1). A frozen
    # artifact is immutable; only a Change Proposal may change it. Read the frozen
    # map (fail loud if status is corrupt, like every status reader).
    frozen = frozen_artifacts_status(feature_root)
    if frozen.get(REQUIREMENTS_ARTIFACT):
        raise FrozenArtifactWriteError(
            f"artifact {REQUIREMENTS_ARTIFACT!r} is frozen; promote may only "
            f"overwrite an unfrozen artifact (use a Change Proposal to change a "
            f"frozen one, §4.2/§17)"
        )

    doc, allocated = build_canonical_requirements(
        feature_root, feature_id, proposal, origin=origin, timestamp=timestamp
    )

    json_path = feature_root / REQUIREMENTS_JSON
    md_path = feature_root / REQUIREMENTS_MD
    write_json(json_path, doc)
    md_path.write_text(render_requirements_md(feature_id, doc))

    append_audit_event(
        feature_root,
        event=_PROMOTE_EVENT,
        payload={
            "stage": "requirements",
            "artifact": REQUIREMENTS_ARTIFACT,
            "feature": feature_id,
            "allocated": allocated,
        },
        timestamp=timestamp,
        origin=origin,
    )

    return PromoteResult(
        stage="requirements",
        artifact=REQUIREMENTS_ARTIFACT,
        json_path=json_path,
        md_path=md_path,
        allocated={k: tuple(v) for k, v in allocated.items()},
    )


# ---------------------------------------------------------------------------
# Design stage (ticket 03): stitch requirement_mapping against the FROZEN
# requirements upstream (first live use of RefResolver.add_upstream) + allocate
# DES ids + render 02-design.{json,md}.
# ---------------------------------------------------------------------------


def read_frozen_requirements_doc(feature_root: Path) -> Mapping[str, Any]:
    """Read the frozen ``01-requirements.json`` (the design upstream, ADR-0008 D2).

    Design may only stitch cross-references against a *frozen* requirements
    artifact - the upstream ids a design proposal's ``requirement_mapping`` refs
    resolve against. The authoritative frozen state is
    ``status.frozen_artifacts_status`` (``status/feature-status.yml``), **not**
    the requirements doc's ``frozen`` field: promote always writes that field
    ``False`` and the human ``freeze`` never touches the doc, so the doc field
    is a decorative mirror that stays ``False`` even when frozen. Fails loud
    (§24.2) if requirements is not frozen (freeze requirements first) or if the
    canonical doc is missing/unreadable.
    """
    frozen = frozen_artifacts_status(feature_root)
    if not frozen.get(REQUIREMENTS_ARTIFACT):
        raise ValueError(
            f"artifact {REQUIREMENTS_ARTIFACT!r} is not frozen; design may only "
            f"stitch against a frozen upstream (ADR-0008 D2). Freeze requirements "
            f"first (§4.2)."
        )
    doc = read_json_object(feature_root / REQUIREMENTS_JSON)
    if doc is None:
        raise ValueError(
            f"{REQUIREMENTS_JSON} missing or unreadable at {feature_root} (§24.2)"
        )
    return doc


def frozen_req_ids(req_doc: Mapping[str, Any]) -> list[str]:
    """The frozen REQ ids a design refs resolve against (the upstream id set).

    Shared by :func:`build_canonical_design` (registers them via
    ``add_upstream``) and :mod:`coverage` (the set every ``requirement_mapping``
    must cover at the freeze gate). Returns the ids in doc order; callers that
    need set semantics wrap in ``set(...)``. Defensive against a hand-edited doc
    (skips non-mapping entries / non-string ids).
    """
    return [
        r["id"]
        for r in req_doc.get("requirements", [])
        if isinstance(r, Mapping) and isinstance(r.get("id"), str) and r["id"]
    ]


# The optional §7.3 prose facets a design proposal may carry alongside its
# design_elements / requirement_mapping. Split by render shape: the
# "structural" facets (architecture decision / data model / API-CLI contract /
# file layout) are free-form (string or compound) and render in a fenced block;
# the list facets (invariants / risks / dependencies) render as bullets. Listed
# once so adding a facet is a one-place edit.
_DESIGN_STRUCTURAL_FACETS: tuple[str, ...] = (
    "architecture_decision",
    "data_model",
    "api_cli_contract",
    "file_layout",
)
_DESIGN_LIST_FACETS: tuple[str, ...] = ("invariants", "risks", "dependencies")
_DESIGN_FACETS: tuple[str, ...] = _DESIGN_STRUCTURAL_FACETS + _DESIGN_LIST_FACETS
_DESIGN_FACET_HEADINGS: Mapping[str, str] = {
    "architecture_decision": "Architecture decision",
    "data_model": "Data model",
    "api_cli_contract": "API / CLI contract",
    "file_layout": "File layout",
    "invariants": "Invariants",
    "risks": "Risks",
    "dependencies": "Dependencies",
}


def build_canonical_design(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    origin: str | None,
    timestamp: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Allocate DES ids + stitch requirement_mapping -> canonical design doc.

    The pure allocation/stitch core of ``promote_design`` (split out for the same
    reason as ``build_canonical_requirements``: a thin render/write/audit shell
    over a unit-testable core). Returns ``(canonical_doc, allocated)`` where
    ``allocated`` is ``{"DES": [...]}`` in allocation order.

    Two resolution paths exercise the generic :class:`RefResolver` against two
    different ref kinds - this is the first stage with a real frozen upstream:

    * **DES local refs** - each ``requirement_mapping`` entry's
      ``design_elements`` are *local* keys of this proposal's
      ``design_elements[]``. Allocated ``DES-NNN`` ids are registered local
      (``register_local``) as the proposal is walked, then each mapping's
      ``design_elements`` list resolves to the allocated DES ids (same shape as
      AC -> REQ in requirements).
    * **REQ upstream refs** - each ``requirement_mapping`` entry's
      ``requirement`` is a canonical ``REQ-NNN`` read from the *frozen*
      ``01-requirements.json`` (the model references REQs by their canonical id;
      it does not re-allocate them). ``add_upstream("REQ", frozen_req_ids)``
      registers the frozen set, then ``resolve`` verifies each ref is a real
      frozen REQ - a ref to a non-existent REQ (e.g. ``REQ-099``) raises
      :class:`UnresolvedRefError` (D3 reference-integrity) and the whole promote
      fails loud, no partial artifact.

    ``requirement_mapping`` entries are not a §5.2 id type, so promote allocates
    no id for them - the model's local ``key`` is carried as provenance (as AC
    ``key`` is). ``timestamp`` threads into every ``allocate_id`` call so all
    id-allocation audit records inside one promote share its timestamp.
    """
    resolver = RefResolver()
    allocated: dict[str, list[str]] = {"DES": []}

    # Upstream: the frozen REQ ids design refs resolve against (the first live
    # use of add_upstream - requirements, the root, never calls it).
    req_doc = read_frozen_requirements_doc(feature_root)
    resolver.add_upstream("REQ", frozen_req_ids(req_doc))

    design_elements: list[dict[str, Any]] = []
    for i, entry in enumerate(
        _entries(proposal.get("design_elements"), "design_elements")
    ):
        key = _required_str(entry, "key", i, what="design_elements")
        name = _required_str(entry, "name", i, what="design_elements")
        des_id = allocate_id(
            feature_root, "DES", origin=origin, timestamp=timestamp
        )
        resolver.register_local("DES", key, des_id)
        allocated["DES"].append(des_id)
        element: dict[str, Any] = {"id": des_id, "key": key, "name": name}
        for opt in ("description", "rationale", "type"):
            if opt in entry and entry.get(opt) is not None:
                element[opt] = entry[opt]
        design_elements.append(element)

    requirement_mapping: list[dict[str, Any]] = []
    for i, entry in enumerate(
        _entries(proposal.get("requirement_mapping"), "requirement_mapping")
    ):
        key = _required_str(entry, "key", i, what="requirement_mapping")
        # D3 reference-integrity: resolve raises UnresolvedRefError if the
        # mapping's REQ ref is not a real frozen REQ id - fail loud, write nothing.
        req_id = resolver.resolve(
            "REQ", _required_str(entry, "requirement", i, what="requirement_mapping")
        )
        # design_elements are local DES refs; resolve_list fails loud on a
        # non-list or an unresolvable member.
        des_ids = resolver.resolve_list("DES", entry.get("design_elements"))
        mapping: dict[str, Any] = {
            "key": key,
            "requirement": req_id,
            "design_elements": des_ids,
        }
        if "rationale" in entry and entry.get("rationale") is not None:
            mapping["rationale"] = entry["rationale"]
        requirement_mapping.append(mapping)

    doc: dict[str, Any] = {
        "feature": feature_id,
        # D1: promote writes the canonical-unfrozen artifact; the frozen flag is
        # the human gate's to flip. The frozen guard in ``promote_design``
        # refuses a frozen artifact outright, so this is always false here.
        "frozen": False,
        "design_elements": design_elements,
        "requirement_mapping": requirement_mapping,
    }
    # Optional §7.3 prose facets carried through verbatim when present.
    for facet in _DESIGN_FACETS:
        if facet in proposal and proposal.get(facet) is not None:
            doc[facet] = proposal[facet]

    return doc, allocated


def render_design_md(feature_id: str, doc: Mapping[str, Any]) -> str:
    """Render the human ``02-design.md`` mirror from the canonical doc.

    The sole md renderer for the design stage (ADR-0008 D2): markdown is always a
    rendered mirror of canonical JSON, never authored independently. Deterministic
    given the doc. The header carries the frozen state and points at the canonical
    JSON; the requirement-mapping section shows the stitched REQ/DES refs.
    """
    frozen = bool(doc.get("frozen", False))
    lines: list[str] = [
        f"# Design - {feature_id}",
        "",
        f"Frozen: {str(frozen).lower()}",
        "",
        "> Stable IDs (DES-NNN) are allocated by `promote` from the per-type id",
        "> counter and recorded in `02-design.json`. This markdown is a rendered",
        "> mirror; the JSON is canonical (§4.3, ADR-0008 D2). The",
        "> `requirement_mapping` `requirement` refs are stitched canonical REQ ids,",
        "> resolved from the frozen `01-requirements.json`; `design_elements` refs",
        "> are stitched DES ids, resolved from the proposal's local keys.",
        "",
        "## Design elements (DES-NNN)",
        "",
    ]

    elements = doc.get("design_elements") or []
    if not elements:
        lines.append("_None yet._\n")
    for el in elements:
        eid = el.get("id", "?")
        name = el.get("name", "")
        lines.append(f"### {eid} - {name}")
        if el.get("description") is not None:
            lines.append("")
            lines.append(str(el["description"]))
        if el.get("type") is not None:
            lines.append(f"- type: {_render_inline(el['type'])}")
        if el.get("rationale") is not None:
            lines.append(f"- rationale: {_render_inline(el['rationale'])}")
        lines.append("")

    lines.append("## Requirement mapping (-> REQ / DES)")
    lines.append("")
    mapping = doc.get("requirement_mapping") or []
    if not mapping:
        lines.append("_None yet._\n")
    for m in mapping:
        req_ref = m.get("requirement", "?")
        des_refs = ", ".join(m.get("design_elements", [])) or "-"
        line = f"- REQ **{req_ref}** <- [{des_refs}]"
        if m.get("rationale"):
            line += f" - {m['rationale']}"
        lines.append(line)
    if mapping:
        lines.append("")

    # Optional §7.3 prose facets. Structural facets render in a fenced block
    # (string verbatim, compound as JSON); list facets render as bullets.
    for facet in _DESIGN_STRUCTURAL_FACETS:
        if doc.get(facet) is None:
            continue
        lines.append(f"## {_DESIGN_FACET_HEADINGS[facet]}")
        lines.append("")
        value = doc[facet]
        if isinstance(value, str):
            lines.append(value)
        else:
            lines.append("```json")
            lines.append(json.dumps(value, indent=2, ensure_ascii=False))
            lines.append("```")
        lines.append("")
    _render_list_facets(lines, _DESIGN_LIST_FACETS, _DESIGN_FACET_HEADINGS, doc)

    return "\n".join(lines)


def promote_design(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> PromoteResult:
    """Promote an id-free design proposal to the canonical artifact (ticket 03).

    The deterministic stitcher/renderer (ADR-0008 D2) for the design stage:
    allocates DES ids from the counter, stitches each ``requirement_mapping``
    entry's ``requirement`` ref to its frozen REQ id and its ``design_elements``
    local refs to their allocated DES ids (reference-integrity, D3 - fails loud on
    an unresolvable ref), writes ``02-design.json``, and renders ``02-design.md``
    (the sole md renderer). Refuses to overwrite a frozen design artifact (§4.2)
    and refuses if the requirements upstream is not yet frozen (D2 - design may
    only stitch against a frozen upstream). Appends one ``promote`` audit record
    carrying the allocated DES ids.

    ``proposal`` is the parsed Planner output (the run's ``result.json``);
    ticket 03's ``generate-design`` reads that file and calls here. Pure at the
    seam apart from the deterministic writes (id counter, canonical json/md,
    audit) - no subprocess, no model.
    """
    if not feature_id:
        raise ValueError("feature_id must be a non-empty string")

    # §4.2 guard: promote targets the canonical-*unfrozen* design artifact (D1).
    frozen = frozen_artifacts_status(feature_root)
    if frozen.get(DESIGN_ARTIFACT):
        raise FrozenArtifactWriteError(
            f"artifact {DESIGN_ARTIFACT!r} is frozen; promote may only "
            f"overwrite an unfrozen artifact (use a Change Proposal to change a "
            f"frozen one, §4.2/§17)"
        )

    doc, allocated = build_canonical_design(
        feature_root, feature_id, proposal, origin=origin, timestamp=timestamp
    )

    json_path = feature_root / DESIGN_JSON
    md_path = feature_root / DESIGN_MD
    write_json(json_path, doc)
    md_path.write_text(render_design_md(feature_id, doc))

    append_audit_event(
        feature_root,
        event=_PROMOTE_EVENT,
        payload={
            "stage": "design",
            "artifact": DESIGN_ARTIFACT,
            "feature": feature_id,
            "allocated": allocated,
        },
        timestamp=timestamp,
        origin=origin,
    )

    return PromoteResult(
        stage="design",
        artifact=DESIGN_ARTIFACT,
        json_path=json_path,
        md_path=md_path,
        allocated={k: tuple(v) for k, v in allocated.items()},
    )
