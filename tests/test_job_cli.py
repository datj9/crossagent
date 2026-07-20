"""Tests for durable job CLI subcommands (Phase 3).

These tests cover Phase 3 exit criteria:
  1. ``start`` returns while the worker continues; result is retrievable later.
  2. A separate process (another ``main(argv)`` invocation) can retrieve status/result.
  3. Bounded ``wait`` returns on time with an explicit ``running`` state.
  4. ``cancel`` produces terminal ``cancelled`` and cleans up the process tree.
  5. Stale ``running`` state reconciles to ``abandoned`` on ``status``.
  6. Prompt text never appears in state.json, status output, or the worker command line.

All tests use a fake ``codex`` executable placed on a temporary PATH; no real model
CLI is ever invoked.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crossagent import jobs as jobs_mod
from crossagent.cli import main


_FAKE_CODEX_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys
import time

sleep = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
stdout_count = int(os.environ.get("FAKE_CODEX_STDOUT_COUNT", "1"))
stdout_size = int(os.environ.get("FAKE_CODEX_STDOUT_SIZE", "30"))
stderr_count = int(os.environ.get("FAKE_CODEX_STDERR_COUNT", "0"))
stderr_size = int(os.environ.get("FAKE_CODEX_STDERR_SIZE", "30"))
exit_code = int(os.environ.get("FAKE_CODEX_EXIT_CODE", "0"))
failure = os.environ.get("FAKE_CODEX_FAILURE", "")

if sleep:
    time.sleep(sleep)

print(json.dumps({"type": "thread.started", "thread_id": "thread_test_123"}))
print(json.dumps({"type": "turn.started"}))

for i in range(stdout_count):
    payload = max(0, stdout_size - 14)
    content = f"stdout {i:05d} " + "x" * payload
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "content": content}}))

for i in range(stderr_count):
    payload = max(0, stderr_size - 14)
    print(f"stderr {i:05d} " + "x" * payload, file=sys.stderr)

if failure:
    print(json.dumps({"type": "turn.failed", "error": failure}))
else:
    print(json.dumps({"type": "turn.completed"}))

sys.exit(exit_code)
"""


@pytest.fixture
def state_dir(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "jobs"
    monkeypatch.setenv("CROSSAGENT_STATE_DIR", str(root))
    return root


@pytest.fixture
def fake_codex_in_path(monkeypatch, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_path = bin_dir / "codex"
    codex_path.write_text(_FAKE_CODEX_SCRIPT, encoding="utf-8")
    codex_path.chmod(0o755)
    path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", path)


@pytest.fixture(autouse=True)
def _reset_codex_env(monkeypatch):
    """Clear fake-codex env vars before each test to avoid cross-test leakage."""
    for name in (
        "FAKE_CODEX_SLEEP",
        "FAKE_CODEX_STDOUT_COUNT",
        "FAKE_CODEX_STDOUT_SIZE",
        "FAKE_CODEX_STDERR_COUNT",
        "FAKE_CODEX_STDERR_SIZE",
        "FAKE_CODEX_EXIT_CODE",
        "FAKE_CODEX_FAILURE",
    ):
        monkeypatch.delenv(name, raising=False)


def _wait_for_terminal(job_id: str, timeout: float = 10.0) -> jobs_mod.Job:
    """Poll status until the job reaches a terminal state or timeout expires."""
    state_root = jobs_mod.default_state_root()
    job_dir = jobs_mod.job_dir_path(state_root, job_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            job = jobs_mod.load_state(job_dir)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        job = jobs_mod.reconcile_stale(job, job_dir)
        if jobs_mod.is_terminal(job.status):
            return job
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not reach a terminal state in {timeout}s")


# =========================================================================
# Exit criterion 1: start returns, worker continues, result retrievable
# =========================================================================


def test_start_returns_job_id_and_worker_continues(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    # Keep the advisor alive briefly so start's snapshot cannot race past
    # running into succeeded on a fast machine.
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "hello worker",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert response["schema_version"] == 2
    assert response["job_id"].startswith("job_")
    assert response["status"] in ("pending", "running")
    assert response["advisor"] == "codex"
    assert "started_at" in response

    # start has returned; the worker continues in the background.
    job_id = response["job_id"]
    job = _wait_for_terminal(job_id)
    assert job.status == jobs_mod.JobState.SUCCEEDED


# =========================================================================
# Exit criterion 2: separate process can retrieve status and result
# =========================================================================


def test_status_and_result_from_separate_invocation(
    state_dir, fake_codex_in_path, capsys
):
    main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "hello separate",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    job = _wait_for_terminal(job_id)
    assert job.status == jobs_mod.JobState.SUCCEEDED

    # status from a new invocation
    code = main(["status", job_id, "--json"])
    captured = capsys.readouterr()
    assert code == 0
    status = json.loads(captured.out)
    assert status["job_id"] == job_id
    assert status["status"] == "succeeded"

    # result from a new invocation
    code = main(["result", job_id])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip()


# =========================================================================
# Exit criterion 3: bounded wait returns running for active job
# =========================================================================


def test_wait_returns_running_within_timeout(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "5")
    main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "sleepy",
            "--json",
            "--max-runtime",
            "60",
        ]
    )
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    start = time.monotonic()
    code = main(["wait", job_id, "--timeout", "0.5", "--json"])
    elapsed = time.monotonic() - start
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert elapsed < 2.0, f"wait took too long: {elapsed}s"
    state = json.loads(captured.out)
    assert state["job_id"] == job_id
    assert state["status"] == "running"
    assert "elapsed_seconds" in state

    # Clean up: cancel the long-running job so the suite doesn't hang.
    main(["cancel", job_id])


# =========================================================================
# Exit criterion 4: cancel produces terminal cancelled
# =========================================================================


def test_cancel_terminates_and_persists_cancelled(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "600")
    main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "cancel me",
            "--json",
            "--max-runtime",
            "60",
        ]
    )
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    code = main(["cancel", job_id, "--wait", "--timeout", "3"])
    captured = capsys.readouterr()
    assert code == 0, captured.err

    job = jobs_mod.load_state(
        jobs_mod.job_dir_path(jobs_mod.default_state_root(), job_id)
    )
    assert job.status == jobs_mod.JobState.CANCELLED


