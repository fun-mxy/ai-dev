"""``ai-dev project-github`` — basic GitHub projection (v0.5 ticket 07, ADR-0006).

Every path is driven through a mocked ``gh`` (a scripted ``FakeGh``) so no real
network is touched — real GitHub evidence is ticket 08. The tests pin: idempotent
create-then-edit, the ``projections/github/mapping.json`` resume point, the
``--pr`` issues-only split, the pre-flight failures (token unset / gh missing /
rate-limit exhausted / PR missing), the mid-stream fail-loud + resume contract
(D4), the PR comment post/update, and the CLI / ``--dry-run`` surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.github_projection import (
    GITHUB_TOKEN_ENV,
    GhResult,
    MAPPING_JSON,
    project_github,
)
from ai_dev.issue_bundle import ISSUES_DIR
from ai_dev.json_artifact import read_json_object, write_json


# ---------------------------------------------------------------------------
# A scripted `gh` stand-in: records every argv and returns canned results.
# ---------------------------------------------------------------------------


class FakeGh:
    """Records ``gh`` argv and returns scripted ``GhResult``s.

    * ``api rate_limit`` -> ok, ``remaining`` defaults to 5000 (settable).
    * ``pr view <N>`` -> ok (the PR "exists") unless ``pr_missing``.
    * ``issue create`` -> ok, returns a URL with the next monotonic number.
    * ``issue edit <N>`` -> ok unless that number is in ``fail_edit``.
    * ``pr comment <PR>`` / ``pr comment <PR> --edit <id>`` -> ok, returns a
      comment URL (create) or empty (edit).

    ``fail_create_on`` halts after the Nth create (mid-stream failure test):
    the issue is *not* recorded as pushed and its mapping entry is not written.
    """

    def __init__(
        self,
        *,
        rate_remaining: int = 5000,
        rate_fail: bool = False,
        pr_missing: bool = False,
        first_issue_number: int = 11,
        first_comment_id: int = 9001,
        fail_create_on: int | None = None,
        fail_edit: set[int] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._rate_remaining = rate_remaining
        self._rate_fail = rate_fail
        self._pr_missing = pr_missing
        self._next_number = first_issue_number
        self._next_comment = first_comment_id
        self._fail_create_on = fail_create_on
        self._creates = 0
        self._fail_edit = fail_edit or set()

    def __call__(self, argv: list[str]) -> GhResult:
        self.calls.append(list(argv))
        head = argv[0] if argv else ""
        if head == "api":
            if self._rate_fail:
                return GhResult(1, "", "rate_limit probe failed")
            return GhResult(0, str(self._rate_remaining), "")
        if head == "pr" and len(argv) > 1 and argv[1] == "view":
            if self._pr_missing:
                return GhResult(1, "", f"no pull requests found for #{argv[2]}")
            return GhResult(0, json.dumps({"number": int(argv[2])}), "")
        if head == "issue" and len(argv) > 1 and argv[1] == "create":
            self._creates += 1
            if self._fail_create_on is not None and self._creates == self._fail_create_on:
                return GhResult(1, "", "create failed (mid-stream)")
            number = self._next_number
            self._next_number += 1
            url = f"https://github.com/owner/repo/issues/{number}"
            return GhResult(0, url, "")
        if head == "issue" and len(argv) > 1 and argv[1] == "edit":
            number = int(argv[2])
            if number in self._fail_edit:
                return GhResult(1, "", f"edit #{number} failed")
            return GhResult(0, "", "")
        if head == "pr" and len(argv) > 1 and argv[1] == "comment":
            if "--edit" in argv:
                return GhResult(0, "", "")
            cid = self._next_comment
            self._next_comment += 1
            url = f"https://github.com/owner/repo/pull/{argv[2]}#issuecomment-{cid}"
            return GhResult(0, url, "")
        return GhResult(1, "", f"unexpected gh argv: {argv}")


# ---------------------------------------------------------------------------
# Staging helpers.
# ---------------------------------------------------------------------------


def _stage_feature(repo_root: Path, *, issues: int = 2, with_report: bool = True) -> str:
    """Create a feature run + N canonical ISSUE-NNN.json + optional final-report."""
    feature_id = create_feature_run(repo_root, "project github intent", origin="test")
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    issue_root = feature_root / ISSUES_DIR
    for i in range(1, issues + 1):
        issue = {
            "id": f"ISSUE-00{i}",
            "source": "reviewer" if i % 2 else "spec-gap",
            "severity": "P2" if i % 2 else "P1",
            "title": f"Sample issue {i}",
            "description": f"Body of issue {i}.",
            "evidence": [{"file": "src/app.py", "line": i * 10}],
            "recommendation": f"Fix issue {i}.",
        }
        write_json(issue_root / f"ISSUE-00{i}.json", issue)
    if with_report:
        (feature_root / "final-report.md").write_text("# Final Report\n\nverdict: pass\n")
        write_json(
            feature_root / "final-report.json",
            {"feature": feature_id, "verdict": "pass"},
        )
    return feature_id


def _mapping(repo_root: Path, feature_id: str) -> dict[str, Any]:
    path = (
        repo_root / ".ai-dev" / "features" / feature_id
        / "projections" / "github" / MAPPING_JSON
    )
    data = read_json_object(path)
    return data if isinstance(data, dict) else {}


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a (fake) GITHUB_TOKEN by name so pre-flight's name check passes.

    The value is a placeholder; the projection references the variable by name
    only and never reads the value (invariant #11). Cleared per-test so a
    pre-flight-failure test can delete it explicitly.
    """
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "ghp-fake-not-a-real-token")


