"""Escalate-on-failure ladder for delegated work (slice S5).

When a delegation fails — a failing check, a scope violation, or a failing
independent verification, all of which resolve to
:func:`~crossagent.jobs.delegation_verdict` == ``"failed"`` — the caller may
declare an *escalation ladder*: an ordered list of larger peers to re-dispatch
the same task to. On failure the first rung is spawned; the remaining rungs are
handed to that child so a further failure climbs the next rung.

Recording (option (a) — satisfies the shipped analytics definition)
-------------------------------------------------------------------
``analytics.py`` (merged before this slice) computes the escalation rate as
"a failed delegation counts as escalated if it has a **same-trace child**". So a
re-dispatch is recorded as exactly that: a child job with the failed job as its
``parent_job_id`` and the **same ``trace_id``**, resolved through the existing
:func:`~crossagent.jobs.resolve_lineage` machinery. Nothing in ``analytics.py``
changes; the escalation column starts reflecting reality the moment this ships.

Runaway protection
------------------
An escalation ladder is a recursion source. It is bounded twice over:

1. The ladder is a finite list that shrinks by one rung each hop, so it
   self-terminates even if every rung fails.
2. Each child is one level deeper, and lineage resolution enforces
   :data:`~crossagent.jobs.MAX_NESTING_DEPTH`; a rung that would exceed the cap
   raises :class:`~crossagent.jobs.LineageError`, which is caught and recorded as
   a skipped escalation rather than crashing or looping.

Security posture carried to the child
-------------------------------------
An escalated child is a delegate too. It goes through the ordinary worker path,
so credential scrubbing (``credentials.py``) and the diff-scope assertion apply
to it unchanged: the declared ``scope_paths`` allowlist and ``pass_env`` opt-ins
are propagated so the larger peer is held to the *same* write boundary and the
*same* credential withholding as the original delegate. The check and
verification gates are propagated too, so each rung's output is judged the same
way before the ladder climbs again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from . import advisors as advisors_mod
from . import jobs as jobs_mod
from .advisors import Advisor
from .jobs import Job, delegation_verdict

# Launches a detached worker for a child job. Injectable so escalation can be
# unit-tested without spawning a real process.
Launcher = Callable[[str, Path], Any]


def parse_rungs(rungs: Optional[list[str]]) -> list[tuple[str, Optional[str]]]:
    """Parse ``advisor[:model]`` ladder rungs into ``(advisor, model)`` pairs.

    Splits on the first colon only, so a model containing no colon (the usual
    case: ``opus``, ``gpt-5.6-sol``) is preserved intact. Blank entries are
    dropped so a stray empty ``--escalate-to`` cannot spawn a nameless job.
    """
    parsed: list[tuple[str, Optional[str]]] = []
    for raw in rungs or []:
        entry = raw.strip()
        if not entry:
            continue
        advisor_name, sep, model = entry.partition(":")
        advisor_name = advisor_name.strip()
        if not advisor_name:
            continue
        parsed.append((advisor_name, model.strip() if sep else None))
    return parsed


def _build_child_argv(advisor: Advisor, model: Optional[str]) -> list[str]:
    """Build the escalated child's advisor argv — a FRESH delegation.

    Mirrors the advisor-invocation core of ``crossagent start`` (default stream
    mode) but adds no session-attachment flag: an escalation re-dispatches the
    task to a new, larger peer, so there is no prior session to resume. The
    child does not inherit the parent's fine-grained invocation flags
    (``--tools``, ``--safe-mode``, ``--permission-mode``); its write boundary is
    enforced structurally by the propagated scope allowlist instead.
    """
    cmd = [advisor.executable, *advisor.base_args, *advisor.invoke_args]
    if model and advisor.model_flag:
        cmd.extend([advisor.model_flag, model])
    if advisor.supports_stream:
        cmd.extend(advisor.stream_args)
    return cmd


def maybe_escalate(
    failed_job: Job,
    *,
    prompt: str,
    state_root: Path,
    job_dir: Path,
    cwd: str,
    registry_path: str,
    escalate_to: Optional[list[str]],
    check: Optional[str],
    check_timeout: float,
    scope_paths: Optional[list[str]],
    pass_env: list[str],
    verify_with: Optional[str],
    verify_model: Optional[str],
    launcher: Optional[Launcher] = None,
) -> Optional[str]:
    """Re-dispatch a *failed* delegation to the next ladder rung, if any.

    Returns the spawned child's job id, or ``None`` when nothing was escalated
    (the delegate did not finish cleanly, no declared gate failed, no rungs
    remain, the rung advisor is unknown, or the depth cap was reached). Never
    raises: an escalation that cannot be launched is recorded and skipped, never
    allowed to crash the worker.
    """
    if not _is_escalatable_failure(failed_job):
        return None

    rungs = parse_rungs(escalate_to)
    if not rungs:
        return None
    advisor_name, model = rungs[0]
    # ``remaining`` keeps the raw ``advisor[:model]`` strings for the child, minus
    # the rung being spawned now — the ladder that a further failure will climb.
    remaining = _drop_first_nonblank(escalate_to)

    try:
        advisor = advisors_mod.resolve(advisor_name)
    except KeyError as exc:
        _audit_skip(job_dir, reason=f"unknown escalation advisor: {exc}")
        return None

    child_id = jobs_mod.generate_job_id()
    try:
        lineage = jobs_mod.resolve_lineage(
            parent_flag=failed_job.job_id,
            state_root=state_root,
            new_job_id=child_id,
        )
    except jobs_mod.LineageError as exc:
        # Depth cap reached (or a corrupt chain): the ladder stops here. This is
        # the runaway guard doing its job, not an error.
        _audit_skip(job_dir, reason=f"escalation halted: {exc}")
        return None

    if not _create_and_save_child(
        state_root=state_root,
        job_dir=job_dir,
        child_id=child_id,
        prompt=prompt,
        advisor=advisor,
        model=model,
        cwd=cwd,
        registry_path=registry_path,
        check=check,
        check_timeout=check_timeout,
        scope_paths=scope_paths,
        pass_env=pass_env,
        verify_with=verify_with,
        verify_model=verify_model,
        escalate_to=remaining,
        lineage=lineage,
        failed_job=failed_job,
    ):
        return None

    _, trace_id, _, depth = lineage
    jobs_mod.append_event(
        job_dir,
        "escalation",
        actor="system:escalate",
        child_job_id=child_id,
        advisor=advisor.name,
        model=model,
        trace_id=trace_id,
        depth=depth,
    )

    launch = launcher if launcher is not None else _default_launcher
    try:
        launch(child_id, state_root)
    except OSError as exc:
        _audit_skip(job_dir, reason=f"escalation worker failed to launch: {exc}")
        return None
    return child_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_escalatable_failure(failed_job: Job) -> bool:
    """Return True only for a delegate that FINISHED but FAILED a declared gate.

    Escalation re-dispatches work that failed a check, a scope violation, or a
    failing verification (this module's stated scope). A job that did not finish
    cleanly is deliberately out of scope, so gate on SUCCEEDED first rather than
    on ``delegation_verdict != "failed"`` alone (which also returns "failed" for
    CANCELLED / TIMED_OUT / a crashed delegate):

    * CANCELLED is explicit user intent to stop; re-dispatching to a larger,
      costlier peer is the opposite of cancelling and spends real money.
    * TIMED_OUT (and any other non-success terminal status) produced no graded
      artifact — there is no gate failure to escalate, and a bigger model is
      generally slower, so it is at least as likely to time out again under the
      same budget. A hard task that needs a bigger model is a fresh dispatch
      decision, not an automatic ladder climb that silently burns budget. So
      TIMED_OUT does NOT escalate.

    Gating on SUCCEEDED means ``delegation_verdict == "failed"`` can only be a
    declared-gate failure — mirroring cli._failed_reason's "did not finish
    cleanly" vs. gate-failure distinction.
    """
    if failed_job.status != jobs_mod.JobState.SUCCEEDED:
        return False
    return delegation_verdict(failed_job) == "failed"


def _drop_first_nonblank(rungs: Optional[list[str]]) -> list[str]:
    """Return the non-blank rungs with the first one (the spawned rung) removed."""
    cleaned = [raw for raw in (rungs or []) if raw.strip()]
    return cleaned[1:]


def _write_child_prompt(child_dir: Path, prompt: str) -> None:
    prompt_path = child_dir / "prompt"
    prompt_path.write_text(prompt, encoding="utf-8")
    try:
        prompt_path.chmod(0o600)
    except OSError:
        pass


def _write_child_command(
    child_dir: Path,
    *,
    advisor: Advisor,
    model: Optional[str],
    cwd: str,
    registry_path: str,
    check: Optional[str],
    check_timeout: float,
    scope_paths: Optional[list[str]],
    pass_env: list[str],
    verify_with: Optional[str],
    verify_model: Optional[str],
    escalate_to: list[str],
) -> None:
    command_payload = {
        "command": _build_child_argv(advisor, model),
        "prompt_delivery": advisor.prompt_delivery,
        "cwd": cwd,
        "result_parser": advisor.result_parser,
        "registry_path": registry_path,
        "key": "",
        "name": None,
        "model": model or "",
        "advisor": advisor.name,
        "check": check,
        "check_timeout": check_timeout,
        # Security posture propagated to the larger peer (same write boundary,
        # same credential withholding) — see the module docstring.
        "scope_paths": scope_paths,
        "pass_env": pass_env,
        # The verification gate is re-run on the escalated output, and the
        # remaining ladder lets a further failure climb the next rung.
        "verify_with": verify_with,
        "verify_model": verify_model,
        "escalate_to": escalate_to,
    }
    jobs_mod.atomic_json_write(command_payload, child_dir / "command.json")


def _build_child_job(
    child_id: str,
    advisor: Advisor,
    cwd: str,
    lineage: tuple[Optional[str], str, Optional[str], Optional[int]],
    failed_job: Job,
) -> Job:
    """Assemble the PENDING child ``Job`` record for an escalation re-dispatch.

    *lineage* is the ``(parent_id, trace_id, label, depth)`` tuple returned by
    :func:`~crossagent.jobs.resolve_lineage`; runtime bounds are inherited from
    *failed_job* so the larger peer runs under the same limits.
    """
    parent_id, trace_id, label, depth = lineage
    return Job(
        job_id=child_id,
        status=jobs_mod.JobState.PENDING,
        advisor=advisor.name,
        name="",
        cwd=cwd,
        redacted_command="",
        started_at=_now(),
        updated_at=_now(),
        last_activity_at=_now(),
        last_event="escalation.created",
        max_runtime_seconds=failed_job.max_runtime_seconds,
        termination_grace_seconds=failed_job.termination_grace_seconds,
        parent_job_id=parent_id,
        trace_id=trace_id,
        orchestrator_label=label,
        nesting_depth=depth,
    )


def _create_and_save_child(
    *,
    state_root: Path,
    job_dir: Path,
    child_id: str,
    prompt: str,
    advisor: Advisor,
    model: Optional[str],
    cwd: str,
    registry_path: str,
    check: Optional[str],
    check_timeout: float,
    scope_paths: Optional[list[str]],
    pass_env: list[str],
    verify_with: Optional[str],
    verify_model: Optional[str],
    escalate_to: list[str],
    lineage: tuple[Optional[str], str, Optional[str], Optional[int]],
    failed_job: Job,
) -> bool:
    """Stage the escalated child on disk: prompt, command, and state record.

    Staging touches the filesystem (mkdir, two file writes, a state save). A
    read-only or full disk raises OSError; it is caught here so the caller's
    "never raises" contract holds — a child that cannot be staged is recorded on
    *job_dir* and skipped, exactly like a launch failure. Returns True on
    success, False when staging was skipped.
    """
    try:
        child_dir = jobs_mod.create_job_dir(state_root, child_id)
        _write_child_prompt(child_dir, prompt)
        _write_child_command(
            child_dir,
            advisor=advisor,
            model=model,
            cwd=cwd,
            registry_path=registry_path,
            check=check,
            check_timeout=check_timeout,
            scope_paths=scope_paths,
            pass_env=pass_env,
            verify_with=verify_with,
            verify_model=verify_model,
            escalate_to=escalate_to,
        )
        child = _build_child_job(child_id, advisor, cwd, lineage, failed_job)
        jobs_mod.save_state(child_dir, child)
    except OSError as exc:
        _audit_skip(job_dir, reason=f"escalation could not be staged: {exc}")
        return False
    return True


def _audit_skip(job_dir: Path, *, reason: str) -> None:
    jobs_mod.append_event(
        job_dir, "escalation_skipped", actor="system:escalate", reason=reason
    )


def _default_launcher(child_id: str, state_root: Path) -> Any:
    # Lazy import breaks the worker <-> escalate import cycle.
    from .worker import start_worker

    return start_worker(child_id, state_root)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
