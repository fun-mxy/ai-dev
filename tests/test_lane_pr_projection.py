"""``ai-dev project-lane-pr`` - lane PR projection (v0.7 ticket 05, ADR-0009 D5/D6).

Every path is driven through a mocked ``gh`` (a scripted ``FakeGh``) and a
mocked ``git push`` (a ``FakeGitPush``) so no real network or remote is touched
- real GitHub evidence is the v0.7 capstone (ticket 07). The tests pin: the
lane-gate-pass requirement (``proposed_done`` alone is insufficient), push +
create + mapping, the PR body fields, idempotent update (mapping reuse),
partial failure (push fail / pr-create fail) with successes kept, pre-flight
failures (token unset / gh missing / rate-limit exhausted), no token
persistence, the projection's non-mutation of lane/feature verdicts, the
compute seam, corrupt-mapping fail-loud, and the CLI / ``--dry-run`` surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_dev.cli import main
from ai_dev.github_projection import GITHUB_TOKEN_ENV, GhResult
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.lane_gate import (
    LANE_DECISION_JSON,
    evaluate_lane_gate,
)
from ai_dev.lane_pr_projection import (
    LANE_PR_MAPPING_JSON,
    LanePrProjectionResult,
    compute_lane_pr_plan,
    project_lane_pr,
)
from ai_dev.lane_worktree import LANE_WORKTREE_FILE
from ai_dev.paths import lane_dir
from ai_dev.shell_verifier import VERIFICATION_REPORT_JSON
from ai_dev.status import load_feature_status
from ai_dev.templates import DESIGN_JSON

# Reuse the lane-gate test staging: it stands up a frozen feature + a real
# implement run + review/spec-gap/verify/bundle artifacts. We then run
# ``evaluate_lane_gate`` to write a passing ``lane-decision.json`` and add a
# ``worktree.json`` so the projection has a branch to push.
from test_lane_gate import _stage_lane_gate_inputs  # noqa: E402
from test_implement_leg import _feature_root  # noqa: E402


# ---------------------------------------------------------------------------
# Scripted `gh` + `git push` stand-ins.
# ---------------------------------------------------------------------------


class FakeGh:
    """Records ``gh`` argv and returns scripted ``GhResult``s.

    * ``api rate_limit`` -> ok, ``remaining`` defaults to 5000 (settable).
    * ``repo view`` -> ok, returns the default branch name (``main`` by default).
    * ``pr create`` -> ok, returns a PR URL with the next monotonic number.
    * ``pr edit <N>`` -> ok unless ``fail_edit``.
    """

    def __init__(
        self,
        *,
        rate_remaining: int = 5000,
        rate_fail: bool = False,
        default_branch: str = "main",
        repo_view_fail: bool = False,
        first_pr_number: int = 7,
        fail_create: bool = False,
        fail_edit: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self._rate_remaining = rate_remaining
        self._rate_fail = rate_fail
        self._default_branch = default_branch
        self._repo_view_fail = repo_view_fail
        self._next_pr = first_pr_number
        self._fail_create = fail_create
        self._fail_edit = fail_edit

    def __call__(self, argv: list[str]) -> GhResult:
        self.calls.append(list(argv))
        head = argv[0] if argv else ""
        if head == "api" and len(argv) > 1 and argv[1] == "rate_limit":
            if self._rate_fail:
                return GhResult(1, "", "rate_limit probe failed")
            return GhResult(0, str(self._rate_remaining), "")
        if head == "repo" and len(argv) > 1 and argv[1] == "view":
            if self._repo_view_fail:
                return GhResult(1, "", "repo view failed")
            return GhResult(0, self._default_branch, "")
        if head == "pr" and len(argv) > 1 and argv[1] == "create":
            if self._fail_create:
                return GhResult(1, "", "pr create failed (mid-stream)")
            number = self._next_pr
            self._next_pr += 1
            url = f"https://github.com/owner/repo/pull/{number}"
            return GhResult(0, url, "")
        if head == "pr" and len(argv) > 1 and argv[1] == "edit":
            if self._fail_edit:
                return GhResult(1, "", "pr edit failed")
            return GhResult(0, "", "")
        return GhResult(1, "", f"unexpected gh argv: {argv}")


class FakeGitPush:
    """Records ``git push -u <remote> <branch>`` calls; returns a ``GhResult``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail = fail

    def __call__(self, remote: str, branch: str, repo_root: Path) -> GhResult:
        self.calls.append((remote, branch, str(repo_root)))
        if self._fail:
            return GhResult(1, "", "fake push failure (no remote configured)")
        return GhResult(0, "", "")