# ---------------------------------------------------------------------------
# Happy path: create, then idempotent edit-in-place.
# ---------------------------------------------------------------------------


def test_first_projection_creates_issues_and_writes_mapping(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=2)
    gh = FakeGh()

    result = project_github(
        repo_root, feature_id, gh_runner=gh, gh_available=lambda: True
    )

    assert result.complete
    assert [i.issue_id for i in result.issues] == ["ISSUE-001", "ISSUE-002"]
    assert [i.action for i in result.issues] == ["created", "created"]
    assert [i.number for i in result.issues] == [11, 12]
    mapping = _mapping(repo_root, feature_id)
    assert mapping["issues"] == {"ISSUE-001": 11, "ISSUE-002": 12}
    assert mapping["pr_number"] is None
    # The created issue body carries the projection traceability marker.
    create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
    body = create_call[create_call.index("--body") + 1]
    assert f"feature={feature_id}" in body
    assert "**severity:** P2" in body  # ISSUE-001 stages as P2 (odd index)


def test_re_run_edits_in_place_no_duplicates(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=2)
    gh = FakeGh()
    project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: True)
    first_create_count = sum(1 for c in gh.calls if c[:2] == ["issue", "create"])

    # Second run with a fresh runner: every issue should be edited, not created.
    gh2 = FakeGh(first_issue_number=999)
    result = project_github(repo_root, feature_id, gh_runner=gh2, gh_available=lambda: True)

    assert result.complete
    assert [i.action for i in result.issues] == ["updated", "updated"]
    second_create_count = sum(1 for c in gh2.calls if c[:2] == ["issue", "create"])
    assert second_create_count == 0
    edit_count = sum(1 for c in gh2.calls if c[:2] == ["issue", "edit"])
    assert edit_count == 2
    # Mapping numbers unchanged (edits do not reassign).
    assert _mapping(repo_root, feature_id)["issues"] == {"ISSUE-001": 11, "ISSUE-002": 12}
    assert first_create_count == 2  # sanity: first run did create


# ---------------------------------------------------------------------------
# --pr split (D3): issues-only without it; PR comment + stored PR with it.
# ---------------------------------------------------------------------------


def test_without_pr_is_issues_only(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1, with_report=True)
    gh = FakeGh()
    result = project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: True)

    assert result.pr_number is None
    assert result.pr_comment_action is None
    assert not any(c[:2] == ["pr", "comment"] for c in gh.calls)


def test_with_pr_posts_comment_and_stores_pr(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    gh = FakeGh()
    result = project_github(
        repo_root, feature_id, pr_number=42, gh_runner=gh, gh_available=lambda: True
    )

    assert result.pr_number == 42
    assert result.pr_comment_action == "created"
    mapping = _mapping(repo_root, feature_id)
    assert mapping["pr_number"] == 42
    assert mapping["pr_comment_id"] == 9001
    # The comment body carries the final-report + the projection marker.
    comment_call = next(
        c for c in gh.calls if c[:2] == ["pr", "comment"] and "--edit" not in c
    )
    body = comment_call[comment_call.index("--body") + 1]
    assert "Final Report" in body
    assert f"feature={feature_id}" in body


def test_with_pr_re_run_updates_comment_in_place(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    gh = FakeGh()
    project_github(repo_root, feature_id, pr_number=42, gh_runner=gh, gh_available=lambda: True)

    gh2 = FakeGh()
    result = project_github(repo_root, feature_id, pr_number=42, gh_runner=gh2, gh_available=lambda: True)

    assert result.pr_comment_action == "updated"
    # Second run edits the stored comment id (no new comment created).
    edit_call = next(c for c in gh2.calls if c[:2] == ["pr", "comment"] and "--edit" in c)
    assert "--edit" in edit_call
    assert "9001" in edit_call


def test_pr_without_final_report_fails_loud(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1, with_report=False)
    gh = FakeGh()
    # Issues push fine; the PR comment step fail-louds on the missing report.
    result = project_github(
        repo_root, feature_id, pr_number=42, gh_runner=gh, gh_available=lambda: True
    )
    assert not result.complete
    assert result.failure_reason is not None
    assert "final-report" in result.failure_reason
    # Issues were still pushed + mapped (D4: keep successes).
    assert _mapping(repo_root, feature_id)["issues"] == {"ISSUE-001": 11}


# ---------------------------------------------------------------------------
# Pre-flight failures (D4): no pushes on failure.
# ---------------------------------------------------------------------------


def test_preflight_token_unset_no_pushes(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    gh = FakeGh()
    result = project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: True)

    assert not result.complete
    assert GITHUB_TOKEN_ENV in (result.failure_reason or "")
    # No gh push happened (only the pre-flight would have run, which failed at
    # the token check before even the rate-limit probe).
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)
    # No mapping written.
    assert _mapping(repo_root, feature_id) == {}


def test_preflight_gh_missing_no_pushes(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    gh = FakeGh()
    result = project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: False)

    assert "gh` CLI not found" in (result.failure_reason or "")
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)


