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
from typing import Any, Callable, Mapping, NamedTuple

import yaml

from ai_dev.audit import append_audit_event
from ai_dev.feature_ids import allocate_id
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.status import TASK_STATUS_FILE, frozen_artifacts_status
from ai_dev.templates import (
    DESIGN_JSON,
    DESIGN_MD,
    LANE_GRAPH_YML,
    REQUIREMENTS_JSON,
    REQUIREMENTS_MD,
    TASKS_JSON,
    TASKS_MD,
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

# The §4.2 artifact names the tasks stage writes (ticket 04). ``tasks`` is the
# canonical task content (03-tasks.{json,md}); ``lane_graph`` is the single lane
# the same promote populates (04-lane-graph.yml) - the two freeze together at the
# task gate (§18.3), so promote writes both in one step and refuses if either is
# frozen. Public so the coverage precheck / freeze dispatch reference them.
TASKS_ARTIFACT = "tasks"
LANE_GRAPH_ARTIFACT = "lane_graph"


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
    return _promote_artifact(
        feature_root,
        feature_id,
        REQUIREMENTS_ARTIFACT,
        proposal,
        timestamp=timestamp,
        origin=origin,
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
    return _promote_artifact(
        feature_root,
        feature_id,
        DESIGN_ARTIFACT,
        proposal,
        timestamp=timestamp,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Tasks stage (ticket 04): stitch each task's REQ+DES refs against the FROZEN
# requirements AND design upstreams + allocate TASK ids + render 03-tasks.{json,md}
# + seed status/task-status.yml (all pending) + populate the single lane in
# 04-lane-graph.yml (purpose/tasks/files). The first stage with TWO frozen
# upstreams and the first that writes four files in one promote.
# ---------------------------------------------------------------------------


def read_frozen_design_doc(feature_root: Path) -> Mapping[str, Any]:
    """Read the frozen ``02-design.json`` (the tasks upstream, ADR-0008 D2).

    Tasks may only stitch cross-references against a *frozen* design artifact -
    the upstream DES ids a tasks proposal's ``related_design`` refs resolve
    against (REQs resolve against the frozen requirements, also read here via
    :func:`read_frozen_requirements_doc`). The authoritative frozen state is
    ``status.frozen_artifacts_status`` (not the design doc's ``frozen`` field,
    which promote always writes ``False``). Fails loud (§24.2) if design is not
    frozen (freeze design first) or the canonical doc is missing/unreadable.
    """
    frozen = frozen_artifacts_status(feature_root)
    if not frozen.get(DESIGN_ARTIFACT):
        raise ValueError(
            f"artifact {DESIGN_ARTIFACT!r} is not frozen; tasks may only stitch "
            f"against a frozen upstream (ADR-0008 D2). Freeze design first (§4.2)."
        )
    doc = read_json_object(feature_root / DESIGN_JSON)
    if doc is None:
        raise ValueError(
            f"{DESIGN_JSON} missing or unreadable at {feature_root} (§24.2)"
        )
    return doc


def frozen_des_ids(des_doc: Mapping[str, Any]) -> list[str]:
    """The frozen DES ids a tasks proposal's ``related_design`` refs resolve against.

    Shared by :func:`build_canonical_tasks` (registers them via ``add_upstream``)
    and :mod:`coverage` (the set every task's ``related_design`` must cover at
    the freeze gate). Returns the ids in doc order; callers needing set semantics
    wrap in ``set(...)``. Defensive against a hand-edited doc.
    """
    return [
        e["id"]
        for e in des_doc.get("design_elements", [])
        if isinstance(e, Mapping) and isinstance(e.get("id"), str) and e["id"]
    ]


def _str_list(
    raw: Any, field: str, index: int, *, what: str
) -> list[str]:
    """Coerce a proposal list-of-strings field (fail loud, §24.2).

    Used for the non-ref list fields a task carries (``expected_files`` /
    ``exclusive_files``): each must be a list of non-empty strings, but the
    members are NOT resolved against an upstream (they are file paths the model
    declares, not refs). A non-list field or a non-string/empty member is a
    malformed proposal, not a silent skip. (Ref lists use
    :meth:`RefResolver.resolve_list`, which resolves each member too.)
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"proposal {what}[{index}] {field!r} must be a list (§24.2); "
            f"got {type(raw).__name__}"
        )
    out: list[str] = []
    for j, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            raise ValueError(
                f"proposal {what}[{index}] {field}[{j}] must be a non-empty "
                f"string (§24.2)"
            )
        out.append(item)
    return out


def _normalize_verify_commands(raw: Any) -> list[dict[str, str]]:
    """Validate the proposal's top-level ``verification_commands`` (fail loud, §24.2).

    The optional lane verify command set (v0.6 capstone, ticket 05): a list of
    ``{name, command}`` mappings the model authors and promote writes onto the
    single lane in ``04-lane-graph.yml`` so the shell Verifier (§9.5) runs
    Planner-generated commands - the zero-hand-authored-planning bar. Returns the
    normalized ``[{"name": ..., "command": ...}, ...]`` (stripped), or ``[]`` when
    the proposal omits the field (a refinement draft may lag; the lane entry's
    verify commands are then left empty). A non-list field or a mapping lacking a
    non-empty ``name`` / ``command`` is a malformed proposal, not a silent skip -
    mirroring ``implement_leg._require_dict_list`` (shape) plus the verifier's own
    semantic check (the verifier re-validates each entry it executes).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            "proposal 'verification_commands' must be a list (§24.2); "
            f"got {type(raw).__name__}"
        )
    out: list[dict[str, str]] = []
    for j, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"proposal 'verification_commands'[{j}] must be an object with "
                f"'name' and 'command' (§9.5/§24.2); got {type(item).__name__}"
            )
        name = item.get("name")
        command = item.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"proposal 'verification_commands'[{j}] has no non-empty 'name' "
                f"(§9.5/§24.2)"
            )
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"proposal 'verification_commands'[{j}] (name={name!r}) has no "
                f"non-empty 'command' (§9.5/§24.2)"
            )
        out.append({"name": name.strip(), "command": command.strip()})
    return out


