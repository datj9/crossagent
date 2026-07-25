"""Tests for the detached worker and logging parser wrapper."""

from __future__ import annotations

import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from crossagent import jobs as jobs_mod
from crossagent.jobs import Job, JobState
from crossagent.parsers import EventParser
from crossagent.worker import _LoggingParser, build_advisor_env, worker_main


class _SilentParser(EventParser):
    """Parser stub that does nothing — just satisfies the interface."""

    def consume_stdout(self, line: str) -> None:
        pass

    def consume_stderr(self, line: str) -> None:
        pass

    def finish(self, exit_code: int) -> None:
        return None


def test_logging_parser_flushes_after_write(tmp_path):
    """After consume_stdout/consume_stderr writes a line, the data must be
    present on disk without closing the file (i.e. it was flushed)."""
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    parser = _SilentParser()
    lp = _LoggingParser(parser, stdout_log, stderr_log)

    # Write stdout — must be readable without closing
    lp.consume_stdout("line one\n")
    assert stdout_log.read_text(encoding="utf-8") == "line one\n"

    # Write stderr — must be readable without closing
    lp.consume_stderr("error line\n")
    assert stderr_log.read_text(encoding="utf-8") == "error line\n"

    # Second write — both lines present
    lp.consume_stdout("line two\n")
    assert stdout_log.read_text(encoding="utf-8") == "line one\nline two\n"

    lp.finish(0)


# =========================================================================
# build_advisor_env
# =========================================================================


def test_build_advisor_env_sets_lineage_vars():
    job = Job(
        job_id="job_abc",
        trace_id="trace_xyz",
        orchestrator_label="my-label",
        nesting_depth=3,
    )
    state_root = Path("/tmp/test_state")
    env = build_advisor_env(job, state_root)

    assert env["CROSSAGENT_PARENT_JOB_ID"] == "job_abc"
    assert env["CROSSAGENT_TRACE_ID"] == "trace_xyz"
    assert env["CROSSAGENT_ORCHESTRATOR_LABEL"] == "my-label"
    assert env["CROSSAGENT_NESTING_DEPTH"] == "3"
    assert env["CROSSAGENT_STATE_DIR"] == str(state_root)


def test_build_advisor_env_none_fields_use_empty_string():
    job = Job(
        job_id="job_abc",
        trace_id=None,
        orchestrator_label=None,
        nesting_depth=None,
    )
    state_root = Path("/tmp/test_state")
    env = build_advisor_env(job, state_root)

    assert env["CROSSAGENT_PARENT_JOB_ID"] == "job_abc"
    assert env["CROSSAGENT_TRACE_ID"] == ""
    assert env["CROSSAGENT_ORCHESTRATOR_LABEL"] == ""
    assert env["CROSSAGENT_NESTING_DEPTH"] == ""


def test_build_advisor_env_overwrites_existing_env_var(monkeypatch):
    """The helper must overwrite, not setdefault, so a grandchild does not
    inherit the grandparent's CROSSAGENT_PARENT_JOB_ID."""
    monkeypatch.setenv("CROSSAGENT_PARENT_JOB_ID", "job_grandparent")
    monkeypatch.setenv("CROSSAGENT_TRACE_ID", "trace_old")

    job = Job(
        job_id="job_child",
        trace_id="trace_child",
        orchestrator_label="child-label",
        nesting_depth=2,
    )
    state_root = Path("/tmp/state")
    env = build_advisor_env(job, state_root)

    assert env["CROSSAGENT_PARENT_JOB_ID"] == "job_child"
    assert env["CROSSAGENT_TRACE_ID"] == "trace_child"


def test_build_advisor_env_contains_state_dir():
    job = Job(job_id="job_id")
    state_root = Path("/custom/state/root")
    env = build_advisor_env(job, state_root)
    assert env["CROSSAGENT_STATE_DIR"] == "/custom/state/root"


# =========================================================================
# End-to-end metric persistence (slice S1): drive a real job through
# worker_main to a terminal state and assert the metrics landed on disk.
# A test that only exercised transition_to(**overrides) would not catch the
# worker forgetting to forward them — this drives the whole path.
# =========================================================================