def test_preflight_rate_limit_exhausted_no_pushes(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    gh = FakeGh(rate_remaining=0)
    result = project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: True)

    assert "rate limit" in (result.failure_reason or "").lower()
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)


def test_preflight_rate_probe_fails_no_pushes(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    gh = FakeGh(rate_fail=True)
    result = project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: True)

    assert "rate-limit probe failed" in (result.failure_reason or "")


def test_preflight_pr_missing_no_pushes(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    gh = FakeGh(pr_missing=True)
    result = project_github(
        repo_root, feature_id, pr_number=99, gh_runner=gh, gh_available=lambda: True
    )

    assert "PR #99" in (result.failure_reason or "")
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)


# ---------------------------------------------------------------------------
# Mid-stream failure + resume (D4): stop, keep successes, resume from mapping.
# ---------------------------------------------------------------------------


def test_mid_stream_failure_keeps_successes_and_resumes(repo_root: Path) -> None:
    feature_id = _stage_feature(repo_root, issues=3)
    # ISSUE-001 creates ok (#11), ISSUE-002's create fails mid-stream.
    gh = FakeGh(fail_create_on=2)
    result = project_github(repo_root, feature_id, gh_runner=gh, gh_available=lambda: True)

    assert not result.complete
    assert result.failure_reason is not None and "create" in result.failure_reason
    # ISSUE-001 was pushed + mapped; ISSUE-002 was not; ISSUE-003 never reached.
    mapping = _mapping(repo_root, feature_id)
    assert mapping["issues"] == {"ISSUE-001": 11}
    assert [i.issue_id for i in result.issues] == ["ISSUE-001"]
    # The failed + unstarted items are reported pending.
    assert "ISSUE-002" in result.pending
    assert "ISSUE-003" in result.pending

    # Resume: a fresh runner edits ISSUE-001 in place and creates ISSUE-002/003.
    gh2 = FakeGh(first_issue_number=200)
    resumed = project_github(repo_root, feature_id, gh_runner=gh2, gh_available=lambda: True)

    assert resumed.complete
    actions = {i.issue_id: i.action for i in resumed.issues}
    assert actions == {"ISSUE-001": "updated", "ISSUE-002": "created", "ISSUE-003": "created"}
    final_mapping = _mapping(repo_root, feature_id)
    assert final_mapping["issues"] == {"ISSUE-001": 11, "ISSUE-002": 200, "ISSUE-003": 201}


def test_missing_feature_fail_loud(repo_root: Path) -> None:
    with pytest.raises(ValueError, match="FEATURE-999"):
        project_github(repo_root, "FEATURE-999", gh_runner=FakeGh(), gh_available=lambda: True)


# ---------------------------------------------------------------------------
# CLI surface.
# ---------------------------------------------------------------------------


