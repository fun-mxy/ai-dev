"""End-to-end integration - ticket 05, the v0.1 walking-skeleton proof.

Strings the v0.0 and v0.1 deterministic runtime together on one feature run:
``create_feature_run`` (v0.0) -> ``prepare_run`` (ticket 02) -> ``run_headless``
(ticket 03) -> ``validate_run`` (ticket 04), with no manual intervention between
steps. The artifact chain is asserted end to end - exit code, result.json schema
validity, changed_files within the allowed boundary, validate PASS, RUN-NNN under
the feature run's ``runs/``, a complete metadata.json, and the token never
persisted - exactly the ticket-05 checklist.

Two drive modes, covering two seams:

* **Library** (``TestEndToEndLibrary``) - calls the public functions directly,
  so the handoff between modules is the thing under test (the path / ID /
  interface alignment the ticket names). A fake ``claude`` binary stands in for
  the real CLI so the run is deterministic and token-free.
* **CLI** (``TestEndToEndCli``) - drives the ``ai-dev`` console entry for all
  four subcommands with the fake ``claude`` on ``PATH``, so the argparse +
  dispatch wiring is exercised across the whole pipeline (not just one command).

``TestAllowedFilesSeam`` pins the integration seam ticket 05 fixes: a run that
writes a workspace file must declare it via ``allowed_files`` (or
``--allowed-file``) or the §14.2 boundary check fails.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.checking_legs import (
    REVIEW_DIR,
    REVIEW_REPORT_JSON,
    REVIEW_REPORT_MD,
    SPEC_GAP_DIR,
    SPEC_GAP_REPORT_JSON,
    SPEC_GAP_REPORT_MD,
)
from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON, IMPLEMENT_RESULT_MD, run_implementer_leg
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON, ISSUE_BUNDLE_MD, collect_issue_bundle
from ai_dev.lane_gate import LANE_DECISION_JSON, LANE_DECISION_MD, evaluate_lane_gate
from ai_dev.paths import lane_dir, run_dir
from ai_dev.profiles import load_profile
from ai_dev.run_prepare import ALLOWED_FILES_FILE, prepare_run
from ai_dev.run_wrapper import run_headless
from ai_dev.shell_verifier import VERIFICATION_DIR, VERIFICATION_REPORT_JSON, VERIFICATION_REPORT_MD, run_verifier
from ai_dev.status import freeze_artifact
from ai_dev.templates import LANE_GRAPH_YML, TASKS_MD
from ai_dev.validate import validate_run

# A fake ``claude`` binary: ignores argv, writes the §13.1 agent outputs
# (result.json / result.md / a workspace file) into its cwd, prints one
# stream-json ``result`` line to stdout, and exits 0. Stands in for the real CLI
# so the orchestrator is exercised end-to-end without network or token.
# ``__PY__`` is replaced with the test interpreter so the shebang resolves under
# ``uv run`` (string replace, not ``.format``, so the JSON braces are literal).
_FAKE_CLAUDE = """\
#!__PY__
import json, os, sys
os.makedirs("workspace", exist_ok=True)
os.makedirs("output", exist_ok=True)
with open("workspace/hello.py", "w") as f:
    f.write("# throwaway prototype module\\n")
    f.write("def answer():\\n    return 42\\n")
