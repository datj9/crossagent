"""Durable job state model for cross-tool delegation.

Phase 1 of the durable cross-tool delegation plan: defines the job state
machine, persistent storage with atomic writes, and basic lifecycle primitives
so that a delegated advisor can outlive the parent tool call that started it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class JobState(str, Enum):
    """All possible job lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


_TERMINAL_STATES = frozenset({
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.TIMED_OUT,
    JobState.CANCELLED,
    JobState.ABANDONED,
})

# A job is persisted before its detached worker has had a chance to record its
# PID. Readers such as the dashboard must not treat that normal launch window
# as evidence that the worker died.
_PENDING_STARTUP_GRACE_SECONDS = 10


def is_terminal(state: JobState) -> bool:
    """Return True when *state* is a terminal outcome."""
    return state in _TERMINAL_STATES


def assert_valid_transition(from_state: JobState, to_state: JobState) -> None:
    """Raise :class:`ValueError` if moving from a terminal state to a different one.

    Transitioning to the same state is always allowed (no-op).

    Special case: ABANDONED → RUNNING is permitted so that a slow-starting
    worker (whose startup exceeded the pending grace window) can reclaim the
    job instead of crashing and leaving it permanently stuck.  ABANDONED is a
    reconciler *presumption*, not a confirmed terminal outcome, so this
    one reclaim edge is safe.  All other terminal states remain frozen.
    """
    if is_terminal(from_state) and from_state != to_state:
        # Allow the reconciler-presumed ABANDONED state to be reclaimed by a
        # late-booting worker transitioning back to RUNNING.
        if from_state == JobState.ABANDONED and to_state == JobState.RUNNING:
            return
        raise ValueError(
            f"Cannot transition from terminal state '{from_state.value}' "
            f"to '{to_state.value}'"
        )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    """Immutable representation of a durable job's persisted state.

    Every JSON response includes *schema_version*, *job_id*, and *status*.
    """

    schema_version: int = 1
    job_id: str = ""
    status: JobState = JobState.PENDING
    advisor: str = ""
    name: str = ""
    cwd: str = ""
    redacted_command: str = ""
    worker_pid: Optional[int] = None
    advisor_pid: Optional[int] = None
    process_group_id: Optional[int] = None
    started_at: str = ""
    updated_at: str = ""
    last_activity_at: str = ""
    last_event: str = ""
    max_runtime_seconds: Optional[int] = None
    termination_grace_seconds: int = 10
    advisor_session_id: Optional[str] = None
    advisor_exit_code: Optional[int] = None
    error: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None


# ---------------------------------------------------------------------------
# Job ID generation
# ---------------------------------------------------------------------------

