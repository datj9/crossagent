"""Tests for the job state model, persistence, and lifecycle.

These tests cover Phase 1 exit criteria:
  1. State transitions reject invalid regressions.
  2. Concurrent readers never observe partially written JSON (atomic write).
  3. Prompt and unredacted command data do not appear in list/status output.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import subprocess

import pytest

from crossagent.jobs import (
    CorruptStateError,
    InvalidStateError,
    Job,
    JobState,
    _job_to_dict,
    _dict_to_job,
    _pid_exists,
    assert_valid_transition,
    atomic_json_read,
    atomic_json_write,
    cancel_requested,
    create_cancel_request,
    create_job_dir,
    default_state_root,
    generate_job_id,
    is_terminal,
    job_dir_path,
    list_status,
    load_state,
    reconcile_stale,
    runtime_status,
    save_state,
    status_response,
    transition_to,
)

# =========================================================================
# State classification
# =========================================================================


def test_is_terminal():
    assert not is_terminal(JobState.PENDING)
    assert not is_terminal(JobState.RUNNING)
    assert is_terminal(JobState.SUCCEEDED)
    assert is_terminal(JobState.FAILED)
    assert is_terminal(JobState.TIMED_OUT)
    assert is_terminal(JobState.CANCELLED)
    assert is_terminal(JobState.ABANDONED)


# =========================================================================
# Transition validation  (exit criterion 1)
# =========================================================================


def test_transition_non_terminal_to_any():
    assert_valid_transition(JobState.PENDING, JobState.RUNNING)
    assert_valid_transition(JobState.PENDING, JobState.SUCCEEDED)
    assert_valid_transition(JobState.RUNNING, JobState.SUCCEEDED)
    assert_valid_transition(JobState.RUNNING, JobState.FAILED)
    assert_valid_transition(JobState.RUNNING, JobState.TIMED_OUT)


def test_transition_same_terminal_allowed():
    assert_valid_transition(JobState.SUCCEEDED, JobState.SUCCEEDED)
    assert_valid_transition(JobState.FAILED, JobState.FAILED)
    assert_valid_transition(JobState.ABANDONED, JobState.ABANDONED)


def test_transition_terminal_to_different_raises():
    from_state = JobState.SUCCEEDED
    for to_state in JobState:
        if to_state == from_state:
            continue
        try:
            assert_valid_transition(from_state, to_state)
            assert False, f"Expected ValueError: {from_state} -> {to_state}"
        except ValueError:
            pass


def test_transition_all_terminal_to_non_terminal_raises():
    # ABANDONED → RUNNING is the one permitted reclaim edge; skip that pair.
    reclaim_edge = (JobState.ABANDONED, JobState.RUNNING)
    for terminal in JobState:
        if not is_terminal(terminal):
            continue
        for non_terminal in (JobState.PENDING, JobState.RUNNING):
            if (terminal, non_terminal) == reclaim_edge:
                # This edge must NOT raise — it is intentionally allowed.
                assert_valid_transition(terminal, non_terminal)  # no exception
                continue
            try:
                assert_valid_transition(terminal, non_terminal)
                assert False, f"Expected ValueError: {terminal} -> {non_terminal}"
            except ValueError:
                pass


# =========================================================================
# transition_to functional tests
# =========================================================================


def test_transition_to_basic():
    job = Job(job_id="job_test_1", status=JobState.PENDING)
    updated = transition_to(job, JobState.RUNNING)
    assert updated.status == JobState.RUNNING
    assert updated.job_id == "job_test_1"
    assert updated.schema_version == 1
    assert updated.updated_at != ""
    assert updated.finished_at is None


def test_transition_to_terminal_sets_finished_at():
    job = Job(job_id="job_test_1", status=JobState.RUNNING, started_at="2026-01-01T00:00:00")
    updated = transition_to(job, JobState.SUCCEEDED)
    assert updated.status == JobState.SUCCEEDED
    assert updated.finished_at is not None
    assert updated.duration_seconds is not None
    assert updated.duration_seconds >= 0


def test_transition_to_terminal_without_start_at():
    job = Job(job_id="job_test_1", status=JobState.RUNNING, started_at="")
    updated = transition_to(job, JobState.SUCCEEDED)
    assert updated.finished_at is None
    assert updated.duration_seconds is None


def test_transition_to_rejects_regression():
    job = Job(job_id="job_test_1", status=JobState.SUCCEEDED)
    try:
        transition_to(job, JobState.RUNNING)
        assert False, "Expected ValueError"
    except ValueError:
        pass


def test_transition_to_applies_overrides():
    job = Job(job_id="job_test_1", status=JobState.RUNNING)
    updated = transition_to(job, JobState.FAILED, error="Something went wrong", advisor_exit_code=1)
    assert updated.status == JobState.FAILED
    assert updated.error == "Something went wrong"
    assert updated.advisor_exit_code == 1


def test_transition_to_persists_when_job_dir_given(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_persist")
    job = Job(job_id="job_persist", status=JobState.RUNNING, started_at="2026-01-01T00:00:00")
    updated = transition_to(job, JobState.SUCCEEDED, job_dir=job_dir)
    assert updated.status == JobState.SUCCEEDED
    loaded = load_state(job_dir)
    assert loaded.status == JobState.SUCCEEDED


# =========================================================================
# Job ID generation
# =========================================================================


def test_generate_job_id_format():
    jid = generate_job_id()
    assert jid.startswith("job_")
    assert len(jid) > len("job_")
    assert "/" not in jid
    assert " " not in jid


def test_generate_job_id_unique():
    ids = {generate_job_id() for _ in range(100)}
    assert len(ids) == 100


# =========================================================================
# State-root resolution
# =========================================================================


def test_default_state_root_fallback():
    root = default_state_root()
    assert root.suffix == ""
    assert "crossagent" in str(root)


def test_default_state_root_env_var(monkeypatch):
    monkeypatch.setenv("CROSSAGENT_STATE_DIR", "/custom/state")
    root = default_state_root()
    assert str(root) == "/custom/state"


def test_default_state_root_xdg(monkeypatch):
    monkeypatch.delenv("CROSSAGENT_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    root = default_state_root()
    assert str(root) == "/xdg/state/crossagent/jobs"


# =========================================================================
# Job directory helpers
# =========================================================================


def test_create_job_dir(tmp_path):
    state_root = tmp_path / "jobs"
    jdir = create_job_dir(state_root, "job_test123")
    assert jdir.exists()
    assert jdir.is_dir()
    assert jdir.name == "job_test123"
    assert jdir.parent == state_root
    if sys.platform != "win32":
        assert (jdir.stat().st_mode & 0o777) == 0o700


def test_job_dir_path(tmp_path):
    jdir = job_dir_path(tmp_path, "job_test123")
    assert str(jdir) == str(tmp_path / "job_test123")
    assert not jdir.exists()


# =========================================================================
# Save / load round-trip
# =========================================================================


def test_save_and_load_state(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_roundtrip")
    job = Job(
        job_id="job_roundtrip",
        status=JobState.RUNNING,
        advisor="codex",
        name="test-delegation",
        cwd="/tmp",
        redacted_command="codex exec <prompt>",
        worker_pid=1234,
        started_at="2026-07-18T10:00:00Z",
        updated_at="2026-07-18T10:00:01Z",
    )
    save_state(job_dir, job)
    state_path = job_dir / "state.json"
    assert state_path.exists()
    if sys.platform != "win32":
        assert (state_path.stat().st_mode & 0o777) == 0o600

    loaded = load_state(job_dir)
    assert loaded.job_id == "job_roundtrip"
    assert loaded.status == JobState.RUNNING
    assert loaded.advisor == "codex"
    assert loaded.name == "test-delegation"
    assert loaded.worker_pid == 1234


def test_load_unknown_fields_forward_compat(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_compat")
    state_path = job_dir / "state.json"
    data = {
        "schema_version": 1,
        "job_id": "job_compat",
        "status": "running",
        "unknown_field": "should_not_crash",
        "another_unknown": 42,
    }
    atomic_json_write(data, state_path)
    loaded = load_state(job_dir)
    assert loaded.job_id == "job_compat"
    assert loaded.status == JobState.RUNNING
    assert not hasattr(loaded, "unknown_field")


def test_save_state_idempotent(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_idem")
    job = Job(job_id="job_idem", status=JobState.RUNNING)
    save_state(job_dir, job)
    save_state(job_dir, job)
    save_state(job_dir, job)
    loaded = load_state(job_dir)
    assert loaded.status == JobState.RUNNING


# =========================================================================
# State loading errors
# =========================================================================


def test_load_state_file_not_found(tmp_path):
    job_dir = tmp_path / "nonexistent"
    try:
        load_state(job_dir)
        assert False, "Expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_load_state_corrupt_json(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_corrupt")
    (job_dir / "state.json").write_text("{invalid json", encoding="utf-8")
    try:
        load_state(job_dir)
        assert False, "Expected CorruptStateError"
    except CorruptStateError:
        pass


def test_load_state_wrong_schema_version(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_badver")
    atomic_json_write({"schema_version": 999, "job_id": "job_badver", "status": "running"}, job_dir / "state.json")
    try:
        load_state(job_dir)
        assert False, "Expected InvalidStateError"
    except InvalidStateError:
        pass


def test_load_state_unknown_status(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_badstatus")
    atomic_json_write({"schema_version": 1, "job_id": "job_badstatus", "status": "unknown_status"}, job_dir / "state.json")
    try:
        load_state(job_dir)
        assert False, "Expected InvalidStateError"
    except InvalidStateError:
        pass


def test_load_state_non_dict(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_notdict")
    atomic_json_write([1, 2, 3], job_dir / "state.json")
    try:
        load_state(job_dir)
        assert False, "Expected InvalidStateError"
    except InvalidStateError:
        pass


# =========================================================================
# Status response (exit criterion 3)
# =========================================================================


def test_status_response_includes_required_fields():
    job = Job(job_id="job_sr1", status=JobState.RUNNING, advisor="claude")
    resp = status_response(job)
    assert resp == {"schema_version": 1, "job_id": "job_sr1", "status": "running"}


def test_status_response_excludes_prompt_and_command():
    job = Job(job_id="job_sr2", status=JobState.RUNNING, redacted_command="claude -p <prompt>")
    resp = status_response(job)
    assert "prompt" not in resp
    assert "redacted_command" not in resp


def test_list_status_excludes_redacted_command():
    job = Job(
        job_id="job_ls1",
        status=JobState.RUNNING,
        advisor="codex",
        redacted_command="codex exec <prompt>",
        worker_pid=42,
    )
    safe = list_status(job)
    assert "redacted_command" not in safe
    assert safe["job_id"] == "job_ls1"
    assert safe["status"] == "running"
    assert safe["advisor"] == "codex"
    assert safe["worker_pid"] == 42


# =========================================================================
# Cancel request
# =========================================================================


def test_create_cancel_request(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_cancel1")
    path = create_cancel_request(job_dir)
    assert path.exists()
    assert path.name == "cancel.request"
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_cancel_requested_true(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_cancel2")
    create_cancel_request(job_dir)
    assert cancel_requested(job_dir)


def test_cancel_requested_false(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_cancel3")
    assert not cancel_requested(job_dir)


# =========================================================================
# Atomic write  (exit criterion 2)
# =========================================================================


def test_atomic_write_creates_valid_json(tmp_path):
    path = tmp_path / "test.json"
    data = {"key": "value", "number": 42}
    atomic_json_write(data, path)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_atomic_write_no_partial_on_failure(tmp_path):
    path = tmp_path / "test.json"
    original_data = {"key": "before"}
    atomic_json_write(original_data, path)
    old_text = path.read_text(encoding="utf-8")

    bad_data = {"key": "will_fail"}
    fd, tmp = tempfile.mkstemp(dir=str(tmp_path), prefix="test.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("incomplete json that will be")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    path.write_text(old_text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == old_text


def test_atomic_write_mid_write_never_partial(tmp_path):
    atomic_json_write({"a": 1}, tmp_path / "test.json")
    for _ in range(50):
        atomic_json_write({"counter": _}, tmp_path / "test.json")
        parsed = json.loads((tmp_path / "test.json").read_text(encoding="utf-8"))
        assert "counter" in parsed


# =========================================================================
# Reconcile stale
# =========================================================================


def test_reconcile_stale_terminal_noop(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec1")
    job = Job(job_id="job_rec1", status=JobState.SUCCEEDED, worker_pid=999999)
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.SUCCEEDED


def test_reconcile_stale_no_worker_pid(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec2")
    job = Job(job_id="job_rec2", status=JobState.RUNNING, worker_pid=None)
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.ABANDONED
    assert result.error is not None


def test_reconcile_stale_recent_pending_job_keeps_starting(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec_pending_recent")
    now = datetime.now(timezone.utc).isoformat()
    job = Job(job_id="job_rec_pending_recent", status=JobState.PENDING,
              worker_pid=None, updated_at=now)
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.PENDING


def test_reconcile_stale_old_pending_job_is_abandoned(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec_pending_old")
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    job = Job(job_id="job_rec_pending_old", status=JobState.PENDING,
              worker_pid=None, updated_at=old)
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.ABANDONED


def test_reconcile_stale_missing_worker(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec3")
    job = Job(job_id="job_rec3", status=JobState.RUNNING, worker_pid=99999999)
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.ABANDONED


def test_reconcile_stale_worker_exists(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec4")
    job = Job(job_id="job_rec4", status=JobState.RUNNING, worker_pid=os.getpid())
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.RUNNING


# =========================================================================
# Serialisation helpers
# =========================================================================


def test_job_to_dict_converts_enum():
    job = Job(job_id="job_s1", status=JobState.RUNNING)
    d = _job_to_dict(job)
    assert d["status"] == "running"
    assert isinstance(d["status"], str)


def test_dict_to_job_handles_enum():
    d = {"schema_version": 1, "job_id": "job_s2", "status": "succeeded"}
    job = _dict_to_job(d)
    assert job.status == JobState.SUCCEEDED
    assert isinstance(job.status, JobState)


def test_dict_to_job_missing_optional_defaults():
    d = {"schema_version": 1, "job_id": "job_s3", "status": "running"}
    job = _dict_to_job(d)
    assert job.worker_pid is None
    assert job.advisor == ""


# =========================================================================
# Regression tests for the three-part TOCTOU / abandoned-race bug fix
# =========================================================================


def test_reconcile_stale_does_not_overwrite_terminal_write_after_load(tmp_path):
    """Race #1 (TOCTOU): stale RUNNING dashboard copy must not clobber SUCCEEDED.

    Sequence under test:
      1. Dashboard loads state.json while worker is RUNNING → gets a RUNNING copy.
      2. Worker writes SUCCEEDED to disk and exits (PID gone).
      3. reconcile_stale is called with the stale RUNNING copy.

    Fixed behaviour: reconcile_stale re-reads disk after seeing the PID gone
    and finds SUCCEEDED → returns SUCCEEDED without persisting ABANDONED.
    Old behaviour: would call transition_to(ABANDONED) on the stale copy and
    overwrite the on-disk SUCCEEDED.
    """
    job_dir = create_job_dir(tmp_path, "job_toctou")

    # Spawn a real subprocess as the fake worker so we have a live PID.
    worker_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker_pid = worker_proc.pid

    try:
        # Step 1: persist RUNNING with the live worker PID.
        running_job = Job(
            job_id="job_toctou",
            status=JobState.RUNNING,
            worker_pid=worker_pid,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        save_state(job_dir, running_job)

        # Step 2: dashboard loads the state (simulates the stale in-memory copy).
        stale_dashboard_copy = load_state(job_dir)
        assert stale_dashboard_copy.status == JobState.RUNNING

        # Step 3: worker finishes — write SUCCEEDED to disk, then kill and wait.
        succeeded_job = transition_to(stale_dashboard_copy, JobState.SUCCEEDED, job_dir=job_dir)
        assert succeeded_job.status == JobState.SUCCEEDED
    finally:
        # Terminate the subprocess and wait so the PID is truly gone (and no
        # 60-second sleeper leaks if an assertion above fails).
        worker_proc.kill()
        worker_proc.wait()

    # Step 4: reconcile_stale is called with the stale RUNNING copy (not the fresh one).
    # The PID is now gone, but state.json on disk already says SUCCEEDED.
    result = reconcile_stale(stale_dashboard_copy, job_dir)

    # Both the returned job and on-disk state must be SUCCEEDED, not ABANDONED.
    assert result.status == JobState.SUCCEEDED, (
        f"Expected SUCCEEDED but got {result.status!r} — "
        "reconcile_stale overwrote the worker's terminal state with ABANDONED"
    )
    on_disk = load_state(job_dir)
    assert on_disk.status == JobState.SUCCEEDED, (
        f"On-disk status is {on_disk.status!r} — reconcile_stale persisted ABANDONED "
        "on top of the worker-written SUCCEEDED"
    )


def test_reconcile_stale_does_not_abandon_job_that_booted_after_snapshot(tmp_path):
    """A worker that boots between the dashboard snapshot and the reconcile
    write must survive reconciliation.

    Sequence under test:
      1. Dashboard loads a PENDING job whose startup grace has expired
         (no worker_pid) → stale snapshot says "abandon me".
      2. The late worker boots and persists RUNNING with its live PID.
      3. reconcile_stale is called with the stale PENDING snapshot.

    Fixed behaviour: the fresh re-read is re-evaluated with the full liveness
    guards — RUNNING with a live PID is honoured, nothing is persisted.
    Old behaviour: the fresh copy was only checked for terminality, so the
    live RUNNING job was overwritten with ABANDONED.
    """
    job_dir = create_job_dir(tmp_path, "job_lateboot")

    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
    pending_job = Job(
        job_id="job_lateboot",
        status=JobState.PENDING,
        started_at=stale_ts,
        updated_at=stale_ts,
        last_activity_at=stale_ts,
    )
    save_state(job_dir, pending_job)

    # Step 1: dashboard snapshots the expired PENDING state.
    stale_dashboard_copy = load_state(job_dir)
    assert stale_dashboard_copy.status == JobState.PENDING

    # Step 2: the late worker boots — persists RUNNING with a live PID
    # (this test process stands in for the worker).
    transition_to(
        stale_dashboard_copy,
        JobState.RUNNING,
        job_dir=job_dir,
        worker_pid=os.getpid(),
    )

    # Step 3: reconcile from the stale PENDING snapshot.
    result = reconcile_stale(stale_dashboard_copy, job_dir)

    assert result.status == JobState.RUNNING, (
        f"Expected RUNNING but got {result.status!r} — reconcile_stale "
        "abandoned a job whose worker booted after the snapshot"
    )
    on_disk = load_state(job_dir)
    assert on_disk.status == JobState.RUNNING, (
        f"On-disk status is {on_disk.status!r} — reconcile_stale persisted "
        "ABANDONED over a live RUNNING job"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill(pid, 0) semantics only")
def test_pid_exists_treats_permission_error_as_alive(monkeypatch):
    """EPERM from os.kill means the process EXISTS but is not signalable.

    Old code caught (OSError, PermissionError) together → returned False.
    Fixed code catches PermissionError separately → returns True.
    """
    import crossagent.jobs as jobs_module

    def raise_permission_error(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(jobs_module.os, "kill", raise_permission_error)
    assert jobs_module._pid_exists(12345) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill(pid, 0) semantics only")
def test_pid_exists_treats_process_lookup_error_as_dead(monkeypatch):
    """ESRCH from os.kill means no process with this PID exists."""
    import crossagent.jobs as jobs_module

    def raise_no_such_process(pid: int, sig: int) -> None:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(jobs_module.os, "kill", raise_no_such_process)
    assert jobs_module._pid_exists(12345) is False


def test_abandoned_job_can_be_reclaimed_to_running():
    """ABANDONED → RUNNING must be a permitted transition (reclaim edge).

    All other terminal → RUNNING (or any non-terminal) transitions must still raise.
    """
    # Reclaim edge: must NOT raise.
    assert_valid_transition(JobState.ABANDONED, JobState.RUNNING)

    # Other terminal states must remain frozen against non-terminal targets.
    for from_state in (JobState.SUCCEEDED, JobState.CANCELLED, JobState.FAILED, JobState.TIMED_OUT):
        try:
            assert_valid_transition(from_state, JobState.RUNNING)
            assert False, f"Expected ValueError for {from_state} -> RUNNING"
        except ValueError:
            pass

    # ABANDONED → SUCCEEDED (same terminal, different value) must still raise.
    try:
        assert_valid_transition(JobState.ABANDONED, JobState.SUCCEEDED)
        assert False, "Expected ValueError for ABANDONED -> SUCCEEDED"
    except ValueError:
        pass


def test_reclaim_clears_stale_abandonment_fields(tmp_path):
    """Reclaiming an ABANDONED job via transition_to(RUNNING) clears error/finished_at/duration_seconds.

    When the reconciler wrongly abandons a job (race #2), the late-booting worker
    reclaims it with ABANDONED → RUNNING.  The abandonment artefacts written during
    the false ABANDONED transition must not carry forward onto the recovered job.
    """
    job_dir = create_job_dir(tmp_path, "job_reclaim_fields")
    started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    # Build an ABANDONED job that has all the error/timing artefacts set.
    abandoned_job = Job(
        job_id="job_reclaim_fields",
        status=JobState.ABANDONED,
        started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=5.0,
        error="Worker process no longer exists",
    )

    # Reclaim via transition_to — the fixed code clears abandonment artefacts.
    reclaimed = transition_to(abandoned_job, JobState.RUNNING)

    assert reclaimed.status == JobState.RUNNING
    assert reclaimed.error is None, f"error should be cleared, got {reclaimed.error!r}"
    assert reclaimed.finished_at is None, f"finished_at should be cleared, got {reclaimed.finished_at!r}"
    assert reclaimed.duration_seconds is None, f"duration_seconds should be cleared, got {reclaimed.duration_seconds!r}"


def test_reconciled_abandoned_pending_job_recovers_when_worker_boots(tmp_path):
    """End-to-end of the slow-boot race (race #2).

    Sequence:
      1. PENDING job saved with timestamps 15+ seconds in the past, no worker_pid.
      2. reconcile_stale runs → sees no PID, grace window expired → persists ABANDONED.
      3. Late worker boots: loads state, sees ABANDONED, calls transition_to(RUNNING).
         Fixed behaviour: transition succeeds (ABANDONED → RUNNING reclaim edge allowed).
         Old behaviour: ValueError raised, worker crashes, job stuck abandoned forever.
    """
    job_dir = create_job_dir(tmp_path, "job_slow_boot")
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()

    # Step 1: persist an old PENDING job with no worker_pid.
    pending_job = Job(
        job_id="job_slow_boot",
        status=JobState.PENDING,
        worker_pid=None,
        updated_at=stale_timestamp,
        started_at=stale_timestamp,
    )
    save_state(job_dir, pending_job)

    # Step 2: reconcile_stale should abandon the stale pending job.
    result = reconcile_stale(pending_job, job_dir)
    assert result.status == JobState.ABANDONED, (
        f"Expected reconcile_stale to produce ABANDONED for an old PENDING job, got {result.status!r}"
    )
    on_disk_after_reconcile = load_state(job_dir)
    assert on_disk_after_reconcile.status == JobState.ABANDONED

    # Step 3: simulate the late worker booting — load state, see ABANDONED, transition to RUNNING.
    # Use os.getpid() as a known-live worker_pid.
    late_boot_state = load_state(job_dir)
    assert late_boot_state.status == JobState.ABANDONED

    # This must NOT raise ValueError — ABANDONED → RUNNING is the reclaim edge.
    recovered = transition_to(
        late_boot_state,
        JobState.RUNNING,
        job_dir=job_dir,
        worker_pid=os.getpid(),
    )

    assert recovered.status == JobState.RUNNING
    on_disk_recovered = load_state(job_dir)
    assert on_disk_recovered.status == JobState.RUNNING
    assert on_disk_recovered.worker_pid == os.getpid()


# =========================================================================
# Z-suffix timestamp tolerance (Python 3.9/3.10 compat)
# =========================================================================


def test_runtime_status_handles_z_suffix_timestamps():
    """Z-suffixed UTC timestamps must not crash runtime_status on Python 3.9/3.10."""
    job = Job(
        job_id="job_z_suffix",
        status=JobState.RUNNING,
        started_at="2026-07-19T00:00:00Z",
        last_activity_at="2026-07-19T00:00:30Z",
    )
    result = runtime_status(job)
    assert result["status"] == "running"
    assert result["elapsed_seconds"] >= 0
    assert result["idle_seconds"] >= 0


def test_transition_to_handles_z_suffix_started_at(tmp_path):
    job = Job(
        job_id="job_z_transition",
        status=JobState.RUNNING,
        started_at="2026-07-19T00:00:00Z",
    )
    updated = transition_to(job, JobState.SUCCEEDED, job_dir=tmp_path)
    assert updated.status == JobState.SUCCEEDED
    assert updated.finished_at is not None
    assert updated.duration_seconds is not None
    assert updated.duration_seconds >= 0