# ---------------------------------------------------------------------------
# Staging helpers.
# ---------------------------------------------------------------------------


_DESIGN_WITH_MAPPING = {
    "design_elements": [
        {"id": "DES-001", "key": "mod", "name": "answer module"},
        {"id": "DES-002", "key": "exp", "name": "usage example"},
    ],
    "requirement_mapping": [
        {"key": "m1", "requirement": "REQ-001", "design_elements": ["DES-001"]},
        {"key": "m2", "requirement": "REQ-002", "design_elements": ["DES-002"]},
    ],
}


def _write_worktree(repo_root: Path, feature_id: str, lane_id: str) -> str:
    """Write a ``worktree.json`` for the lane with a deterministic branch."""
    branch = f"ai-dev/{feature_id}/{lane_id}"
    write_json(
        lane_dir(repo_root, feature_id, lane_id) / LANE_WORKTREE_FILE,
        {
            "lane_id": lane_id,
            "feature_id": feature_id,
            "branch": branch,
            "base_ref": "HEAD",
            "path": str(
                repo_root / ".ai-dev" / "worktrees" / feature_id / lane_id
            ),
            "created_at": "2026-07-26T00:00:00Z",
            "updated_at": "2026-07-26T00:00:00Z",
            "lifecycle": "active",
            "clean": True,
        },
    )
    return branch


def _stage_passed_lane(
    repo_root: Path, *, with_design_mapping: bool = False
) -> tuple[str, str, str]:
    """Stand up a lane whose gate has PASSED, plus a ``worktree.json``.

    Returns ``(feature_id, lane_id, branch)``. Reuses the lane-gate test
    staging (frozen feature + implement/review/spec-gap/verify/bundle) and
    runs ``evaluate_lane_gate`` so ``lane-decision.json`` records a pass.
    """
    feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
    result = evaluate_lane_gate(repo_root, feature_id, lane_id)
    assert result.passed, f"staged lane gate should pass: {result.failed_conditions}"
    branch = _write_worktree(repo_root, feature_id, lane_id)
    if with_design_mapping:
        root = _feature_root(repo_root, feature_id)
        doc = {"feature": feature_id, "frozen": True, **_DESIGN_WITH_MAPPING}
        write_json(root / DESIGN_JSON, doc)
    return feature_id, lane_id, branch


def _stage_unpassed_lane(repo_root: Path) -> tuple[str, str]:
    """A lane with an implement-result (proposed_done) but NO lane-decision.

    The projection must refuse: ``proposed_done`` alone is insufficient
    (ADR-0009 D5).
    """
    feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
    _write_worktree(repo_root, feature_id, lane_id)
    return feature_id, lane_id


def _stage_failed_gate_lane(repo_root: Path) -> tuple[str, str]:
    """A lane whose gate evaluated to FAIL (failing verification)."""
    from test_lane_gate import _FAILING_RESULTS

    feature_id, lane_id = _stage_lane_gate_inputs(
        repo_root, verification_results=_FAILING_RESULTS
    )
    result = evaluate_lane_gate(repo_root, feature_id, lane_id)
    assert not result.passed
    _write_worktree(repo_root, feature_id, lane_id)
    return feature_id, lane_id


def _mapping(repo_root: Path, feature_id: str) -> dict[str, Any]:
    path = (
        repo_root / ".ai-dev" / "features" / feature_id
        / "projections" / "github" / LANE_PR_MAPPING_JSON
    )
    data = read_json_object(path)
    return data if isinstance(data, dict) else {}


def _pr_create_body(gh: FakeGh) -> str:
    """The ``--body`` argv passed to ``gh pr create``."""
    call = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    return call[call.index("--body") + 1]


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a (fake) GITHUB_TOKEN by name so pre-flight's name check passes."""
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghp-fake-not-a-real-token")


# ---------------------------------------------------------------------------
# Lane-gate-pass requirement (D5): proposed_done alone is insufficient.
# ---------------------------------------------------------------------------


def test_refuses_when_no_lane_decision(repo_root: Path) -> None:
    """No lane-decision.json -> refuse (proposed_done is not a gate pass)."""
    feature_id, lane_id = _stage_unpassed_lane(repo_root)
    with pytest.raises(ValueError, match="proposed_done"):
        compute_lane_pr_plan(repo_root, feature_id, lane_id)


