"""implement_leg - v0.2 ticket 01, the Implementer leg.

The Implementer leg is the first half of the v0.2 loop (§26.3): from a feature
run whose tasks + lane-graph are frozen, build an Implementer input package
(task text from ``03-tasks.md``, allowed-files from ``04-lane-graph.yml``'s
expected/exclusive files), reuse the v0.1 ``prepare_run`` / ``run_headless`` /
``validate_run`` to run one implement run, write the run's task status back to
canonical ``task-status.yml`` as ``proposed_done`` (deterministic runtime only,
§4.3), and roll the run's result + metadata up into lane-level
``implement-result.{md,json}`` (§4.4).

These tests pin the three seams the ticket names: input-package assembly from
frozen artifacts, the ``proposed_done`` writeback, and the ``implement-result``
rollup - plus an orchestration test that wires prepare -> run -> validate ->
writeback -> rollup with a fake ``claude`` (mirroring the v0.1 e2e).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Callable

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.implement_leg import (
    IMPLEMENT_RESULT_JSON,
    IMPLEMENT_RESULT_MD,
    ImplementerLegResult,
    build_implementer_input_package,
    lane_allowed_files,
    read_lane_entry,
    read_task_text,
    run_implementer_leg,
    write_implement_result,
)
from ai_dev.paths import lane_dir, run_dir
from ai_dev.profiles import load_profile
from ai_dev.run_prepare import (
    ALLOWED_FILES_FILE,
    ROLE_FILE,
    TASK_PACKAGE_FILE,
    prepare_run,
)
from ai_dev.status import TASK_STATUS_FILE, freeze_artifact, mark_task_proposed_done
from ai_dev.templates import LANE_GRAPH_YML, TASKS_MD
from ai_dev.validate import validate_run

_TASK_BODY = "Create workspace/hello.py defining answer() returning 42."
_EXPECTED = ["workspace/hello.py"]
_EXCLUSIVE = ["workspace/hello.py"]


def _feature_root(repo_root: Path, feature_id: str) -> Path:
    return repo_root / ".ai-dev" / "features" / feature_id


def _fill_artifacts(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    task_body: str = _TASK_BODY,
    expected_files: list[str] | None = None,
    exclusive_files: list[str] | None = None,
    tasks: list[str] | None = None,
) -> None:
    """Overwrite the seeded 03-tasks.md + 04-lane-graph.yml with filled content.

    ``create_feature_run`` seeds both as empty placeholders; the v0.2 implementer
    leg consumes *filled* frozen artifacts, so tests stand up realistic content
    (a Planner would do this at the task gate, §9.1/§18.3).
    """
    root = _feature_root(repo_root, feature_id)
    (root / TASKS_MD).write_text(
        f"# Tasks - {feature_id}\n"
        f"\n"
        f"Frozen: false\n"
        f"\n"
        f"> Canonical task state lives in `status/task-status.yml`.\n"
        f"\n"
        f"## Tasks (TASK-NNN)\n"
        f"\n"
        f"{task_body}\n"
    )
    lane_graph = {
        "feature": feature_id,
        "frozen": False,
        "lanes": [
            {
                "id": lane_id,
                "purpose": "Implement the run contract",
                "tasks": tasks if tasks is not None else [],
                "depends_on": [],
                "expected_files": expected_files if expected_files is not None else [],
                "exclusive_files": exclusive_files if exclusive_files is not None else [],
                "provides": [],
                "consumes": [],
                "verification_scope": [],
                "merge_policy": {
                    "auto_merge": False,
                    "allowed_mechanical_resolutions": [],
                    "semantic_conflict_policy": "human_triage",
                },
            }
        ],
    }
    with (root / LANE_GRAPH_YML).open("w") as f:
        yaml.safe_dump(
            lane_graph,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


def _seed_frozen_feature(
    repo_root: Path,
    *,
    task_body: str = _TASK_BODY,
    expected_files: list[str] | None = None,
    exclusive_files: list[str] | None = None,
    tasks: list[str] | None = None,
    freeze: bool = True,
) -> tuple[str, str]:
    """Create a feature run, fill its tasks/lane-graph, and freeze them.

    Returns ``(feature_id, lane_id)``. The first feature run's first lane is
    always ``FEATURE-001`` / ``LANE-001`` (the allocator is monotonic from 001),
    so tests can assert on the concrete ids. ``freeze=False`` leaves the
    artifacts unfrozen to exercise the frozen-precondition guard.
    """
    feature_id = create_feature_run(repo_root, "implementer leg test")
    lane_id = "LANE-001"
    _fill_artifacts(
        repo_root,
        feature_id,
        lane_id,
        task_body=task_body,
        expected_files=expected_files,
        exclusive_files=exclusive_files,
        tasks=tasks,
    )
    if freeze:
        root = _feature_root(repo_root, feature_id)
        freeze_artifact(root, "tasks")
        freeze_artifact(root, "lane_graph")
    return feature_id, lane_id


def _read_allowed(path: Path) -> set[str]:
    entries: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


# ---------------------------------------------------------------------------
# Fake claude (shared with the v0.1 e2e): writes the §13.1 outputs + a workspace
# file, prints one stream-json result line, exits 0. ``__TASK_ID__`` is the id
# the fake declares proposed_done in result.json (default TASK-001); tests vary
# it to exercise the lane-scoping guard.
# ---------------------------------------------------------------------------
_FAKE_CLAUDE = """\
#!__PY__
import json, os, sys
os.makedirs("workspace", exist_ok=True)
os.makedirs("output", exist_ok=True)
with open("workspace/hello.py", "w") as f:
    f.write("# throwaway prototype module\\n")