def _read_lane_graph(feature_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``04-lane-graph.yml`` + return ``(graph, first_lane)`` (MVP one lane).

    The shared open/parse/validate cascade for the two lane-graph writers
    (``_seeded_lane_id`` reads the id; ``_populate_lane_graph_from_doc`` writes
    purpose/tasks/files onto the lane). The lane is structural: allocated at
    feature-run creation and seeded into ``04-lane-graph.yml`` (ticket 03), never
    by promote. Fails loud (§24.2) if the lane-graph is missing/mis-shaped or its
    first lane has no string id - a feature run always has one seeded lane.
    """
    path = feature_root / LANE_GRAPH_YML
    if not path.is_file():
        raise ValueError(f"{LANE_GRAPH_YML} missing at {path} (§7.5)")
    try:
        graph = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{LANE_GRAPH_YML} at {path} is not valid YAML: {exc} (§7.5)"
        ) from exc
    if not isinstance(graph, dict):
        raise ValueError(f"{LANE_GRAPH_YML} at {path} is not a mapping (§7.5)")
    lanes = graph.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise ValueError(f"{LANE_GRAPH_YML} at {path} has no 'lanes' list (§7.5)")
    first = lanes[0]
    if not isinstance(first, dict) or not isinstance(first.get("id"), str) or not first["id"]:
        raise ValueError(
            f"{LANE_GRAPH_YML} at {path} first lane has no string id (§7.5)"
        )
    return graph, first


def _seeded_lane_id(feature_root: Path) -> str:
    """Read the single seeded lane id from ``04-lane-graph.yml`` (MVP one lane).

    The lane is structural: allocated at feature-run creation and seeded into
    ``04-lane-graph.yml`` (ticket 03), never by promote. The tasks proposal
    supplies the lane's *purpose* but not its id; promote assigns every task to
    this one seeded lane (single-lane assignment, §5.3).
    """
    _, first = _read_lane_graph(feature_root)
    return first["id"]


def build_canonical_tasks(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    origin: str | None,
    timestamp: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Allocate TASK ids + stitch REQ/DES refs -> canonical tasks doc + status rows.

    The pure allocation/stitch core of ``promote_tasks`` (split out for the same
    reason as the other build cores: a thin render/write/audit shell over a
    unit-testable core). Returns
    ``(canonical_doc, allocated, task_status_rows, verification_commands)`` where
    ``allocated`` is ``{"TASK": [...]}`` in allocation order,
    ``task_status_rows`` is ``{task_id: §8.1-row}`` (all ``pending``) for the
    runtime ``task-status.yml`` promote seeds, and ``verification_commands`` is
    the validated lane verify command set (v0.6 capstone, ticket 05) promote
    writes onto the lane in ``04-lane-graph.yml`` - ``[]`` when the proposal
    omits it. The verify commands are a lane-level concern, so they are NOT
    written into ``03-tasks.json`` (the task-content doc); they travel out of
    band to the lane-graph writer.

    Three resolution paths exercise the generic :class:`RefResolver` - this is the
    first stage with TWO frozen upstreams:

    * **REQ upstream refs** - each task's ``related_requirements`` are canonical
      ``REQ-NNN`` read from the frozen ``01-requirements.json``.
      ``add_upstream("REQ", frozen_req_ids)`` registers the frozen set, then
      ``resolve_list`` verifies each ref is a real frozen REQ (a ref to a
      non-existent REQ raises :class:`UnresolvedRefError`, D3).
    * **DES upstream refs** - each task's ``related_design`` are canonical
      ``DES-NNN`` read from the frozen ``02-design.json``. Same upstream path
      (``add_upstream("DES", frozen_des_ids)``); same reference-integrity.
    * **TASK local refs** - none in v0.6 (tasks are leaves of the planning DAG;
      nothing refs a task by local key). TASK ids are allocated + registered local
      for forward use, but no proposal member resolves against them.

    The lane id is read from the seeded ``04-lane-graph.yml`` (single MVP lane);
    every task is assigned to it. ``related_acceptance_criteria`` is *derived*
    (not authored): the ACs whose ``requirement`` traces to one of the task's
    ``related_requirements`` (TASK -> REQ -> AC, the §8.1 traceability chain the
    Implementer writeback later preserves). ``timestamp`` threads into every
    ``allocate_id`` call so all id-allocation audit records share the promote's
    timestamp.
    """
    resolver = RefResolver()
    allocated: dict[str, list[str]] = {"TASK": []}

    # Two frozen upstreams: REQ ids (from frozen requirements) + DES ids (from
    # frozen design). tasks is the first stage stitching against two upstreams.
    req_doc = read_frozen_requirements_doc(feature_root)
    resolver.add_upstream("REQ", frozen_req_ids(req_doc))
    des_doc = read_frozen_design_doc(feature_root)
    resolver.add_upstream("DES", frozen_des_ids(des_doc))

    # AC -> REQ index: derive each task's related_acceptance_criteria (the ACs
    # tracing to the task's REQs) from the frozen requirements doc, read once.
    acs_by_req: dict[str, list[str]] = {}
    for ac in req_doc.get("acceptance_criteria", []) or []:
        if not isinstance(ac, Mapping):
            continue
        ref = str(ac.get("requirement", ""))
        aid = ac.get("id")
        if ref and isinstance(aid, str) and aid:
            acs_by_req.setdefault(ref, []).append(aid)

    # Top-level lane_purpose (the single MVP lane's purpose, written into the
    # lane-graph by promote). Required non-empty: a task gate with no lane
    # purpose is a malformed proposal.
    lane_purpose = proposal.get("lane_purpose")
    if not isinstance(lane_purpose, str) or not lane_purpose:
        raise ValueError(
            "proposal needs a non-empty string 'lane_purpose' (§24.2)"
        )
    lane_id = _seeded_lane_id(feature_root)

    tasks: list[dict[str, Any]] = []
    task_status_rows: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(_entries(proposal.get("tasks"), "tasks")):
        key = _required_str(entry, "key", i, what="tasks")
        summary = _required_str(entry, "summary", i, what="tasks")
        # D3 reference-integrity: resolve_list raises UnresolvedRefError if a REQ
        # or DES ref is not a real frozen upstream id - fail loud, write nothing.
        req_refs = resolver.resolve_list("REQ", entry.get("related_requirements"))
        des_refs = resolver.resolve_list("DES", entry.get("related_design"))
        task_id = allocate_id(
            feature_root, "TASK", origin=origin, timestamp=timestamp
        )
        resolver.register_local("TASK", key, task_id)
        allocated["TASK"].append(task_id)
        expected_files = _str_list(
            entry.get("expected_files"), "expected_files", i, what="tasks"
        )
        exclusive_files = _str_list(
            entry.get("exclusive_files"), "exclusive_files", i, what="tasks"
        )
        task: dict[str, Any] = {
            "id": task_id,
            "key": key,
            "lane": lane_id,
            "summary": summary,
            "related_requirements": req_refs,
            "related_design": des_refs,
            "expected_files": expected_files,
            "exclusive_files": exclusive_files,
        }
        if "description" in entry and entry.get("description") is not None:
            task["description"] = entry["description"]
        if "verification" in entry and entry.get("verification") is not None:
            task["verification"] = entry["verification"]
        tasks.append(task)

        # Derive related_acceptance_criteria: ACs tracing to this task's REQs
        # (TASK -> REQ -> AC, §8.1). Preserved verbatim by the Implementer
        # writeback later (only status/proposed_done_by move).
        related_acs: list[str] = []
        seen_ac: set[str] = set()
        for req in req_refs:
            for aid in acs_by_req.get(req, []):
                if aid not in seen_ac:
                    seen_ac.add(aid)
                    related_acs.append(aid)
        task_status_rows[task_id] = {
            "status": "pending",
            "lane": lane_id,
            "owner_run": None,
            "proposed_done_by": None,
            "accepted_done": False,
            "related_requirements": req_refs,
            "related_acceptance_criteria": related_acs,
        }

    doc: dict[str, Any] = {
        "feature": feature_id,
        # D1: promote writes the canonical-unfrozen artifact; the frozen flag is
        # the human gate's to flip. The frozen guard in ``promote_tasks`` refuses
        # a frozen tasks/lane_graph outright, so this is always false here.
        "frozen": False,
        "lane_purpose": lane_purpose,
        "tasks": tasks,
    }
    # v0.6 capstone (ticket 05): the optional lane verify command set travels out
    # of band to the lane-graph writer (it is a lane-level concern, not task
    # content), so it is NOT stored on the doc that becomes 03-tasks.json.
    verification_commands = _normalize_verify_commands(proposal.get("verification_commands"))
    return doc, allocated, task_status_rows, verification_commands


def render_tasks_md(feature_id: str, doc: Mapping[str, Any]) -> str:
    """Render the human ``03-tasks.md`` mirror from the canonical doc.

    The sole md renderer for the tasks stage (ADR-0008 D2): markdown is always a
    rendered mirror of canonical JSON, never authored independently. Deterministic
    given the doc. The ``## Tasks`` section body is what the Implementer leg later
    reads verbatim (``read_task_text``), so nothing follows it; the single lane's
    purpose renders in its own section *above* ``## Tasks`` to keep the task list
    clean for the implementer.
    """
    frozen = bool(doc.get("frozen", False))
    lines: list[str] = [
        f"# Tasks - {feature_id}",
        "",
        f"Frozen: {str(frozen).lower()}",
        "",
        "> Stable IDs (TASK-NNN) are allocated by `promote` from the per-type id",
        "> counter and recorded in `03-tasks.json`. This markdown is a rendered",
        "> mirror; the JSON is canonical (§4.3, ADR-0008 D2). Each task's",
        "> `related_requirements` / `related_design` refs are stitched canonical",
        "> REQ / DES ids, resolved against the frozen upstreams. Runtime state",
        "> (pending -> proposed_done) lives in `status/task-status.yml` (§8.1).",
        "",
    ]

    purpose = doc.get("lane_purpose")
    if purpose is not None:
        lines.append("## Lane purpose (single lane)")
        lines.append("")
        lines.append(str(purpose))
        lines.append("")

    # ``## Tasks`` is the LAST section: the Implementer reads its body verbatim,
    # so nothing may follow it (no trailing sections).
    lines.append("## Tasks (TASK-NNN)")
    lines.append("")
    tasks = doc.get("tasks") or []
    if not tasks:
        lines.append("_None yet._\n")
    for t in tasks:
        tid = t.get("id", "?")
        summary = t.get("summary", "")
        lines.append(f"### {tid} - {summary}")
        lines.append(f"- lane: {t.get('lane', '?')}")
        reqs = ", ".join(t.get("related_requirements", [])) or "-"
        dess = ", ".join(t.get("related_design", [])) or "-"
        lines.append(f"- related_requirements: {reqs}")
        lines.append(f"- related_design: {dess}")
        lines.append(
            f"- expected_files: {', '.join(t.get('expected_files', []) or []) or '-'}"
        )
        lines.append(
            f"- exclusive_files: {', '.join(t.get('exclusive_files', []) or []) or '-'}"
        )
        if t.get("description") is not None:
            lines.append("")
            lines.append(str(t["description"]))
        if t.get("verification"):
            lines.append(f"- verification: {', '.join(t['verification'])}")
        lines.append("")

    return "\n".join(lines)


def _seed_task_status(
    feature_root: Path, task_status_rows: Mapping[str, Mapping[str, Any]]
) -> None:
    """Seed ``status/task-status.yml`` with one ``pending`` row per task (§8.1).

    Runtime state, separate from the frozen content artifacts: promote seeds it
    (every task ``pending``) but does NOT freeze it - the task/lane gate freezes
    ``03-tasks`` + ``04-lane-graph`` together, not ``task-status.yml`` (§18.3).
    Re-promote overwrites cleanly: the unfrozen tasks stage is being replaced, and
    no Implementer has run yet (the implementer leg runs only after the task gate
    freezes), so there is no runtime state to preserve. Matches the
    ``write_initial_task_status`` / ``mark_task_proposed_done`` yaml style.
    """
    path = feature_root / "status" / TASK_STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"tasks": dict(task_status_rows)}
    with path.open("w") as f:
        yaml.safe_dump(
            doc, f, sort_keys=False, default_flow_style=False, allow_unicode=True
        )