def test_refuses_when_gate_failed(repo_root: Path) -> None:
    """A FAIL lane-decision must not project a PR."""
    feature_id, lane_id = _stage_failed_gate_lane(repo_root)
    with pytest.raises(ValueError, match="proposed_done"):
        compute_lane_pr_plan(repo_root, feature_id, lane_id)


def test_project_lane_pr_refuses_before_gate_pass_via_result(
    repo_root: Path,
) -> None:
    """``project_lane_pr`` surfaces the gate-pass refusal as a ValueError, not
    a silent result - it is a §24.2 precondition, not a side-effect failure."""
    feature_id, lane_id = _stage_unpassed_lane(repo_root)
    with pytest.raises(ValueError, match="proposed_done"):
        project_lane_pr(
            repo_root,
            feature_id,
            lane_id,
            gh_runner=FakeGh(),
            git_push=FakeGitPush(),
            gh_available=lambda: True,
        )


# ---------------------------------------------------------------------------
# Happy path: push + create + mapping.
# ---------------------------------------------------------------------------


def test_first_projection_pushes_creates_pr_and_writes_mapping(
    repo_root: Path,
) -> None:
    feature_id, lane_id, branch = _stage_passed_lane(repo_root)
    gh = FakeGh()
    push = FakeGitPush()

    result = project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh,
        git_push=push,
        gh_available=lambda: True,
    )

    assert result.complete
    assert result.pushed is True
    assert result.pr_action == "created"
    assert result.pr_number == 7
    assert result.pr_url == "https://github.com/owner/repo/pull/7"
    assert result.head_branch == branch
    assert result.base_branch == "main"  # detected via `gh repo view`
    assert result.remote == "origin"
    # The lane branch was pushed to origin.
    assert push.calls == [("origin", branch, str(repo_root))]
    # The mapping records lane -> PR number/URL/head/base/remote.
    mapping = _mapping(repo_root, feature_id)
    entry = mapping["lanes"][lane_id]
    assert entry["pr_number"] == 7
    assert entry["pr_url"] == "https://github.com/owner/repo/pull/7"
    assert entry["head_branch"] == branch
    assert entry["base_branch"] == "main"
    assert entry["remote"] == "origin"
    # Mapping carries no token (invariant #11).
    mapping_text = json.dumps(mapping)
    assert "ghp-fake" not in mapping_text
    assert "GITHUB_TOKEN" not in mapping_text


def test_explicit_base_branch_skips_detection(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    gh = FakeGh()
    result = project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        base_branch="develop",
        gh_runner=gh,
        git_push=FakeGitPush(),
        gh_available=lambda: True,
    )
    assert result.complete
    assert result.base_branch == "develop"
    # `gh repo view` was NOT called (base was given).
    assert not any(c[:2] == ["repo", "view"] for c in gh.calls)
    # The PR create argv names develop as the base.
    create_call = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert create_call[create_call.index("--base") + 1] == "develop"


# ---------------------------------------------------------------------------
# PR body fields (D5): lane/feature/task ids, REQ/AC/DES, gate verdict,
# verification summary, issue summary, worktree metadata, artifact pointers.
# ---------------------------------------------------------------------------


def test_pr_body_carries_all_required_fields(repo_root: Path) -> None:
    feature_id, lane_id, branch = _stage_passed_lane(
        repo_root, with_design_mapping=True
    )
    gh = FakeGh()
    project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh,
        git_push=FakeGitPush(),
        gh_available=lambda: True,
    )
    body = _pr_create_body(gh)

    # Lane / feature identity + projection marker (traceability).
    assert f"feature={feature_id}" in body
    assert f"lane={lane_id}" in body
    # Task ids (from the implement-result's declared tasks).
    assert "TASK-001" in body
    # Related REQ / AC (implement-result declarations).
    assert "REQ-001" in body
    assert "AC-001" in body
    # Related DES (02-design.json requirement_mapping -> REQ-001 -> DES-001).
    assert "DES-001" in body
    assert "DES-002" not in body  # REQ-002 is not this lane's
    # Lane gate verdict.
    assert "pass" in body.lower()
    # Verification summary (the staged verifier runs pytest -> pass 1/1).
    assert "1/1" in body or "1 / 1" in body
    # Worktree / branch metadata.
    assert branch in body
    # Canonical artifact pointers (relative paths under the feature root).
    assert "lane-decision.json" in body
    assert "implement-result.json" in body
    assert "verification-report.json" in body
    assert "issue-bundle.json" in body
    assert "worktree.json" in body
    # Projection disclaimer (one-way; no auto-merge - ADR-0009 D6/D7).
    assert "projection" in body.lower()
    assert "merge" in body.lower()