# =========================================================================
# Exit criterion 5: stale state reconciles to abandoned
# =========================================================================


def test_status_reconciles_stale_state_to_abandoned(state_dir, capsys):
    job_id = "job_stale_manual"
    job_dir = state_dir / job_id
    job_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc).isoformat()
    job = jobs_mod.Job(
        schema_version=1,
        job_id=job_id,
        status=jobs_mod.JobState.RUNNING,
        advisor="codex",
        name="",
        cwd=os.getcwd(),
        redacted_command="codex exec <prompt>",
        worker_pid=99999999,
        started_at=now,
        updated_at=now,
        last_activity_at=now,
        last_event="worker.started",
    )
    jobs_mod.save_state(job_dir, job)
    (job_dir / "prompt").write_text("hello", encoding="utf-8")
    jobs_mod.atomic_json_write(
        {
            "command": ["codex", "exec"],
            "prompt_delivery": "positional",
            "cwd": os.getcwd(),
            "stream": False,
            "registry_path": str(
                Path.home() / ".config" / "crossagent" / "sessions.json"
            ),
            "key": "",
            "name": None,
            "model": "",
            "advisor": "codex",
        },
        job_dir / "command.json",
    )

    code = main(["status", job_id, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    status = json.loads(captured.out)
    assert status["job_id"] == job_id
    assert status["status"] == "abandoned"


# =========================================================================
# Exit criterion 6: prompt does not leak into state, command, or status
# =========================================================================


def test_prompt_not_exposed_in_state_or_status(state_dir, fake_codex_in_path, capsys):
    secret = "SECRET_PROMPT_42"
    main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            secret,
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    _wait_for_terminal(job_id)

    job_dir = state_dir / job_id

    state_text = (job_dir / "state.json").read_text(encoding="utf-8")
    assert secret not in state_text

    command_text = (job_dir / "command.json").read_text(encoding="utf-8")
    assert secret not in command_text

    main(["status", job_id, "--json"])
    status_text = capsys.readouterr().out
    assert secret not in status_text


# =========================================================================
# Additional CLI behavior
# =========================================================================


def test_result_fails_for_non_terminal_job(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "600")
    main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "not done",
            "--json",
            "--max-runtime",
            "60",
        ]
    )
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    code = main(["result", job_id])
    captured = capsys.readouterr()
    assert code == 1
    assert "no result available" in captured.err

    main(["cancel", job_id])