def _run_job_through_worker(
    tmp_path: Path,
    fake_advisor_body: str,
    *,
    job_id: str = "job_e2e",
    check: str | None = None,
    check_timeout: float = 30.0,
) -> Job:
    """Set up a job whose advisor is a fake claude-stream script, run the
    worker synchronously, and return the Job reloaded from disk.

    When *check* is given it is written into command.json so the worker runs
    the S3 check-gate after the delegate finishes."""
    state_dir = tmp_path / "state"
    job_dir = jobs_mod.create_job_dir(state_dir, job_id)

    fake_advisor = tmp_path / "fake_claude.py"
    fake_advisor.write_text(textwrap.dedent(fake_advisor_body), encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    jobs_mod.save_state(
        job_dir,
        Job(
            job_id=job_id,
            status=JobState.PENDING,
            advisor="claude",
            cwd=str(tmp_path),
            started_at=now,
            updated_at=now,
        ),
    )
    (job_dir / "prompt").write_text("hello", encoding="utf-8")
    command_info = {
        "command": [sys.executable, str(fake_advisor)],
        "prompt_delivery": "positional",
        "cwd": str(tmp_path),
        "result_parser": "claude-stream",
        "registry_path": str(tmp_path / "sessions.json"),
        "key": "",
        "name": None,
        "model": "",
        "advisor": "claude",
    }
    if check is not None:
        command_info["check"] = check
        command_info["check_timeout"] = check_timeout
    jobs_mod.atomic_json_write(command_info, job_dir / "command.json")

    exit_code = worker_main(job_id, state_dir)
    assert exit_code == 0
    return jobs_mod.load_state(job_dir)


_RESULT_OK = (
    "import json\n"
    'print(json.dumps({"type": "result", "subtype": "success", "result": "ok"}))\n'
)
_NO_RESULT = "pass\n"


def _check_cmd(exit_code: int) -> str:
    """A portable check command that exits with *exit_code*."""
    return f'{sys.executable} -c "import sys; sys.exit({exit_code})"'


def test_worker_persists_advisor_metrics_end_to_end(tmp_path):
    """A completed claude job persists non-empty usage_details and a
    cost_details labelled cost_source='advisor' (S1 acceptance)."""
    job = _run_job_through_worker(
        tmp_path,
        """
        import json
        print(json.dumps({"type": "system", "subtype": "init",
                          "model": "claude-opus-4", "session_id": "sess_e2e"}))
        print(json.dumps({"type": "result", "subtype": "success",
                          "session_id": "sess_e2e", "result": "ok",
                          "total_cost_usd": 0.0123, "duration_ms": 4200,
                          "usage": {"input_tokens": 100, "output_tokens": 50,
                                    "cache_read_input_tokens": 200}}))
        """,
    )
    assert job.status == JobState.SUCCEEDED
    assert job.usage_details == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 200,
    }
    assert job.cost_details == {"total": 0.0123}
    assert job.cost_source == "advisor"
    assert job.duration_ms == 4200
    assert job.model_reported == "claude-opus-4"


def test_worker_persists_unknown_when_no_telemetry(tmp_path):
    """An advisor emitting no telemetry persists unknown, never 0.0 (D7),
    and the job still succeeds."""
    job = _run_job_through_worker(
        tmp_path,
        """
        import json
        print(json.dumps({"type": "result", "subtype": "success",
                          "result": "ok"}))
        """,
    )
    assert job.status == JobState.SUCCEEDED
    assert job.usage_details == {}
    assert job.cost_details == {}
    assert job.cost_source == "unknown"
    assert job.duration_ms is None
    assert job.model_reported is None
    # The unmeasured shape must not look like a measured zero.
    assert job.cost_details != {"total": 0.0}


def test_worker_malformed_telemetry_does_not_fail_job(tmp_path):
    """Malformed telemetry (wrong-typed fields) must degrade to unknown and
    must NOT fail the job (D4)."""
    job = _run_job_through_worker(
        tmp_path,
        """
        import json
        print(json.dumps({"type": "result", "subtype": "success",
                          "result": "ok", "usage": ["not", "a", "dict"],
                          "total_cost_usd": "not-a-number",
                          "duration_ms": {"nested": "garbage"}}))
        """,
    )
    assert job.status == JobState.SUCCEEDED
    assert job.usage_details == {}
    assert job.cost_details == {}
    assert job.cost_source == "unknown"
    assert job.duration_ms is None


# =========================================================================
# End-to-end check-gate persistence (slice S3): drive a real job through
# worker_main WITH a --check and assert the check_result landed on the
# terminal record on disk. Like the metric tests above, this drives the whole
# path — a test that only called transition_to(check_result=...) directly
# could not catch the worker forgetting to forward it.
# =========================================================================


