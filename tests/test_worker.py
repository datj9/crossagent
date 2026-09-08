"""Tests for the detached worker and logging parser wrapper."""

from __future__ import annotations

import json
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
    pass_env: list[str] | None = None,
    verify_with: str | None = None,
    verify_model: str | None = None,
    escalate_to: list[str] | None = None,
) -> Job:
    """Set up a job whose advisor is a fake claude-stream script, run the
    worker synchronously, and return the Job reloaded from disk.

    When *check* is given it is written into command.json so the worker runs
    the S3 check-gate after the delegate finishes. ``verify_with`` /
    ``escalate_to`` drive the S5 verification pass and escalation ladder."""
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
            trace_id=f"trace_{job_id}",
            nesting_depth=1,
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
    if pass_env is not None:
        command_info["pass_env"] = pass_env
    if verify_with is not None:
        command_info["verify_with"] = verify_with
        command_info["verify_model"] = verify_model
    if escalate_to is not None:
        command_info["escalate_to"] = escalate_to
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


# =========================================================================
# End-to-end delegation security posture (slice S4): drive a real job through
# worker_main and reload from disk. Unit-green is not working — these prove the
# worker actually forwards scope_result/withheld_env onto the terminal record
# and that credential env never reaches the child or any persisted artifact.
# =========================================================================

import subprocess  # noqa: E402

import pytest  # noqa: E402

from crossagent import scope as scope_mod  # noqa: E402


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _run_scope_job(
    tmp_path: Path,
    advisor_body: str,
    scope_paths,
    *,
    git_init: bool = True,
    pre_commit=None,
    job_id: str = "job_scope",
) -> Job:
    """Run a real job whose cwd is a git repo SEPARATE from the state dir, so
    crossagent's own state files are never seen as delegate edits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if git_init:
        _git(["init"], repo)
        _git(["config", "user.email", "t@e.com"], repo)
        _git(["config", "user.name", "T"], repo)
    for rel, content in pre_commit or []:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(["add", rel], repo)
        _git(["commit", "-m", f"add {rel}"], repo)

    state_dir = tmp_path / "state"
    job_dir = jobs_mod.create_job_dir(state_dir, job_id)
    fake_advisor = tmp_path / "fake_advisor.py"  # OUTSIDE the repo
    fake_advisor.write_text(textwrap.dedent(advisor_body), encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    jobs_mod.save_state(
        job_dir,
        Job(
            job_id=job_id,
            status=JobState.PENDING,
            advisor="claude",
            cwd=str(repo),
            started_at=now,
            updated_at=now,
        ),
    )
    (job_dir / "prompt").write_text("hello", encoding="utf-8")
    command_info = {
        "command": [sys.executable, str(fake_advisor)],
        "prompt_delivery": "positional",
        "cwd": str(repo),
        "result_parser": "claude-stream",
        "registry_path": str(tmp_path / "sessions.json"),
        "key": "",
        "name": None,
        "model": "",
        "advisor": "claude",
        "scope_paths": scope_paths,
    }
    jobs_mod.atomic_json_write(command_info, job_dir / "command.json")

    assert worker_main(job_id, state_dir) == 0
    return jobs_mod.load_state(job_dir)


# A fake advisor that writes *rel* under its cwd, then emits a clean result.
def _writer_advisor(rel: str) -> str:
    return f"""
    import json, os
    os.makedirs(os.path.dirname({rel!r}) or '.', exist_ok=True)
    with open({rel!r}, 'w') as handle:
        handle.write('delegate wrote this\\n')
    print(json.dumps({{"type": "result", "subtype": "success", "result": "ok"}}))
    """


def test_worker_scope_violation_is_failed_delegation_end_to_end(tmp_path):
    """A delegate that writes outside the declared allowlist yields a violated
    scope_result — persisted on disk — and a failed verdict, no green badge."""
    job = _run_scope_job(
        tmp_path,
        _writer_advisor("evil.py"),
        scope_paths=["src"],
    )
    assert job.status == JobState.SUCCEEDED  # the delegate DID finish
    assert job.scope_result is not None
    assert job.scope_result["status"] == "violated"
    assert "evil.py" in job.scope_result["violating_paths"]
    assert jobs_mod.delegation_verdict(job) == "failed"


def test_worker_scope_in_bounds_passes_end_to_end(tmp_path):
    """A delegate that edits only a declared path yields scope ok, persisted."""
    job = _run_scope_job(
        tmp_path,
        _writer_advisor("src/app.py"),
        scope_paths=["src"],
        pre_commit=[("src/app.py", "clean\n")],
    )
    assert job.status == JobState.SUCCEEDED
    assert job.scope_result is not None
    assert job.scope_result["status"] == "ok"
    assert job.scope_result["violating_paths"] == []
    # No check declared -> unverified (scope ok does not make it verified).
    assert jobs_mod.delegation_verdict(job) == "unverified"


def test_worker_non_git_cwd_scope_is_undetermined_not_pass(tmp_path):
    """A declared scope in a non-git cwd is undetermined (fail closed) and fails
    the delegation, never silently passes."""
    job = _run_scope_job(
        tmp_path,
        _writer_advisor("evil.py"),
        scope_paths=["src"],
        git_init=False,
    )
    assert job.scope_result is not None
    assert job.scope_result["status"] == "undetermined"
    assert jobs_mod.delegation_verdict(job) == "failed"


def test_worker_no_scope_declared_leaves_scope_result_none(tmp_path):
    """With no allowlist declared, scope enforcement is off: scope_result stays
    None (distinct from a satisfied scope) and the pre-S4 verdict is unchanged."""
    job = _run_scope_job(
        tmp_path,
        _writer_advisor("anything.py"),
        scope_paths=None,
    )
    assert job.scope_result is None
    assert jobs_mod.delegation_verdict(job) == "unverified"


def test_worker_scope_audit_event_records_status_and_paths(tmp_path):
    _run_scope_job(
        tmp_path,
        _writer_advisor("evil.py"),
        scope_paths=["src"],
    )
    events_path = tmp_path / "state" / "job_scope" / "events.jsonl"
    lines = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scope_events = [event for event in lines if event.get("event") == "scope"]
    assert len(scope_events) == 1
    assert scope_events[0]["status"] == "violated"
    assert "evil.py" in scope_events[0]["violating_paths"]


# --- No secret propagation -----------------------------------------------

_SECRET_NAME = "AWS_SECRET_ACCESS_KEY"
_SECRET_VALUE = "crossagent-super-secret-value-xyz"

# A fake advisor that records whether it received the secret env var.
_ENV_PROBE_ADVISOR = f"""
    import json, os
    with open('env_probe.txt', 'w') as handle:
        handle.write(os.environ.get({_SECRET_NAME!r}, 'ABSENT'))
    print(json.dumps({{"type": "result", "subtype": "success", "result": "ok"}}))
