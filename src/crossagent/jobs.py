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


def is_terminal(state: JobState) -> bool:
    """Return True when *state* is a terminal outcome."""
    return state in _TERMINAL_STATES


def assert_valid_transition(from_state: JobState, to_state: JobState) -> None:
    """Raise :class:`ValueError` if moving from a terminal state to a different one.

    Transitioning to the same state is always allowed (no-op).
    """
    if is_terminal(from_state) and from_state != to_state:
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
    **overrides: Any,
) -> Job:
    """Return a new ``Job`` with *new_status*, rejecting invalid regressions.

    If *job_dir* is supplied the updated state is also persisted atomically.
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
    new_job = _update_job(job, updates)
    if job_dir is not None:
        save_state(job_dir, new_job)
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
    """
    if is_terminal(job.status):
        return job
    if job.worker_pid is None or not _pid_exists(job.worker_pid):
        return transition_to(
            job,
            JobState.ABANDONED,
            job_dir=job_dir,
            error="Worker process no longer exists",
        )
    return job


def _pid_exists(pid: int) -> bool:
    """Best-effort check whether *pid* refers to a live process."""
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
    except (OSError, PermissionError):
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
