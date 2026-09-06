"""Tests for the escalate-on-failure ladder (slice S5)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from crossagent import jobs as jobs_mod
from crossagent.escalate import _drop_first_nonblank, maybe_escalate, parse_rungs
from crossagent.jobs import (
    MAX_NESTING_DEPTH,
    Job,
    JobState,
    load_state,
    save_state,
)


# ---------------------------------------------------------------------------
# Rung parsing
# ---------------------------------------------------------------------------


def test_parse_rungs_advisor_only():
    assert parse_rungs(["claude"]) == [("claude", None)]


def test_parse_rungs_advisor_and_model():
    assert parse_rungs(["claude:opus", "codex:gpt-5.6-sol"]) == [
        ("claude", "opus"),
        ("codex", "gpt-5.6-sol"),
    ]


def test_parse_rungs_splits_on_first_colon_only():
    assert parse_rungs(["claude:some:model"]) == [("claude", "some:model")]


def test_parse_rungs_drops_blank_entries():
    assert parse_rungs(["", "  ", "claude"]) == [("claude", None)]


def test_parse_rungs_none_is_empty():
    assert parse_rungs(None) == []


def test_drop_first_nonblank_removes_only_the_spawned_rung():
    assert _drop_first_nonblank(["claude:opus", "codex"]) == ["codex"]
    assert _drop_first_nonblank(["", "claude", "codex"]) == ["codex"]
    assert _drop_first_nonblank(["only"]) == []


# ---------------------------------------------------------------------------
# maybe_escalate
# ---------------------------------------------------------------------------


def _failed_parent(
    state_root: Path,
    *,
    job_id="job_parent",
    depth=1,
    trace="trace_x",
    parent_job_id=None,
) -> Job:
    """Persist a failed delegation (failing check) as the escalation parent."""
    job_dir = jobs_mod.create_job_dir(state_root, job_id)
    now = datetime.now(timezone.utc).isoformat()
    job = Job(
        job_id=job_id,
        status=JobState.SUCCEEDED,  # the delegate FINISHED...
        advisor="codex",
        cwd=str(state_root),
        started_at=now,
        updated_at=now,
        trace_id=trace,
        parent_job_id=parent_job_id,
        nesting_depth=depth,
        # ...but the check failed -> delegation_verdict == "failed".
        check_result={
            "command": "pytest",
            "exit_code": 1,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )
    save_state(job_dir, job)
    return job


def _spawns() -> tuple[list[tuple[str, Path]], object]:
    spawned: list[tuple[str, Path]] = []

    def launcher(child_id: str, state_root: Path) -> None:
        spawned.append((child_id, state_root))

    return spawned, launcher


def _escalate(job, state_root, escalate_to, launcher, **overrides):
    kwargs = dict(
        prompt="do the task",
        state_root=state_root,
        job_dir=state_root / job.job_id,
        cwd=str(state_root),
        registry_path=str(state_root / "sessions.json"),
        escalate_to=escalate_to,
        check="pytest",
        check_timeout=30.0,
        scope_paths=["src"],
        pass_env=["ANTHROPIC_API_KEY"],
        verify_with="claude",
        verify_model=None,
        launcher=launcher,
    )
    kwargs.update(overrides)
    return maybe_escalate(job, **kwargs)


def test_escalation_creates_same_trace_child_with_parent_link(tmp_path):
    """Option (a): the re-dispatch is a same-trace child with parent_job_id set,
    which is exactly what analytics.py counts as an escalation."""
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    spawned, launcher = _spawns()

    child_id = _escalate(parent, state_root, ["claude:opus"], launcher)

    assert child_id is not None
    child = load_state(state_root / child_id)
    assert child.parent_job_id == "job_parent"
    assert child.trace_id == "trace_x"  # SAME trace
    assert child.nesting_depth == 2  # one deeper than the parent
    assert child.advisor == "claude"
    # The worker was actually launched for the child.
    assert spawned == [(child_id, state_root)]


def test_escalation_is_recognised_by_analytics(tmp_path):
    """The whole point of option (a): the shipped analytics escalation rate must
    now see the re-dispatch, without any change to analytics.py."""
    from crossagent.analytics import build_analytics

    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    _spawned, launcher = _spawns()
    child_id = _escalate(parent, state_root, ["claude:opus"], launcher)

    parent = load_state(state_root / "job_parent")
    child = load_state(state_root / child_id)
    escalation = build_analytics([parent, child])["totals"]["escalation"]
    assert escalation["failed"] == 1
    assert escalation["escalated"] == 1
    assert escalation["rate"] == 1.0


def test_escalation_propagates_remaining_ladder_and_gates(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    _spawned, launcher = _spawns()
    child_id = _escalate(
        parent, state_root, ["claude:opus", "codex:gpt-5.6-sol"], launcher
    )

    command = json.loads((state_root / child_id / "command.json").read_text())
    # The spawned rung is dropped; the rest is handed to the child.
    assert command["escalate_to"] == ["codex:gpt-5.6-sol"]
    # Security posture + gates carried to the larger peer.
    assert command["scope_paths"] == ["src"]
    assert command["pass_env"] == ["ANTHROPIC_API_KEY"]
    assert command["check"] == "pytest"
    assert command["verify_with"] == "claude"


def test_no_escalation_when_delegation_did_not_fail(tmp_path):
    """A verified/unverified delegation is never escalated."""
    state_root = tmp_path / "state"
    job_dir = jobs_mod.create_job_dir(state_root, "job_ok")
    now = datetime.now(timezone.utc).isoformat()
    job = Job(
        job_id="job_ok",
        status=JobState.SUCCEEDED,
        cwd=str(state_root),
        started_at=now,
        updated_at=now,
        trace_id="trace_ok",
        nesting_depth=1,
        check_result={
            "command": "pytest",
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )
    save_state(job_dir, job)
    spawned, launcher = _spawns()
    assert _escalate(job, state_root, ["claude"], launcher) is None
    assert spawned == []


def test_no_escalation_when_no_rungs(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    spawned, launcher = _spawns()
    assert _escalate(parent, state_root, None, launcher) is None
    assert _escalate(parent, state_root, [], launcher) is None
    assert spawned == []


def test_escalation_halts_at_depth_cap(tmp_path):
    """The runaway guard: a parent already at MAX_NESTING_DEPTH cannot spawn a
    deeper child. The ladder stops and the skip is audited, not crashed."""
    state_root = tmp_path / "state"
    # A parent already at the cap whose ancestor chain is broken: lineage
    # resolution falls back to parent.nesting_depth + 1, which exceeds the cap.
    parent = _failed_parent(
        state_root, depth=MAX_NESTING_DEPTH, parent_job_id="job_missing_ancestor"
    )
    spawned, launcher = _spawns()

    result = _escalate(parent, state_root, ["claude:opus"], launcher)

    assert result is None
    assert spawned == []
    events = _events(state_root / "job_parent")
    skips = [e for e in events if e.get("event") == "escalation_skipped"]
    assert len(skips) == 1
    assert (
        "depth" in skips[0]["reason"].lower() or "nesting" in skips[0]["reason"].lower()
    )


def test_escalation_unknown_advisor_is_skipped_not_crashed(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    spawned, launcher = _spawns()
    assert _escalate(parent, state_root, ["nosuchadvisor"], launcher) is None
    assert spawned == []
    events = _events(state_root / "job_parent")
    assert any(e.get("event") == "escalation_skipped" for e in events)


def test_escalation_audit_event_on_parent(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    _spawned, launcher = _spawns()
    child_id = _escalate(parent, state_root, ["claude:opus"], launcher)

    events = _events(state_root / "job_parent")
    escalations = [e for e in events if e.get("event") == "escalation"]
    assert len(escalations) == 1
    assert escalations[0]["child_job_id"] == child_id
    assert escalations[0]["advisor"] == "claude"
    assert escalations[0]["trace_id"] == "trace_x"


# ---------------------------------------------------------------------------
# HIGH 1: escalation is scoped to a FINISHED delegate whose declared gate
# failed — never to a job that did not finish cleanly (CANCELLED / TIMED_OUT).
# ---------------------------------------------------------------------------


def _terminal_parent(
    state_root: Path, *, status: JobState, job_id: str = "job_term"
) -> Job:
    """Persist a parent in a non-success terminal state.

    Carries a *failing* check_result, mirroring a real cancel/timeout that
    interrupts the delegate mid-run: under the old ``delegation_verdict !=
    "failed"`` gate (which returns "failed" for ANY non-success terminal status)
    this would have been re-dispatched.
    """
    job_dir = jobs_mod.create_job_dir(state_root, job_id)
    now = datetime.now(timezone.utc).isoformat()
    job = Job(
        job_id=job_id,
        status=status,
        advisor="codex",
        cwd=str(state_root),
        started_at=now,
        updated_at=now,
        trace_id="trace_term",
        nesting_depth=1,
        check_result={
            "command": "pytest",
            "exit_code": 1,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )
    save_state(job_dir, job)
    return job


def test_cancelled_parent_is_never_escalated(tmp_path):
    """A user who cancels a job must not have it silently re-dispatched to a
    larger, costlier peer — that is the opposite of cancelling (HIGH 1)."""
    state_root = tmp_path / "state"
    parent = _terminal_parent(state_root, status=JobState.CANCELLED)
    spawned, launcher = _spawns()
    assert _escalate(parent, state_root, ["claude:opus"], launcher) is None
    assert spawned == []


def test_timed_out_parent_is_never_escalated(tmp_path):
    """Decision: a TIMED_OUT delegate produced no graded artifact, and a bigger,
    slower peer is at least as likely to time out again under the same budget, so
    it is NOT auto-escalated — the ladder is for gate failures on completed work,
    not for work that never finished (HIGH 1)."""
    state_root = tmp_path / "state"
    parent = _terminal_parent(state_root, status=JobState.TIMED_OUT)
    spawned, launcher = _spawns()
    assert _escalate(parent, state_root, ["claude:opus"], launcher) is None
    assert spawned == []


def test_gate_failure_parent_still_escalates(tmp_path):
    """The working path is not regressed: a SUCCEEDED delegate whose declared
    gate (here, the check) failed is still escalated (HIGH 1)."""
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)  # SUCCEEDED + failing check
    spawned, launcher = _spawns()
    child_id = _escalate(parent, state_root, ["claude:opus"], launcher)
    assert child_id is not None
    assert spawned == [(child_id, state_root)]


def _events(job_dir: Path) -> list[dict]:
    path = job_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Delegation mode propagation: a --write job must escalate to a write-capable
# peer with the mode re-expanded against the CHILD advisor, never silently drop
# to read-only.
# ---------------------------------------------------------------------------


def test_escalation_reexpands_write_mode_against_child_advisor(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    _spawned, launcher = _spawns()
    child_id = _escalate(parent, state_root, ["claude:opus"], launcher, mode="write")

    assert child_id is not None
    command = json.loads((state_root / child_id / "command.json").read_text())
    # Claude's write flag, not commandcode's — expanded for the child advisor.
    assert command["command"][command["command"].index("--permission-mode") + 1] == (
        "bypassPermissions"
    )
    # Semantic mode carried so a further escalation re-expands again.
    assert command["mode"] == "write"


def test_escalation_refuses_write_onto_readonly_rung(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    spawned, launcher = _spawns()
    # gemini has no write mode; escalating a write onto it would silently run
    # read-only, so the rung is refused and audited.
    result = _escalate(parent, state_root, ["gemini"], launcher, mode="write")

    assert result is None
    assert spawned == []
    events = _events(state_root / "job_parent")
    skips = [e for e in events if e.get("event") == "escalation_skipped"]
    assert len(skips) == 1
    assert "write mode" in skips[0]["reason"]


def test_escalation_write_mode_emits_child_write_flag(tmp_path):
    # Regression guard: a --write escalation onto codex must actually put codex
    # into workspace-write, not silently re-run read-only.
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    _spawned, launcher = _spawns()
    child_id = _escalate(parent, state_root, ["codex"], launcher, mode="write")

    assert child_id is not None
    command = json.loads((state_root / child_id / "command.json").read_text())
    argv = command["command"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_escalation_plan_mode_is_carried(tmp_path):
    state_root = tmp_path / "state"
    parent = _failed_parent(state_root)
    _spawned, launcher = _spawns()
    child_id = _escalate(parent, state_root, ["claude:opus"], launcher, mode="plan")

    command = json.loads((state_root / child_id / "command.json").read_text())
    assert command["mode"] == "plan"
    assert command["command"][command["command"].index("--permission-mode") + 1] == (
        "plan"
    )
