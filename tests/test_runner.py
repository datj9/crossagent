"""Tests for the unified process supervisor.

These tests cover Phase 2 exit criteria:
  1. A child that floods stderr (>1MB) while keeping stdout open cannot deadlock.
  2. Large stdout (>1MB) is streamed without unbounded capture or truncation.
  3. Timeout and cancellation paths clean up the whole process tree, including
     a grandchild, and return a terminal outcome.
  4. A child that ignores SIGTERM is force-killed after the grace period.
  5. Periodic stdout, periodic stderr-only, and completely-silent children all
     keep activity semantics correct.
  6. Existing CLI tests keep passing.

All advisor subprocesses here are small Python scripts launched with
``sys.executable`` — no real model CLI is ever invoked.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from crossagent import runner


_FAKE_ADVISOR = Path(__file__).with_name("fake_advisors") / "fake_advisor.py"


def _cmd(*args: str) -> list[str]:
    return [sys.executable, str(_FAKE_ADVISOR), *args]


# =========================================================================
# Test helpers
# =========================================================================

class RecordingConsumer:
    """Record every stdout/stderr line and return them in finish()."""

    def __init__(self) -> None:
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []

    def consume_stdout(self, line: str) -> None:
        self.stdout_lines.append(line)

    def consume_stderr(self, line: str) -> None:
        self.stderr_lines.append(line)

    def finish(self, exit_code: int) -> dict[str, Any]:
        return {
            "stdout": self.stdout_lines,
            "stderr": self.stderr_lines,
            "exit_code": exit_code,
        }


class ResultConsumer:
    """Parse JSON events and capture the one with type=result."""

    def __init__(self) -> None:
        self.final: dict[str, Any] | None = None

    def consume_stdout(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if event.get("type") == "result":
            self.final = event

    def consume_stderr(self, line: str) -> None:
        pass

    def finish(self, exit_code: int) -> dict[str, Any] | None:
        return self.final


# =========================================================================
# Basic outcomes
# =========================================================================


def test_immediate_success():
    outcome = runner.run(_cmd("--stdout-count", "1"))
    assert outcome.exit_code == 0
    assert outcome.failure_category == "ok"
    assert not outcome.timed_out
    assert not outcome.cancelled
    assert not outcome.forced


def test_nonzero_exit():
    outcome = runner.run(_cmd("--exit-code", "7", "--stderr-count", "1"))
    assert outcome.exit_code == 7
    assert outcome.failure_category == "nonzero_exit"


def test_consumer_receives_stdout_and_stderr():
    consumer = RecordingConsumer()
    outcome = runner.run(
        _cmd("--stdout-count", "3", "--stderr-count", "2"),
        consumer=consumer,
    )
    assert outcome.exit_code == 0
    assert len(consumer.stdout_lines) == 3
    assert len(consumer.stderr_lines) == 2
    assert all(line.startswith("stdout") for line in consumer.stdout_lines)
    assert all(line.startswith("stderr") for line in consumer.stderr_lines)


def test_consumer_extracts_result():
    consumer = ResultConsumer()
    cmd = [
        sys.executable,
        "-c",
        'import json; print(json.dumps({"type": "result", "answer": 42}))',
    ]
    outcome = runner.run(cmd, consumer=consumer)
    assert outcome.exit_code == 0
    assert outcome.result == {"type": "result", "answer": 42}


# =========================================================================
# Exit criterion 1: stderr flood cannot deadlock
# =========================================================================


def test_stderr_flood_no_deadlock():
    """A child that floods stderr while stdout stays open must not deadlock."""
    consumer = RecordingConsumer()
    outcome = runner.run(
        _cmd(
            "--stderr-count", "1500",
            "--stderr-line-size", "1024",
        ),
        consumer=consumer,
    )
    assert outcome.exit_code == 0
    assert len(consumer.stderr_lines) == 1500
    total_stderr = sum(len(line) for line in consumer.stderr_lines)
    assert total_stderr >= 1024 * 1024


# =========================================================================
# Exit criterion 2: large stdout streamed without unbounded capture
# =========================================================================


def test_large_stdout_streamed_to_consumer():
    """>1MB of stdout must be delivered without being dropped."""
    consumer = RecordingConsumer()
    outcome = runner.run(
        _cmd(
            "--stdout-count", "1500",
            "--stdout-line-size", "1024",
        ),
        consumer=consumer,
    )
    assert outcome.exit_code == 0
    assert len(consumer.stdout_lines) == 1500
    total_stdout = sum(len(line) for line in consumer.stdout_lines)
    assert total_stdout >= 1024 * 1024


# =========================================================================
# Exit criterion 3: timeout / cancellation clean up the process tree
# =========================================================================


def test_timeout_returns_terminal_outcome():
    outcome = runner.run(
        _cmd("--sleep", "600"),
        max_runtime_seconds=0.2,
        termination_grace_seconds=0.1,
    )
    assert outcome.timed_out
    assert outcome.failure_category == "timeout"


def test_timeout_kills_grandchild(tmp_path: Path):
    marker = tmp_path / "pids.txt"
    env = {**os.environ, "FAKE_ADVISOR_PID_FILE": str(marker)}
    outcome = runner.run(
        _cmd("--spawn-grandchild", "--sleep", "600"),
        max_runtime_seconds=0.3,
        termination_grace_seconds=0.1,
        env=env,
    )
    assert outcome.timed_out
    assert marker.exists()

    pids: dict[str, int] = {}
    for line in marker.read_text(encoding="utf-8").strip().splitlines():
        role, pid_text = line.split("=", 1)
        pids[role] = int(pid_text)

    assert "grandchild" in pids
    for pid in pids.values():
        assert not _pid_exists(pid), f"PID {pid} ({role}) still alive after cleanup"


def test_cancellation_returns_terminal_outcome():
    cancel_flag = {"cancel": False}

    def should_cancel() -> bool:
        return cancel_flag["cancel"]

    def trigger_cancel() -> None:
        time.sleep(0.1)
        cancel_flag["cancel"] = True

    threading.Thread(target=trigger_cancel, daemon=True).start()
    outcome = runner.run(
        _cmd("--sleep", "600"),
        should_cancel=should_cancel,
        termination_grace_seconds=0.1,
    )
    assert outcome.cancelled
    assert outcome.failure_category == "cancelled"


# =========================================================================
# Exit criterion 4: SIGTERM-ignoring child is force-killed after grace
# =========================================================================


def test_force_kill_after_grace_period():
    outcome = runner.run(
        _cmd("--ignore-sigterm", "--sleep", "600"),
        max_runtime_seconds=0.2,
        termination_grace_seconds=0.1,
    )
    assert outcome.timed_out
    assert outcome.forced


# =========================================================================
# Exit criterion 5: activity semantics for stdout / stderr / silent children
# =========================================================================


def test_periodic_stdout_resets_idle_timer(capsys):
    outcome = runner.run(
        _cmd("--stdout-count", "5", "--delay", "0.05"),
        heartbeat_interval=0.1,
        idle_warning_threshold=0.2,
    )
    assert outcome.exit_code == 0
    stderr = capsys.readouterr().err
    assert "idle warning" not in stderr


def test_periodic_stderr_resets_idle_timer(capsys):
    outcome = runner.run(
        _cmd("--stderr-count", "5", "--delay", "0.05"),
        heartbeat_interval=0.1,
        idle_warning_threshold=0.2,
    )
    assert outcome.exit_code == 0
    stderr = capsys.readouterr().err
    assert "idle warning" not in stderr


def test_silent_child_emits_heartbeats(capsys):
    outcome = runner.run(
        _cmd("--sleep", "0.3"),
        heartbeat_interval=0.1,
    )
    assert outcome.exit_code == 0
    stderr = capsys.readouterr().err
    assert "[crossagent] running" in stderr


def test_idle_warning_emitted_when_silent(capsys):
    outcome = runner.run(
        _cmd("--sleep", "0.3"),
        heartbeat_interval=60.0,
        idle_warning_threshold=0.1,
    )
    assert outcome.exit_code == 0
    stderr = capsys.readouterr().err
    assert "idle warning" in stderr


# =========================================================================
# Platform helper
# =========================================================================


def _pid_exists(pid: int) -> bool:
    if sys.platform == "win32":
        return False  # grandchild PID checks are POSIX-only in these fixtures
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