# ---------------------------------------------------------------------------
# Idempotent rerun: mapping reuse -> edit PR in place (no duplicate).
# ---------------------------------------------------------------------------


def test_re_run_pushes_and_edits_pr_in_place(repo_root: Path) -> None:
    feature_id, lane_id, branch = _stage_passed_lane(repo_root)
    gh = FakeGh()
    project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh,
        git_push=FakeGitPush(),
        gh_available=lambda: True,
    )
    first_create_count = sum(1 for c in gh.calls if c[:2] == ["pr", "create"])

    # Second run: branch re-pushed, PR edited in place (no new PR created).
    gh2 = FakeGh()
    push2 = FakeGitPush()
    result = project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh2,
        git_push=push2,
        gh_available=lambda: True,
    )

    assert result.complete
    assert result.pr_action == "updated"
    assert result.pr_number == 7  # unchanged
    assert push2.calls == [("origin", branch, str(repo_root))]  # re-pushed
    second_create_count = sum(1 for c in gh2.calls if c[:2] == ["pr", "create"])
    assert second_create_count == 0
    edit_calls = [c for c in gh2.calls if c[:2] == ["pr", "edit"]]
    assert len(edit_calls) == 1
    assert edit_calls[0][2] == "7"  # edits PR #7
    # Mapping number unchanged.
    assert _mapping(repo_root, feature_id)["lanes"][lane_id]["pr_number"] == 7
    assert first_create_count == 1  # sanity: first run did create


# ---------------------------------------------------------------------------
# Partial failure (D6): keep successful side effects, report pending, exit 1.
# ---------------------------------------------------------------------------


def test_push_failure_keeps_no_pr_and_reports_pending(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    gh = FakeGh()
    result = project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh,
        git_push=FakeGitPush(fail=True),
        gh_available=lambda: True,
    )

    assert not result.complete
    assert result.pushed is False
    assert "push" in result.pending
    assert result.failure_reason is not None
    assert "push" in result.failure_reason
    # No PR was created.
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)
    # No mapping entry recorded (the PR never existed).
    assert _mapping(repo_root, feature_id) == {}


def test_pr_create_failure_keeps_push_reports_pending(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    gh = FakeGh(fail_create=True)
    push = FakeGitPush()
    result = project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh,
        git_push=push,
        gh_available=lambda: True,
    )

    assert not result.complete
    assert result.pushed is True  # the push succeeded (kept)
    assert "pr" in result.pending
    assert result.failure_reason is not None
    assert "pr create" in result.failure_reason
    # The branch WAS pushed (successful side effect preserved).
    assert push.calls == [("origin", _branch_for(repo_root, feature_id, lane_id), str(repo_root))]
    # No mapping entry (PR create failed - nothing to record).
    assert _mapping(repo_root, feature_id) == {}
    # Resume: a fresh run creates the PR (push is idempotent).
    gh2 = FakeGh()
    resumed = project_lane_pr(
        repo_root,
        feature_id,
        lane_id,
        gh_runner=gh2,
        git_push=FakeGitPush(),
        gh_available=lambda: True,
    )
    assert resumed.complete
    assert resumed.pr_action == "created"


def _branch_for(repo_root: Path, feature_id: str, lane_id: str) -> str:
    return f"ai-dev/{feature_id}/{lane_id}"


# ---------------------------------------------------------------------------
# Pre-flight failures (D6): no pushes on failure.
# ---------------------------------------------------------------------------


def test_preflight_token_unset_no_pushes(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    gh = FakeGh()
    push = FakeGitPush()
    result = project_lane_pr(
        repo_root, feature_id, lane_id, gh_runner=gh, git_push=push,
        gh_available=lambda: True,
    )
    assert not result.complete
    assert GITHUB_TOKEN_ENV in (result.failure_reason or "")
    assert not push.calls
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)