def test_cli_project_github_happy_path(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    # Patch default_gh_runner + gh availability so the CLI uses the fake.
    from ai_dev import github_projection as gp

    gh = FakeGh()
    orig_runner = gp.default_gh_runner
    orig_avail = gp._gh_available
    gp.default_gh_runner = gh
    gp._gh_available = lambda: True  # type: ignore[assignment]
    try:
        rc = main(["project-github", feature_id, "--pr", "7", "--repo-root", str(repo_root)])
    finally:
        gp.default_gh_runner = orig_runner
        gp._gh_available = orig_avail  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROJECT-GITHUB" in out
    assert "issues_created=['ISSUE-001']" in out
    assert "pr=7" in out


def test_cli_project_github_mid_stream_exit1(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feature_id = _stage_feature(repo_root, issues=2)
    from ai_dev import github_projection as gp

    gh = FakeGh(fail_create_on=1)
    orig_runner = gp.default_gh_runner
    orig_avail = gp._gh_available
    gp.default_gh_runner = gh
    gp._gh_available = lambda: True  # type: ignore[assignment]
    try:
        rc = main(["project-github", feature_id, "--repo-root", str(repo_root)])
    finally:
        gp.default_gh_runner = orig_runner
        gp._gh_available = orig_avail  # type: ignore[assignment]
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "resume from the mapping" in err


def test_cli_dry_run_no_network_no_mapping(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    feature_id = _stage_feature(repo_root, issues=2)
    # Make the non-network gh-on-PATH check deterministic (not machine-dependent).
    from ai_dev import dry_run as dr

    monkeypatch.setattr(dr, "_which_gh", lambda: True)
    rc = main(["project-github", feature_id, "--pr", "5", "--dry-run", "--repo-root", str(repo_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROJECT-GITHUB DRY-RUN" in out
    assert "2 create, 0 edit" in out
    assert "no network call" in out
    # Dry-run wrote no mapping.
    assert _mapping(repo_root, feature_id) == {}


def test_cli_dry_run_refused_when_token_unset(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    rc = main(["project-github", feature_id, "--dry-run", "--repo-root", str(repo_root)])
    out = capsys.readouterr().out
    assert rc == 0  # a "would be refused" is a successful dry-run answer
    assert "would be REFUSED" in out
    assert GITHUB_TOKEN_ENV in out


# ---------------------------------------------------------------------------
# Compute seam (public create-vs-edit split) + corrupt-mapping fail-loud.
# ---------------------------------------------------------------------------


def test_compute_plan_create_then_edit_split(repo_root: Path) -> None:
    """compute_github_plan: first call = all create; after a push = all edit."""
    from ai_dev.github_projection import compute_github_plan

    feature_id = _stage_feature(repo_root, issues=2)
    plan = compute_github_plan(repo_root, feature_id, pr_number=None)
    assert plan.would_create == ["ISSUE-001", "ISSUE-002"]
    assert plan.would_edit == []
    assert plan.pr_number is None

    # One real push records the mapping; re-planning now reports edits.
    project_github(repo_root, feature_id, gh_runner=FakeGh(), gh_available=lambda: True)
    plan2 = compute_github_plan(repo_root, feature_id, pr_number=None)
    assert plan2.would_create == []
    assert plan2.would_edit == ["ISSUE-001", "ISSUE-002"]


def test_compute_plan_pr_override_resolves_stored(repo_root: Path) -> None:
    """--pr overrides; without it the stored mapping PR applies."""
    from ai_dev.github_projection import compute_github_plan

    feature_id = _stage_feature(repo_root, issues=1)
    # Seed a stored PR via a first projection.
    project_github(
        repo_root, feature_id, pr_number=42, gh_runner=FakeGh(), gh_available=lambda: True
    )
    # No --pr on re-plan -> stored PR 42 still applies.
    assert compute_github_plan(repo_root, feature_id, pr_number=None).pr_number == 42
    # --pr overrides the stored value.
    assert compute_github_plan(repo_root, feature_id, pr_number=99).pr_number == 99


def test_corrupt_mapping_fails_loud_not_reset(repo_root: Path) -> None:
    """A present-but-corrupt mapping aborts (fail loud) instead of silently
    resetting (which would create duplicates — D4 forbids the silent path)."""
    feature_id = _stage_feature(repo_root, issues=1)
    mapping_path = (
        repo_root / ".ai-dev" / "features" / feature_id
        / "projections" / "github" / MAPPING_JSON
    )
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text("[1, 2, 3]\n")  # valid JSON, not an object
    with pytest.raises(ValueError, match="corrupt"):
        project_github(repo_root, feature_id, gh_runner=FakeGh(), gh_available=lambda: True)


def test_cli_corrupt_mapping_exit1(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feature_id = _stage_feature(repo_root, issues=1)
    mapping_path = (
        repo_root / ".ai-dev" / "features" / feature_id
        / "projections" / "github" / MAPPING_JSON
    )
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text('{"issues": "not-a-dict"}\n')
    rc = main(["project-github", feature_id, "--repo-root", str(repo_root)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "corrupt" in err


def test_dry_run_shows_edit_split_on_rerun(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry-run on a re-run reports edits (idempotency preview), not re-creates."""
    from ai_dev import dry_run as dr

    feature_id = _stage_feature(repo_root, issues=2)
    project_github(repo_root, feature_id, gh_runner=FakeGh(), gh_available=lambda: True)
    monkeypatch.setattr(dr, "_which_gh", lambda: True)
    rc = main(["project-github", feature_id, "--dry-run", "--repo-root", str(repo_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 create, 2 edit" in out