with open("output/result.md", "w") as f:
    f.write("Wrote workspace/hello.py for the run.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Wrote workspace/hello.py for the run.",
            "tasks": [
                {"id": "TASK-001", "status": "proposed_done",
                 "evidence": ["workspace/hello.py"]}
            ],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# The three files a real Implementer run (and the fake claude) writes: the two
# §13.1 mandatory outputs plus the declared workspace file.
_EXPECTED_CHANGED_FILES = [
    "output/result.json",
    "output/result.md",
    "workspace/hello.py",
]

_TASK = "Create workspace/hello.py defining answer() returning 42."


_V02_TASK_BODY = "Create workspace/hello.py defining answer() returning 42."
_V02_REVIEW_PASS_PAYLOAD: dict[str, Any] = {"issues": []}
_V02_GAP_PASS_PAYLOAD: dict[str, Any] = {"issues": []}
_V02_REVIEW_FAIL_PAYLOAD: dict[str, Any] = {
    "issues": [
        {
            "id": "agent-review-p1",
            "source": "code_review",
            "severity": "P1",
            "title": "Injected review blocker",
            "description": "Deliberate P1 blocker for the v0.2 FAIL evidence path.",
            "related_tasks": ["TASK-001"],
            "related_requirements": [],
            "related_acceptance_criteria": [],
            "evidence": [{"file": "workspace/hello.py", "line": 1}],
            "recommendation": "Address the injected blocker before passing the lane gate.",
            "requires_change_proposal": False,
        }
    ]
}


def _feature_root(repo_root: Path, feature_id: str) -> Path:
    return repo_root / ".ai-dev" / "features" / feature_id


def _fill_v02_artifacts(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    verification_command: str,
) -> None:
    """Fill the seeded task + lane-graph artifacts with a runnable v0.2 lane."""
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
        f"{_V02_TASK_BODY}\n"
    )
    lane_graph = {
        "feature": feature_id,
        "frozen": False,
        "lanes": [
            {
                "id": lane_id,
                "purpose": "End-to-end v0.2 walking skeleton lane",
                "tasks": ["TASK-001"],
                "depends_on": [],
                "expected_files": ["workspace/hello.py"],
                "exclusive_files": ["workspace/hello.py"],
                "provides": [],
                "consumes": [],
                "verification_scope": ["workspace/hello.py"],
                "verification_commands": [
                    {"name": "answer-returns-42", "command": verification_command}
                ],
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


def _freeze_v02_lane(repo_root: Path, feature_id: str, lane_id: str, *, verification_command: str) -> None:
    _fill_v02_artifacts(
        repo_root, feature_id, lane_id, verification_command=verification_command
    )
    root = _feature_root(repo_root, feature_id)
    freeze_artifact(root, "tasks")
    freeze_artifact(root, "lane_graph")


def _write_fake_claude_sequence(bin_dir: Path, payloads: list[dict[str, Any]]) -> Path:
    """Fake claude that emits one queued payload per invocation.

    Invocation 1 is the Implementer (writes workspace/hello.py); invocation 2 is
    the Code Reviewer; invocation 3 is the Spec Gap Analyst. The queue file lives
    next to the fake binary, not under the repo/run tree, so no token-like test
    sentinel can leak into lane artifacts.
    """
    import base64

    bin_dir.mkdir(parents=True, exist_ok=True)
    queue_path = bin_dir / "payloads.json"
    queue_path.write_text(json.dumps(payloads))
    b64_queue = base64.b64encode(str(queue_path).encode("utf-8")).decode("ascii")
    script = bin_dir / "claude"
    script.write_text(
        "#!__PY__\n"
        "import base64, json, os, pathlib, sys\n"
        f"queue = pathlib.Path(base64.b64decode({b64_queue!r}).decode('utf-8'))\n"
        "payloads = json.loads(queue.read_text())\n"
        "if not payloads:\n"
        "    raise SystemExit('fake claude payload queue is empty')\n"
        "payload = payloads.pop(0)\n"
        "queue.write_text(json.dumps(payloads))\n"
        "os.makedirs('output', exist_ok=True)\n"
        "if 'tasks' in payload:\n"
        "    os.makedirs('workspace', exist_ok=True)\n"
        "    with open('workspace/hello.py', 'w') as f:\n"
        "        f.write('# throwaway prototype module\\n')\n"
        "        f.write('def answer():\\n    return 42\\n')\n"
        "with open('output/result.md', 'w') as f:\n"
        "    f.write('Fake claude completed this v0.2 leg.\\n')\n"
        "with open('output/result.json', 'w') as f:\n"
        "    json.dump(payload, f)\n"
        "sys.stdout.write('{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false}\\n')\n"
        "sys.exit(0)\n".replace("__PY__", sys.executable)
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _v02_implement_payload() -> dict[str, Any]:
    return {
        "status": "proposed_done",
        "summary": "Wrote workspace/hello.py for the run.",
        "tasks": [
            {
                "id": "TASK-001",
                "status": "proposed_done",
                "evidence": ["workspace/hello.py"],
            }
        ],
        "related_requirements": ["REQ-001"],
        "related_acceptance_criteria": ["AC-001"],
        "known_issues": [],
        "change_proposals": [],
    }


def _assert_v02_artifact_chain(repo_root: Path, feature_id: str, lane_id: str) -> None:
    lane_root = lane_dir(repo_root, feature_id, lane_id)
    assert (lane_root / IMPLEMENT_RESULT_MD).is_file()
    assert (lane_root / IMPLEMENT_RESULT_JSON).is_file()
    assert (lane_root / REVIEW_DIR / REVIEW_REPORT_MD).is_file()
    assert (lane_root / REVIEW_DIR / REVIEW_REPORT_JSON).is_file()
    assert (lane_root / SPEC_GAP_DIR / SPEC_GAP_REPORT_MD).is_file()
    assert (lane_root / SPEC_GAP_DIR / SPEC_GAP_REPORT_JSON).is_file()
    assert (lane_root / VERIFICATION_DIR / VERIFICATION_REPORT_MD).is_file()
    assert (lane_root / VERIFICATION_DIR / VERIFICATION_REPORT_JSON).is_file()
    assert (lane_root / ISSUE_BUNDLE_MD).is_file()
    assert (lane_root / ISSUE_BUNDLE_JSON).is_file()
    assert (lane_root / LANE_DECISION_MD).is_file()
    assert (lane_root / LANE_DECISION_JSON).is_file()


def _assert_no_token_in_feature_artifacts(repo_root: Path, feature_id: str, sentinel: str) -> None:
    """§10.2 / invariant #11 across the whole feature run tree."""
    _assert_no_token_leak(_feature_root(repo_root, feature_id), sentinel)


def _write_fake_claude(bin_dir: Path) -> Path:
    """Write the fake ``claude`` script into ``bin_dir`` and return its path.

    Named ``claude`` so ``shutil.which("claude")`` resolves it when ``bin_dir``
    is on ``PATH`` (the CLI e2e path); the library e2e passes the path directly
    via ``claude_path``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(_FAKE_CLAUDE.replace("__PY__", sys.executable))
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _audit_records(repo_root: Path, feature_id: str) -> list[dict[str, object]]:
    log = repo_root / ".ai-dev" / "features" / feature_id / AUDIT_LOG_JSON
    return json.loads(log.read_text())


def _read_allowed(repo_root: Path, feature_id: str, run_id: str) -> set[str]:
    allowed_path = (
        run_dir(repo_root, feature_id, run_id) / "input" / ALLOWED_FILES_FILE
    )
    entries: set[str] = set()
    for line in allowed_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def _assert_no_token_leak(root: Path, sentinel: str) -> None:
    """§10.2 / invariant #11: the token value must not appear in any file under
    ``root``. A distinctive sentinel makes a leak visible across every artifact.
    """
    for path in root.rglob("*"):
        if path.is_file():
            assert sentinel not in path.read_text(errors="ignore"), (
                f"token leaked into {path}"
            )


class TestEndToEndLibrary:
    """The full pipeline through the public functions, driven by a fake claude.

    This is the walking-skeleton proof: every ticket-05 checkbox asserted against
    one integrated run, with the path / ID / interface alignment exercised for
    real (create returns FEATURE-NNN -> prepare takes it and returns RUN-NNN ->
    run takes both + profile -> validate takes both).
    """

    def test_full_pipeline_passes_validation(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        # A distinctive token sentinel: §10.2/invariant #11 says it must never
        # land on disk inside the run directory.
        sentinel = "tok-E2E-LEAK-CHECK-7d2a9e"
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        fake = _write_fake_claude(tmp_path / "bin")

        # STEP 1 - create (v0.0): intent -> FEATURE-001.
        feature_id = create_feature_run(repo_root, "de-risk the v0.1 run adapter")
        assert feature_id == "FEATURE-001"

        # STEP 2 - prepare (ticket 02): feature + role + task + allowed_files ->
        # RUN-001. The workspace file is declared here (ticket 05 seam).
        run_id = prepare_run(
            repo_root,
            feature_id,
            "Implementer",
            _TASK,
            allowed_files=["workspace/hello.py"],
        )
        assert run_id == "RUN-001"

        # RUN-NNN lives under THIS feature run's runs/ (v0.0 skeleton <-> v0.1
        # run path integration).
        assert (
            repo_root / ".ai-dev" / "features" / feature_id / "runs" / run_id
        ).is_dir()

        # STEP 3 - run (ticket 03): headless capture via the profile.
        result = run_headless(
            repo_root,
            feature_id,
            run_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z",
            ended_at="2026-07-20T10:00:05Z",
        )

        # exit_code 0 (the captured run succeeded).
        assert result.exit_code == 0
        # changed_files == the actual workspace changes (§13.2).
        assert result.changed_files == _EXPECTED_CHANGED_FILES

        # STEP 4 - validate (ticket 04): the §14 three checks -> PASS.
        verdict = validate_run(repo_root, feature_id, run_id)
        assert verdict.passed, f"validation failed: {verdict.issues}"
        assert verdict.failed_check is None

        # changed_files all within allowed-files.txt (§14.2).
        allowed = _read_allowed(repo_root, feature_id, run_id)
        assert set(result.changed_files).issubset(allowed)

        # result.json is schema-valid (§14.1) - re-check independently of the
        # verdict to pin the schema check specifically.
        run_root = run_dir(repo_root, feature_id, run_id)
        result_json = json.loads((run_root / "output" / "result.json").read_text())
        assert result_json["status"] == "proposed_done"
        assert result_json["tasks"][0]["evidence"] == ["workspace/hello.py"]

        # metadata.json field-complete + changed_files consistent with workspace.
        md = json.loads((run_root / "output" / "metadata.json").read_text())
        assert md["run_id"] == run_id
        assert md["profile"] == "cc-glm52"
        assert md["cli"] == "claude"
        assert md["backend"] == "glm"
        assert md["model"] == "glm-5.2"
        assert md["exit_code"] == 0
        assert md["changed_files"] == _EXPECTED_CHANGED_FILES
        assert md["commits"] == []
        assert md["checks"] == []

        # token never persisted anywhere in the run directory (§10.2/#11).
        _assert_no_token_leak(run_root, sentinel)

    def test_audit_log_records_full_lifecycle(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # §2.1: every lifecycle op flows through the audit log, in order.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        fake = _write_fake_claude(tmp_path / "bin")

        feature_id = create_feature_run(repo_root, "e2e audit trail")
        prepare_run(
            repo_root, feature_id, "Implementer", _TASK,
            allowed_files=["workspace/hello.py"],
        )
        run_headless(
            repo_root, feature_id, "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )
        validate_run(repo_root, feature_id, "RUN-001")

        events = [str(r["event"]) for r in _audit_records(repo_root, feature_id)]
        # The four user-visible lifecycle ops, in pipeline order (allocate_id
        # mints the RUN id inside prepare, so it precedes prepare_run).
        assert "create" in events
        assert "prepare_run" in events
        assert "run" in events
        assert "validate" in events
        assert events.index("create") < events.index("prepare_run")
        assert events.index("prepare_run") < events.index("run")
        assert events.index("run") < events.index("validate")


class TestEndToEndCli:
    """The full pipeline through the ``ai-dev`` console entry, fake claude on PATH.

    Exercises argparse + dispatch across all four subcommands in one run, so the
    CLI wiring (not just the library seam) is proven end to end. The fake
    ``claude`` is placed on ``PATH`` so ``run-headless`` resolves it via
    ``shutil.which`` like the real binary.
    """

    def test_four_commands_in_sequence_exit_zero_and_validate_passes(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-e2e")
        fake_bin = _write_fake_claude(tmp_path / "bin")
        # Prepend the fake bin dir to PATH so shutil.which("claude") finds it.
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")

        # 1. create-feature-run -> prints FEATURE-001.
        assert main(
            ["create-feature-run", "e2e via CLI", "--repo-root", str(repo_root)]
        ) == 0
        assert "FEATURE-001" in capsys.readouterr().out

        # 2. prepare-run with --allowed-file -> prints RUN-001.
        assert main(
            [
                "prepare-run", "FEATURE-001",
                "--role", "Implementer",
                "--task", _TASK,
                "--allowed-file", "workspace/hello.py",
                "--repo-root", str(repo_root),
            ]
        ) == 0
        assert "RUN-001" in capsys.readouterr().out

        # 3. run-headless -> captures, prints a summary, exits 0 (capture, not
        # verdict - even a non-zero claude exit returns 0 from run-headless).
        assert main(
            [
                "run-headless", "FEATURE-001", "RUN-001",
                "--profile", "cc-glm52",
                "--repo-root", str(repo_root),
            ]
        ) == 0
        summary = capsys.readouterr().out
        assert "RUN-001" in summary
        assert "exit_code=0" in summary

        # 4. validate-run -> VALIDATE PASS, exit 0.
        assert main(
            [
                "validate-run", "FEATURE-001", "RUN-001",
                "--repo-root", str(repo_root),
            ]
        ) == 0
        out = capsys.readouterr().out
        assert "VALIDATE PASS" in out

        # The artifact chain landed on disk through the CLI path too.
        run_root = run_dir(repo_root, "FEATURE-001", "RUN-001")
        md = json.loads((run_root / "output" / "metadata.json").read_text())
        assert md["changed_files"] == _EXPECTED_CHANGED_FILES

    def test_token_not_on_disk_through_cli_path(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §10.2/invariant #11 through the CLI: the resolved token value never
        # lands on disk inside the run directory.
        write_profiles(repo_root)
        sentinel = "tok-CLI-LEAK-CHECK-3b1f8c"
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        fake_bin = _write_fake_claude(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")

        main(["create-feature-run", "token safety", "--repo-root", str(repo_root)])
        main(
            ["prepare-run", "FEATURE-001", "--role", "Implementer",
             "--task", _TASK, "--allowed-file", "workspace/hello.py",
             "--repo-root", str(repo_root)]
        )
        main(
            ["run-headless", "FEATURE-001", "RUN-001", "--profile", "cc-glm52",
             "--repo-root", str(repo_root)]
        )
        capsys.readouterr()

        run_root = run_dir(repo_root, "FEATURE-001", "RUN-001")
        _assert_no_token_leak(run_root, sentinel)


class TestV02EndToEndIntegration:
    """Ticket 06: v0.2 walking skeleton over one real feature run.

    Freezes tasks/lane-graph, then executes the five v0.2 stages in sequence:
    implement -> review + spec-gap + verify -> collect-issues -> lane-gate. The
    PASS and FAIL scenarios prove the full lane artifact chain and final decision
    behavior without manual intervention between stages.
    """

    def test_library_pipeline_passes_lane_gate_and_writes_full_artifact_chain(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        sentinel = "tok-V02-PASS-LEAK-CHECK-4d7c2a"
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        py = sys.executable
        verify_command = (
            f'{py} -c "import hello; import sys; '
            f'sys.exit(0 if hello.answer()==42 else 1)"'
        )
        fake = _write_fake_claude_sequence(
            tmp_path / "bin-pass",
            [_v02_implement_payload(), _V02_REVIEW_PASS_PAYLOAD, _V02_GAP_PASS_PAYLOAD],
        )

        # 1. intent -> FEATURE-001, then freeze filled tasks + lane-graph.
        feature_id = create_feature_run(repo_root, "ship the v0.2 lane loop")
        assert feature_id == "FEATURE-001"
        lane_id = "LANE-001"
        _freeze_v02_lane(
            repo_root, feature_id, lane_id, verification_command=verify_command
        )

        # 2. implement(01) -> implement-result.{md,json}.
        implement = run_implementer_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z",
            ended_at="2026-07-20T10:00:05Z",
        )
        assert implement.run_id == "RUN-001"
        assert implement.validation.passed
        assert implement.task_ids_marked == ["TASK-001"]

        # 3. review + spec-gap(02) + verify(03).
        from ai_dev.checking_legs import run_reviewer_leg, run_spec_gap_leg

        review = run_reviewer_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:01:00Z",
            ended_at="2026-07-20T10:01:05Z",
        )
        gap = run_spec_gap_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:02:00Z",
            ended_at="2026-07-20T10:02:05Z",
        )
        verification = run_verifier(
            repo_root,
            feature_id,
            lane_id,
            started_at="2026-07-20T10:03:00Z",
            ended_at="2026-07-20T10:03:01Z",
        )
        assert review.run_id == "RUN-002"
        assert gap.run_id == "RUN-003"
        assert verification.verdict == "pass"

        # 4. collect-issues(04) -> issue-bundle, then lane-gate(05) -> PASS.
        bundle = collect_issue_bundle(repo_root, feature_id, lane_id)
        decision = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert bundle.issue_ids == []
        assert decision.decision == "pass"
        _assert_v02_artifact_chain(repo_root, feature_id, lane_id)

        lane_root = lane_dir(repo_root, feature_id, lane_id)
        decision_json = json.loads((lane_root / LANE_DECISION_JSON).read_text())
        assert decision_json["decision"] == "pass"
        assert all(c["passed"] for c in decision_json["conditions"])
        # RUN/LANE/ISSUE ids remain correctly scoped: three agent RUNs under this
        # feature, the seeded lane id, and no issue ids allocated in the green path.
        assert sorted(p.name for p in (_feature_root(repo_root, feature_id) / "runs").iterdir()) == [
            "RUN-001",
            "RUN-002",
            "RUN-003",
        ]
        assert (lane_root).is_dir()
        counters = (_feature_root(repo_root, feature_id) / "id-counters.yml").read_text()
        assert "LANE: 1" in counters
        assert "RUN: 3" in counters
        assert "ISSUE:" not in counters
        _assert_no_token_in_feature_artifacts(repo_root, feature_id, sentinel)

    def test_cli_pipeline_fail_decision_on_p1_review_issue(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        sentinel = "tok-V02-FAIL-LEAK-CHECK-1a9b8e"
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        py = sys.executable
        verify_command = (
            f'{py} -c "import hello; import sys; '
            f'sys.exit(0 if hello.answer()==42 else 1)"'
        )
        fake_bin = _write_fake_claude_sequence(
            tmp_path / "bin-fail",
            [_v02_implement_payload(), _V02_REVIEW_FAIL_PAYLOAD, _V02_GAP_PASS_PAYLOAD],
        )
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")

        assert main(
            ["create-feature-run", "v0.2 fail evidence", "--repo-root", str(repo_root)]
        ) == 0
        feature_id = "FEATURE-001"
        lane_id = "LANE-001"
        _freeze_v02_lane(
            repo_root, feature_id, lane_id, verification_command=verify_command
        )

        assert main(["implement", feature_id, lane_id, "--repo-root", str(repo_root)]) == 0
        assert main(["review", feature_id, lane_id, "--repo-root", str(repo_root)]) == 0
        assert main(["spec-gap", feature_id, lane_id, "--repo-root", str(repo_root)]) == 0
        assert main(["verify", feature_id, lane_id, "--repo-root", str(repo_root)]) == 0
        assert main(["collect-issues", feature_id, lane_id, "--repo-root", str(repo_root)]) == 0
        assert main(["lane-gate", feature_id, lane_id, "--repo-root", str(repo_root)]) == 1
        out = capsys.readouterr().out
        assert "LANE-GATE FAIL" in out
        assert "failed_conditions=review_no_blocking_issues" in out

        _assert_v02_artifact_chain(repo_root, feature_id, lane_id)
        lane_root = lane_dir(repo_root, feature_id, lane_id)
        bundle = json.loads((lane_root / ISSUE_BUNDLE_JSON).read_text())
        decision = json.loads((lane_root / LANE_DECISION_JSON).read_text())
        assert [issue["id"] for issue in bundle["issues"]] == ["ISSUE-001"]
        assert bundle["issues"][0]["severity"] == "P1"
        assert decision["decision"] == "fail"
        assert decision["blocking_issue_count"] == 1
        assert decision["blocking_issues"][0]["id"] == "ISSUE-001"
        counters = (_feature_root(repo_root, feature_id) / "id-counters.yml").read_text()
        assert "LANE: 1" in counters
        assert "RUN: 3" in counters
        assert "ISSUE: 1" in counters
        _assert_no_token_in_feature_artifacts(repo_root, feature_id, sentinel)


class TestAllowedFilesSeamE2E:
    """The integration seam ticket 05 fixes: declaring task-specific workspace files.

    A run that writes a workspace file must declare it via ``allowed_files`` (or
    ``--allowed-file``) - otherwise the §14.2 boundary check fails even though
    schema and exit code are clean. Before the seam there was no API to declare
    them, so every workspace-writing run failed validation.
    """

    def test_undeclared_workspace_file_fails_boundary(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The same run as the passing e2e, but workspace/hello.py is NOT declared
        # -> the §14.2 boundary check rejects it.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        fake = _write_fake_claude(tmp_path / "bin")

        feature_id = create_feature_run(repo_root, "seam: undeclared workspace")
        # No allowed_files -> allowed-files.txt has only result.json + result.md.
        prepare_run(repo_root, feature_id, "Implementer", _TASK)
        run_headless(
            repo_root, feature_id, "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )

        verdict = validate_run(repo_root, feature_id, "RUN-001")

        assert not verdict.passed
        assert verdict.failed_check == "boundary"
        boundary_issues = [i for i in verdict.issues if i.check == "boundary"]
        assert len(boundary_issues) == 1
        assert boundary_issues[0].path == "workspace/hello.py"

    def test_declared_workspace_file_passes_boundary(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Declaring workspace/hello.py via the seam flips the same run to PASS.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        fake = _write_fake_claude(tmp_path / "bin")

        feature_id = create_feature_run(repo_root, "seam: declared workspace")
        prepare_run(
            repo_root, feature_id, "Implementer", _TASK,
            allowed_files=["workspace/hello.py"],
        )
        run_headless(
            repo_root, feature_id, "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )

        verdict = validate_run(repo_root, feature_id, "RUN-001")

        assert verdict.passed, f"expected PASS with declared file: {verdict.issues}"
