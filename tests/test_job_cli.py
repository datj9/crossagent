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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crossagent import jobs as jobs_mod
from crossagent.cli import main


_FAKE_CODEX_SCRIPT = '''#!/usr/bin/env python3
import os
import sys
import time

sleep = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
stdout_count = int(os.environ.get("FAKE_CODEX_STDOUT_COUNT", "1"))
stdout_size = int(os.environ.get("FAKE_CODEX_STDOUT_SIZE", "30"))
stderr_count = int(os.environ.get("FAKE_CODEX_STDERR_COUNT", "0"))
stderr_size = int(os.environ.get("FAKE_CODEX_STDERR_SIZE", "30"))
exit_code = int(os.environ.get("FAKE_CODEX_EXIT_CODE", "0"))

if sleep:
    time.sleep(sleep)

for i in range(stdout_count):
    payload = max(0, stdout_size - 14)
    print(f"stdout {i:05d} " + "x" * payload)

for i in range(stderr_count):
    payload = max(0, stderr_size - 14)
    print(f"stderr {i:05d} " + "x" * payload, file=sys.stderr)

sys.exit(exit_code)
'''


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


def test_start_returns_job_id_and_worker_continues(state_dir, fake_codex_in_path, capsys):
    code = main([
        "start", "--agent", "codex",
        "--prompt", "hello worker",
        "--json",
        "--max-runtime", "30",
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    response = json.loads(captured.out)
    assert response["schema_version"] == 1
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


def test_status_and_result_from_separate_invocation(state_dir, fake_codex_in_path, capsys):
    main([
        "start", "--agent", "codex",
        "--prompt", "hello separate",
        "--json",
        "--max-runtime", "30",
    ])
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


def test_wait_returns_running_within_timeout(state_dir, fake_codex_in_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "5")
    main([
        "start", "--agent", "codex",
        "--prompt", "sleepy",
        "--json",
        "--max-runtime", "60",
    ])
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


def test_cancel_terminates_and_persists_cancelled(state_dir, fake_codex_in_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "600")
    main([
        "start", "--agent", "codex",
        "--prompt", "cancel me",
        "--json",
        "--max-runtime", "60",
    ])
    job_id = json.loads(capsys.readouterr().out)["job_id"]

    code = main(["cancel", job_id, "--wait", "--timeout", "3"])
    captured = capsys.readouterr()
    assert code == 0, captured.err

    job = jobs_mod.load_state(jobs_mod.job_dir_path(jobs_mod.default_state_root(), job_id))
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
            "registry_path": str(Path.home() / ".config" / "crossagent" / "sessions.json"),
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
    main([
        "start", "--agent", "codex",
        "--prompt", secret,
        "--json",
        "--max-runtime", "30",
    ])
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


def test_result_fails_for_non_terminal_job(state_dir, fake_codex_in_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "600")
    main([
        "start", "--agent", "codex",
        "--prompt", "not done",
        "--json",
        "--max-runtime", "60",
    ])
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


def test_logs_reads_stdout(state_dir, fake_codex_in_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_STDOUT_COUNT", "1")
    monkeypatch.setenv("FAKE_CODEX_STDOUT_SIZE", "30")
    main([
        "start", "--agent", "codex",
        "--prompt", "log me",
        "--json",
        "--max-runtime", "30",
    ])
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    _wait_for_terminal(job_id)

    code = main(["logs", job_id])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip().startswith("stdout 00000")