def test_worker_check_failure_is_a_failed_delegation(tmp_path):
    """A delegate that exits 0 (clean success) whose check exits non-zero is a
    FAILED delegation carrying the real check exit code — status stays
    SUCCEEDED (the delegate DID finish) but the verdict is failed (S3
    acceptance: no green badge)."""
    job = _run_job_through_worker(tmp_path, _RESULT_OK, check=_check_cmd(1))
    # The delegate process finished cleanly...
    assert job.status == JobState.SUCCEEDED
    # ...but the independent check failed, so the delegation failed.
    assert job.check_result is not None
    assert job.check_result["exit_code"] == 1
    assert job.check_result["command"] == _check_cmd(1)
    assert jobs_mod.delegation_verdict(job) == "failed"


def test_worker_check_pass_yields_verified(tmp_path):
    """A clean delegate whose check exits 0 is a verified delegation."""
    job = _run_job_through_worker(tmp_path, _RESULT_OK, check=_check_cmd(0))
    assert job.status == JobState.SUCCEEDED
    assert job.check_result is not None
    assert job.check_result["exit_code"] == 0
    assert jobs_mod.delegation_verdict(job) == "verified"


def test_worker_missing_check_is_unverified_not_pass(tmp_path):
    """With no --check the job is labelled unverified, never a pass (S3
    acceptance) — check_result stays None so it is distinguishable from a
    check that ran and passed."""
    job = _run_job_through_worker(tmp_path, _RESULT_OK)
    assert job.status == JobState.SUCCEEDED
    assert job.check_result is None
    assert jobs_mod.delegation_verdict(job) == "unverified"


def test_worker_check_output_captured_when_delegate_crashes(tmp_path):
    """The check runs and its output is captured even when the delegate itself
    crashed (no result event -> FAILED). The verdict is failed because the
    delegate never finished, but the check ran for diagnostics (S3
    acceptance)."""
    job = _run_job_through_worker(
        tmp_path,
        _NO_RESULT,
        check=(
            f'{sys.executable} -c "import sys; '
            "print('check-ran-marker'); sys.exit(0)\""
        ),
    )
    assert job.status == JobState.FAILED
    assert job.check_result is not None
    assert "check-ran-marker" in job.check_result["stdout_tail"]
    assert jobs_mod.delegation_verdict(job) == "failed"


def test_worker_nonexistent_check_does_not_crash_worker(tmp_path):
    """A check command that isn't executable is captured as a non-zero exit and
    does NOT crash the worker or fail to persist a terminal state (S3
    acceptance)."""
    job = _run_job_through_worker(
        tmp_path, _RESULT_OK, check="crossagent-no-such-check-cmd-xyz"
    )
    assert job.status == JobState.SUCCEEDED
    assert job.check_result is not None
    assert job.check_result["exit_code"] != 0
    assert jobs_mod.delegation_verdict(job) == "failed"


def test_worker_enormous_check_output_is_truncated_on_record(tmp_path):
    """A check that emits huge output persists only a bounded tail — the whole
    Job record is loaded into the dashboard, so it must not carry megabytes."""
    from crossagent import check as check_mod

    job = _run_job_through_worker(
        tmp_path,
        _RESULT_OK,
        check=f"{sys.executable} -c \"print('x' * 500000)\"",
    )
    assert job.check_result is not None
    assert len(job.check_result["stdout_tail"]) <= (
        check_mod.CHECK_OUTPUT_TAIL_CHARS + len(check_mod._TRUNCATION_MARKER)
    )


def test_worker_check_logs_command_and_code_but_not_output(tmp_path):
    """The audit log records the check's command and exit code but NEVER its
    output (which may contain secrets). The output is put in a script file so
    the secret marker lives only in the check's OUTPUT, not its command."""
    import json

    check_script = tmp_path / "leaky_check.py"
    check_script.write_text(
        "import sys\nprint('secret-in-output')\nsys.exit(2)\n",
        encoding="utf-8",
    )
    job = _run_job_through_worker(
        tmp_path,
        _RESULT_OK,
        check=f"{sys.executable} {check_script}",
    )
    # The secret IS captured in the private state record (0o600) for debugging.
    assert job.check_result is not None
    assert "secret-in-output" in job.check_result["stdout_tail"]

    # ...but the audit log carries only the command + exit code, never output.
    events_path = tmp_path / "state" / "job_e2e" / "events.jsonl"
    lines = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    check_events = [event for event in lines if event.get("event") == "check"]
    assert len(check_events) == 1
    assert check_events[0]["exit_code"] == 2
    assert "secret-in-output" not in json.dumps(check_events[0])
