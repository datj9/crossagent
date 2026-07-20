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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import jobs as jobs_mod
from . import parsers as parsers_mod
from . import registry as reg_mod
from . import runner as runner_mod


# ---------------------------------------------------------------------------
# Command metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JobCommand:
    command: list[str]
    prompt_delivery: str
    cwd: str
    result_parser: str
    registry_path: str
    key: str
    name: Optional[str]
    model: str
    advisor: str


# ---------------------------------------------------------------------------
# Logging parser wrapper
# ---------------------------------------------------------------------------


class _LoggingParser:
    """Wrap a parser and mirror stdout/stderr to their log files."""

    def __init__(
        self,
        parser: parsers_mod.EventParser,
        stdout_log: Path,
        stderr_log: Path,
    ) -> None:
        self._parser = parser
        self._stdout_file = open(stdout_log, "w", encoding="utf-8", buffering=1)
        self._stderr_file = open(stderr_log, "w", encoding="utf-8", buffering=1)

    def consume_stdout(self, line: str) -> None:
        self._stdout_file.write(line)
        self._stdout_file.flush()
        self._parser.consume_stdout(line)

    def consume_stderr(self, line: str) -> None:
        self._stderr_file.write(line)
        self._stderr_file.flush()
        self._parser.consume_stderr(line)

    def finish(self, exit_code: int) -> parsers_mod.ParsedResult:
        self._stdout_file.close()
        self._stderr_file.close()
        return self._parser.finish(exit_code)


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

    last_state_write = time.monotonic()

    def _on_activity(stream: str) -> None:
        nonlocal job, last_state_write
        now = time.monotonic()
        if now - last_state_write < 1.0:
            return
        last_state_write = now
        job = jobs_mod.transition_to(
            job,
            jobs_mod.JobState.RUNNING,
            job_dir=job_dir,
            last_activity_at=datetime.now(timezone.utc).isoformat(),
            last_event=f"{stream}.activity",
        )

    parser = parsers_mod.get_parser(command.result_parser, on_activity=_on_activity)
    consumer = _LoggingParser(parser, stdout_log, stderr_log)

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

    advisor_env = build_advisor_env(job, state_dir)

    try:
        outcome = runner_mod.run(
            cmd,
            cwd=command.cwd,
            env=advisor_env,
            consumer=consumer,
            max_runtime_seconds=job.max_runtime_seconds,
            termination_grace_seconds=job.termination_grace_seconds,
            should_cancel=_should_cancel,
        )
    finally:
        consumer.finish(0)

    parsed = (
        outcome.result
        if isinstance(outcome.result, parsers_mod.ParsedResult)
        else parsers_mod.ParsedResult()
    )

    if parsed.session_id:
        job = jobs_mod.transition_to(
            job,
            jobs_mod.JobState.RUNNING,
            job_dir=job_dir,
            advisor_session_id=parsed.session_id,
        )
        if command.key:
            registry = reg_mod.load(Path(command.registry_path))
            reg_mod.record(
                Path(command.registry_path),
                registry,
                command.key,
                session_id=parsed.session_id,
                name=command.name,
                cwd=command.cwd,
                advisor=command.advisor,
                model=command.model,
            )

    if parsed.result is not None:
        result_path.write_text(parsed.result, encoding="utf-8")
        _chmod_private(result_path)

    if outcome.timed_out:
        final_state = jobs_mod.JobState.TIMED_OUT
        error = f"Maximum runtime of {job.max_runtime_seconds}s exceeded"
    elif outcome.cancelled:
        final_state = jobs_mod.JobState.CANCELLED
        error = "Cancelled by user"
    elif parsed.failure:
        final_state = jobs_mod.JobState.FAILED
        error = parsed.error or "Advisor reported a failure"
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
        result_parser=str(data.get("result_parser", "text")),
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
# Advisor environment builder
# ---------------------------------------------------------------------------


def build_advisor_env(job: jobs_mod.Job, state_root: Path) -> dict[str, str]:
    """Build the environment dict for the advisor subprocess with lineage vars.

    Starts from ``os.environ.copy()`` and overwrites (does not setdefault) the
    ``CROSSAGENT_PARENT_JOB_ID``, ``CROSSAGENT_TRACE_ID``,
    ``CROSSAGENT_ORCHESTRATOR_LABEL``, ``CROSSAGENT_NESTING_DEPTH``,
    and ``CROSSAGENT_STATE_DIR`` variables so a nested ``crossagent start``
    inside the advisor inherits the correct lineage.

    This function is deliberately side-effect-free and testable without spawning
    a process.
    """
    env = os.environ.copy()
    env["CROSSAGENT_PARENT_JOB_ID"] = job.job_id
    env["CROSSAGENT_TRACE_ID"] = job.trace_id or ""
    env["CROSSAGENT_ORCHESTRATOR_LABEL"] = job.orchestrator_label or ""
    env["CROSSAGENT_NESTING_DEPTH"] = (
        str(job.nesting_depth) if job.nesting_depth is not None else ""
    )
    env["CROSSAGENT_STATE_DIR"] = str(state_root)
    return env


# ---------------------------------------------------------------------------
# Worker launcher
# ---------------------------------------------------------------------------


def start_worker(job_id: str, state_root: Path) -> subprocess.Popen[Any]:
    """Launch a detached worker process for *job_id* and return its handle."""
    cmd = [
        sys.executable,
        "-m",
        "crossagent",
        "worker",
        job_id,
        "--state-dir",
        str(state_root),
    ]
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