def test_unknown_job_returns_error(state_dir, capsys):
    code = main(["status", "job_does_not_exist", "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown job" in captured.err
    assert captured.out == ""


# =========================================================================
# list: dashboard over all jobs — nothing silently dropped
# =========================================================================


def _write_manual_job(
    state_root: Path,
    job_id: str,
    status: jobs_mod.JobState,
    *,
    advisor: str = "codex",
    name: str = "",
    started_at: str = "",
    worker_pid: "int | None" = None,
    prompt: str = "hello",
) -> None:
    """Persist a job state dir directly, without launching a real worker."""
    job_dir = state_root / job_id
    job_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    job = jobs_mod.Job(
        schema_version=1,
        job_id=job_id,
        status=status,
        advisor=advisor,
        name=name,
        cwd=os.getcwd(),
        redacted_command=f"{advisor} exec <prompt>",
        worker_pid=worker_pid,
        started_at=started_at or now,
        updated_at=now,
        last_activity_at=now,
        last_event="worker.started",
    )
    jobs_mod.save_state(job_dir, job)
    (job_dir / "prompt").write_text(prompt, encoding="utf-8")


def test_list_shows_all_jobs_newest_first(state_dir, capsys):
    _write_manual_job(
        state_dir,
        "job_20260718T090000_aaaa1111",
        jobs_mod.JobState.SUCCEEDED,
        started_at="2026-07-18T09:00:00+00:00",
        name="older-job",
    )
    _write_manual_job(
        state_dir,
        "job_20260718T110000_bbbb2222",
        jobs_mod.JobState.FAILED,
        started_at="2026-07-18T11:00:00+00:00",
        name="newer-job",
    )

    code = main(["list", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert response["schema_version"] == 1
    job_ids = [entry["job_id"] for entry in response["jobs"]]
    assert job_ids == ["job_20260718T110000_bbbb2222", "job_20260718T090000_aaaa1111"]
    newest = response["jobs"][0]
    assert newest["status"] == "failed"
    assert newest["advisor"] == "codex"
    assert newest["name"] == "newer-job"
    assert "elapsed_seconds" in newest
    assert "idle_seconds" in newest
    assert "last_event" in newest


def test_list_reconciles_stale_running_to_abandoned(state_dir, capsys):
    _write_manual_job(
        state_dir, "job_stale_for_list", jobs_mod.JobState.RUNNING, worker_pid=99999999
    )

    code = main(["list", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert len(response["jobs"]) == 1
    assert response["jobs"][0]["status"] == "abandoned"


def test_list_filters_by_status(state_dir, capsys):
    _write_manual_job(state_dir, "job_list_ok", jobs_mod.JobState.SUCCEEDED)
    _write_manual_job(state_dir, "job_list_bad", jobs_mod.JobState.FAILED)

    code = main(["list", "--status", "failed", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert [entry["job_id"] for entry in response["jobs"]] == ["job_list_bad"]


def test_list_respects_limit(state_dir, capsys):
    for hour in ("09", "10", "11"):
        _write_manual_job(
            state_dir,
            f"job_20260718T{hour}0000_cccc3333",
            jobs_mod.JobState.SUCCEEDED,
            started_at=f"2026-07-18T{hour}:00:00+00:00",
        )

    code = main(["list", "--limit", "2", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert len(response["jobs"]) == 2
    assert response["jobs"][0]["job_id"] == "job_20260718T110000_cccc3333"


def test_list_empty_state_root_is_ok(state_dir, capsys):
    code = main(["list", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert response["jobs"] == []


def test_list_human_output_is_a_table(state_dir, capsys):
    _write_manual_job(
        state_dir, "job_table_row", jobs_mod.JobState.SUCCEEDED, name="table-test"
    )

    code = main(["list"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "JOB ID" in captured.out
    assert "job_table_row" in captured.out
    assert "succeeded" in captured.out
    assert "table-test" in captured.out


def test_list_never_exposes_prompt(state_dir, capsys):
    secret = "LIST_SECRET_PROMPT_99"
    _write_manual_job(
        state_dir,
        "job_list_secret",
        jobs_mod.JobState.RUNNING,
        worker_pid=99999999,
        prompt=secret,
    )

    main(["list", "--json"])
    assert secret not in capsys.readouterr().out
    main(["list"])
    assert secret not in capsys.readouterr().out


def test_list_skips_corrupt_state_with_warning(state_dir, capsys):
    _write_manual_job(state_dir, "job_list_good", jobs_mod.JobState.SUCCEEDED)
    corrupt_dir = state_dir / "job_list_corrupt"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "state.json").write_text("{not json", encoding="utf-8")

    code = main(["list", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    response = json.loads(captured.out)
    assert [entry["job_id"] for entry in response["jobs"]] == ["job_list_good"]
    assert "job_list_corrupt" in captured.err  # skipped loudly, not silently


def test_logs_reads_stdout(state_dir, fake_codex_in_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_STDOUT_COUNT", "1")
    monkeypatch.setenv("FAKE_CODEX_STDOUT_SIZE", "30")
    main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "log me",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    _wait_for_terminal(job_id)

    code = main(["logs", job_id])
    captured = capsys.readouterr()
    assert code == 0
    # stdout.log contains the raw Codex JSONL lines.
    assert "stdout 00000" in captured.out
    assert "item.completed" in captured.out


# =========================================================================
# Lineage persistence on start
# =========================================================================


def test_start_persists_resolved_lineage(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    """A start invocation with lineage flags persists the resolved lineage."""
    # Create a real parent so --parent resolves and inherits trace.
    parent_id = "job_parent_001"
    parent_dir = state_dir / parent_id
    parent_dir.mkdir(parents=True)
    parent_job = jobs_mod.Job(
        job_id=parent_id,
        schema_version=2,
        status=jobs_mod.JobState.SUCCEEDED,
        trace_id="trace_parent",
    )
    jobs_mod.save_state(parent_dir, parent_job)

    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "lineage test",
            "--parent",
            parent_id,
            "--orchestrator-label",
            "my-tree",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    assert code == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    job = jobs_mod.load_state(state_dir / job_id)
    assert job.parent_job_id == parent_id
    assert job.trace_id == "trace_parent"  # inherited from parent
    assert job.orchestrator_label == "my-tree"
    assert job.nesting_depth == 2  # 1 ancestor (parent) + 1


def test_start_persists_lineage_from_env_vars(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    """A start invocation with env vars persists the resolved lineage."""
    # Create a real parent so the inherited env resolves.
    parent_id = "job_env_parent"
    parent_dir = state_dir / parent_id
    parent_dir.mkdir(parents=True)
    parent_job = jobs_mod.Job(
        job_id=parent_id,
        schema_version=2,
        status=jobs_mod.JobState.SUCCEEDED,
        trace_id="trace_env_001",
    )
    jobs_mod.save_state(parent_dir, parent_job)

    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    monkeypatch.setenv("CROSSAGENT_PARENT_JOB_ID", parent_id)
    monkeypatch.setenv("CROSSAGENT_TRACE_ID", "trace_env_001")
    monkeypatch.setenv("CROSSAGENT_ORCHESTRATOR_LABEL", "env-tree")

    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "env lineage test",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    assert code == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    job = jobs_mod.load_state(state_dir / job_id)
    assert job.parent_job_id == parent_id
    assert job.trace_id == "trace_env_001"
    assert job.orchestrator_label == "env-tree"
    assert job.nesting_depth == 2  # 1 ancestor (parent) + 1


def test_start_with_no_parent_flag_resets_lineage(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    """--no-parent forces top-level regardless of env."""
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    monkeypatch.setenv("CROSSAGENT_PARENT_JOB_ID", "job_env_parent")

    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "no parent test",
            "--no-parent",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    assert code == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    job = jobs_mod.load_state(state_dir / job_id)
    assert job.parent_job_id is None
    assert job.nesting_depth == 1


def test_start_lineage_explicit_flag_overrides_env(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    """Explicit CLI flags override env vars."""
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    monkeypatch.setenv("CROSSAGENT_TRACE_ID", "env_trace_should_not_appear")

    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "flag override test",
            "--trace-id",
            "flag_trace_value",
            "--orchestrator-label",
            "flag_label",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    assert code == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    job = jobs_mod.load_state(state_dir / job_id)
    assert job.trace_id == "flag_trace_value"
    assert job.orchestrator_label == "flag_label"


# =========================================================================
# Lineage validation  (CLI integration)
# =========================================================================


def test_start_explicit_missing_parent_exits_nonzero(state_dir, monkeypatch, capsys):
    """--parent with a non-existent job exits nonzero with a clear message."""
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "test",
            "--parent",
            "job_does_not_exist",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    captured = capsys.readouterr()
    assert code != 0, f"expected nonzero exit, got {code}"
    assert "not found" in captured.err


def test_start_inherited_missing_parent_produces_orphan(
    state_dir, fake_codex_in_path, monkeypatch, capsys
):
    """CROSSAGENT_PARENT_JOB_ID pointing at a non-existent job is an orphan:
    succeeds, keeps parent_job_id, depth None."""
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    monkeypatch.setenv("CROSSAGENT_PARENT_JOB_ID", "job_nonexistent_parent")
    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "orphan test",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    assert code == 0, f"expected success for inherited orphan, got {code}"
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    job = jobs_mod.load_state(state_dir / job_id)
    assert job.parent_job_id == "job_nonexistent_parent"
    assert job.nesting_depth is None


def test_start_trace_conflict_with_parent_exits_nonzero(state_dir, monkeypatch, capsys):
    """--trace-id that differs from a loadable parent's trace exits nonzero."""
    parent_id = "job_parent_tc"
    parent_dir = state_dir / parent_id
    parent_dir.mkdir(parents=True)
    parent_job = jobs_mod.Job(
        job_id=parent_id,
        schema_version=2,
        status=jobs_mod.JobState.SUCCEEDED,
        trace_id="trace_parent_val",
    )
    jobs_mod.save_state(parent_dir, parent_job)

    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "trace conflict",
            "--parent",
            parent_id,
            "--trace-id",
            "trace_different",
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    captured = capsys.readouterr()
    assert code != 0, f"expected nonzero exit, got {code}"
    assert "Trace ID conflict" in captured.err


def test_start_cycle_detected_exits_nonzero(state_dir, monkeypatch, capsys):
    """A cycle in the parent chain exits nonzero."""
    # Build A -> B -> A
    job_a_id = "job_cycle_cli_a"
    job_b_id = "job_cycle_cli_b"
    job_a = jobs_mod.Job(
        job_id=job_a_id,
        schema_version=2,
        status=jobs_mod.JobState.SUCCEEDED,
        parent_job_id=job_b_id,
    )
    job_b = jobs_mod.Job(
        job_id=job_b_id,
        schema_version=2,
        status=jobs_mod.JobState.SUCCEEDED,
        parent_job_id=job_a_id,
    )
    (state_dir / job_a_id).mkdir(parents=True)
    (state_dir / job_b_id).mkdir(parents=True)
    jobs_mod.save_state(state_dir / job_a_id, job_a)
    jobs_mod.save_state(state_dir / job_b_id, job_b)

    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    code = main(
        [
            "start",
            "--agent",
            "codex",
            "--prompt",
            "cycle test",
            "--parent",
            job_a_id,
            "--json",
            "--max-runtime",
            "30",
        ]
    )
    captured = capsys.readouterr()
    assert code != 0, f"expected nonzero exit, got {code}"
    assert "Cycle detected" in captured.err


def test_start_depth_cap_exits_nonzero(state_dir, monkeypatch, capsys):
    """Starting a child beyond MAX_NESTING_DEPTH exits nonzero."""
    jobs_mod.MAX_NESTING_DEPTH = 3  # lower for test

    try:
        prev_id = None
        for i in range(3, 0, -1):
            jid = f"job_dc_{i}"
            job = jobs_mod.Job(
                job_id=jid,
                schema_version=2,
                status=jobs_mod.JobState.SUCCEEDED,
                parent_job_id=prev_id,
            )
            (state_dir / jid).mkdir(parents=True)
            jobs_mod.save_state(state_dir / jid, job)
            prev_id = jid

        monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
        code = main(
            [
                "start",
                "--agent",
                "codex",
                "--prompt",
                "depth cap",
                "--parent",
                prev_id,
                "--json",
                "--max-runtime",
                "30",
            ]
        )
        captured = capsys.readouterr()
        assert code != 0, f"expected nonzero exit, got {code}"
        assert "Maximum nesting depth" in captured.err
        assert "exceeded" in captured.err
    finally:
        jobs_mod.MAX_NESTING_DEPTH = 8  # restore