def _populate_lane_graph_from_doc(
    feature_root: Path,
    doc: Mapping[str, Any],
    *,
    verification_commands: list[dict[str, str]] | None = None,
) -> None:
    """Populate the single lane's purpose/tasks/files (+ verify commands) in
    ``04-lane-graph.yml``.

    Reads the seeded lane-graph (one MVP lane), writes the lane's ``purpose``
    (from the proposal), its ``tasks`` (the allocated TASK ids, in order), and the
    union of all tasks' ``expected_files`` / ``exclusive_files`` (deduped + sorted
    for determinism). The lane's other §7.5 fields (``id`` / ``depends_on`` /
    ``provides`` / ``consumes`` / ``verification_scope`` / ``merge_policy``) are
    preserved - promote fills only what the tasks proposal carries. The lane's
    files are the union of task files so the Implementer's ``lane_allowed_files``
    (which reads the lane entry) sees every file any task touches.

    v0.6 capstone (ticket 05): when ``verification_commands`` is non-empty, write
    the Planner-generated lane verify command set onto the lane entry so the
    shell Verifier (§9.5) runs model-generated commands - the
    zero-hand-authored-planning bar (the v0.4 dogfood hand-authored these). When
    empty/None the lane entry's existing ``verification_commands`` is left
    untouched (backward compatible with proposals that omit it).
    """
    graph, lane = _read_lane_graph(feature_root)

    tasks = doc.get("tasks") or []
    expected: set[str] = set()
    exclusive: set[str] = set()
    task_ids: list[str] = []
    for t in tasks:
        if not isinstance(t, Mapping):
            continue
        tid = t.get("id")
        if isinstance(tid, str) and tid:
            task_ids.append(tid)
        for f in t.get("expected_files", []) or []:
            if isinstance(f, str) and f:
                expected.add(f)
        for f in t.get("exclusive_files", []) or []:
            if isinstance(f, str) and f:
                exclusive.add(f)

    lane["purpose"] = doc.get("lane_purpose")
    lane["tasks"] = task_ids
    lane["expected_files"] = sorted(expected)
    lane["exclusive_files"] = sorted(exclusive)
    if verification_commands:
        lane["verification_commands"] = verification_commands
        # ``verification_scope`` is the human label set (§7.5) - the names of the
        # commands above, derived so the two never drift.
        lane["verification_scope"] = [vc["name"] for vc in verification_commands]
    with (feature_root / LANE_GRAPH_YML).open("w") as f:
        yaml.safe_dump(
            graph, f, sort_keys=False, default_flow_style=False, allow_unicode=True
        )