def test_preflight_gh_missing_no_pushes(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    gh = FakeGh()
    push = FakeGitPush()
    result = project_lane_pr(
        repo_root, feature_id, lane_id, gh_runner=gh, git_push=push,
        gh_available=lambda: False,
    )
    assert "`gh` CLI not found" in (result.failure_reason or "")
    assert not push.calls


def test_preflight_rate_limit_exhausted_no_pushes(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    gh = FakeGh(rate_remaining=0)
    push = FakeGitPush()
    result = project_lane_pr(
        repo_root, feature_id, lane_id, gh_runner=gh, git_push=push,
        gh_available=lambda: True,
    )
    assert "rate limit" in (result.failure_reason or "").lower()
    assert not push.calls


# ---------------------------------------------------------------------------
# Projection failure does NOT mutate lane/feature verdicts (D6, invariant #10).
# ---------------------------------------------------------------------------


def test_projection_failure_does_not_mutate_verdicts(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    feature_root = _feature_root(repo_root, feature_id)
    lane_root = lane_dir(repo_root, feature_id, lane_id)

    # Snapshot the canonical verdict-bearing state before a failed projection.
    pre_feature = load_feature_status(feature_root)
    pre_decision = json.loads((lane_root / LANE_DECISION_JSON).read_text())

    # PR-create failure: a network side-effect fails mid-stream.
    result = project_lane_pr(
        repo_root, feature_id, lane_id, gh_runner=FakeGh(fail_create=True),
        git_push=FakeGitPush(), gh_available=lambda: True,
    )
    assert not result.complete

    # Feature status + lane decision are byte-for-byte unchanged (the projection
    # is one-way; GitHub state never writes back - ADR-0009 D6 / invariant #10).
    post_feature = load_feature_status(feature_root)
    post_decision = json.loads((lane_root / LANE_DECISION_JSON).read_text())
    assert post_feature == pre_feature
    assert post_decision == pre_decision
    # No projection-related audit event was appended (projection is not a gate).
    audit_text = (feature_root / "audit.log.json").read_text()
    assert "lane_pr" not in audit_text


# ---------------------------------------------------------------------------
# No token persistence (invariant #11): the mapping never carries a token.
# ---------------------------------------------------------------------------


def test_mapping_and_pr_body_carry_no_token(repo_root: Path) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    gh = FakeGh()
    project_lane_pr(
        repo_root, feature_id, lane_id, gh_runner=gh, git_push=FakeGitPush(),
        gh_available=lambda: True,
    )
    mapping_text = json.dumps(_mapping(repo_root, feature_id))
    assert "ghp-fake" not in mapping_text
    body = _pr_create_body(gh)
    assert "ghp-fake" not in body


# ---------------------------------------------------------------------------
# Missing feature / missing worktree fail loud (§24.2).
# ---------------------------------------------------------------------------


def test_missing_feature_fail_loud(repo_root: Path) -> None:
    with pytest.raises(ValueError, match="FEATURE-999"):
        project_lane_pr(
            repo_root, "FEATURE-999", "LANE-001", gh_runner=FakeGh(),
            git_push=FakeGitPush(), gh_available=lambda: True,
        )


def test_missing_worktree_fail_loud(repo_root: Path) -> None:
    """A passed lane gate without a worktree.json cannot project (no branch)."""
    feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
    result = evaluate_lane_gate(repo_root, feature_id, lane_id)
    assert result.passed
    # No worktree.json written -> the projection fail-louds.
    with pytest.raises(ValueError, match="worktree"):
        compute_lane_pr_plan(repo_root, feature_id, lane_id)


# ---------------------------------------------------------------------------
# Compute seam (pure create-vs-edit split) + corrupt-mapping fail-loud.
# ---------------------------------------------------------------------------


def test_compute_plan_create_then_update_split(repo_root: Path) -> None:
    feature_id, lane_id, branch = _stage_passed_lane(repo_root)
    plan = compute_lane_pr_plan(repo_root, feature_id, lane_id)
    assert plan.would_create_pr is True
    assert plan.existing_pr_number is None
    assert plan.head_branch == branch
    assert plan.lane_gate_passed is True

    # One real projection records the mapping; re-planning now reports an update.
    project_lane_pr(
        repo_root, feature_id, lane_id, gh_runner=FakeGh(),
        git_push=FakeGitPush(), gh_available=lambda: True,
    )
    plan2 = compute_lane_pr_plan(repo_root, feature_id, lane_id)
    assert plan2.would_create_pr is False
    assert plan2.existing_pr_number == 7


@pytest.mark.parametrize(
    "corrupt_body, expected",
    [
        ("[1, 2, 3]\n", "not a JSON object"),  # valid JSON, not an object
        ("{not json\n", "unparseable JSON"),  # syntactically invalid JSON
    ],
)
def test_corrupt_mapping_fails_loud_not_reset(
    repo_root: Path, corrupt_body: str, expected: str
) -> None:
    """A present-but-corrupt mapping aborts rather than silently resetting
    (which would create a duplicate PR - D6 forbids the silent path). Both a
    non-object JSON body and unparseable JSON surface as a fail-loud ``corrupt``
    error with a precise reason (mirrors ``github_projection._load_mapping``)."""
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    mapping_path = (
        repo_root / ".ai-dev" / "features" / feature_id
        / "projections" / "github" / LANE_PR_MAPPING_JSON
    )
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(corrupt_body)
    with pytest.raises(ValueError, match="corrupt") as excinfo:
        compute_lane_pr_plan(repo_root, feature_id, lane_id)
    # The precise reason is in the message so an operator can tell a hand-edit
    # mishap (not an object) from a truncated write (unparseable).
    assert expected in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI surface.
# ---------------------------------------------------------------------------


def test_cli_project_lane_pr_happy_path(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    from ai_dev import lane_pr_projection as lpp

    gh = FakeGh()
    push = FakeGitPush()
    orig_gh = lpp.default_gh_runner
    orig_push = lpp.default_git_push
    orig_avail = lpp._gh_available
    lpp.default_gh_runner = gh
    lpp.default_git_push = push
    lpp._gh_available = lambda: True  # type: ignore[assignment]
    try:
        rc = main([
            "project-lane-pr", feature_id, lane_id,
            "--base", "main", "--repo-root", str(repo_root),
        ])
    finally:
        lpp.default_gh_runner = orig_gh
        lpp.default_git_push = orig_push
        lpp._gh_available = orig_avail  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROJECT-LANE-PR" in out
    assert f"lane={lane_id}" in out
    assert "pr=7" in out
    assert "created" in out


def test_cli_project_lane_pr_mid_stream_exit1(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    from ai_dev import lane_pr_projection as lpp

    gh = FakeGh(fail_create=True)
    push = FakeGitPush()
    orig_gh = lpp.default_gh_runner
    orig_push = lpp.default_git_push
    orig_avail = lpp._gh_available
    lpp.default_gh_runner = gh
    lpp.default_git_push = push
    lpp._gh_available = lambda: True  # type: ignore[assignment]
    try:
        rc = main([
            "project-lane-pr", feature_id, lane_id,
            "--base", "main", "--repo-root", str(repo_root),
        ])
    finally:
        lpp.default_gh_runner = orig_gh
        lpp.default_git_push = orig_push
        lpp._gh_available = orig_avail  # type: ignore[assignment]
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "resume" in err


def test_cli_dry_run_no_network_no_mapping(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    from ai_dev import dry_run as dr

    monkeypatch.setattr(dr, "_which_gh", lambda: True)
    rc = main([
        "project-lane-pr", feature_id, lane_id,
        "--dry-run", "--repo-root", str(repo_root),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROJECT-LANE-PR DRY-RUN" in out
    assert "would push" in out.lower() or "push" in out.lower()
    assert "no network call" in out
    # Dry-run wrote no mapping.
    assert _mapping(repo_root, feature_id) == {}


def test_cli_dry_run_refused_when_token_unset(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    feature_id, lane_id, _ = _stage_passed_lane(repo_root)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    from ai_dev import dry_run as dr

    monkeypatch.setattr(dr, "_which_gh", lambda: True)
    rc = main([
        "project-lane-pr", feature_id, lane_id,
        "--dry-run", "--repo-root", str(repo_root),
    ])
    out = capsys.readouterr().out
    assert rc == 0  # a "would be refused" is a successful dry-run answer
    assert "would be REFUSED" in out
    assert GITHUB_TOKEN_ENV in out


def test_cli_dry_run_refuses_before_gate_pass(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry-run on an un-passed lane surfaces the gate-pass refusal as exit 1
    (a §24.2 precondition, not a would-be-refused answer)."""
    feature_id, lane_id = _stage_unpassed_lane(repo_root)
    from ai_dev import dry_run as dr

    monkeypatch.setattr(dr, "_which_gh", lambda: True)
    rc = main([
        "project-lane-pr", feature_id, lane_id,
        "--dry-run", "--repo-root", str(repo_root),
    ])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "proposed_done" in err
