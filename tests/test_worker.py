"""Tests for the detached worker and logging parser wrapper."""

from __future__ import annotations

from pathlib import Path

from crossagent.jobs import Job
from crossagent.parsers import EventParser
from crossagent.worker import _LoggingParser, build_advisor_env


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
