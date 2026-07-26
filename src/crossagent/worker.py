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

from . import advisors as advisors_mod
from . import check as check_mod
from . import credentials as credentials_mod
from . import escalate as escalate_mod
from . import jobs as jobs_mod
from . import parsers as parsers_mod
from . import registry as reg_mod
from . import runner as runner_mod
from . import scope as scope_mod
from . import verify as verify_mod


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
    check: Optional[str]
    check_timeout: float
    # Delegation security posture (slice S4). ``scope_paths`` is the declared
    # allowlist — ``None`` means no scope was declared (enforcement off), which
    # stays distinct from a declared-and-empty list. ``pass_env`` names the
    # credential env vars the caller opted to pass through to the delegate.
    scope_paths: Optional[list[str]]
    pass_env: list[str]
    # Independent verification + escalation (slice S5). ``verify_with`` names the
    # advisor that grades the delegate's artifact in a fresh session (``None`` =
    # off). ``escalate_to`` is the ordered ladder of ``advisor[:model]`` rungs a
    # failed delegation is re-dispatched up ([] = off).
    verify_with: Optional[str]
    verify_model: Optional[str]
    escalate_to: list[str]


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

    # Capture the pre-delegation working-tree baseline BEFORE the delegate runs,
    # so a tree that was already dirty is not later misattributed to it (S4).
    # Only when a scope was declared — otherwise enforcement is off entirely.
    scope_baseline = (
        scope_mod.capture_baseline(command.cwd)
        if command.scope_paths is not None
        else None
    )

    # Credential-bearing env vars are withheld from the delegate by default
    # (S4). Record only the NAMES withheld (never values) for the audit trail;
    # both the launch env and this record use the same predicate, so they agree.
    withheld = credentials_mod.withheld_names(os.environ, command.pass_env)
    advisor_env = build_advisor_env(job, state_dir, pass_env=command.pass_env)
    if withheld:
        jobs_mod.append_event(
            job_dir,
            "env_scrub",
            actor="system:security",
            withheld=withheld,
            count=len(withheld),
        )

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

    # Run the independent check-gate (S3) if one was configured. This runs
    # regardless of the delegate's own outcome so failing-check output is
    # captured even when the delegate crashed. ``None`` means no check ran →
    # the delegation is *unverified*, never silently a pass (D5).
    check_result = _run_configured_check(command, job_dir)

    # Run the diff-scope assertion (S4) if an allowlist was declared. It fails
    # closed: a violation OR an inability to determine what changed is recorded
    # distinctly and drives the delegation verdict to failed, never a pass.
    scope_result = _run_scope_assertion(command, scope_baseline, job_dir)

    # Run the independent verification pass (S5) if a verifier was declared. A
    # FRESH peer session grades the delegate's artifact supplied as user-turn
    # input (D6). A structured ``fail`` verdict blocks the green path; a
    # prose-only or errored verifier degrades to inconclusive, never a pass.
    verify_result = _run_verification(command, prompt, parsed.result, job_dir)

    now = datetime.now(timezone.utc).isoformat()
    # Persist the advisor telemetry the parser extracted (S1) and the check-gate
    # outcome (S3) on the SAME terminal transition — the worker is the only
    # writer of terminal state, so any field not forwarded here never lands on
    # disk. ``parsed`` fields and ``check_result`` already default to unknown /
    # None when nothing was measured (D4/D7), so this never fails the job and
    # never turns an unmeasured metric into a zero or an unrun check into a pass.
    job = jobs_mod.transition_to(
        job,
        final_state,
        job_dir=job_dir,
        error=error,
        advisor_exit_code=outcome.exit_code,
        last_activity_at=now,
        last_event=final_state.value,
        usage_details=parsed.usage_details,
        cost_details=parsed.cost_details,
        duration_ms=parsed.duration_ms,
        cost_source=parsed.cost_source,
        model_reported=parsed.model_reported,
        check_result=check_result,
        scope_result=scope_result,
        withheld_env=withheld,
        verify_result=verify_result,
    )

    # Escalate-on-failure (S5). Runs AFTER the terminal state is persisted, so
    # the failed parent is complete on disk before its same-trace child is
    # spawned. maybe_escalate is a no-op unless the delegation FAILED and an
    # escalation ladder remains; it never raises and respects MAX_NESTING_DEPTH.
    escalate_mod.maybe_escalate(
        job,
        prompt=prompt,
        state_root=state_dir,
        job_dir=job_dir,
        cwd=command.cwd,
        registry_path=command.registry_path,
        escalate_to=command.escalate_to,
        check=command.check,
        check_timeout=command.check_timeout,
        scope_paths=command.scope_paths,
        pass_env=command.pass_env,
        verify_with=command.verify_with,
        verify_model=command.verify_model,
    )

    return 0


def _run_configured_check(
    command: _JobCommand, job_dir: Path
) -> Optional[jobs_mod.CheckResultDict]:
    """Run the caller's ``--check`` command, if any, and return its outcome.

    Returns ``None`` when no check was configured — a missing gate is recorded
    as *unverified*, never as a pass (D5). The check runs in the delegate's cwd
    so it observes the delegate's edits.
    """
    if not command.check:
        return None
    outcome = check_mod.run_check(
        command.check,
        cwd=command.cwd,
        timeout=command.check_timeout,
    )
    # Audit the verdict, but never the check's OUTPUT (which can contain
    # secrets) — only the caller-supplied command and its deterministic exit
    # code go to the append-only audit log.
    jobs_mod.append_event(
        job_dir,
        "check",
        actor="system:check",
        command=command.check,
        exit_code=outcome.exit_code,
    )
    return outcome.to_dict()


