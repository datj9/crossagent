"""Detached worker for durable cross-tool jobs.

Phase 3 of the durable cross-tool delegation plan: launched by ``crossagent start``,
reads the persisted job metadata and prompt, runs the advisor via the shared
runner, and is the only writer of the job's lifecycle state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import jobs as jobs_mod
from . import runner as runner_mod


# ---------------------------------------------------------------------------
# Command metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _JobCommand:
    command: list[str]
    prompt_delivery: str
    cwd: str
    stream: bool
    registry_path: str
    key: str
    name: Optional[str]
    model: str
    advisor: str


# ---------------------------------------------------------------------------
# Worker consumer
# ---------------------------------------------------------------------------

class _WorkerTextConsumer:
    """Streams stdout/stderr to their log files and writes stdout to result.md."""

    def __init__(self, stdout_log: Path, stderr_log: Path, result_path: Path) -> None:
        self._stdout_file = open(stdout_log, "w", encoding="utf-8")
        self._stderr_file = open(stderr_log, "w", encoding="utf-8")
        self._result_file = open(result_path, "w", encoding="utf-8")

    def consume_stdout(self, line: str) -> None:
        self._stdout_file.write(line)
        self._result_file.write(line)

    def consume_stderr(self, line: str) -> None:
        self._stderr_file.write(line)

    def finish(self, exit_code: int) -> None:
        self._stdout_file.close()
        self._stderr_file.close()
        self._result_file.close()

    def get_result(self) -> Optional[str]:
        return None


class _WorkerStreamConsumer:
    """Logs stdout/stderr, delegates stream-json parsing to the CLI consumer, and
    records the final result.
    """

    def __init__(
        self,
        stdout_log: Path,
        stderr_log: Path,
        registry_path: Path,
        key: str,
        name: Optional[str],
        cwd: str,
        advisor: str,
        model: str,
    ) -> None:
        self._stdout_file = open(stdout_log, "w", encoding="utf-8")
        self._stderr_file = open(stderr_log, "w", encoding="utf-8")
        self._registry_path = registry_path
        self._key = key
        self._name = name
        self._cwd = cwd
        self._advisor = advisor
        self._model = model
        # Lazy import to avoid circular dependency with cli.py.
        from . import cli as cli_mod
        self._stream_consumer = cli_mod._StreamConsumer()

    def consume_stdout(self, line: str) -> None:
        self._stdout_file.write(line)
        self._stream_consumer.consume_stdout(line)

    def consume_stderr(self, line: str) -> None:
        self._stderr_file.write(line)
        self._stream_consumer.consume_stderr(line)

    def finish(self, exit_code: int) -> Optional[dict[str, Any]]:
        return self._stream_consumer.finish(exit_code)

    def get_result(self) -> Optional[str]:
        final = self._stream_consumer.finish(0)
        if not final:
            return None
        self._record_session(final)
        result = final.get("result")
        if result is not None:
            return result
        structured = final.get("structured_output")
        if structured is not None:
            return json.dumps(structured, indent=2, sort_keys=True)
        return None

    def _record_session(self, final: dict[str, Any]) -> None:
        if not self._key or not self._registry_path:
            return
        session_id = final.get("session_id")
        if not session_id:
            return
        from . import registry as reg_mod
        registry = reg_mod.load(self._registry_path)
        reg_mod.record(
            self._registry_path,
            registry,
            self._key,
            session_id=session_id,
            name=self._name,
            cwd=self._cwd,
            advisor=self._advisor,
            model=self._model,
        )

    def close(self) -> None:
        self._stdout_file.close()
        self._stderr_file.close()


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------

def worker_main(job_id: str, state_dir: Path) -> int:
    """Run the advisor for *job_id* and persist the full lifecycle.

    This is the entry point for the detached worker process launched by
    ``crossagent start``.
    """
    job_dir = state_dir / job_id
    job = jobs_mod.load_state(job_dir)
    command = _load_command(job_dir)
    prompt = (job_dir / "prompt").read_text(encoding="utf-8")

    cmd = list(command.command)
    _append_prompt(cmd, command.prompt_delivery, prompt)

    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"
    result_path = job_dir / "result.md"

    if command.stream:
        consumer: Any = _WorkerStreamConsumer(
            stdout_log,
            stderr_log,
            Path(command.registry_path),
            command.key,
            command.name,
            command.cwd,
            command.advisor,
            command.model,
        )
    else:
        consumer = _WorkerTextConsumer(stdout_log, stderr_log, result_path)

    def _should_cancel() -> bool:
        return jobs_mod.cancel_requested(job_dir)

    now = datetime.now(timezone.utc).isoformat()
    job = jobs_mod.transition_to(
        job,
        jobs_mod.JobState.RUNNING,
        job_dir=job_dir,
        worker_pid=os.getpid(),
        last_activity_at=now,
        last_event="worker.started",
    )

    try:
        outcome = runner_mod.run(
            cmd,
            cwd=command.cwd,
            consumer=consumer,
            max_runtime_seconds=job.max_runtime_seconds,
            termination_grace_seconds=job.termination_grace_seconds,
            should_cancel=_should_cancel,
        )
    finally:
        if hasattr(consumer, "close"):
            consumer.close()

    result = consumer.get_result()
    if result is not None:
        result_path.write_text(result, encoding="utf-8")
        _chmod_private(result_path)

    if outcome.timed_out:
        final_state = jobs_mod.JobState.TIMED_OUT
        error = f"Maximum runtime of {job.max_runtime_seconds}s exceeded"
    elif outcome.cancelled:
        final_state = jobs_mod.JobState.CANCELLED
        error = "Cancelled by user"
    elif outcome.exit_code == 0:
        final_state = jobs_mod.JobState.SUCCEEDED
        error = None
    else:
        final_state = jobs_mod.JobState.FAILED
        error = f"Advisor exited with code {outcome.exit_code}"

    now = datetime.now(timezone.utc).isoformat()
    jobs_mod.transition_to(
        job,
        final_state,
        job_dir=job_dir,
        error=error,
        advisor_exit_code=outcome.exit_code,
        last_activity_at=now,
        last_event=final_state.value,
    )

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_command(job_dir: Path) -> _JobCommand:
    path = job_dir / "command.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return _JobCommand(
        command=list(data["command"]),
        prompt_delivery=str(data["prompt_delivery"]),
        cwd=str(data["cwd"]),
        stream=bool(data["stream"]),
        registry_path=str(data["registry_path"]),
        key=str(data["key"]),
        name=data.get("name"),
        model=str(data.get("model", "")),
        advisor=str(data["advisor"]),
    )


def _append_prompt(cmd: list[str], delivery: str, prompt: str) -> None:
    if delivery == "dashdash":
        cmd.extend(["--", prompt])
    elif delivery.startswith("flag:"):
        cmd.extend([delivery.split(":", 1)[1], prompt])
    else:
        cmd.append(prompt)


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Worker launcher
# ---------------------------------------------------------------------------

def start_worker(job_id: str, state_root: Path) -> subprocess.Popen[Any]:
    """Launch a detached worker process for *job_id* and return its handle."""
    cmd = [sys.executable, "-m", "crossagent", "worker", job_id, "--state-dir", str(state_root)]
    # Ensure the worker can import the crossagent package even when the parent
    # was launched via PYTHONPATH/sys.path manipulation (e.g. pytest, editable installs).
    import crossagent
    src_dir = os.path.dirname(os.path.dirname(crossagent.__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


# ---------------------------------------------------------------------------
# CLI entry used by __main__.py
# ---------------------------------------------------------------------------

def parse_worker_args(argv: list[str]) -> tuple[str, Path]:
    import argparse
    parser = argparse.ArgumentParser(prog="crossagent worker")
    parser.add_argument("job_id")
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    return args.job_id, Path(args.state_dir)