with open("output/result.md", "w") as f:
    f.write("Wrote workspace/hello.py for the run.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Wrote workspace/hello.py for the run.",
            "tasks": [
                {"id": "__TASK_ID__", "status": "proposed_done",
                 "evidence": ["workspace/hello.py"]}
            ],
            "related_requirements": ["REQ-001"],
            "related_acceptance_criteria": ["AC-001"],
            "known_issues": [],
            "change_proposals": [],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""


def _write_fake_claude(bin_dir: Path, *, task_id: str = "TASK-001") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        _FAKE_CLAUDE.replace("__PY__", sys.executable).replace(
            "__TASK_ID__", task_id
        )
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ===========================================================================
# Seam 1: input-package assembly from frozen artifacts.
# ===========================================================================


class TestBuildImplementerInputPackage:
    """The implementer input package is assembled from frozen artifacts, not
    invented: task text from ``03-tasks.md``, allowed-files from the lane's
    expected/exclusive files, role pinned to ``Implementer``."""

    def test_task_text_flows_from_frozen_tasks_md(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root)

        run_id = build_implementer_input_package(repo_root, feature_id, lane_id)

        assert run_id == "RUN-001"
        task_pkg = (
            run_dir(repo_root, feature_id, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert _TASK_BODY in task_pkg

    def test_allowed_files_come_from_lane_expected_and_exclusive(
        self, repo_root: Path
    ) -> None:
        # expected_files and exclusive_files both feed the allow-list; the
        # §13.1 mandatory outputs (result.json/result.md) stay seeded.
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py", "workspace/util.py"],
        )

        run_id = build_implementer_input_package(repo_root, feature_id, lane_id)

        allowed = _read_allowed(
            run_dir(repo_root, feature_id, run_id) / "input" / ALLOWED_FILES_FILE
        )
        assert "workspace/hello.py" in allowed
        assert "workspace/util.py" in allowed
        assert "output/result.json" in allowed
        assert "output/result.md" in allowed

    def test_role_pinned_to_implementer(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root)

        run_id = build_implementer_input_package(repo_root, feature_id, lane_id)

        role = (
            run_dir(repo_root, feature_id, run_id) / "input" / ROLE_FILE
        ).read_text()
        assert role == f"You are the Implementer for {run_id}.\n"

    def test_fails_loud_when_tasks_not_frozen(self, repo_root: Path) -> None:
        # §4.2: the implementer leg builds on *frozen* tasks + lane-graph. An
        # unfrozen precondition is rejected before any run is prepared.
        feature_id, lane_id = _seed_frozen_feature(repo_root, freeze=False)

        with pytest.raises(ValueError, match="frozen"):
            build_implementer_input_package(repo_root, feature_id, lane_id)

    def test_fails_loud_when_lane_not_in_graph(self, repo_root: Path) -> None:
        feature_id, _ = _seed_frozen_feature(repo_root)

        with pytest.raises(ValueError, match="LANE-999"):
            build_implementer_input_package(repo_root, feature_id, "LANE-999")


class TestReadFrozenArtifacts:
    """The pure readers the input-package builder composes."""

    def test_read_task_text_extracts_tasks_section_body(self, repo_root: Path) -> None:
        feature_id, _ = _seed_frozen_feature(repo_root, task_body="Do the thing.")

        body = read_task_text(_feature_root(repo_root, feature_id))

        assert "Do the thing." in body
        # The header / frozen marker above the section are NOT in the body.
        assert "## Tasks" not in body

    def test_read_task_text_fails_loud_without_tasks_section(
        self, repo_root: Path
    ) -> None:
        feature_id = create_feature_run(repo_root, "no tasks section")
        # Overwrite tasks.md with no ## Tasks header.
        (_feature_root(repo_root, feature_id) / TASKS_MD).write_text(
            "# Tasks - FEATURE-001\n\nFrozen: false\n"
        )

        with pytest.raises(ValueError, match="Tasks"):
            read_task_text(_feature_root(repo_root, feature_id))

    def test_read_lane_entry_returns_expected_and_exclusive(
        self, repo_root: Path
    ) -> None:
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py"],
            tasks=["TASK-001"],
        )

        lane = read_lane_entry(_feature_root(repo_root, feature_id), lane_id)

        assert lane.id == lane_id
        assert lane.expected_files == ["workspace/hello.py"]
        assert lane.exclusive_files == ["workspace/hello.py"]
        assert lane.tasks == ["TASK-001"]

    def test_lane_allowed_files_dedupes_expected_and_exclusive(self) -> None:
        from ai_dev.implement_leg import LaneEntry

        lane = LaneEntry(
            id="LANE-001",
            purpose=None,
            tasks=[],
            depends_on=[],
            expected_files=["workspace/hello.py", "workspace/util.py"],
            exclusive_files=["workspace/hello.py"],
            provides=[],
            consumes=[],
            verification_scope=[],
            merge_policy=None,
        )

        assert lane_allowed_files(lane) == ["workspace/hello.py", "workspace/util.py"]


# ===========================================================================
# Seam 2: the proposed_done writeback (deterministic runtime, §4.3/§9.2).
# ===========================================================================


def _task_status(feature_root: Path) -> dict:
    return yaml.safe_load(
        (feature_root / "status" / TASK_STATUS_FILE).read_text()
    )


class TestMarkTaskProposedDone:
    """``mark_task_proposed_done`` is the one canonical write of the implementer
    leg - deterministic runtime only (§4.3). It registers the §8.1 row, sets
    ``proposed_done`` + ``proposed_done_by``, and never ``accepted_done``."""

    def test_registers_row_and_sets_proposed_done(self, repo_root: Path) -> None:
        feature_id = create_feature_run(repo_root, "writeback test")
        root = _feature_root(repo_root, feature_id)

        mark_task_proposed_done(
            root, "TASK-001", lane_id="LANE-001", run_id="RUN-001"
        )

        row = _task_status(root)["tasks"]["TASK-001"]
        assert row["status"] == "proposed_done"
        assert row["proposed_done_by"] == "RUN-001"
        assert row["owner_run"] == "RUN-001"
        assert row["lane"] == "LANE-001"
        assert row["accepted_done"] is False  # §9.2 / invariant #7
        assert row["related_requirements"] == []
        assert row["related_acceptance_criteria"] == []

    def test_never_sets_accepted_done_on_re_mark(self, repo_root: Path) -> None:
        # Re-marking stays proposed_done; accepted_done never flips to true.
        feature_id = create_feature_run(repo_root, "re-mark test")
        root = _feature_root(repo_root, feature_id)

        mark_task_proposed_done(root, "TASK-001", lane_id="LANE-001", run_id="RUN-001")
        mark_task_proposed_done(root, "TASK-001", lane_id="LANE-001", run_id="RUN-002")

        row = _task_status(root)["tasks"]["TASK-001"]
        assert row["status"] == "proposed_done"
        assert row["proposed_done_by"] == "RUN-002"
        assert row["accepted_done"] is False

    def test_preserves_planner_registered_related_ids(self, repo_root: Path) -> None:
        # If the Planner already registered the row with related REQ/AC, the
        # writeback flips only the proposal fields - it does not clobber traceability.
        feature_id = create_feature_run(repo_root, "planner-row test")
        root = _feature_root(repo_root, feature_id)
        # Pre-seed a Planner-registered row directly in task-status.yml.
        ts_path = root / "status" / TASK_STATUS_FILE
        yaml.safe_dump(
            {
                "tasks": {
                    "TASK-001": {
                        "status": "pending",
                        "lane": "LANE-001",
                        "owner_run": None,
                        "proposed_done_by": None,
                        "accepted_done": False,
                        "related_requirements": ["REQ-001", "REQ-002"],
                        "related_acceptance_criteria": ["AC-001"],
                    }
                }
            },
            ts_path.open("w"),
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )

        mark_task_proposed_done(root, "TASK-001", lane_id="LANE-001", run_id="RUN-001")

        row = _task_status(root)["tasks"]["TASK-001"]
        assert row["status"] == "proposed_done"
        assert row["related_requirements"] == ["REQ-001", "REQ-002"]
        assert row["related_acceptance_criteria"] == ["AC-001"]

    def test_audits_the_canonical_write(self, repo_root: Path) -> None:
        feature_id = create_feature_run(repo_root, "audit test")
        root = _feature_root(repo_root, feature_id)

        mark_task_proposed_done(root, "TASK-001", lane_id="LANE-001", run_id="RUN-001")

        records = json.loads((root / AUDIT_LOG_JSON).read_text())
        writebacks = [
            r for r in records if r["event"] == "mark_task_proposed_done"
        ]
        assert len(writebacks) == 1
        assert writebacks[0]["payload"] == {
            "task": "TASK-001",
            "lane": "LANE-001",
            "run": "RUN-001",
            "status": "proposed_done",
        }

    def test_rejects_empty_ids(self, repo_root: Path) -> None:
        feature_id = create_feature_run(repo_root, "empty id test")
        root = _feature_root(repo_root, feature_id)

        with pytest.raises(ValueError, match="task_id"):
            mark_task_proposed_done(root, "", lane_id="LANE-001", run_id="RUN-001")


# ===========================================================================
# Seam 3: the implement-result rollup (§4.4 md+json double product).
# ===========================================================================


_RESULT_JSON = {
    "status": "proposed_done",
    "summary": "Wrote workspace/hello.py for the run.",
    "tasks": [
        {"id": "TASK-001", "status": "proposed_done", "evidence": ["workspace/hello.py"]}
    ],
    "related_requirements": ["REQ-001"],
    "related_acceptance_criteria": ["AC-001"],
    "known_issues": [],
    "change_proposals": [],
}

_METADATA_JSON = {
    "run_id": "RUN-001",
    "profile": "cc-glm52",
    "cli": "claude",
    "backend": "glm",
    "model": "glm-5.2",
    "started_at": "2026-07-20T10:00:00Z",
    "ended_at": "2026-07-20T10:00:05Z",
    "exit_code": 0,
    "changed_files": ["output/result.json", "output/result.md", "workspace/hello.py"],
    "commits": [],
    "checks": [],
}


def _stage_run_with_outputs(
    repo_root: Path, feature_id: str
) -> tuple[str, ValidationResult]:
    """Prepare a run and write a passing result.json + metadata.json into it.

    Returns ``(run_id, validation)``. The workspace file is declared as an
    allowed-file so the §14.2 boundary check passes; validation is the real
    ``validate_run`` verdict (PASS) the rollup consumes.
    """
    run_id = prepare_run(
        repo_root,
        feature_id,
        "Implementer",
        _TASK_BODY,
        allowed_files=["workspace/hello.py"],
    )
    out = run_dir(repo_root, feature_id, run_id) / "output"
    (out / "result.json").write_text(json.dumps(_RESULT_JSON))
    (out / "result.md").write_text("Wrote workspace/hello.py for the run.\n")
    (out / "metadata.json").write_text(json.dumps(_METADATA_JSON))
    validation = validate_run(repo_root, feature_id, run_id)
    assert validation.passed, f"staging run should validate: {validation.issues}"
    return run_id, validation


class TestWriteImplementResult:
    """``write_implement_result`` rolls the run's result + metadata + validation
    into the lane-level ``implement-result.{md,json}`` (§4.4), field-complete
    and consistent with the run's artifacts."""

    def test_writes_md_and_json_under_lane_dir(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        run_id, validation = _stage_run_with_outputs(repo_root, feature_id)

        md_path, json_path = write_implement_result(
            root,
            lane_id,
            run_id=run_id,
            result=_RESULT_JSON,
            metadata=_METADATA_JSON,
            validation=validation,
        )

        assert md_path == root / "lanes" / lane_id / IMPLEMENT_RESULT_MD
        assert json_path == root / "lanes" / lane_id / IMPLEMENT_RESULT_JSON
        assert md_path.is_file() and json_path.is_file()

    def test_json_rollup_is_field_complete_and_consistent(
        self, repo_root: Path
    ) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        run_id, validation = _stage_run_with_outputs(repo_root, feature_id)

        _, json_path = write_implement_result(
            root,
            lane_id,
            run_id=run_id,
            result=_RESULT_JSON,
            metadata=_METADATA_JSON,
            validation=validation,
        )
        rollup = json.loads(json_path.read_text())

        # Provenance.
        assert rollup["feature"] == feature_id
        assert rollup["lane"] == lane_id
        assert rollup["run"] == run_id
        assert rollup["role"] == "Implementer"
        # Agent-declared result fields carried verbatim (§13.1).
        assert rollup["status"] == "proposed_done"
        assert rollup["summary"] == _RESULT_JSON["summary"]
        assert rollup["tasks"] == _RESULT_JSON["tasks"]
        assert rollup["related_requirements"] == ["REQ-001"]
        assert rollup["related_acceptance_criteria"] == ["AC-001"]
        assert rollup["known_issues"] == []
        assert rollup["change_proposals"] == []
        # Wrapper-computed metadata nested (§13.2).
        assert rollup["run_metadata"]["profile"] == "cc-glm52"
        assert rollup["run_metadata"]["model"] == "glm-5.2"
        assert rollup["run_metadata"]["exit_code"] == 0
        assert rollup["run_metadata"]["changed_files"] == _METADATA_JSON["changed_files"]
        # Validation verdict carried.
        assert rollup["validation"]["passed"] is True
        assert rollup["validation"]["attempt"] == 1
        assert rollup["validation"]["issues"] == []
        # §9.2 / invariant #7: never final done.
        assert rollup["accepted_done"] is False

    def test_md_mirror_carries_key_fields(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        run_id, validation = _stage_run_with_outputs(repo_root, feature_id)

        md_path, _ = write_implement_result(
            root,
            lane_id,
            run_id=run_id,
            result=_RESULT_JSON,
            metadata=_METADATA_JSON,
            validation=validation,
        )
        md = md_path.read_text()

        assert f"lane: {lane_id}" in md
        assert f"run: {run_id}" in md
        assert "status: proposed_done" in md
        assert "TASK-001" in md
        assert "cc-glm52" in md
        assert "passed: True" in md
        # §9.2 reminder that final done is not the implementer's call.
        assert "accepted_done: False" in md

    def test_rollup_reports_failed_validation_without_writeback_gating(
        self, repo_root: Path
    ) -> None:
        # A run whose validation FAILED still gets a rollup - it records the
        # failure. (writeback gating is the leg's job, not the rollup's; the
        # rollup just reports the verdict it is given.)
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        run_id = prepare_run(repo_root, feature_id, "Implementer", _TASK_BODY)
        out = run_dir(repo_root, feature_id, run_id) / "output"
        # result.json present but a workspace file the agent wrote is NOT in the
        # allow-list -> boundary FAIL.
        (out / "result.json").write_text(json.dumps(_RESULT_JSON))
        (out / "result.md").write_text("ok\n")
        (out / "metadata.json").write_text(
            json.dumps({**_METADATA_JSON, "changed_files": ["workspace/hello.py"]})
        )
        validation = validate_run(repo_root, feature_id, run_id)
        assert not validation.passed
        assert validation.failed_check == "boundary"

        _, json_path = write_implement_result(
            root,
            lane_id,
            run_id=run_id,
            result=_RESULT_JSON,
            metadata=_METADATA_JSON,
            validation=validation,
        )
        rollup = json.loads(json_path.read_text())

        assert rollup["validation"]["passed"] is False
        assert rollup["validation"]["failed_check"] == "boundary"
        assert rollup["accepted_done"] is False


# ===========================================================================
# Seam 4: orchestration (prepare -> run -> validate -> writeback -> rollup).
# ===========================================================================


class TestRunImplementerLeg:
    """The leg wires the v0.1 seams together: a passing implement run writes
    ``proposed_done`` back and produces the lane rollup; the §9.2 limits are
    enforced by gating the writeback on validation."""

    def test_passing_run_writebacks_proposed_done_and_rolls_up(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-leg")
        fake = _write_fake_claude(tmp_path / "bin")
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py"],
            tasks=["TASK-001"],
        )

        result = run_implementer_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z",
            ended_at="2026-07-20T10:00:05Z",
        )

        # The run was captured and validated.
        assert result.run_id == "RUN-001"
        assert result.exit_code == 0
        assert result.validation.passed
        assert result.result_status == "proposed_done"
        # The proposed_done writeback happened (gated on validation pass).
        assert result.task_ids_marked == ["TASK-001"]
        root = _feature_root(repo_root, feature_id)
        row = _task_status(root)["tasks"]["TASK-001"]
        assert row["status"] == "proposed_done"
        assert row["proposed_done_by"] == result.run_id
        assert row["accepted_done"] is False
        # The lane-level rollup landed.
        lane_root = lane_dir(repo_root, feature_id, lane_id)
        assert (lane_root / IMPLEMENT_RESULT_MD).is_file()
        rollup = json.loads((lane_root / IMPLEMENT_RESULT_JSON).read_text())
        assert rollup["run"] == result.run_id
        assert rollup["status"] == "proposed_done"
        assert rollup["accepted_done"] is False

    def test_failed_validation_skips_writeback(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A run that breaches the boundary (writes a file NOT in the lane's
        # expected/exclusive allow-list) fails validation -> no proposed_done
        # writeback, but the rollup still records the failure.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-leg-fail")
        fake = _write_fake_claude(tmp_path / "bin")
        # Lane declares NO workspace files -> the fake's workspace/hello.py is
        # out of bounds -> boundary FAIL.
        feature_id, lane_id = _seed_frozen_feature(
            repo_root, expected_files=[], exclusive_files=[], tasks=["TASK-001"]
        )

        result = run_implementer_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z",
            ended_at="2026-07-20T10:00:05Z",
        )

        assert not result.validation.passed
        assert result.validation.failed_check == "boundary"
        assert result.task_ids_marked == []  # §9.2: no writeback on failed run
        root = _feature_root(repo_root, feature_id)
        assert "TASK-001" not in _task_status(root)["tasks"]
        # Rollup still produced, recording the failure.
        rollup = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / IMPLEMENT_RESULT_JSON).read_text()
        )
        assert rollup["validation"]["passed"] is False

    def test_audit_log_records_leg_lifecycle(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-leg-audit")
        fake = _write_fake_claude(tmp_path / "bin")
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py"],
            tasks=["TASK-001"],
        )

        run_implementer_leg(
            repo_root, feature_id, lane_id, profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )

        root = _feature_root(repo_root, feature_id)
        events = [str(r["event"]) for r in json.loads((root / AUDIT_LOG_JSON).read_text())]
        # The v0.1 lifecycle (prepare_run -> run -> validate) plus the v0.2
        # canonical writeback, in order. (allocate_id mints the RUN id inside
        # prepare_run, so it precedes prepare_run; freeze events precede those
        # from _seed_frozen_feature.)
        assert events.index("prepare_run") < events.index("run")
        assert events.index("run") < events.index("validate")
        assert events.index("validate") < events.index("mark_task_proposed_done")

    def test_out_of_lane_proposed_done_fails_loud(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # §9.2 lane scoping: a passing run that declares proposed_done for a
        # task NOT in the lane's task set is rejected - the model cannot propose
        # work outside its lane, and nothing reaches canonical task-status.yml.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-lane-scope")
        fake = _write_fake_claude(tmp_path / "bin", task_id="TASK-999")
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py"],
            tasks=["TASK-001"],  # the lane only owns TASK-001
        )

        with pytest.raises(ValueError, match="not in lane"):
            run_implementer_leg(
                repo_root, feature_id, lane_id, profile, claude_path=str(fake),
                started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
            )

        # No canonical write happened for the out-of-lane task (or any task).
        root = _feature_root(repo_root, feature_id)
        tasks = _task_status(root)["tasks"]
        assert "TASK-999" not in tasks
        assert "TASK-001" not in tasks

    def test_empty_lane_tasks_skips_scoping_and_writebacks_all(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # When the lane declares no tasks (Planner has not filled ``tasks:``),
        # the scoping check is skipped and the model's declaration is trusted.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-empty-lane")
        fake = _write_fake_claude(tmp_path / "bin", task_id="TASK-001")
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py"],
            tasks=[],  # no declared tasks -> scoping skipped
        )

        result = run_implementer_leg(
            repo_root, feature_id, lane_id, profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )

        assert result.validation.passed
        assert result.task_ids_marked == ["TASK-001"]


# ===========================================================================
# CLI: `ai-dev implement <FEATURE> <LANE>`.
# ===========================================================================


class TestImplementCli:
    """The ``implement`` console command drives the leg end to end - argparse +
    dispatch + profile load + the leg - with the fake claude on PATH, mirroring
    the v0.1 e2e CLI test."""

    def test_implement_pass_exit_zero_and_writeback(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-implement")
        fake_bin = _write_fake_claude(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")
        feature_id, lane_id = _seed_frozen_feature(
            repo_root,
            expected_files=["workspace/hello.py"],
            exclusive_files=["workspace/hello.py"],
            tasks=["TASK-001"],
        )

        rc = main(
            [
                "implement", feature_id, lane_id,
                "--profile", "cc-glm52",
                "--repo-root", str(repo_root),
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "IMPLEMENT PASS" in out
        assert "RUN-001" in out
        assert "tasks_marked=['TASK-001']" in out
        # Canonical writeback landed through the CLI path.
        root = _feature_root(repo_root, feature_id)
        assert _task_status(root)["tasks"]["TASK-001"]["status"] == "proposed_done"
        # Lane rollup landed.
        assert (lane_dir(repo_root, feature_id, lane_id) / IMPLEMENT_RESULT_JSON).is_file()

    def test_implement_unfrozen_exits_one_with_error(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §4.2 precondition: unfrozen tasks/lane-graph -> the leg refuses before
        # any run is prepared, exit 1 with an error: line (not a traceback).
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        feature_id, lane_id = _seed_frozen_feature(repo_root, freeze=False)

        rc = main(
            ["implement", feature_id, lane_id, "--repo-root", str(repo_root)]
        )

        assert rc == 1
        err = capsys.readouterr().err
        assert "frozen" in err

