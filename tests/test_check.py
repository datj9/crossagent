"""Tests for the independent check-gate (slice S3).

``run_check`` runs the caller's verification command after a delegate finishes
and turns its real exit code into the delegation verdict (D5). It must never
raise — an un-runnable check is captured as a non-zero exit code (not verified),
never a crash and never a silent pass.
"""

from __future__ import annotations

import sys


from crossagent import check as check_mod
from crossagent.check import CheckOutcome, run_check, sanitized_check_env


def _py(code: str) -> str:
    """Return a shell-splittable command that runs *code* under this Python."""
    return f'{sys.executable} -c "{code}"'


def test_run_check_passing_command_reports_exit_zero(tmp_path):
    outcome = run_check(_py("import sys; sys.exit(0)"), cwd=str(tmp_path))
    assert isinstance(outcome, CheckOutcome)
    assert outcome.exit_code == 0
    assert outcome.command == _py("import sys; sys.exit(0)")


def test_run_check_failing_command_reports_real_exit_code(tmp_path):
    outcome = run_check(_py("import sys; sys.exit(3)"), cwd=str(tmp_path))
    assert outcome.exit_code == 3


def test_run_check_captures_stdout_and_stderr(tmp_path):
    outcome = run_check(
        _py("import sys; print('out-marker'); print('err-marker', file=sys.stderr)"),
        cwd=str(tmp_path),
    )
    assert "out-marker" in outcome.stdout_tail
    assert "err-marker" in outcome.stderr_tail


def test_run_check_missing_executable_is_not_a_crash_and_not_a_pass(tmp_path):
    outcome = run_check("crossagent-definitely-no-such-cmd-xyz", cwd=str(tmp_path))
    assert outcome.exit_code != 0
    assert outcome.exit_code == check_mod._EXIT_NOT_FOUND


def test_run_check_unparseable_command_is_handled(tmp_path):
    # An unbalanced quote makes shlex.split raise — must be captured, not raised.
    outcome = run_check('echo "unterminated', cwd=str(tmp_path))
    assert outcome.exit_code != 0
    assert "pars" in outcome.stderr_tail.lower()


def test_run_check_empty_command_is_handled(tmp_path):
    outcome = run_check("   ", cwd=str(tmp_path))
    assert outcome.exit_code != 0


def test_run_check_enormous_output_is_truncated(tmp_path):
    outcome = run_check(
        _py("print('x' * 500000)"),
        cwd=str(tmp_path),
    )
    assert outcome.exit_code == 0
    # The tail is bounded well under the raw output size.
    assert len(outcome.stdout_tail) <= check_mod.CHECK_OUTPUT_TAIL_CHARS + len(
        check_mod._TRUNCATION_MARKER
    )
    assert outcome.stdout_tail.startswith(check_mod._TRUNCATION_MARKER)


def test_run_check_timeout_is_captured_as_failure(tmp_path):
    outcome = run_check(
        _py("import time; time.sleep(30)"),
        cwd=str(tmp_path),
        timeout=0.5,
    )
    assert outcome.exit_code == check_mod._EXIT_TIMEOUT
    assert "timed out" in outcome.stderr_tail.lower()


def test_run_check_runs_in_declared_cwd(tmp_path):
    (tmp_path / "sentinel.txt").write_text("here", encoding="utf-8")
    outcome = run_check(
        _py("import os,sys; sys.exit(0 if os.path.exists('sentinel.txt') else 1)"),
        cwd=str(tmp_path),
    )
    assert outcome.exit_code == 0


def test_run_check_does_not_leak_lineage_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSAGENT_PARENT_JOB_ID", "job_secret_parent")
    monkeypatch.setenv("CROSSAGENT_TRACE_ID", "trace_secret")
    outcome = run_check(
        _py(
            "import os,sys; "
            "sys.exit(1 if 'CROSSAGENT_PARENT_JOB_ID' in os.environ "
            "or 'CROSSAGENT_TRACE_ID' in os.environ else 0)"
        ),
        cwd=str(tmp_path),
    )
    assert outcome.exit_code == 0


def test_sanitized_check_env_strips_crossagent_but_keeps_path(monkeypatch):
    monkeypatch.setenv("CROSSAGENT_NESTING_DEPTH", "4")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = sanitized_check_env()
    assert "PATH" in env
    assert all(not key.startswith("CROSSAGENT_") for key in env)


def test_check_outcome_to_dict_shape(tmp_path):
    outcome = run_check(_py("import sys; sys.exit(0)"), cwd=str(tmp_path))
    as_dict = outcome.to_dict()
    assert set(as_dict) == {"command", "exit_code", "stdout_tail", "stderr_tail"}
    assert as_dict["exit_code"] == 0
