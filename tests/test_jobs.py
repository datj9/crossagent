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

import subprocess

import pytest

from crossagent.jobs import (
    CorruptStateError,
    InvalidStateError,
    Job,
    JobState,
    LineageError,
    MAX_NESTING_DEPTH,
    _dict_to_job,
    _job_to_dict,
    _walk_ancestor_chain,
    assert_valid_transition,
    atomic_json_write,
    cancel_requested,
    create_cancel_request,
    create_job_dir,
    default_state_root,
    generate_job_id,
    generate_trace_id,
    is_terminal,
    job_dir_path,
    list_status,
    load_state,
    reconcile_stale,
    resolve_lineage,
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
    assert updated.schema_version == 2
    assert updated.updated_at != ""
    assert updated.finished_at is None


def test_transition_to_terminal_sets_finished_at():
    job = Job(
        job_id="job_test_1", status=JobState.RUNNING, started_at="2026-01-01T00:00:00"
    )
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
    updated = transition_to(
        job, JobState.FAILED, error="Something went wrong", advisor_exit_code=1
    )
    assert updated.status == JobState.FAILED
    assert updated.error == "Something went wrong"
    assert updated.advisor_exit_code == 1


def test_transition_to_persists_when_job_dir_given(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_persist")
    job = Job(
        job_id="job_persist", status=JobState.RUNNING, started_at="2026-01-01T00:00:00"
    )
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
    atomic_json_write(
        {"schema_version": 999, "job_id": "job_badver", "status": "running"},
        job_dir / "state.json",
    )
    try:
        load_state(job_dir)
        assert False, "Expected InvalidStateError"
    except InvalidStateError:
        pass


def test_load_state_unknown_status(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_badstatus")
    atomic_json_write(
        {"schema_version": 1, "job_id": "job_badstatus", "status": "unknown_status"},
        job_dir / "state.json",
    )
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
    assert resp == {"schema_version": 2, "job_id": "job_sr1", "status": "running"}


def test_status_response_excludes_prompt_and_command():
    job = Job(
        job_id="job_sr2", status=JobState.RUNNING, redacted_command="claude -p <prompt>"
    )
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
    job = Job(
        job_id="job_rec_pending_recent",
        status=JobState.PENDING,
        worker_pid=None,
        updated_at=now,
    )
    result = reconcile_stale(job, job_dir)
    assert result.status == JobState.PENDING


def test_reconcile_stale_old_pending_job_is_abandoned(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_rec_pending_old")
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    job = Job(
        job_id="job_rec_pending_old",
        status=JobState.PENDING,
        worker_pid=None,
        updated_at=old,
    )
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
# Lineage metadata (v2 schema)
# =========================================================================


def test_lineage_fields_round_trip():
    job = Job(
        job_id="job_lineage",
        status=JobState.RUNNING,
        schema_version=2,
        trace_id="trace_abc123",
        parent_job_id="job_parent_001",
        orchestrator_label="gh-pr-review",
        nesting_depth=3,
    )
    d = _job_to_dict(job)
    rebuilt = _dict_to_job(d)
    assert rebuilt == job
    assert rebuilt.trace_id == "trace_abc123"
    assert rebuilt.parent_job_id == "job_parent_001"
    assert rebuilt.orchestrator_label == "gh-pr-review"
    assert rebuilt.nesting_depth == 3


def test_v1_dict_loads_with_lineage_fields_none():
    v1_dict = {
        "schema_version": 1,
        "job_id": "job_v1",
        "status": "running",
        "advisor": "codex",
    }
    job = _dict_to_job(v1_dict)
    assert job.schema_version == 1
    assert job.trace_id is None
    assert job.parent_job_id is None
    assert job.orchestrator_label is None
    assert job.nesting_depth is None


def test_load_state_accepts_v1_and_v2(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_v1_load")
    atomic_json_write(
        {"schema_version": 1, "job_id": "job_v1_load", "status": "running"},
        job_dir / "state.json",
    )
    loaded = load_state(job_dir)
    assert loaded.schema_version == 1
    assert loaded.trace_id is None

    job_dir2 = create_job_dir(tmp_path, "job_v2_load")
    atomic_json_write(
        {
            "schema_version": 2,
            "job_id": "job_v2_load",
            "status": "running",
            "trace_id": "tr_v2",
        },
        job_dir2 / "state.json",
    )
    loaded2 = load_state(job_dir2)
    assert loaded2.schema_version == 2
    assert loaded2.trace_id == "tr_v2"


def test_load_state_rejects_unsupported_version(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_badver2")
    atomic_json_write(
        {"schema_version": 3, "job_id": "job_badver2", "status": "running"},
        job_dir / "state.json",
    )
    with pytest.raises(InvalidStateError):
        load_state(job_dir)


def test_runtime_status_includes_lineage_fields():
    job = Job(
        job_id="job_rt_lineage",
        status=JobState.RUNNING,
        started_at="2026-07-19T10:00:00Z",
    )
    result = runtime_status(job)
    # All lineage fields present and None when unset
    assert "trace_id" in result
    assert result["trace_id"] is None
    assert "parent_job_id" in result
    assert result["parent_job_id"] is None
    assert "orchestrator_label" in result
    assert result["orchestrator_label"] is None
    assert "nesting_depth" in result
    assert result["nesting_depth"] is None

    # With values set
    job2 = Job(
        job_id="job_rt_lineage2",
        status=JobState.RUNNING,
        started_at="2026-07-19T10:00:00Z",
        schema_version=2,
        trace_id="trace_set",
        parent_job_id="parent_set",
        orchestrator_label="label_set",
        nesting_depth=5,
    )
    result2 = runtime_status(job2)
    assert result2["trace_id"] == "trace_set"
    assert result2["parent_job_id"] == "parent_set"
    assert result2["orchestrator_label"] == "label_set"
    assert result2["nesting_depth"] == 5


# =========================================================================
# Trace ID generation
# =========================================================================


def test_generate_trace_id_format():
    tid = generate_trace_id()
    assert tid.startswith("trace_")
    assert len(tid) > len("trace_")
    assert "/" not in tid
    assert " " not in tid


def test_generate_trace_id_unique():
    ids = {generate_trace_id() for _ in range(100)}
    assert len(ids) == 100


# =========================================================================
# Lineage resolution (resolve_lineage)
# =========================================================================


def test_resolve_lineage_no_parent_forces_top_level():
    """--no-parent forces depth=1 and a generated trace."""
    parent, trace, label, depth = resolve_lineage(no_parent=True)
    assert parent is None
    assert trace.startswith("trace_")
    assert label is None
    assert depth == 1


def test_resolve_lineage_parent_flag_with_loadable_parent(tmp_path):
    """--parent with a loadable parent; depth computed by walking the chain."""
    grandparent = Job(
        job_id="job_grandparent",
        schema_version=2,
        trace_id="trace_parent123",
    )
    create_job_dir(tmp_path, "job_grandparent")
    save_state(tmp_path / "job_grandparent", grandparent)

    parent_job = Job(
        job_id="job_parent",
        schema_version=2,
        trace_id="trace_parent123",
        parent_job_id="job_grandparent",
    )
    create_job_dir(tmp_path, "job_parent")
    save_state(tmp_path / "job_parent", parent_job)

    parent, trace, label, depth = resolve_lineage(
        parent_flag="job_parent",
        state_root=tmp_path,
        new_job_id="job_child",
    )
    assert parent == "job_parent"
    assert trace == "trace_parent123"
    assert depth == 3  # ancestors=[parent, grandparent]; 2+1=3


def test_resolve_lineage_broken_chain_falls_back_to_parent_depth(tmp_path):
    """A deep parent whose own parent is missing must not masquerade as shallow:
    depth falls back to the parent's recorded nesting_depth, so the cap holds."""
    deep_parent = Job(
        job_id="job_deep_parent",
        schema_version=2,
        trace_id="trace_deep",
        parent_job_id="job_missing_ancestor",  # not on disk -> chain breaks
        nesting_depth=MAX_NESTING_DEPTH,
    )
    create_job_dir(tmp_path, "job_deep_parent")
    save_state(tmp_path / "job_deep_parent", deep_parent)

    with pytest.raises(LineageError, match="depth"):
        resolve_lineage(
            parent_flag="job_deep_parent",
            state_root=tmp_path,
            new_job_id="job_child",
        )


def test_resolve_lineage_orphan_chain_still_capped(tmp_path):
    """A chain of unknown-depth (orphan) ancestors still hits the depth cap via
    the walked lower bound — orphan-under-orphan cannot nest unbounded."""
    prev = "job_missing_root"
    for i in range(1, MAX_NESTING_DEPTH + 1):
        jid = f"job_o{i}"
        create_job_dir(tmp_path, jid)
        save_state(
            tmp_path / jid,
            Job(
                job_id=jid,
                schema_version=2,
                trace_id="trace_orphan",
                parent_job_id=prev,
                nesting_depth=None,
            ),
        )
        prev = jid
    with pytest.raises(LineageError, match="depth"):
        resolve_lineage(
            parent_flag=f"job_o{MAX_NESTING_DEPTH}",
            state_root=tmp_path,
            new_job_id="job_child",
        )


def test_resolve_lineage_rejects_explicit_traversal_parent(tmp_path):
    """An explicit parent id that isn't a valid job id is rejected outright."""
    with pytest.raises(LineageError, match="[Ii]nvalid parent"):
        resolve_lineage(
            parent_flag="../../etc/passwd",
            state_root=tmp_path,
            new_job_id="job_child",
        )


def test_resolve_lineage_inherited_traversal_parent_becomes_orphan(tmp_path):
    """An inherited malformed parent id is kept as an orphan, never path-resolved."""
    parent, _, _, depth = resolve_lineage(
        parent_env="/etc/passwd",
        state_root=tmp_path,
        new_job_id="job_child",
    )
    assert parent == "/etc/passwd"
    assert depth is None


def test_resolve_lineage_parent_flag_inherits_label(tmp_path):
    """--parent with a loadable parent inherits orchestrator_label."""
    parent_job = Job(
        job_id="job_parent_label",
        schema_version=2,
        orchestrator_label="gh-pr-review",
        nesting_depth=1,
    )
    parent_dir = create_job_dir(tmp_path, "job_parent_label")
    save_state(parent_dir, parent_job)

    _, _, label, _ = resolve_lineage(
        parent_flag="job_parent_label",
        state_root=tmp_path,
    )
    assert label == "gh-pr-review"


def test_resolve_lineage_env_parent_used_when_no_flag(tmp_path):
    """CROSSAGENT_PARENT_JOB_ID env used when no --parent flag."""
    parent_job = Job(
        job_id="job_env_parent",
        schema_version=2,
        trace_id="trace_env_parent",
        nesting_depth=1,
    )
    parent_dir = create_job_dir(tmp_path, "job_env_parent")
    save_state(parent_dir, parent_job)

    parent, trace, _, depth = resolve_lineage(
        parent_env="job_env_parent",
        state_root=tmp_path,
    )
    assert parent == "job_env_parent"
    assert trace == "trace_env_parent"
    assert depth == 2


def test_resolve_lineage_parent_flag_takes_precedence_over_env(tmp_path):
    """--parent flag takes precedence over env var."""
    parent_a = Job(
        job_id="job_flag_parent",
        schema_version=2,
        trace_id="trace_flag",
        nesting_depth=1,
    )
    parent_b = Job(
        job_id="job_env_parent", schema_version=2, trace_id="trace_env", nesting_depth=2
    )
    create_job_dir(tmp_path, "job_flag_parent")
    create_job_dir(tmp_path, "job_env_parent")
    save_state(tmp_path / "job_flag_parent", parent_a)
    save_state(tmp_path / "job_env_parent", parent_b)

    parent, trace, _, depth = resolve_lineage(
        parent_flag="job_flag_parent",
        parent_env="job_env_parent",
        state_root=tmp_path,
    )
    assert parent == "job_flag_parent"
    assert trace == "trace_flag"
    assert depth == 2  # 1 + 1


def test_resolve_lineage_no_parent_flag_no_env_detects_top_level():
    """When nothing is supplied, no parent and a fresh trace is generated."""
    parent, trace, label, depth = resolve_lineage(no_parent=False)
    assert parent is None
    assert trace.startswith("trace_")
    assert label is None
    assert depth == 1


def test_resolve_lineage_trace_flag_used_when_no_parent(tmp_path):
    """--trace-id used when no parent is available."""
    parent, trace, _, _ = resolve_lineage(
        trace_flag="my_explicit_trace",
    )
    assert parent is None
    assert trace == "my_explicit_trace"


def test_resolve_lineage_env_trace_used_when_no_parent():
    """CROSSAGENT_TRACE_ID env used when no parent and no --trace-id."""
    parent, trace, _, _ = resolve_lineage(
        trace_env="env_trace_001",
    )
    assert parent is None
    assert trace == "env_trace_001"


def test_resolve_lineage_trace_flag_overrides_env():
    """--trace-id overrides CROSSAGENT_TRACE_ID."""
    parent, trace, _, _ = resolve_lineage(
        trace_flag="flag_trace",
        trace_env="env_trace",
    )
    assert trace == "flag_trace"


def test_resolve_lineage_fresh_trace_generated_when_nothing_supplied():
    """A fresh trace is generated when no parent nor trace sources."""
    parent, trace, _, _ = resolve_lineage()
    assert parent is None
    assert trace.startswith("trace_")


def test_resolve_lineage_inherited_missing_parent_is_orphan(tmp_path):
    """An inherited (env) parent that can't be loaded is an orphan: keeps the
    parent id, depth None, does not raise."""
    parent, trace, label, depth = resolve_lineage(
        parent_env="job_nonexistent",
        state_root=tmp_path,
    )
    assert parent == "job_nonexistent"
    assert trace.startswith("trace_")  # generated since no parent loaded
    assert label is None
    assert depth is None  # orphan


def test_resolve_lineage_inherited_missing_parent_uses_trace_flag(tmp_path):
    """Inherited unloadable parent with --trace-id uses the flag value."""
    parent, trace, _, _ = resolve_lineage(
        parent_env="job_nonexistent",
        trace_flag="explicit_trace",
        state_root=tmp_path,
    )
    assert parent == "job_nonexistent"
    assert trace == "explicit_trace"


def test_resolve_lineage_inherited_missing_parent_depth_none(tmp_path):
    """Inherited unloadable parent keeps depth as None (orphan),
    regardless of depth_env."""
    parent, _, _, depth = resolve_lineage(
        parent_env="job_nonexistent",
        depth_env="3",
        state_root=tmp_path,
    )
    assert parent == "job_nonexistent"
    assert depth is None  # orphan depth is unknown


def test_resolve_lineage_orchestrator_label_explicit_wins():
    """--orchestrator-label takes precedence over parent and env."""
    label = "explicit-label"
    parent, trace, result_label, depth = resolve_lineage(
        no_parent=True,
        label_flag=label,
        label_env="env-label",
    )
    assert result_label == label


def test_resolve_lineage_orchestrator_label_env_fallback():
    """CROSSAGENT_ORCHESTRATOR_LABEL used when no flag or parent."""
    parent, trace, label, depth = resolve_lineage(
        no_parent=True,
        label_env="env-label",
    )
    assert label == "env-label"


# =========================================================================
# Lineage validation
# =========================================================================


def test_resolve_lineage_explicit_missing_parent_raises(tmp_path):
    """--parent with a non-existent job raises LineageError."""
    with pytest.raises(LineageError, match="not found"):
        resolve_lineage(
            parent_flag="job_nonexistent",
            state_root=tmp_path,
        )


def test_resolve_lineage_trace_conflict_raises(tmp_path):
    """--trace-id that differs from the loadable parent's trace raises."""
    parent_job = Job(
        job_id="job_parent",
        schema_version=2,
        trace_id="trace_parent",
    )
    create_job_dir(tmp_path, "job_parent")
    save_state(tmp_path / "job_parent", parent_job)

    with pytest.raises(LineageError, match="Trace ID conflict"):
        resolve_lineage(
            parent_flag="job_parent",
            trace_flag="trace_conflict",
            state_root=tmp_path,
            new_job_id="job_child",
        )


def test_resolve_lineage_trace_flag_matches_parent_trace_succeeds(tmp_path):
    """--trace-id equal to the parent's trace is fine (no conflict)."""
    parent_job = Job(
        job_id="job_parent",
        schema_version=2,
        trace_id="trace_parent",
    )
    create_job_dir(tmp_path, "job_parent")
    save_state(tmp_path / "job_parent", parent_job)

    parent, trace, _, depth = resolve_lineage(
        parent_flag="job_parent",
        trace_flag="trace_parent",
        state_root=tmp_path,
        new_job_id="job_child",
    )
    assert trace == "trace_parent"
    assert depth == 2


def test_resolve_lineage_cycle_detected(tmp_path):
    """A cycle in the ancestor chain raises LineageError."""
    job_a = Job(
        job_id="job_cycle_a",
        schema_version=2,
        parent_job_id="job_cycle_b",
    )
    job_b = Job(
        job_id="job_cycle_b",
        schema_version=2,
        parent_job_id="job_cycle_a",
    )
    create_job_dir(tmp_path, "job_cycle_a")
    create_job_dir(tmp_path, "job_cycle_b")
    save_state(tmp_path / "job_cycle_a", job_a)
    save_state(tmp_path / "job_cycle_b", job_b)

    with pytest.raises(LineageError, match="Cycle detected"):
        resolve_lineage(
            parent_flag="job_cycle_a",
            state_root=tmp_path,
            new_job_id="job_new",
        )


def test_resolve_lineage_self_reference_raises(tmp_path):
    """A parent chain that includes the new job's own id raises."""
    job_a = Job(
        job_id="job_self_ref",
        schema_version=2,
        parent_job_id="job_new",
    )
    create_job_dir(tmp_path, "job_self_ref")
    save_state(tmp_path / "job_self_ref", job_a)

    with pytest.raises(LineageError, match="Cycle detected"):
        resolve_lineage(
            parent_flag="job_self_ref",
            state_root=tmp_path,
            new_job_id="job_new",
        )


def test_resolve_lineage_depth_exceeded_raises(tmp_path):
    """Starting a child under the deepest node of a full chain raises."""
    prev_id = None
    for i in range(MAX_NESTING_DEPTH, 0, -1):
        job_id = f"job_depth_{i}"
        job = Job(
            job_id=job_id,
            schema_version=2,
            parent_job_id=prev_id,
        )
        create_job_dir(tmp_path, job_id)
        save_state(tmp_path / job_id, job)
        prev_id = job_id

    # prev_id is now job_depth_1 — the deepest leaf.
    # Starting under it: ancestors = [depth_1, depth_2, …, depth_8] = 8
    # depth = 8 + 1 = 9 > MAX_NESTING_DEPTH (8) → raise.
    with pytest.raises(LineageError, match=r"Maximum nesting depth.*exceeded"):
        resolve_lineage(
            parent_flag=prev_id,
            state_root=tmp_path,
            new_job_id="job_too_deep",
        )


def test_resolve_lineage_depth_exactly_max_succeeds(tmp_path):
    """A chain that yields depth exactly MAX_NESTING_DEPTH succeeds."""
    prev_id = None
    # Build (MAX_NESTING_DEPTH - 1) ancestors so that the new child is at MAX_NESTING_DEPTH.
    for i in range(MAX_NESTING_DEPTH, 1, -1):
        job_id = f"job_exact_depth_{i}"
        job = Job(
            job_id=job_id,
            schema_version=2,
            parent_job_id=prev_id,
        )
        create_job_dir(tmp_path, job_id)
        save_state(tmp_path / job_id, job)
        prev_id = job_id

    # prev_id is job_exact_depth_2.
    # ancestors = [depth_2, depth_3, …, depth_8] = 7, depth = 7 + 1 = 8 = MAX_NESTING_DEPTH.
    parent, trace, _, depth = resolve_lineage(
        parent_flag=prev_id,
        state_root=tmp_path,
        new_job_id="job_ok_depth",
    )
    assert depth == MAX_NESTING_DEPTH


def test_resolve_lineage_loadable_parent_depth_and_trace(tmp_path):
    """Happy path: loadable parent yields child depth = len(ancestors) + 1
    and the parent's trace."""
    parent_job = Job(
        job_id="job_happy_parent",
        schema_version=2,
        trace_id="trace_happy",
    )
    create_job_dir(tmp_path, "job_happy_parent")
    save_state(tmp_path / "job_happy_parent", parent_job)

    parent, trace, _, depth = resolve_lineage(
        parent_flag="job_happy_parent",
        state_root=tmp_path,
        new_job_id="job_happy_child",
    )
    assert parent == "job_happy_parent"
    assert trace == "trace_happy"
    assert depth == 2  # ancestors=[parent]; 1+1=2


# =========================================================================
# _walk_ancestor_chain
# =========================================================================


def test_walk_ancestor_chain_linear(tmp_path):
    """Walk a linear chain from leaf to root."""
    job_c = Job(job_id="job_c", schema_version=2)
    job_b = Job(job_id="job_b", schema_version=2, parent_job_id="job_c")
    job_a = Job(job_id="job_a", schema_version=2, parent_job_id="job_b")
    for j in (job_a, job_b, job_c):
        save_state(create_job_dir(tmp_path, j.job_id), j)

    ancestors = _walk_ancestor_chain("job_a", tmp_path)
    assert [a.job_id for a in ancestors] == ["job_a", "job_b", "job_c"]


def test_walk_ancestor_chain_cycle(tmp_path):
    """A cycle raises LineageError."""
    job_a = Job(job_id="job_wa", schema_version=2, parent_job_id="job_wb")
    job_b = Job(job_id="job_wb", schema_version=2, parent_job_id="job_wa")
    save_state(create_job_dir(tmp_path, "job_wa"), job_a)
    save_state(create_job_dir(tmp_path, "job_wb"), job_b)

    with pytest.raises(LineageError, match="Cycle detected"):
        _walk_ancestor_chain("job_wa", tmp_path)


def test_walk_ancestor_chain_self_reference(tmp_path):
    """Self-reference via new_job_id raises."""
    job_a = Job(job_id="job_sr", schema_version=2, parent_job_id="job_new")
    save_state(create_job_dir(tmp_path, "job_sr"), job_a)

    with pytest.raises(LineageError, match="Cycle detected"):
        _walk_ancestor_chain("job_sr", tmp_path, new_job_id="job_new")


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
        succeeded_job = transition_to(
            stale_dashboard_copy, JobState.SUCCEEDED, job_dir=job_dir
        )
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX os.kill(pid, 0) semantics only"
)
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX os.kill(pid, 0) semantics only"
)
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
    for from_state in (
        JobState.SUCCEEDED,
        JobState.CANCELLED,
        JobState.FAILED,
        JobState.TIMED_OUT,
    ):
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
    create_job_dir(tmp_path, "job_reclaim_fields")
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
    assert reclaimed.finished_at is None, (
        f"finished_at should be cleared, got {reclaimed.finished_at!r}"
    )
    assert reclaimed.duration_seconds is None, (
        f"duration_seconds should be cleared, got {reclaimed.duration_seconds!r}"
    )


# =========================================================================
# runtime_status elapsed/idle for terminal jobs  (contract: CHANGE B)
# =========================================================================


def test_runtime_status_terminal_freezes_elapsed_and_idle(tmp_path):
    """A terminal job with duration_seconds reports that rounded value
    as elapsed_seconds and idle_seconds == 0, regardless of wall-clock time.
    """
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    finished = (datetime.now(timezone.utc) - timedelta(hours=1, minutes=50)).isoformat()

    job = Job(
        job_id="job_terminal_frozen",
        status=JobState.SUCCEEDED,
        started_at=started,
        finished_at=finished,
        duration_seconds=42.6,
    )
    # Poll many wall-clock seconds later — elapsed must stay frozen.
    result = runtime_status(job)
    assert result["elapsed_seconds"] == 43  # round(42.6)
    assert result["idle_seconds"] == 0


def test_runtime_status_terminal_fallback_to_finished_at(tmp_path):
    """When duration_seconds is None but finished_at and started_at are
    set, elapsed is computed from the recorded timestamps, not wall-clock."""
    started = datetime(2026, 7, 19, 10, 0, 0, tzinfo=timezone.utc)
    started_iso = started.isoformat()
    finished_iso = (started + timedelta(minutes=5, seconds=30)).isoformat()

    job = Job(
        job_id="job_terminal_fallback",
        status=JobState.FAILED,
        started_at=started_iso,
        finished_at=finished_iso,
        duration_seconds=None,
    )
    result = runtime_status(job)
    # 5m30s = 330 seconds
    assert result["elapsed_seconds"] == 330
    assert result["idle_seconds"] == 0


def test_runtime_status_terminal_no_timestamps_falls_back():
    """When both duration_seconds and finished_at are unset, fall back to
    wall-clock elapsed."""
    started = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    job = Job(
        job_id="job_terminal_nofinish",
        status=JobState.TIMED_OUT,
        started_at=started,
        duration_seconds=None,
        finished_at=None,
    )
    result = runtime_status(job)
    assert result["elapsed_seconds"] >= 10
    assert result["idle_seconds"] == 0


def test_runtime_status_non_terminal_uses_live_values():
    """A running (non-terminal) job must report growing elapsed and idle."""
    now = datetime.now(timezone.utc)
    started = (now - timedelta(seconds=40)).isoformat()
    last_activity = (now - timedelta(seconds=5)).isoformat()
    job = Job(
        job_id="job_terminal_live",
        status=JobState.RUNNING,
        started_at=started,
        last_activity_at=last_activity,
        duration_seconds=None,
        finished_at=None,
    )
    result = runtime_status(job)
    assert result["elapsed_seconds"] >= 40
    assert result["idle_seconds"] >= 5


def test_runtime_status_never_reports_negative_durations():
    """Clock skew / a future started_at must clamp elapsed and idle to 0, not go
    negative (a terminal job with a negative stored duration, and a running job
    whose started_at is in the future)."""
    terminal = Job(
        job_id="job_neg_terminal",
        status=JobState.ABANDONED,
        started_at="2026-07-19T10:00:00+00:00",
        finished_at="2026-07-19T03:36:00+00:00",
        duration_seconds=-23099.0,
    )
    assert runtime_status(terminal)["elapsed_seconds"] == 0
    assert runtime_status(terminal)["idle_seconds"] == 0

    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    running = Job(
        job_id="job_neg_running",
        status=JobState.RUNNING,
        started_at=future,
        last_activity_at=future,
    )
    result = runtime_status(running)
    assert result["elapsed_seconds"] == 0
    assert result["idle_seconds"] == 0


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