def _run_scope_assertion(
    command: _JobCommand,
    baseline: Optional[scope_mod.ScopeBaseline],
    job_dir: Path,
) -> Optional[jobs_mod.ScopeResultDict]:
    """Assert the delegate's writes against the declared allowlist (S4).

    Returns ``None`` when no scope was declared (enforcement off) — kept
    distinct from a declared scope that passed (D7). Otherwise the outcome is
    audited (status + offending paths, which are file paths, never secrets) and
    persisted. ``assert_scope`` never raises, so this can never crash the worker
    nor degrade a scope failure into a silent pass.
    """
    if command.scope_paths is None or baseline is None:
        return None
    outcome = scope_mod.assert_scope(baseline, command.scope_paths, command.cwd)
    jobs_mod.append_event(
        job_dir,
        "scope",
        actor="system:scope",
        status=outcome.status,
        declared=list(outcome.declared),
        violating_paths=list(outcome.violating_paths),
    )
    return outcome.to_dict()


def _run_verification(
    command: _JobCommand,
    prompt: str,
    result_text: Optional[str],
    job_dir: Path,
) -> Optional[jobs_mod.VerifyResultDict]:
    """Run the independent verification pass (S5), if a verifier was declared.

    Returns ``None`` when no verifier was requested — distinct from a
    verification that ran and failed (D7). The verifier is a FRESH peer session
    grading the delegate's artifact as user-turn input (D6); it is a delegate
    too, so it runs with credentials scrubbed. An unknown verifier advisor or a
    delegate that produced no artifact is recorded as an ``error`` outcome
    (inconclusive), never a crash and never a silent pass.
    """
    if not command.verify_with:
        return None
    try:
        advisor = advisors_mod.resolve(command.verify_with)
    except KeyError as exc:
        outcome = verify_mod.VerifyOutcome(
            command.verify_with,
            command.verify_model,
            "error",
            False,
            f"unknown verifier advisor: {exc}",
        )
    else:
        structured = advisor.json_schema_flag is not None
        if result_text is None:
            outcome = verify_mod.VerifyOutcome(
                advisor.name,
                command.verify_model,
                "error",
                structured,
                "delegate produced no artifact to verify",
            )
        else:
            artifact = verify_mod.build_artifact(prompt, result_text, command.cwd)
            outcome = verify_mod.run_verification(
                advisor,
                command.verify_model,
                artifact,
                cwd=command.cwd,
                pass_env=command.pass_env,
                timeout=verify_mod.VERIFY_DEFAULT_TIMEOUT_SECONDS,
            )
    # Audit the verdict, never the artifact (which can contain repo content).
    jobs_mod.append_event(
        job_dir,
        "verify",
        actor="system:verify",
        advisor=outcome.advisor,
        model=outcome.model,
        verdict=outcome.verdict,
        structured=outcome.structured,
    )
    return outcome.to_dict()


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
        check=data.get("check"),
        check_timeout=float(
            data.get("check_timeout", check_mod.CHECK_DEFAULT_TIMEOUT_SECONDS)
        ),
        scope_paths=_load_scope_paths(data.get("scope_paths")),
        pass_env=[str(name) for name in data.get("pass_env", [])],
        verify_with=data.get("verify_with"),
        verify_model=data.get("verify_model"),
        escalate_to=[str(rung) for rung in data.get("escalate_to", [])],
    )


def _load_scope_paths(raw: Any) -> Optional[list[str]]:
    """Return the declared allowlist, or ``None`` when no scope was declared.

    Only a JSON list is a declaration; anything else (absent key, ``null``)
    means enforcement is off — kept distinct from a declared empty list.
    """
    if not isinstance(raw, list):
        return None
    return [str(pattern) for pattern in raw]


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


def build_advisor_env(
    job: jobs_mod.Job,
    state_root: Path,
    *,
    pass_env: Optional[list[str]] = None,
) -> dict[str, str]:
    """Build the environment dict for the advisor subprocess with lineage vars.

    Starts from ``os.environ.copy()``, then withholds credential-bearing
    variables from the delegate (slice S4) — a delegate is partially untrusted
    and must not receive the caller's ambient secrets. Variables named in
    *pass_env* are the caller's explicit opt-in exceptions (e.g. the advisor's
    own API key). Lineage variables are set AFTER the scrub so they are never
    stripped: it overwrites (does not setdefault) ``CROSSAGENT_PARENT_JOB_ID``,
    ``CROSSAGENT_TRACE_ID``, ``CROSSAGENT_ORCHESTRATOR_LABEL``,
    ``CROSSAGENT_NESTING_DEPTH``, and ``CROSSAGENT_STATE_DIR`` so a nested
    ``crossagent start`` inside the advisor inherits the correct lineage.

    This function is deliberately side-effect-free and testable without spawning
    a process.
    """
    env = credentials_mod.scrub_env(os.environ, pass_through=pass_env or [])
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