"""


def test_worker_secret_env_does_not_reach_child(tmp_path, monkeypatch):
    """A credential-bearing env var is withheld from the delegate child."""
    monkeypatch.setenv(_SECRET_NAME, _SECRET_VALUE)
    _run_job_through_worker(tmp_path, _ENV_PROBE_ADVISOR)
    probe = (tmp_path / "env_probe.txt").read_text(encoding="utf-8")
    assert probe == "ABSENT"


def test_worker_pass_env_opt_in_reaches_child(tmp_path, monkeypatch):
    """An explicitly passed-through credential var DOES reach the delegate."""
    monkeypatch.setenv(_SECRET_NAME, _SECRET_VALUE)
    job = _run_job_through_worker(tmp_path, _ENV_PROBE_ADVISOR, pass_env=[_SECRET_NAME])
    probe = (tmp_path / "env_probe.txt").read_text(encoding="utf-8")
    assert probe == _SECRET_VALUE
    assert job.withheld_env is not None
    assert _SECRET_NAME not in job.withheld_env


def test_worker_secret_value_absent_from_all_persisted_artifacts(tmp_path, monkeypatch):
    """The secret VALUE must not appear in state.json, command.json, events.jsonl
    or the redacted command; only the NAME is recorded (names are not secrets)."""
    monkeypatch.setenv(_SECRET_NAME, _SECRET_VALUE)
    job = _run_job_through_worker(tmp_path, _ENV_PROBE_ADVISOR)

    assert job.withheld_env is not None
    assert _SECRET_NAME in job.withheld_env  # name recorded...
    job_dir = tmp_path / "state" / "job_e2e"
    for artifact in ("state.json", "command.json", "events.jsonl"):
        text = (job_dir / artifact).read_text(encoding="utf-8")
        assert _SECRET_VALUE not in text, artifact  # ...but never the value
    assert _SECRET_VALUE not in job.redacted_command


def test_worker_env_scrub_event_records_name_not_value(tmp_path, monkeypatch):
    monkeypatch.setenv(_SECRET_NAME, _SECRET_VALUE)
    _run_job_through_worker(tmp_path, _ENV_PROBE_ADVISOR)
    events_path = tmp_path / "state" / "job_e2e" / "events.jsonl"
    lines = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scrub_events = [event for event in lines if event.get("event") == "env_scrub"]
    assert len(scrub_events) == 1
    assert _SECRET_NAME in scrub_events[0]["withheld"]
    assert _SECRET_VALUE not in json.dumps(scrub_events[0])


def test_worker_v2_record_without_s4_fields_still_loads(tmp_path):
    """A pre-S4 record on disk (no scope_result / withheld_env) loads with those
    fields absent, not crashing (D7: absent stays distinguishable)."""
    job_dir = jobs_mod.create_job_dir(tmp_path / "state", "job_v2")
    jobs_mod.atomic_json_write(
        {
            "schema_version": 2,
            "job_id": "job_v2",
            "status": "succeeded",
            "advisor": "claude",
        },
        job_dir / "state.json",
    )
    loaded = jobs_mod.load_state(job_dir)
    assert loaded.scope_result is None
    assert loaded.withheld_env is None
    assert jobs_mod.delegation_verdict(loaded) == "unverified"


def test_scope_module_importable_without_error():
    # Guard: the module and its git timeout constant are wired.
    assert scope_mod._GIT_TIMEOUT_SECONDS > 0
    assert pytest is not None


# =========================================================================
# End-to-end independent verification + escalation (slice S5): drive a real
# job through worker_main and reload state from disk. A unit test over the
# combiner alone would not catch the worker forgetting to forward the verify
# result or to spawn the escalation child — this drives the whole path.
# =========================================================================

from crossagent.advisors import Advisor  # noqa: E402


def _fake_verifier(
    tmp_path: Path, verdict: str, *, record_argv: bool = False
) -> Advisor:
    """A fake claude-style verifier advisor emitting a structured verdict."""
    record = (
        "import sys\n"
        "open('verifier_argv.json','w').write(__import__('json').dumps(sys.argv))\n"
        if record_argv
        else ""
    )
    script = tmp_path / "fake_verifier.py"
    script.write_text(
        record
        + "import json\n"
        + "print(json.dumps({'type':'result','subtype':'success',"
        + f"'structured_output': {{'verdict': {verdict!r}, 'reason': 'because'}}}}))\n",
        encoding="utf-8",
    )
    return Advisor(
        name="myverifier",
        executable=sys.executable,
        base_args=(str(script),),
        prompt_delivery="positional",
        result_parser="claude-stream",
        json_args=(),
        json_schema_flag="--json-schema",
    )


def test_worker_verification_fail_blocks_green_end_to_end(tmp_path, monkeypatch):
    """A structured 'fail' from the fresh verifier is persisted and drives the
    delegation verdict to failed even though the delegate exited 0 (D6)."""
    verifier = _fake_verifier(tmp_path, "fail")
    monkeypatch.setattr(
        "crossagent.advisors.resolve", lambda name, config_path=None: verifier
    )

    job = _run_job_through_worker(tmp_path, _RESULT_OK, verify_with="myverifier")

    assert job.status == JobState.SUCCEEDED  # the delegate DID finish
    assert job.verify_result is not None
    assert job.verify_result["verdict"] == "fail"
    assert job.verify_result["advisor"] == "myverifier"
    assert jobs_mod.delegation_verdict(job) == "failed"


def test_worker_verification_pass_yields_verified_end_to_end(tmp_path, monkeypatch):
    verifier = _fake_verifier(tmp_path, "pass")
    monkeypatch.setattr(
        "crossagent.advisors.resolve", lambda name, config_path=None: verifier
    )

    job = _run_job_through_worker(tmp_path, _RESULT_OK, verify_with="myverifier")

    assert job.verify_result["verdict"] == "pass"
    assert job.verify_result["structured"] is True
    assert jobs_mod.delegation_verdict(job) == "verified"


def test_worker_verifier_session_is_fresh_and_artifact_is_user_turn(
    tmp_path, monkeypatch
):
    """The verifier subprocess receives NO session-attachment flag (fresh
    session) and the artifact as its final positional argument (user turn)."""
    verifier = _fake_verifier(tmp_path, "pass", record_argv=True)
    monkeypatch.setattr(
        "crossagent.advisors.resolve", lambda name, config_path=None: verifier
    )

    _run_job_through_worker(tmp_path, _RESULT_OK, verify_with="myverifier")

    argv = json.loads((tmp_path / "verifier_argv.json").read_text())
    for flag in ("--resume", "--fork-session", "--name"):
        assert flag not in argv, flag
    # The artifact is the LAST argument (user-turn input), and it embeds the
    # delegate's answer rather than arriving as prior assistant context.
    assert "DELEGATE'S ANSWER" in argv[-1]
    assert "ok" in argv[-1]


def test_worker_unverified_verifier_does_not_green_end_to_end(tmp_path, monkeypatch):
    """A prose-only verifier (no structured verdict) degrades to unverified: it
    neither greens nor hard-fails the delegation."""
    prose = tmp_path / "fake_prose_verifier.py"
    prose.write_text("print('looks fine to me')\n", encoding="utf-8")
    verifier = Advisor(
        name="prosever",
        executable=sys.executable,
        base_args=(str(prose),),
        prompt_delivery="positional",
        result_parser="text",
        json_schema_flag=None,
    )
    monkeypatch.setattr(
        "crossagent.advisors.resolve", lambda name, config_path=None: verifier
    )

    job = _run_job_through_worker(tmp_path, _RESULT_OK, verify_with="prosever")

    assert job.verify_result["verdict"] == "unverified"
    assert jobs_mod.delegation_verdict(job) == "unverified"


def test_worker_verify_audit_event_records_verdict_not_artifact(tmp_path, monkeypatch):
    verifier = _fake_verifier(tmp_path, "fail")
    monkeypatch.setattr(
        "crossagent.advisors.resolve", lambda name, config_path=None: verifier
    )
    _run_job_through_worker(tmp_path, _RESULT_OK, verify_with="myverifier")

    events_path = tmp_path / "state" / "job_e2e" / "events.jsonl"
    lines = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    verify_events = [event for event in lines if event.get("event") == "verify"]
    assert len(verify_events) == 1
    assert verify_events[0]["verdict"] == "fail"
    assert verify_events[0]["advisor"] == "myverifier"


def test_worker_persists_terminal_record_when_verify_schema_temp_file_fails(
    tmp_path, monkeypatch
):
    """HIGH 2: if the verifier's schema temp file cannot be created — read-only
    /tmp, full disk, restricted TMPDIR — the OSError must NOT propagate out of
    run_verification into the worker after the check + scope gates already ran.
    Otherwise the terminal transition never persists and the job is wedged
    non-terminal forever with the delegate's real work destroyed. The worker must
    still reach and persist a terminal record."""
    import tempfile

    from crossagent import verify as verify_mod

    verifier = _fake_verifier(tmp_path, "pass")  # structured -> mkstemp attempted
    monkeypatch.setattr(
        "crossagent.advisors.resolve", lambda name, config_path=None: verifier
    )

    # Fail ONLY the verifier's schema temp file; leave the state-persistence
    # temp files (a different prefix) working, so this isolates the verify path.
    real_mkstemp = tempfile.mkstemp

    def _selective_mkstemp(*args, **kwargs):
        if kwargs.get("prefix", "").startswith("crossagent-verify-"):
            raise OSError("read-only file system")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(verify_mod.tempfile, "mkstemp", _selective_mkstemp)

    job = _run_job_through_worker(tmp_path, _RESULT_OK, verify_with="myverifier")

    # The worker reached a terminal state and recorded the verification, rather
    # than crashing after the delegate's work with the job left non-terminal.
    assert jobs_mod.is_terminal(job.status)
    assert job.status == JobState.SUCCEEDED
    assert job.verify_result is not None
    # Degraded to non-structured mode: the verdict still parsed from the answer.
    assert job.verify_result["verdict"] == "pass"


def test_worker_escalates_failed_delegation_end_to_end(tmp_path, monkeypatch):
    """A failed delegation (failing check) with an escalation ladder spawns a
    same-trace child with parent_job_id set — the exact shape analytics counts."""
    spawned: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "crossagent.escalate._default_launcher",
        lambda child_id, state_root: spawned.append((child_id, state_root)),
    )

    job = _run_job_through_worker(
        tmp_path, _RESULT_OK, check=_check_cmd(1), escalate_to=["codex:gpt-5.6-sol"]
    )

    assert jobs_mod.delegation_verdict(job) == "failed"
    assert len(spawned) == 1
    child_id, _ = spawned[0]
    child = jobs_mod.load_state(tmp_path / "state" / child_id)
    assert child.parent_job_id == job.job_id
    assert child.trace_id == job.trace_id  # SAME trace (option a)
    assert child.nesting_depth == 2
    assert child.advisor == "codex"


def test_worker_does_not_escalate_a_passing_delegation(tmp_path, monkeypatch):
    spawned: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "crossagent.escalate._default_launcher",
        lambda child_id, state_root: spawned.append((child_id, state_root)),
    )

    job = _run_job_through_worker(
        tmp_path, _RESULT_OK, check=_check_cmd(0), escalate_to=["codex"]
    )

    assert jobs_mod.delegation_verdict(job) == "verified"
    assert spawned == []