def generate_job_id() -> str:
    """Return a unique, sortable-ish, filesystem-safe job ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rand = os.urandom(4).hex()
    return f"job_{ts}_{rand}"


# ---------------------------------------------------------------------------
# State-root resolution
# ---------------------------------------------------------------------------

def default_state_root() -> Path:
    """Return the default root directory for job state.

    Resolution order:
        1. ``CROSSAGENT_STATE_DIR`` environment variable.
        2. ``XDG_STATE_HOME``/crossagent/jobs  (POSIX).
        3. ``~/.local/state/crossagent/jobs``   (fallback — also works on macOS).
    """
    explicit = os.environ.get("CROSSAGENT_STATE_DIR")
    if explicit:
        return Path(explicit).resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "crossagent" / "jobs"
    return Path.home() / ".local" / "state" / "crossagent" / "jobs"


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

_STATUS_RESPONSE_FIELDS = frozenset({"schema_version", "job_id", "status"})


def _job_to_dict(job: Job) -> dict[str, Any]:
    d = asdict(job)
    d["status"] = job.status.value
    return d


def _dict_to_job(data: dict[str, Any]) -> Job:
    known = {f.name for f in fields(Job)}
    kwargs: dict[str, Any] = {}
    for k in known:
        if k not in data:
            continue
        val = data[k]
        if k == "status":
            val = JobState(val)
        kwargs[k] = val
    return Job(**kwargs)


def status_response(job: Job) -> dict[str, Any]:
    """Return a stable status envelope (``schema_version``, ``job_id``, ``status``).

    Prompt text and unredacted command data are intentionally excluded.
    """
    d = _job_to_dict(job)
    return {k: d[k] for k in _STATUS_RESPONSE_FIELDS}


def list_status(job: Job) -> dict[str, Any]:
    """Return job state safe for listing — no prompt or command data."""
    d = _job_to_dict(job)
    unsafe = {"redacted_command"}
    return {k: v for k, v in d.items() if k not in unsafe}


def runtime_status(job: Job) -> dict[str, Any]:
    """Return the live status envelope with elapsed/idle seconds.

    Prompt text and command data are never included.
    """
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(job.started_at) if job.started_at else now
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = int((now - started).total_seconds())
    last_activity = datetime.fromisoformat(job.last_activity_at) if job.last_activity_at else started
    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)
    idle = int((now - last_activity).total_seconds())
    return {
        "schema_version": job.schema_version,
        "job_id": job.job_id,
        "status": job.status.value,
        "advisor": job.advisor,
        "elapsed_seconds": elapsed,
        "idle_seconds": idle,
        "last_event": job.last_event,
        "updated_at": job.updated_at,
    }


def list_entry(job: Job) -> dict[str, Any]:
    """Return a listing entry: runtime status plus identity fields."""
    entry = runtime_status(job)
    entry["name"] = job.name
    entry["started_at"] = job.started_at
    return entry


def collect_jobs(
    state_root: Path,
    on_skip: Optional[Callable[[str, Exception], None]] = None,
) -> list[Job]:
    """Load every job under *state_root*, reconciling stale state.

    Unreadable job directories invoke *on_skip(dir_name, exception)* when
    provided — a job is never silently dropped from a listing.
    """
    if not state_root.is_dir():
        return []
    collected: list[Job] = []
    for job_dir in sorted(state_root.iterdir()):
        if not job_dir.is_dir():
            continue
        try:
            job = load_state(job_dir)
        except (FileNotFoundError, JobError) as exc:
            if on_skip is not None:
                on_skip(job_dir.name, exc)
            continue
        collected.append(reconcile_stale(job, job_dir))
    return collected


# ---------------------------------------------------------------------------
# Atomic I/O
# ---------------------------------------------------------------------------

def atomic_json_write(data: dict[str, Any], path: Path, *, mode: int = 0o600) -> None:
    """Write *data* as JSON to *path* atomically via temp sibling + os.replace.

    The resulting file is created with *mode* permissions where the platform
    supports it.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, str(path))
        path.chmod(mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_json_read(path: Path) -> dict[str, Any]:
    """Return the parsed JSON dict read from *path*."""
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Job directory helpers
# ---------------------------------------------------------------------------

def create_job_dir(state_root: Path, job_id: str) -> Path:
    """Create and return the private directory for *job_id* (mode 0o700)."""
    job_dir = state_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job_dir.chmod(0o700)
    return job_dir


def job_dir_path(state_root: Path, job_id: str) -> Path:
    """Return the directory path for *job_id* without creating it."""
    return state_root / job_id


def _ensure_private_file(path: Path) -> None:
    """Set file permissions to 0o600 where the platform supports it."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cancel request
# ---------------------------------------------------------------------------

def create_cancel_request(job_dir: Path) -> Path:
    """Create a ``cancel.request`` file in *job_dir* and return its path."""
    path = job_dir / "cancel.request"
    path.touch()
    _ensure_private_file(path)
    return path


def cancel_requested(job_dir: Path) -> bool:
    """Return True when a ``cancel.request`` file exists in *job_dir*."""
    return (job_dir / "cancel.request").exists()


# ---------------------------------------------------------------------------
# State lifecycle
# ---------------------------------------------------------------------------

def save_state(job_dir: Path, job: Job) -> Path:
    """Atomically write ``state.json`` in *job_dir* and return its path."""
    path = job_dir / "state.json"
    atomic_json_write(_job_to_dict(job), path)
    return path


def load_state(job_dir: Path) -> Job:
    """Load, validate, and return the ``Job`` from *job_dir*/state.json.

    Raises:
        FileNotFoundError: The state file does not exist.
        CorruptStateError: The state file contains unparseable JSON.
        InvalidStateError: The state violates the expected schema.
    """
    path = job_dir / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise CorruptStateError(str(path), exc) from exc

    if not isinstance(data, dict):
        raise InvalidStateError(f"Expected a mapping, got {type(data).__name__}")

    ver = data.get("schema_version")
    if ver != 1:
        raise InvalidStateError(f"Unsupported schema_version {ver}")

    raw = data.get("status")
    if raw not in {s.value for s in JobState}:
        raise InvalidStateError(f"Unknown status '{raw}'")

    return _dict_to_job(data)


def transition_to(
    job: Job,
    new_status: JobState,
    *,
    job_dir: Optional[Path] = None,
    actor: str = "user",
    **overrides: Any,
) -> Job:
    """Return a new ``Job`` with *new_status*, rejecting invalid regressions.

    If *job_dir* is supplied the updated state is also persisted atomically.

    When reclaiming an ABANDONED job back to a nonterminal state (the one
    permitted ABANDONED → RUNNING edge), stale terminal fields written during
    the abandonment (error, finished_at, duration_seconds) are cleared so they
    do not linger on the recovered job.  Callers may override any of these by
    passing explicit keyword arguments.
    """
    assert_valid_transition(job.status, new_status)
    now_utc = datetime.now(timezone.utc)
    now = now_utc.isoformat()
    updates: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
        **overrides,
    }
    if is_terminal(new_status) and job.started_at:
        started = datetime.fromisoformat(job.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        updates["finished_at"] = now
        updates["duration_seconds"] = (now_utc - started).total_seconds()
    # Reclaim edge: ABANDONED → nonterminal.  Clear stale abandonment artefacts
    # unless the caller explicitly overrides them.
    elif job.status == JobState.ABANDONED and not is_terminal(new_status):
        updates.setdefault("error", None)
        updates.setdefault("finished_at", None)
        updates.setdefault("duration_seconds", None)
    new_job = _update_job(job, updates)
    if job_dir is not None:
        save_state(job_dir, new_job)
        append_event(
            job_dir,
            "transition",
            actor=actor,
            from_state=job.status.value,
            to_state=new_status.value,
            error=updates.get("error"),
        )
    return new_job


def _update_job(job: Job, updates: dict[str, Any]) -> Job:
    """Return a new ``Job`` with *updates* applied (immutable update pattern)."""
    current = _job_to_dict(job)
    current.update(updates)
    return _dict_to_job(current)


# ---------------------------------------------------------------------------
# Stale-state reconciliation
# ---------------------------------------------------------------------------

def reconcile_stale(job: Job, job_dir: Path) -> Job:
    """If *job* has a nonterminal state but the worker is gone, mark abandoned.

    Returns the (potentially updated) Job.  The updated state is persisted
    if a transition occurs.

    TOCTOU guard: the worker always writes its terminal state to disk *before*
    exiting.  After the snapshot fails the liveness checks we therefore re-read
    state.json and re-run the *full* liveness evaluation on the fresh copy —
    the worker may have written a terminal state, booted (RUNNING with a live
    PID), or recorded fresh activity in the meantime.  Only when the fresh copy
    still fails every check do we persist ABANDONED, and we transition from the
    fresh copy, never the snapshot.
    """
    if not _worker_presumed_dead(job):
        return job
    # Re-load from disk to close the TOCTOU window between the caller's
    # snapshot and this decision.
    try:
        fresh_job = load_state(job_dir)
    except (FileNotFoundError, JobError):
        # state.json is unreadable — fall back to the in-memory copy.
        fresh_job = job
    else:
        if not _worker_presumed_dead(fresh_job):
            # Worker persisted its outcome, booted, or is provably alive;
            # honour the fresh state.
            return fresh_job
    return transition_to(
        fresh_job,
        JobState.ABANDONED,
        job_dir=job_dir,
        actor="system:reconcile",
        error="Worker process no longer exists",
    )


def _worker_presumed_dead(job: Job) -> bool:
    """Return True when a nonterminal *job*'s worker appears to be gone.

    Terminal jobs, pending jobs still inside the startup grace window, and
    jobs whose recorded worker PID is alive are all presumed healthy.
    """
    if is_terminal(job.status):
        return False
    if (job.status == JobState.PENDING and job.worker_pid is None
            and _pending_startup_grace_active(job)):
        return False
    return job.worker_pid is None or not _pid_exists(job.worker_pid)


def _pending_startup_grace_active(job: Job) -> bool:
    """Return whether a newly persisted pending job may still be launching."""
    timestamp = job.updated_at or job.started_at
    if not timestamp:
        return False
    try:
        created = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
    return 0 <= age_seconds < _PENDING_STARTUP_GRACE_SECONDS


def _pid_exists(pid: int) -> bool:
    """Best-effort check whether *pid* refers to a live process.

    On POSIX, ``os.kill(pid, 0)`` semantics:
      - No exception        → process exists and is signalable.
      - ``PermissionError`` → EPERM: process EXISTS but is owned by another user.
      - ``ProcessLookupError`` → ESRCH: process does not exist.
      - Other ``OSError``  → treat conservatively as dead.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
        except (OSError, AttributeError):
            pass
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM: the process exists but is owned by a different user.
        return True
    except ProcessLookupError:
        # ESRCH: no process with this PID.
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class JobError(Exception):
    """Base exception for job-related errors."""


class CorruptStateError(JobError):
    """The state file could not be parsed as valid JSON."""

    def __init__(self, path: str, cause: json.JSONDecodeError) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"Corrupt state file {path}: {cause}")


class InvalidStateError(JobError):
    """The stored state violates the expected schema."""


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def append_event(job_dir: Path, event: str, *, actor: str = "user", **payload: Any) -> None:
    """Append one JSON line to <job_dir>/events.jsonl.

    Atomic on POSIX for lines under the pipe buffer (our payloads are ~200 bytes).
    Never raises on failure — the audit log must not break the operation it
    observes. Errors are reported via stderr instead.

    The payload MUST include enough context to reconstruct the transition. The
    caller is responsible for not stashing prompt text or command data — those
    are never audit-relevant.
    """
    import sys
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "actor": actor,
            **payload,
        },
        sort_keys=True,
    )
    try:
        with (job_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        print(f"[crossagent] audit log write failed: {exc}", file=sys.stderr)
