"""run_prepare - RUN-NNN scaffold + input package builder (ticket 02, spec §12).

``prepare_run`` turns a (feature_id, role, task) triple into a persisted
``RUN-NNN`` directory under the feature run's ``runs/`` and writes the §12.2
input package. RUN-NNN is allocated through the v0.0 stable-id allocator
(``allocate_id(feature_root, "RUN")``) so numbering is monotonic across
restarts, and every allocation is audited.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.feature_ids import ID_COUNTERS_FILE
from ai_dev.feature_run import create_feature_run
from ai_dev.paths import run_dir
from ai_dev.run_prepare import (
    ALLOWED_FILES_FILE,
    CONTEXT_DIR,
    OUTPUT_SCHEMA_FILE,
    ROLE_FILE,
    SYSTEM_FILE,
    TASK_PACKAGE_FILE,
    prepare_run,
)

ROLE = "Implementer"
TASK = "Create workspace/hello.py defining answer() returning 42."

# The §12.2 input-package files, in spec listing order. ``context/`` is a
# directory; its seed file (run-context.md) is checked separately.
_INPUT_PACKAGE_FILES = (
    ROLE_FILE,
    SYSTEM_FILE,
    TASK_PACKAGE_FILE,
    OUTPUT_SCHEMA_FILE,
    ALLOWED_FILES_FILE,
)


def _make_feature(repo_root: Path, intent: str = "de-risk the run adapter") -> str:
    """Create a feature run and return its id, so prepare_run has a target."""
    return create_feature_run(repo_root, intent)


def _input_dir(repo_root: Path, feature_id: str, run_id: str) -> Path:
    return run_dir(repo_root, feature_id, run_id) / "input"


def _read_allowed_files(path: Path) -> list[str]:
    """Parse allowed-files.txt the way §14.2 will: strip comments + blanks."""
    entries: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def _audit_records(repo_root: Path, feature_id: str) -> list[dict[str, object]]:
    log = (
        repo_root
        / ".ai-dev"
        / "features"
        / feature_id
        / AUDIT_LOG_JSON
    )
    return json.loads(log.read_text())


class TestRunDirectoryStructure:
    """``runs/RUN-NNN/{input,output,workspace}`` per §12.1 + ticket 02."""

    def test_creates_input_output_workspace_under_run_dir(
        self, repo_root: Path
    ) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        run_root = run_dir(repo_root, feature_id, run_id)
        assert run_root.is_dir()
        for sub in ("input", "output", "workspace"):
            assert (run_root / sub).is_dir(), f"missing {sub}/ under {run_root}"

    def test_run_dir_lives_under_feature_runs(self, repo_root: Path) -> None:
        # The run must be nested in THIS feature's runs/, not a sibling location.
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        expected = (
            repo_root / ".ai-dev" / "features" / feature_id / "runs" / run_id
        )
        assert expected.is_dir()


class TestInputPackage:
    """§12.2: the input package contains every named file, all well-formed."""

    def test_all_section_12_2_files_present(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        pkg = _input_dir(repo_root, feature_id, run_id)
        for name in _INPUT_PACKAGE_FILES:
            assert (pkg / name).is_file(), f"missing input-package file {name}"
        # context/ is a directory with at least the seed run-context.md.
        assert (pkg / CONTEXT_DIR).is_dir()
        assert (pkg / CONTEXT_DIR / "run-context.md").is_file()

    def test_output_schema_is_parseable_json_schema(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        schema = json.loads(
            (_input_dir(repo_root, feature_id, run_id) / OUTPUT_SCHEMA_FILE).read_text()
        )
        # A recognizable JSON Schema shape matching §13.1's result.json.
        assert schema["type"] == "object"
        assert "status" in schema["required"]
        assert "summary" in schema["required"]
        assert "tasks" in schema["required"]
        assert "proposed_done" in schema["properties"]["status"]["enum"]

    def test_allowed_files_non_empty_and_lists_mandatory_outputs(
        self, repo_root: Path
    ) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        entries = _read_allowed_files(
            _input_dir(repo_root, feature_id, run_id) / ALLOWED_FILES_FILE
        )
        assert entries, "allowed-files.txt must be non-empty (ticket 02)"
        # §13.1 mandates these two agent outputs for every run.
        assert "output/result.json" in entries
        assert "output/result.md" in entries

    def test_system_md_contains_section_12_2_global_constraints(
        self, repo_root: Path
    ) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        text = (_input_dir(repo_root, feature_id, run_id) / SYSTEM_FILE).read_text()
        # The four constraints the ticket checklist names explicitly.
        assert "frozen artifact" in text  # 不改 frozen
        assert "allowed-files.txt" in text  # 只写允许文件
        assert "result.json" in text  # 必出 result.json
        assert "canonical status" in text  # 不写 canonical status

    def test_role_md_defines_role_for_run(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        text = (_input_dir(repo_root, feature_id, run_id) / ROLE_FILE).read_text()
        assert ROLE in text
        assert run_id in text

    def test_task_package_embeds_task_and_run(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        text = (
            _input_dir(repo_root, feature_id, run_id) / TASK_PACKAGE_FILE
        ).read_text()
        assert run_id in text
        assert TASK in text  # the verbatim task text is the contract
        # §12.2 names "lane id" as a task-package element; the seed carries a
        # lane line (single-lane MVP, §5.3).
        assert "lane" in text.lower()

    def test_context_references_feature(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        text = (
            _input_dir(repo_root, feature_id, run_id)
            / CONTEXT_DIR
            / "run-context.md"
        ).read_text()
        assert feature_id in text
        assert run_id in text


class TestRunIdAllocation:
    """RUN-NNN via the v0.0 allocator: monotonic, restart-safe, counter-persisted."""

    def test_first_prepare_yields_run_001(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        assert run_id == "RUN-001"

    def test_two_consecutive_prepares_increment(self, repo_root: Path) -> None:
        # Ticket 02 checklist: 连续 prepare 两次得 RUN-001、RUN-002.
        feature_id = _make_feature(repo_root)

        first = prepare_run(repo_root, feature_id, ROLE, TASK)
        second = prepare_run(repo_root, feature_id, "Reviewer", "Review the change.")

        assert first == "RUN-001"
        assert second == "RUN-002"
        # Both run directories coexist.
        assert run_dir(repo_root, feature_id, first).is_dir()
        assert run_dir(repo_root, feature_id, second).is_dir()

    def test_allocation_persisted_in_counter_file(self, repo_root: Path) -> None:
        # "复用 v0.0 分配器" - the RUN high-water mark lands in id-counters.yml.
        feature_id = _make_feature(repo_root)

        prepare_run(repo_root, feature_id, ROLE, TASK)

        counters = yaml.safe_load(
            (repo_root / ".ai-dev" / "features" / feature_id / ID_COUNTERS_FILE).read_text()
        )
        assert counters["RUN"] == 1

    def test_run_numbering_survives_restart(self, repo_root: Path) -> None:
        # A "restart" is just a fresh process: a second prepare_run call reads
        # the persisted counter rather than starting from 1, so no duplicate.
        feature_id = _make_feature(repo_root)

        prepare_run(repo_root, feature_id, ROLE, TASK)
        # Simulate a new process: no in-memory state carried over.
        run_id_after_restart = prepare_run(repo_root, feature_id, ROLE, TASK)

        assert run_id_after_restart == "RUN-002"


class TestValidationErrors:
    """§24.2 fail loud: bad inputs raise before any run directory is created."""

    def test_missing_feature_raises(self, repo_root: Path) -> None:
        # No feature run created at all.
        with pytest.raises(ValueError, match="FEATURE-999"):
            prepare_run(repo_root, "FEATURE-999", ROLE, TASK)

    def test_empty_role_raises(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        with pytest.raises(ValueError, match="role"):
            prepare_run(repo_root, feature_id, "", TASK)

    def test_empty_task_raises(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        with pytest.raises(ValueError, match="task"):
            prepare_run(repo_root, feature_id, ROLE, "")

    def test_bad_inputs_create_no_run_directory(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        with pytest.raises(ValueError):
            prepare_run(repo_root, feature_id, "", TASK)

        runs = repo_root / ".ai-dev" / "features" / feature_id / "runs"
        # The seeded empty runs/ dir exists from create-feature-run, but no
        # RUN-NNN should have been created by the failed prepare.
        assert not any(p.name.startswith("RUN-") for p in runs.iterdir())


class TestAudit:
    """A prepare_run lifecycle event flows through the §2.1 audit log."""

    def test_audits_prepare_run_event(self, repo_root: Path) -> None:
        feature_id = _make_feature(repo_root)

        run_id = prepare_run(repo_root, feature_id, ROLE, TASK)

        records = _audit_records(repo_root, feature_id)
        prepare_events = [
            r for r in records if r.get("event") == "prepare_run"
        ]
        assert len(prepare_events) == 1
        payload = prepare_events[0]["payload"]
        assert payload is not None
        assert isinstance(payload, dict)
        assert payload.get("run") == run_id
        assert payload.get("role") == ROLE

    def test_allocate_id_event_precedes_prepare_run(self, repo_root: Path) -> None:
        # allocate_id(RUN) is audited first (it mints the id), then prepare_run.
        feature_id = _make_feature(repo_root)

        prepare_run(repo_root, feature_id, ROLE, TASK)

        records = _audit_records(repo_root, feature_id)
        events = [r["event"] for r in records]
        assert "allocate_id" in events
        assert "prepare_run" in events
        assert events.index("allocate_id") < events.index("prepare_run")
