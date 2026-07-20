"""Local web dashboard for durable jobs.

Serves a single-page dashboard plus a small JSON API over the same on-disk
job state that ``crossagent list``/``status``/``logs`` read. Standard library
only — no runtime dependencies, no external assets.

Security posture:
- Binds to loopback by default; a non-loopback host prints a warning.
- Job IDs are validated against a strict pattern before any path is built,
  so path traversal is impossible.
- Only ``stdout.log``/``stderr.log`` are servable per job. The ``prompt``
  file and ``command.json`` are never routable.
"""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import jobs as jobs_mod


_JOB_ID_PATTERN = re.compile(r"^job_[A-Za-z0-9_\-]+$")
_ALLOWED_LOG_STREAMS = frozenset({"stdout", "stderr"})
DEFAULT_PORT = 8642


class DashboardServer(ThreadingHTTPServer):
    """HTTP server carrying the resolved job state root."""

    daemon_threads = True

    def __init__(self, address: "tuple[str, int]", state_root: Path) -> None:
        super().__init__(address, DashboardHandler)
        self.state_root = state_root


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, _format: str, *_args: Any) -> None:
        """Silence per-request access logging; errors still surface."""

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", _PAGE_HTML.encode("utf-8"))
            return
        if path == "/api/jobs":
            self._send_json(200, self._jobs_payload())
            return
        match = re.match(r"^/api/jobs/([^/]+)$", path)
        if match:
            self._handle_job_detail(match.group(1))
            return
        match = re.match(r"^/api/jobs/([^/]+)/logs$", path)
        if match:
            self._handle_job_logs(match.group(1), parse_qs(parsed.query))
            return
        self._send_json(404, {"error": "not found"})

    # -- routes -----------------------------------------------------------

    def _jobs_payload(self) -> dict[str, Any]:
        listed = jobs_mod.collect_jobs(self.server.state_root)
        listed.sort(key=lambda job: (job.started_at, job.job_id), reverse=True)
        return {
            "schema_version": 1,
            "jobs": [jobs_mod.list_entry(job) for job in listed],
        }

    def _handle_job_detail(self, job_id: str) -> None:
        job = self._load_job(job_id)
        if job is None:
            self._send_json(404, {"error": "unknown job"})
            return
        detail = jobs_mod.list_entry(job)
        detail["error"] = job.error
        detail["finished_at"] = job.finished_at
        detail["duration_seconds"] = job.duration_seconds
        detail["advisor_exit_code"] = job.advisor_exit_code
        self._send_json(200, detail)

    def _handle_job_logs(self, job_id: str, query: "dict[str, list[str]]") -> None:
        stream = (query.get("stream") or ["stdout"])[0]
        if stream not in _ALLOWED_LOG_STREAMS:
            self._send_json(400, {"error": "stream must be stdout or stderr"})
            return
        job = self._load_job(job_id)
        if job is None:
            self._send_json(404, {"error": "unknown job"})
            return
        log_path = jobs_mod.job_dir_path(self.server.state_root, job_id) / f"{stream}.log"
        content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        self._send(200, "text/plain; charset=utf-8", content.encode("utf-8"))

    # -- helpers ----------------------------------------------------------

    def _load_job(self, job_id: str) -> "jobs_mod.Job | None":
        if not _JOB_ID_PATTERN.match(job_id):
            return None
        job_dir = jobs_mod.job_dir_path(self.server.state_root, job_id)
        try:
            job = jobs_mod.load_state(job_dir)
        except (FileNotFoundError, jobs_mod.JobError):
            return None
        # Concurrent requests may reconcile the same stale job; that is
        # idempotent and each save_state is an atomic rename, so no lock.
        return jobs_mod.reconcile_stale(job, job_dir)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page uses one inline <script>/<style> block and fetches only
        # same-origin JSON; everything else is denied.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; "
                         "img-src 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str, port: int, state_root: Path) -> DashboardServer:
    """Create (but do not start) a dashboard server bound to *host*:*port*."""
    return DashboardServer((host, port), state_root)


