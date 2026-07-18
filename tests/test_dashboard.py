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
    _write_manual_job(state_dir, "job_dash_one", jobs_mod.JobState.SUCCEEDED,
                      name="dash-test")
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
    _write_manual_job(state_dir, "job_dash_running", jobs_mod.JobState.RUNNING,
                      worker_pid=os.getpid())
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
    _write_manual_job(state_dir, "job_dash_stale", jobs_mod.JobState.RUNNING,
                      worker_pid=99999999)
    status, body = _get(server_url + "/api/jobs")
    assert status == 200
    payload = json.loads(body)
    assert payload["jobs"][0]["status"] == "abandoned"


def test_api_job_detail(state_dir, server_url):
    _write_manual_job(state_dir, "job_dash_detail", jobs_mod.JobState.FAILED,
                      name="detail-test")
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
    _write_manual_job(state_dir, "job_dash_logs", jobs_mod.JobState.SUCCEEDED,
                      stdout_log="line one\nline two\n")
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
    _write_manual_job(state_dir, "job_dash_secret", jobs_mod.JobState.SUCCEEDED,
                      prompt=secret, stdout_log="clean output\n")

    for path in ("/", "/api/jobs", "/api/jobs/job_dash_secret",
                 "/api/jobs/job_dash_secret/logs?stream=stdout"):
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
                assert "escapeHtml(" in line or "badge(" in line, line


def test_cli_wires_dashboard_subcommand():
    from crossagent.cli import _JOB_SUBCOMMANDS
    assert "dashboard" in _JOB_SUBCOMMANDS