def promote_tasks(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> PromoteResult:
    """Promote an id-free tasks proposal to the canonical artifacts (ticket 04).

    The deterministic stitcher/renderer (ADR-0008 D2) for the tasks stage, and the
    first promote that writes **four** files in one step (ADR-0008 D2 /
    CONTEXT.md "Artifact model"): allocates TASK ids from the counter, stitches
    each task's ``related_requirements`` / ``related_design`` refs against the
    frozen REQ / DES upstreams (reference-integrity, D3 - fails loud on an
    unresolvable ref), then writes (1) ``03-tasks.json`` (**new in v0.6** - the
    canonical task content) + (2) renders ``03-tasks.md`` (the sole md renderer) +
    (3) seeds ``status/task-status.yml`` (every task ``pending`` - runtime state,
    not frozen here) + (4) populates ``04-lane-graph.yml`` (the single lane's
    ``purpose`` / ``tasks`` / expected/exclusive files). Refuses to overwrite a
    frozen ``tasks`` or ``lane_graph`` (the two freeze together at the task gate,
    §18.3) and refuses if the requirements or design upstream is not yet frozen
    (D2). Appends one ``promote`` audit record carrying the allocated TASK ids.

    ``proposal`` is the parsed Planner output (the run's ``result.json``);
    ticket 04's ``generate-tasks`` reads that file and calls here. Pure at the
    seam apart from the deterministic writes (id counter, canonical json/md,
    task-status, lane-graph, audit) - no subprocess, no model.
    """
    return _promote_artifact(
        feature_root,
        feature_id,
        TASKS_ARTIFACT,
        proposal,
        timestamp=timestamp,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Direct-edit refinement channel (ADR-0008 D4, v0.6 ticket 06 — optional).
#
# Alongside the model-mediated feedback loop (the primary refinement path,
# tickets 02-04), a human may **directly edit the canonical *unfrozen* ``.json``
# of a planning artifact** for a surgical fix (a typo in a statement, rewording a
# DES) without a model round-trip. ``render`` is the deterministic bookend that
# keeps the single-source-of-truth invariant (D2) holding: it re-renders the
# ``.md`` mirror from the edited ``.json`` so markdown never drifts. It targets
# the three json+md artifacts (requirements / design / tasks); ``lane_graph`` is a
# YAML-only artifact with no md mirror, so it is not renderable.
#
# §4.3 reserves ids/status/gate-verdict for deterministic scripts — unfrozen
# *content* is editable; the artifact must still be **unfrozen** (a frozen
# artifact ⇒ Change Proposal, §17, out of scope for v0.6). ``render`` refuses a
# frozen artifact outright (the direct-edit channel is closed past freeze). The
# ``allocate-id`` CLI helper (ticket 06) is the companion that mints the next
# counter id for any *new* item the human adds, so ids stay in the counter and
# out of human hands (§4.3).
# ---------------------------------------------------------------------------

# The audit event for a direct-edit render (the bookend of the human edit
# channel). One record per render, carrying the artifact re-rendered.
_RENDER_EVENT = "render"

# The §4.2 artifacts that carry a json+md pair and are therefore renderable:
# requirements / design / tasks. ``lane_graph`` (``04-lane-graph.yml``) is
# YAML-only with no md mirror, so it is absent — render has nothing to render
# for it. Public so the CLI's argparse ``choices`` share one source of truth.
RENDERABLE_ARTIFACTS: tuple[str, ...] = (
    REQUIREMENTS_ARTIFACT,
    DESIGN_ARTIFACT,
    TASKS_ARTIFACT,
)

# artifact -> canonical ``.json`` filename (the source of truth render reads).
# Public + paired with ``artifact_md_file`` so the renderable-artifact → filename
# knowledge lives in one place (promote owns the artifact model, ADR-0008 D2);
# dry-run / CLI import these rather than re-declaring the map (no Shotgun Surgery
# when a json+md artifact is added).
_ARTIFACT_JSON_FILE: Mapping[str, str] = {
    REQUIREMENTS_ARTIFACT: REQUIREMENTS_JSON,
    DESIGN_ARTIFACT: DESIGN_JSON,
    TASKS_ARTIFACT: TASKS_JSON,
}

# artifact -> ``.md`` mirror filename (the rendered output render writes).
_ARTIFACT_MD_FILE: Mapping[str, str] = {
    REQUIREMENTS_ARTIFACT: REQUIREMENTS_MD,
    DESIGN_ARTIFACT: DESIGN_MD,
    TASKS_ARTIFACT: TASKS_MD,
}


def artifact_json_file(artifact: str) -> str:
    """The canonical ``.json`` filename for a renderable ``artifact``.

    The single source of truth for the artifact → json-filename mapping (paired
    with :func:`artifact_md_file`); callers that need the filename import this
    rather than re-declaring the table. Raises ``ValueError`` for a non-renderable
    artifact, mirroring :func:`render_artifact`.
    """
    try:
        return _ARTIFACT_JSON_FILE[artifact]
    except KeyError as exc:
        raise ValueError(
            f"artifact {artifact!r} is not renderable; expected one of "
            f"{RENDERABLE_ARTIFACTS} (lane_graph has no md mirror)"
        ) from exc


def artifact_md_file(artifact: str) -> str:
    """The ``.md`` mirror filename for a renderable ``artifact`` (see :func:`artifact_json_file`)."""
    try:
        return _ARTIFACT_MD_FILE[artifact]
    except KeyError as exc:
        raise ValueError(
            f"artifact {artifact!r} is not renderable; expected one of "
            f"{RENDERABLE_ARTIFACTS} (lane_graph has no md mirror)"
        ) from exc


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one direct-edit ``render``: the files read + re-rendered.

    Carries the artifact name and the canonical ``.json`` (read, the source of
    truth) + ``.md`` (re-rendered, the mirror) paths so a caller or test can
    assert the mirror was resynced without re-reading it.
    """

    artifact: str
    json_path: Path
    md_path: Path


def render_artifact(
    feature_root: Path,
    feature_id: str,
    artifact: str,
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> RenderResult:
    """Re-render an artifact's ``.md`` mirror from its canonical ``.json`` (D4).

    The deterministic bookend of the direct-edit refinement channel (ADR-0008
    D4): after a human surgically edits the canonical *unfrozen* ``.json`` (a
    typo, a reworded DES), this re-renders the ``.md`` mirror from that JSON so
    the single-source-of-truth invariant (D2 — markdown is always a rendered
    mirror of canonical JSON) holds. Delegates to the sole stage renderer
    (``render_requirements_md`` / ``render_design_md`` / ``render_tasks_md``) —
    the *same* renderer promote used — so a direct edit + render produces a mirror
    byte-identical to what a re-promote of equal content would.

    Three guards, mirroring promote:

    * **frozen** — the direct-edit channel is closed past freeze. A frozen
      artifact is immutable; only a Change Proposal (§17, out of scope for v0.6)
      may change it. render refuses (``FrozenArtifactWriteError``) rather than
      silently resyncing frozen content's mirror.
    * **missing/unreadable JSON** — nothing to render. Fails loud (§24.2): a
      render on an artifact that was never promoted (no ``.json``) or a corrupt
      ``.json`` is a broken precondition, not a silent no-op.
    * **unknown artifact** — ``ValueError`` (``lane_graph`` is not renderable).

    render re-renders content only; it allocates no ids and stitches no refs
    (those are promote's job). The reference-integrity + coverage invariants are
    unchanged: a content edit leaves the stitched refs untouched, and the
    freeze-gate coverage precheck still runs at the subsequent freeze reading the
    edited JSON. Appends one ``render`` audit record so the direct-edit is
    traceable. Pure at the seam apart from the deterministic write (md mirror +
    audit) — no subprocess, no model.
    """
    if not feature_id:
        raise ValueError("feature_id must be a non-empty string")
    if artifact not in _ARTIFACT_JSON_FILE:
        raise ValueError(
            f"artifact {artifact!r} is not renderable; expected one of "
            f"{RENDERABLE_ARTIFACTS} (lane_graph has no md mirror)"
        )

    # §4.2 guard (mirrors promote): the direct-edit channel targets the
    # canonical-*unfrozen* artifact. A frozen artifact is immutable; only a
    # Change Proposal may change it, so its mirror may not be resynced here.
    frozen = frozen_artifacts_status(feature_root)
    if frozen.get(artifact):
        raise FrozenArtifactWriteError(
            f"artifact {artifact!r} is frozen; render may only resync an "
            f"unfrozen artifact's mirror (use a Change Proposal to change a "
            f"frozen one, §4.2/§17)"
        )

    json_name = artifact_json_file(artifact)
    json_path = feature_root / json_name
    doc = read_json_object(json_path)
    if doc is None:
        raise ValueError(
            f"{json_name} missing or unreadable at "
            f"{json_path}; nothing to render (promote {artifact} first, §24.2)"
        )

    # Reference-integrity (ADR-0008 D3) for the direct-edit channel: the human
    # may have introduced a dangling ref (an AC whose ``requirement`` points at a
    # REQ that no longer exists, a design mapping's ``design_elements`` member
    # with no matching DES, an upstream REQ/DES ref that was never allocated).
    # promote enforces this at stitch time, but the direct-edit path bypasses
    # promote, so render re-validates the edited doc here — the bookend of the
    # edit channel — failing loud (§24.2) before resyncing a mirror over a
    # malformed artifact. This is the realization of issue 06's criterion 4
    # (reference-integrity still runs) for the direct-edit path; the freeze-gate
    # coverage precheck handles the coverage half at the subsequent freeze.
    validate_artifact_refs(artifact, doc, feature_root)

    md_name = artifact_md_file(artifact)
    md_path = feature_root / md_name
    md_path.write_text(_ARTIFACT_RENDERER[artifact](feature_id, doc))

    append_audit_event(
        feature_root,
        event=_RENDER_EVENT,
        payload={
            "artifact": artifact,
            "feature": feature_id,
            "source": json_name,
            "mirror": md_name,
        },
        timestamp=timestamp,
        origin=origin,
    )

    return RenderResult(artifact=artifact, json_path=json_path, md_path=md_path)


def _doc_id_set(doc: Mapping[str, Any], field: str) -> set[str]:
    """The set of ``id`` strings under a doc's list ``field`` (defensive).

    Shared by the reference-integrity validators: the canonical ids a ref may
    legitimately point at within this doc (REQ ids, DES ids). Hand-edited docs
    may carry non-mapping entries or non-string ids — skip them rather than
    failing (a malformed entry is the renderer's concern, not a ref violation).
    """
    out: set[str] = set()
    for entry in doc.get(field, []) or []:
        if isinstance(entry, Mapping):
            value = entry.get("id")
            if isinstance(value, str) and value:
                out.add(value)
    return out


def _upstream_id_set(feature_root: Path, json_name: str, field: str) -> set[str] | None:
    """Read a frozen upstream doc's id set, or ``None`` if it is not readable.

    Best-effort cross-doc reference-integrity: a design's ``requirement`` refs
    resolve against the frozen requirements upstream, a task's REQ/DES refs
    against the frozen requirements + design. Returns ``None`` (→ the caller
    skips cross-doc validation) when the upstream file is absent/unreadable, so a
    render never fails merely because an upstream read broke for an unrelated
    reason. Read directly (not via the frozen-guard reader) so validation does
    not conflate "not frozen" with "dangling ref".
    """
    doc = read_json_object(feature_root / json_name)
    if doc is None:
        return None
    return _doc_id_set(doc, field)


class _RefSpec(NamedTuple):
    """One reference-integrity check on a (possibly hand-edited) canonical doc.

    The table-driven core of :func:`validate_artifact_refs` (mirrors the
    ``_ARTIFACT_BUILDER`` table-driven shape candidate 4 gave promote): each row
    says - walk ``walk_field``; on each mapping entry, read ``ref_field`` (a
    scalar or a list of refs per ``cardinality``); every non-empty string ref
    must resolve against the known ids sourced from this doc (``known_json`` is
    ``None`` -> intra-doc ``known_field``) or a frozen upstream (``known_json`` +
    ``known_field``). The message bits reproduce the existing per-context
    diagnostics byte-for-byte via :func:`_ref_violation_msg`. Adding a ref field
    is one row here, not a new ``if/elif`` branch.
    """

    walk_field: str
    ref_field: str
    cardinality: str  # "scalar" | "list"
    known_field: str  # doc field holding the target ids
    known_json: str | None  # upstream json filename, or None for intra-doc
    subject_label: str  # "acceptance criterion" | "design mapping" | "task"
    subject_id_field: str  # entry field shown as the subject ("id" | "key")
    ref_prefix: str  # "" | "design element " | "requirement "
    target_phrase: str  # "a requirement id in this doc" | "a frozen design id" | ...
    known_label: str | None  # None = omit known list; else "known" | "known REQ" | "known DES"


# Per-artifact reference-integrity checks. Each row is one ref-kind; the walker
# below is generic. ``known_json is None`` marks an intra-doc check (always run);
# a frozen-upstream check is best-effort (skipped if the upstream file is
# unreadable). REQ refs in requirements and DES refs in design are intra-doc;
# cross-stage REQ/DES refs resolve against the frozen upstream artifact.
_REF_SPECS: Mapping[str, tuple[_RefSpec, ...]] = {
    REQUIREMENTS_ARTIFACT: (
        _RefSpec(
            walk_field="acceptance_criteria", ref_field="requirement",
            cardinality="scalar",
            known_field="requirements", known_json=None,
            subject_label="acceptance criterion", subject_id_field="id",
            ref_prefix="", target_phrase="a requirement id in this doc",
            known_label="known",
        ),
    ),
    DESIGN_ARTIFACT: (
        _RefSpec(
            walk_field="requirement_mapping", ref_field="requirement",
            cardinality="scalar",
            known_field="requirements", known_json=REQUIREMENTS_JSON,
            subject_label="design mapping", subject_id_field="key",
            ref_prefix="", target_phrase="a frozen requirement id",
            known_label="known REQ",
        ),
        _RefSpec(
            walk_field="requirement_mapping", ref_field="design_elements",
            cardinality="list",
            known_field="design_elements", known_json=None,
            subject_label="design mapping", subject_id_field="key",
            ref_prefix="design element ", target_phrase="defined in this doc",
            known_label="known DES",
        ),
    ),
    TASKS_ARTIFACT: (
        _RefSpec(
            walk_field="tasks", ref_field="related_requirements",
            cardinality="list",
            known_field="requirements", known_json=REQUIREMENTS_JSON,
            subject_label="task", subject_id_field="id",
            ref_prefix="requirement ", target_phrase="a frozen requirement id",
            known_label=None,
        ),
        _RefSpec(
            walk_field="tasks", ref_field="related_design",
            cardinality="list",
            known_field="design_elements", known_json=DESIGN_JSON,
            subject_label="task", subject_id_field="id",
            ref_prefix="design element ", target_phrase="a frozen design id",
            known_label=None,
        ),
    ),
}


def _known_ids(
    spec: _RefSpec, doc: Mapping[str, Any], feature_root: Path
) -> set[str] | None:
    """The known-id set for ``spec``'s target, or ``None`` if upstream is unreadable.

    Intra-doc (``known_json is None``) reads ids from this doc via
    :func:`_doc_id_set` (always returns a set). Upstream reads a frozen artifact
    via :func:`_upstream_id_set` (``None`` when absent/unreadable -> caller skips).
    """
    if spec.known_json is None:
        return _doc_id_set(doc, spec.known_field)
    return _upstream_id_set(feature_root, spec.known_json, spec.known_field)


def _iter_refs(value: Any, cardinality: str) -> list[Any]:
    """Yield the ref(s) held in a ``ref_field`` value (scalar -> one, list -> many).

    A scalar field yields the single value; a list field yields its members
    (``None``/missing -> ``[]``, matching the prior ``... or []`` coercion). The
    walker's ``isinstance(ref, str) and ref`` filter then ignores non-string /
    empty members for both cardinalities - the same shape the per-branch code had.
    """
    if cardinality == "scalar":
        return [value]
    return value or []


def _ref_violation_msg(
    spec: _RefSpec, entry: Mapping[str, Any], ref: str, known: set[str], artifact: str
) -> str:
    """Reproduce the existing per-context violation message byte-for-byte.

    One template; the per-row bits (subject label + id field, ref prefix, target
    phrase, known-list label) carry the variation the 3-branch ``if/elif`` spelled
    inline. ``known_label is None`` omits the known list (the tasks-stage messages).
    """
    subject = f"{spec.subject_label} {entry.get(spec.subject_id_field, '?')!r}"
    ref_phrase = f"{spec.ref_prefix}{ref!r}"
    if spec.known_label is None:
        known_phrase = ""
    else:
        known_phrase = f" ({spec.known_label}: {sorted(known) or '(none)'})"
    return (
        f"{subject} references {ref_phrase}, which is not {spec.target_phrase}"
        f"{known_phrase}; the edited {artifact} artifact is malformed (ADR-0008 D3)"
    )


def validate_artifact_refs(
    artifact: str, doc: Mapping[str, Any], feature_root: Path
) -> None:
    """Re-check reference-integrity on a (possibly hand-edited) canonical doc.

    The direct-edit companion to promote's stitch-time :class:`RefResolver`
    check (ADR-0008 D3): every reference in the doc must resolve to a real id —
    an intra-doc id (an AC's ``requirement`` → a REQ in this doc; a design
    mapping's ``design_elements`` → a DES in this doc) or, for cross-stage refs,
    a real id in the frozen upstream (a design's ``requirement`` → a frozen REQ;
    a task's ``related_requirements`` / ``related_design`` → frozen REQ / DES).
    A dangling ref raises :class:`UnresolvedRefError` (fail loud, §24.2) — the
    mirror is not resynced over a malformed artifact.

    Cross-doc validation is best-effort (:func:`_upstream_id_set`): if an
    upstream doc is unreadable its refs are skipped, so the check never fails for
    an unrelated reason. Intra-doc refs are always checked (the ids live in the
    same doc). Tasks have no intra-doc refs (their refs are all upstream).
    """
    specs = _REF_SPECS.get(artifact, ())
    if not specs:
        # An unknown / non-renderable artifact has no refs to validate; render's
        # own guard rejects it before reaching here, so this is a quiet no-op.
        return
    for spec in specs:
        known = _known_ids(spec, doc, feature_root)
        if known is None:
            continue  # best-effort: an unreadable upstream skips this ref-kind
        for entry in doc.get(spec.walk_field, []) or []:
            if not isinstance(entry, Mapping):
                continue
            for ref in _iter_refs(entry.get(spec.ref_field), spec.cardinality):
                if isinstance(ref, str) and ref and ref not in known:
                    raise UnresolvedRefError(
                        _ref_violation_msg(spec, entry, ref, known, artifact)
                    )


# artifact -> sole stage renderer (resolved lazily after the renderers are
# defined, so this table sits next to the render shell that uses it). Each is
# the *same* renderer promote calls, so a direct edit + render matches a
# re-promote's mirror byte-for-byte.
_ARTIFACT_RENDERER: Mapping[str, Callable[[str, Mapping[str, Any]], str]] = {
    REQUIREMENTS_ARTIFACT: render_requirements_md,
    DESIGN_ARTIFACT: render_design_md,
    TASKS_ARTIFACT: render_tasks_md,
}


# ---------------------------------------------------------------------------
# Table-driven promote dispatch (mirrors render_artifact's table-driven shape).
#
# The three ``promote_*`` shells above differ only in (a) the build core, (b)
# which §4.2 artifacts the frozen guard checks, and (c) an optional post-build
# write (tasks seeds task-status + populates the lane-graph). ``_ARTIFACT_BUILDER``
# is the per-stage table carrying exactly those three varying bits - the json/md
# filenames and the renderer come from the existing render-shared tables
# (``artifact_json_file`` / ``artifact_md_file`` / ``_ARTIFACT_RENDERER``), so the
# builder carries no duplicated metadata. ``_promote_artifact`` is the single
# write/render/audit shell each public ``promote_*`` delegates to - the
# promote-leg analogue of ``render_artifact``'s table-driven dispatch.
# ---------------------------------------------------------------------------


class _ArtifactBuilder(NamedTuple):
    """Per-stage promote descriptor (the table-driven core, mirrors render_artifact).

    Each renderable planning stage (requirements / design / tasks) is one row:
    the build core (id-allocate + stitch, wrapped to a uniform ``(doc, allocated,
    ctx)`` 3-tuple so the 2-tuple req/design cores and the 4-tuple tasks core
    share one shell), the §4.2 artifacts the frozen guard checks (tasks guards
    ``tasks`` and ``lane_graph`` together - they freeze at the same gate), and
    the optional post-build writes (tasks only: seed ``task-status.yml`` +
    populate ``04-lane-graph.yml``; req/design have ``None``). Adding a planning
    stage is one row here + one delegator, not a copy-pasted shell.
    """

    build: Callable[..., tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]]
    frozen_artifacts: tuple[str, ...]
    post_write: Callable[[Path, Mapping[str, Any], Mapping[str, Any]], None] | None


def _build_requirements_artifact(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    origin: str | None,
    timestamp: str | None,
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]:
    doc, allocated = build_canonical_requirements(
        feature_root, feature_id, proposal, origin=origin, timestamp=timestamp
    )
    return doc, allocated, {}


def _build_design_artifact(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    origin: str | None,
    timestamp: str | None,
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]:
    doc, allocated = build_canonical_design(
        feature_root, feature_id, proposal, origin=origin, timestamp=timestamp
    )
    return doc, allocated, {}


def _build_tasks_artifact(
    feature_root: Path,
    feature_id: str,
    proposal: Mapping[str, Any],
    *,
    origin: str | None,
    timestamp: str | None,
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]:
    doc, allocated, task_status_rows, verification_commands = build_canonical_tasks(
        feature_root, feature_id, proposal, origin=origin, timestamp=timestamp
    )
    return doc, allocated, {
        "task_status_rows": task_status_rows,
        "verification_commands": verification_commands,
    }


def _write_tasks_extras(
    feature_root: Path, doc: Mapping[str, Any], ctx: Mapping[str, Any]
) -> None:
    """Tasks-only post-build writes: seed task-status + populate the lane-graph.

    The two deterministic writes ``promote_tasks`` does beyond the shared
    json+md+audit shell (ADR-0008 D2): seed ``status/task-status.yml`` (every
    task ``pending``) and populate the single lane in ``04-lane-graph.yml``
    (purpose/tasks/files + the Planner-generated verify commands). Both run
    after the canonical json+md write and before the audit record, matching the
    pre-refactor ordering byte-for-byte.
    """
    _seed_task_status(feature_root, ctx["task_status_rows"])
    _populate_lane_graph_from_doc(
        feature_root, doc, verification_commands=ctx["verification_commands"]
    )


def _frozen_promote_message(frozen_artifacts: tuple[str, ...]) -> str:
    """The §4.2 frozen-guard message for a stage's guarded artifacts.

    Reproduces the two pre-refactor messages byte-for-byte: a single-artifact
    stage (requirements / design) says ``artifact 'requirements' is frozen;
    promote may only overwrite an unfrozen artifact (...)``; tasks (which guards
    ``tasks`` and ``lane_graph`` together) says ``artifact 'tasks'/'lane_graph'
    is frozen; promote may only overwrite unfrozen artifacts (...)``.
    """
    names = "/".join(repr(a) for a in frozen_artifacts)
    tail = (
        "promote may only overwrite unfrozen artifacts"
        if len(frozen_artifacts) > 1
        else "promote may only overwrite an unfrozen artifact"
    )
    return (
        f"artifact {names} is frozen; {tail} (use a Change Proposal to change a "
        f"frozen one, §4.2/§17)"
    )


_ARTIFACT_BUILDER: Mapping[str, _ArtifactBuilder] = {
    REQUIREMENTS_ARTIFACT: _ArtifactBuilder(
        build=_build_requirements_artifact,
        frozen_artifacts=(REQUIREMENTS_ARTIFACT,),
        post_write=None,
    ),
    DESIGN_ARTIFACT: _ArtifactBuilder(
        build=_build_design_artifact,
        frozen_artifacts=(DESIGN_ARTIFACT,),
        post_write=None,
    ),
    TASKS_ARTIFACT: _ArtifactBuilder(
        build=_build_tasks_artifact,
        frozen_artifacts=(TASKS_ARTIFACT, LANE_GRAPH_ARTIFACT),
        post_write=_write_tasks_extras,
    ),
}


def _promote_artifact(
    feature_root: Path,
    feature_id: str,
    artifact: str,
    proposal: Mapping[str, Any],
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> PromoteResult:
    """The shared write/render/audit shell over a build core (table-driven).

    The promote-leg analogue of :func:`render_artifact`: one shell dispatched by
    the ``_ARTIFACT_BUILDER`` table, so the three public ``promote_*`` functions
    are thin delegators (signatures unchanged) rather than three copy-pasted
    shells. Runs the §4.2 frozen guard (refuses a frozen artifact - the canonical
    state is the human gate's to freeze), calls the stage's build core to
    id-allocate + stitch (reference-integrity, D3 - fails loud, writes nothing),
    writes the canonical ``.json`` + renders the ``.md`` mirror (the sole md
    renderer), runs the optional post-build writes (tasks), and appends one
    ``promote`` audit record carrying the allocated ids. ``artifact`` is always a
    known key (the delegators pass the stage's constant), so the table lookup is
    direct.
    """
    if not feature_id:
        raise ValueError("feature_id must be a non-empty string")

    builder = _ARTIFACT_BUILDER[artifact]
    frozen = frozen_artifacts_status(feature_root)
    if any(frozen.get(a) for a in builder.frozen_artifacts):
        raise FrozenArtifactWriteError(_frozen_promote_message(builder.frozen_artifacts))

    doc, allocated, ctx = builder.build(
        feature_root, feature_id, proposal, origin=origin, timestamp=timestamp
    )

    json_path = feature_root / artifact_json_file(artifact)
    md_path = feature_root / artifact_md_file(artifact)
    write_json(json_path, doc)
    md_path.write_text(_ARTIFACT_RENDERER[artifact](feature_id, doc))
    if builder.post_write is not None:
        builder.post_write(feature_root, doc, ctx)

    append_audit_event(
        feature_root,
        event=_PROMOTE_EVENT,
        payload={
            "stage": artifact,
            "artifact": artifact,
            "feature": feature_id,
            "allocated": allocated,
        },
        timestamp=timestamp,
        origin=origin,
    )
    return PromoteResult(
        stage=artifact,
        artifact=artifact,
        json_path=json_path,
        md_path=md_path,
        allocated={k: tuple(v) for k, v in allocated.items()},
    )