def serve(host: str, port: int, state_root: Path, *, open_browser: bool = True) -> int:
    """Run the dashboard until interrupted. Returns a process exit code."""
    try:
        server = create_server(host, port, state_root)
    except OSError as exc:
        print(f"[crossagent] cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 1

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[crossagent] WARNING: binding to non-loopback host '{host}' exposes "
              f"job metadata and logs to the network.", file=sys.stderr)

    url = f"http://{host}:{server.server_address[1]}/"
    print(f"[crossagent] dashboard: {url}  (Ctrl-C to stop)", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[crossagent] dashboard stopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0


_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crossagent dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #0d1117; color: #e6edf3; }
  header { padding: 14px 20px; border-bottom: 1px solid #21262d;
           display: flex; align-items: baseline; gap: 12px; }
  header h1 { font-size: 16px; margin: 0; }
  header span { color: #7d8590; font-size: 12px; }
  main { display: grid; grid-template-columns: minmax(420px, 1fr) 1.2fr;
         gap: 0; height: calc(100vh - 51px); }
  #jobs-pane { overflow-y: auto; border-right: 1px solid #21262d; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid #161b22;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  th { position: sticky; top: 0; background: #0d1117; color: #7d8590;
       font-weight: 600; font-size: 11px; text-transform: uppercase; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: #161b22; }
  tbody tr.selected { background: #1c2431; }
  .badge { padding: 1px 8px; border-radius: 10px; font-size: 12px; }
  .running   { background: #1f3a5f; color: #79c0ff; }
  .succeeded { background: #1b3a2a; color: #56d364; }
  .failed, .timed_out { background: #4a1e24; color: #ff7b72; }
  .cancelled, .abandoned, .pending { background: #30363d; color: #9da7b3; }
  #detail-pane { overflow-y: auto; padding: 16px 20px; }
  #detail-pane h2 { font-size: 14px; margin: 0 0 10px; word-break: break-all; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px;
       margin: 0 0 14px; font-size: 13px; }
  dt { color: #7d8590; }
  dd { margin: 0; word-break: break-all; }
  .tabs { display: flex; gap: 8px; margin-bottom: 8px; }
  .tabs button { background: #21262d; color: #e6edf3; border: 1px solid #30363d;
                 border-radius: 6px; padding: 3px 12px; cursor: pointer; font: inherit; }
  .tabs button.active { background: #1c2431; border-color: #58a6ff; }
  pre { background: #010409; border: 1px solid #21262d; border-radius: 6px;
        padding: 12px; overflow: auto; max-height: 55vh; white-space: pre-wrap;
        word-break: break-word; font-size: 12px; }
  .empty { color: #7d8590; padding: 24px; }
</style>
</head>
<body>
<header>
  <h1>crossagent dashboard</h1>
  <span id="refreshed"></span>
</header>
<main>
  <div id="jobs-pane">
    <table>
      <thead>
        <tr><th>Job</th><th>Status</th><th>Advisor</th><th>Elapsed</th><th>Idle</th><th>Name</th></tr>
      </thead>
      <tbody id="jobs-body"></tbody>
    </table>
    <div id="jobs-empty" class="empty" hidden>No jobs yet. Start one with <code>crossagent start …</code></div>
  </div>
  <div id="detail-pane">
    <div class="empty">Select a job to see its detail and live logs.</div>
  </div>
</main>
<script>
"use strict";
let selectedJobId = null;
let logStream = "stdout";
const terminal = new Set(["succeeded", "failed", "timed_out", "cancelled", "abandoned"]);
let hasRunningJobs = false;

const knownStatuses = new Set(["pending", "running", "succeeded", "failed",
                               "timed_out", "cancelled", "abandoned"]);

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function badge(status) {
  const cls = knownStatuses.has(status) ? status : "pending";
  return '<span class="badge ' + cls + '">' + escapeHtml(status) + "</span>";
}

function fmtSeconds(total) {
  if (total == null) return "-";
  total = Math.round(total);
  if (total < 60) return total + "s";
  const m = Math.floor(total / 60), s = total % 60;
  if (m < 60) return m + "m" + String(s).padStart(2, "0") + "s";
  return Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m";
}

async function refreshJobs() {
  const response = await fetch("/api/jobs");
  const payload = await response.json();
  const body = document.getElementById("jobs-body");
  body.innerHTML = "";
  document.getElementById("jobs-empty").hidden = payload.jobs.length > 0;
  for (const job of payload.jobs) {
    const row = document.createElement("tr");
    if (job.job_id === selectedJobId) row.classList.add("selected");
    const idle = terminal.has(job.status) ? "-" : fmtSeconds(job.idle_seconds);
    row.innerHTML =
      "<td>" + escapeHtml(job.job_id) + "</td>" +
      "<td>" + badge(job.status) + "</td>" +
      "<td>" + escapeHtml(job.advisor) + "</td>" +
      "<td>" + fmtSeconds(job.elapsed_seconds) + "</td>" +
      "<td>" + idle + "</td>" +
      "<td>" + escapeHtml(job.name || "") + "</td>";
    row.onclick = () => { selectedJobId = job.job_id; refreshDetail(); refreshJobs(); };
    body.appendChild(row);
  }
  hasRunningJobs = false;
  for (const job of payload.jobs) {
    if (job.status === "running" || job.status === "pending") {
      hasRunningJobs = true;
      break;
    }
  }
  document.getElementById("refreshed").textContent =
    "refreshed " + new Date().toLocaleTimeString();
}

async function refreshDetail() {
  if (!selectedJobId) return;
  const pane = document.getElementById("detail-pane");
  const [detailResponse, logsResponse] = await Promise.all([
    fetch("/api/jobs/" + selectedJobId),
    fetch("/api/jobs/" + selectedJobId + "/logs?stream=" + logStream),
  ]);
  if (!detailResponse.ok) { pane.innerHTML = '<div class="empty">Job not found.</div>'; return; }
  const job = await detailResponse.json();
  const logs = await logsResponse.text();
  pane.innerHTML =
    "<h2>" + escapeHtml(job.job_id) + "</h2>" +
    "<dl>" +
    "<dt>status</dt><dd>" + badge(job.status) + "</dd>" +
    "<dt>advisor</dt><dd>" + escapeHtml(job.advisor) + "</dd>" +
    "<dt>name</dt><dd>" + escapeHtml(job.name || "-") + "</dd>" +
    "<dt>elapsed</dt><dd>" + fmtSeconds(job.elapsed_seconds) + "</dd>" +
    "<dt>idle</dt><dd>" + fmtSeconds(job.idle_seconds) + "</dd>" +
    "<dt>last event</dt><dd>" + escapeHtml(job.last_event || "-") + "</dd>" +
    "<dt>error</dt><dd>" + escapeHtml(job.error || "-") + "</dd>" +
    "</dl>" +
    '<div class="tabs">' +
    '<button id="tab-stdout">stdout</button>' +
    '<button id="tab-stderr">stderr</button>' +
    "</div>" +
    "<pre id=\\"logs\\"></pre>";
  document.getElementById("logs").textContent = logs || "(no output yet)";
  for (const stream of ["stdout", "stderr"]) {
    const tab = document.getElementById("tab-" + stream);
    tab.classList.toggle("active", logStream === stream);
    tab.onclick = () => { logStream = stream; refreshDetail(); };
  }
}

function pollJobs() {
  refreshJobs().then(function () {
    setTimeout(pollJobs, hasRunningJobs ? 3000 : 15000);
  });
}
pollJobs();
setInterval(refreshDetail, 3000);
</script>
</body>
</html>
"""
