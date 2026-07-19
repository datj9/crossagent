"""Tests for the local web dashboard (``crossagent dashboard``).

The dashboard is a stdlib-only localhost HTTP server over the same job state
that ``crossagent list``/``status``/``logs`` read. These tests verify:

  1. The index page and JSON API serve job data.
  2. Stale ``running`` jobs reconcile to ``abandoned`` in API responses
     (no silent drops in the dashboard either).
  3. Prompt text is never exposed through any route.
  4. Job-ID path traversal is rejected.

No real advisor CLI is ever invoked; job state is written directly.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crossagent import dashboard as dashboard_mod
from crossagent import jobs as jobs_mod


@pytest.fixture
def state_dir(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "jobs"
    monkeypatch.setenv("CROSSAGENT_STATE_DIR", str(root))
    return root


def _write_manual_job(
    state_root: Path,
    job_id: str,
    status: jobs_mod.JobState,
    *,
    advisor: str = "codex",
    name: str = "",
    worker_pid: "int | None" = None,
    prompt: str = "hello",
    stdout_log: str = "",
) -> None:
    job_dir = state_root / job_id
    job_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    job = jobs_mod.Job(
        schema_version=1,
        job_id=job_id,
        status=status,
        advisor=advisor,
        name=name,
        cwd=os.getcwd(),
        redacted_command=f"{advisor} exec <prompt>",
        worker_pid=worker_pid,
        started_at=now,
        updated_at=now,
        last_activity_at=now,
        last_event="worker.started",
    )
    jobs_mod.save_state(job_dir, job)
    (job_dir / "prompt").write_text(prompt, encoding="utf-8")
    if stdout_log:
        (job_dir / "stdout.log").write_text(stdout_log, encoding="utf-8")


@pytest.fixture
def server_url(state_dir: Path) -> "Iterator[str]":
    server = dashboard_mod.create_server("127.0.0.1", 0, state_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(url: str) -> "tuple[int, bytes]":
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_index_serves_html(server_url):
    status, body = _get(server_url + "/")
    assert status == 200
    text = body.decode("utf-8")
    assert "<html" in text.lower()
    assert "crossagent" in text.lower()


def test_api_jobs_lists_jobs(state_dir, server_url):
    _write_manual_job(
        state_dir, "job_dash_one", jobs_mod.JobState.SUCCEEDED, name="dash-test"
    )
    status, body = _get(server_url + "/api/jobs")
    assert status == 200
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert [entry["job_id"] for entry in payload["jobs"]] == ["job_dash_one"]
    entry = payload["jobs"][0]
    assert entry["status"] == "succeeded"
    assert entry["name"] == "dash-test"
    assert "elapsed_seconds" in entry
    assert "idle_seconds" in entry


def test_api_jobs_lists_live_running_job(state_dir, server_url):
    _write_manual_job(
        state_dir, "job_dash_running", jobs_mod.JobState.RUNNING, worker_pid=os.getpid()
    )
    status, body = _get(server_url + "/api/jobs")
    assert status == 200
    payload = json.loads(body)
    assert payload["jobs"][0]["job_id"] == "job_dash_running"
    assert payload["jobs"][0]["status"] == "running"


def test_api_jobs_does_not_abandon_job_during_startup(state_dir, server_url):
    _write_manual_job(state_dir, "job_dash_starting", jobs_mod.JobState.PENDING)
    status, body = _get(server_url + "/api/jobs")
    assert status == 200
    payload = json.loads(body)
    assert payload["jobs"][0]["job_id"] == "job_dash_starting"
    assert payload["jobs"][0]["status"] == "pending"


def test_api_jobs_reconciles_stale_to_abandoned(state_dir, server_url):
    _write_manual_job(
        state_dir, "job_dash_stale", jobs_mod.JobState.RUNNING, worker_pid=99999999
    )
    status, body = _get(server_url + "/api/jobs")
    assert status == 200
    payload = json.loads(body)
    assert payload["jobs"][0]["status"] == "abandoned"


def test_api_job_detail(state_dir, server_url):
    _write_manual_job(
        state_dir, "job_dash_detail", jobs_mod.JobState.FAILED, name="detail-test"
    )
    status, body = _get(server_url + "/api/jobs/job_dash_detail")
    assert status == 200
    payload = json.loads(body)
    assert payload["job_id"] == "job_dash_detail"
    assert payload["status"] == "failed"
    assert payload["name"] == "detail-test"


def test_api_job_detail_unknown_is_404(server_url):
    status, _ = _get(server_url + "/api/jobs/job_does_not_exist")
    assert status == 404


def test_api_logs_serves_stdout(state_dir, server_url):
    _write_manual_job(
        state_dir,
        "job_dash_logs",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log="line one\nline two\n",
    )
    status, body = _get(server_url + "/api/jobs/job_dash_logs/logs?stream=stdout")
    assert status == 200
    assert b"line one" in body
    assert b"line two" in body


def test_api_logs_rejects_bad_stream(state_dir, server_url):
    _write_manual_job(state_dir, "job_dash_badstream", jobs_mod.JobState.SUCCEEDED)
    status, _ = _get(server_url + "/api/jobs/job_dash_badstream/logs?stream=prompt")
    assert status == 400


def test_traversal_job_id_is_rejected(state_dir, server_url):
    _write_manual_job(state_dir, "job_dash_safe", jobs_mod.JobState.SUCCEEDED)
    for bad in ("..%2F..%2Fetc", "job_x%2F..%2Fjob_dash_safe", "not_a_job"):
        status, _ = _get(server_url + f"/api/jobs/{bad}")
        assert status == 404, bad


def test_prompt_never_exposed(state_dir, server_url):
    secret = "DASHBOARD_SECRET_PROMPT_7"
    _write_manual_job(
        state_dir,
        "job_dash_secret",
        jobs_mod.JobState.SUCCEEDED,
        prompt=secret,
        stdout_log="clean output\n",
    )

    for path in (
        "/",
        "/api/jobs",
        "/api/jobs/job_dash_secret",
        "/api/jobs/job_dash_secret/logs?stream=stdout",
    ):
        _, body = _get(server_url + path)
        assert secret.encode() not in body, path

    # The prompt file itself must not be routable.
    status, _ = _get(server_url + "/api/jobs/job_dash_secret/prompt")
    assert status == 404


def test_unknown_route_is_404(server_url):
    status, _ = _get(server_url + "/nope")
    assert status == 404


def test_security_headers_present(server_url):
    with urllib.request.urlopen(server_url + "/") as response:
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_page_escapes_job_fields_before_dom_insertion(server_url):
    """The page must HTML-escape disk-sourced fields (name, advisor,
    last_event, error) before innerHTML interpolation — XSS guard."""
    _, body = _get(server_url + "/")
    page = body.decode("utf-8")
    assert "function escapeHtml" in page
    for field in ("job.advisor", "job.name", "job.last_event", "job.error"):
        for line in page.splitlines():
            if field in line and "innerHTML" not in line and "fetch(" not in line:
                assert (
                    "escapeHtml(" in line or "badge(" in line or "setCellText(" in line
                ), line


def test_cli_wires_dashboard_subcommand():
    from crossagent.cli import _JOB_SUBCOMMANDS

    assert "dashboard" in _JOB_SUBCOMMANDS


# ---------------------------------------------------------------------------
# Events endpoint
# ---------------------------------------------------------------------------


def test_api_events_offset_zero(state_dir, server_url):
    _write_manual_job(
        state_dir,
        "job_ev_offset",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log="line one\nline two\n",
    )
    status, body = _get(server_url + "/api/jobs/job_ev_offset/events?offset=0")
    assert status == 200
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["job_id"] == "job_ev_offset"
    assert payload["requested_offset"] == 0
    assert payload["file_size"] == len("line one\nline two\n")
    expected_bytes = len("line one\nline two\n")
    assert payload["next_offset"] == expected_bytes
    assert payload["at_eof"] is True
    assert payload["has_more"] is False
    assert payload["reset"] is False
    assert len(payload["events"]) == 2
    assert payload["events"][0]["body"] == "line one"
    assert payload["events"][1]["body"] == "line two"


def test_api_events_partial_final_line(state_dir, server_url):
    # A genuinely RUNNING job (live worker) must NOT consume a partial final
    # line — it waits for the line to be completed. A live worker_pid keeps
    # reconcile_stale from abandoning it.
    _write_manual_job(
        state_dir,
        "job_ev_partial",
        jobs_mod.JobState.RUNNING,
        worker_pid=os.getpid(),
        stdout_log="complete\nincomplete",
    )
    status, body = _get(server_url + "/api/jobs/job_ev_partial/events?offset=0")
    assert status == 200
    payload = json.loads(body)
    assert len(payload["events"]) == 1
    assert payload["events"][0]["body"] == "complete"
    assert payload["next_offset"] == len("complete\n")
    assert payload["at_eof"] is False


def test_api_events_terminal_emits_final_unterminated_line(state_dir, server_url):
    # A terminal job's final line will never gain a trailing newline, so it must
    # be emitted rather than withheld forever.
    _write_manual_job(
        state_dir,
        "job_ev_terminal_tail",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log="first\nlast-no-newline",
    )
    status, body = _get(server_url + "/api/jobs/job_ev_terminal_tail/events?offset=0")
    assert status == 200
    payload = json.loads(body)
    bodies = [ev["body"] for ev in payload["events"]]
    assert "first" in bodies
    assert "last-no-newline" in bodies
    assert payload["next_offset"] == len("first\nlast-no-newline")
    assert payload["at_eof"] is True


def test_api_events_offset_past_eof_resets(state_dir, server_url):
    _write_manual_job(
        state_dir,
        "job_ev_reset",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log="hello\n",
    )
    status, body = _get(server_url + "/api/jobs/job_ev_reset/events?offset=99999")
    assert status == 200
    payload = json.loads(body)
    assert payload["reset"] is True
    assert payload["requested_offset"] == 99999
    assert len(payload["events"]) == 1
    assert payload["events"][0]["body"] == "hello"


def test_api_events_claude_advisor(state_dir, server_url):
    init_line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-3-opus",
            "session_id": "sess_test",
            "cwd": "/tmp",
        }
    )
    assistant_line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello!"}]},
        }
    )
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "message_stop",
            "total_cost_usd": 0.01,
        }
    )
    stdout = init_line + "\n" + assistant_line + "\n" + result_line + "\n"
    _write_manual_job(
        state_dir,
        "job_ev_claude",
        jobs_mod.JobState.SUCCEEDED,
        advisor="claude",
        stdout_log=stdout,
    )
    status, body = _get(server_url + "/api/jobs/job_ev_claude/events")
    assert status == 200
    payload = json.loads(body)
    assert payload["event_format"] == "claude-stream"
    kinds = [e["kind"] for e in payload["events"]]
    assert kinds == ["init", "assistant", "result"]


def test_api_events_codex_advisor(state_dir, server_url):
    # Regression: a codex advisor must yield codex-jsonl normalized events, not
    # raw "text" output. This closes the gap that hid the endpoint wiring bug.
    thread_line = json.dumps({"type": "thread.started", "thread_id": "th_1"})
    message_line = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Done."},
        }
    )
    turn_done = json.dumps({"type": "turn.completed", "usage": {}})
    stdout = thread_line + "\n" + message_line + "\n" + turn_done + "\n"
    _write_manual_job(
        state_dir,
        "job_ev_codex",
        jobs_mod.JobState.SUCCEEDED,
        advisor="codex",
        stdout_log=stdout,
    )
    status, body = _get(server_url + "/api/jobs/job_ev_codex/events")
    assert status == 200
    payload = json.loads(body)
    assert payload["event_format"] == "codex-jsonl"
    kinds = [e["kind"] for e in payload["events"]]
    assert kinds == ["init", "assistant", "result"]


def test_api_events_unknown_job(state_dir, server_url):
    status, body = _get(server_url + "/api/jobs/job_nope/events")
    assert status == 404


def test_api_events_no_stdout_log(state_dir, server_url):
    _write_manual_job(
        state_dir,
        "job_ev_nolog",
        jobs_mod.JobState.PENDING,
    )
    status, body = _get(server_url + "/api/jobs/job_ev_nolog/events")
    assert status == 200
    payload = json.loads(body)
    assert payload["file_size"] == 0
    assert payload["events"] == []
    assert payload["at_eof"] is True


def test_api_events_default_offset_is_zero(state_dir, server_url):
    _write_manual_job(
        state_dir,
        "job_ev_default",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log="data\n",
    )
    status, body = _get(server_url + "/api/jobs/job_ev_default/events")
    assert status == 200
    payload = json.loads(body)
    assert len(payload["events"]) == 1


def test_api_events_negative_offset_treated_as_zero(state_dir, server_url):
    _write_manual_job(
        state_dir,
        "job_ev_neg",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log="data\n",
    )
    status, body = _get(server_url + "/api/jobs/job_ev_neg/events?offset=-5")
    assert status == 200
    payload = json.loads(body)
    assert payload["requested_offset"] == 0


def test_api_events_utf8_byte_offset(state_dir, server_url):
    multi_byte = "héllo\nworld\n"
    raw_bytes = multi_byte.encode("utf-8")
    _write_manual_job(
        state_dir,
        "job_ev_utf8",
        jobs_mod.JobState.SUCCEEDED,
        stdout_log=multi_byte,
    )
    status, body = _get(server_url + "/api/jobs/job_ev_utf8/events?offset=0")
    assert status == 200
    payload = json.loads(body)
    assert len(payload["events"]) == 2
    assert payload["next_offset"] == len(raw_bytes)
    assert payload["events"][0]["body"] == "héllo"
    assert payload["events"][1]["body"] == "world"

    # Re-fetch with offset AFTER the first line (byte offset of 'h' in 'héllo'
    # is 0; 'é' is 2 bytes in UTF-8, so "héllo\n" = 7 bytes).
    first_line_bytes = len("héllo\n".encode("utf-8"))
    status2, body2 = _get(
        server_url + "/api/jobs/job_ev_utf8/events?offset=" + str(first_line_bytes)
    )
    assert status2 == 200
    payload2 = json.loads(body2)
    assert len(payload2["events"]) == 1
    assert payload2["events"][0]["body"] == "world"
