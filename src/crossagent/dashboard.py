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

from . import advisors as advisors_mod
from . import graph as graph_mod
from . import jobs as jobs_mod
from .feed import normalize_stream_line, resolve_event_format

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
        if path == "/api/graph":
            self._send_json(200, self._graph_payload())
            return
        match = re.match(r"^/api/jobs/([^/]+)$", path)
        if match:
            self._handle_job_detail(match.group(1))
            return
        match = re.match(r"^/api/jobs/([^/]+)/logs$", path)
        if match:
            self._handle_job_logs(match.group(1), parse_qs(parsed.query))
            return
        match = re.match(r"^/api/jobs/([^/]+)/events$", path)
        if match:
            self._handle_job_events(match.group(1), parse_qs(parsed.query))
            return
        match = re.match(r"^/api/jobs/([^/]+)/audit$", path)
        if match:
            self._handle_job_audit(match.group(1))
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

    def _graph_payload(self) -> dict[str, Any]:
        jobs = jobs_mod.collect_jobs(self.server.state_root)
        return graph_mod.build_graph(jobs)

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
        log_path = (
            jobs_mod.job_dir_path(self.server.state_root, job_id) / f"{stream}.log"
        )
        content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        self._send(200, "text/plain; charset=utf-8", content.encode("utf-8"))

    _STATUS_TERMINAL = frozenset(
        {"succeeded", "failed", "timed_out", "cancelled", "abandoned"}
    )

    def _handle_job_events(self, job_id: str, query: "dict[str, list[str]]") -> None:
        job = self._load_job(job_id)
        if job is None:
            self._send_json(404, {"error": "unknown job"})
            return

        event_format = self._event_format_for_job(job)
        raw_offset = (query.get("offset") or ["0"])[0]
        try:
            requested_offset = int(raw_offset)
        except (ValueError, TypeError):
            requested_offset = 0
        if requested_offset < 0:
            requested_offset = 0

        log_path = jobs_mod.job_dir_path(self.server.state_root, job_id) / "stdout.log"

        if not log_path.exists():
            self._send_json(
                200,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "event_format": event_format,
                    "requested_offset": requested_offset,
                    "next_offset": 0,
                    "file_size": 0,
                    "has_more": False,
                    "at_eof": True,
                    "reset": False,
                    "terminal": job.status.value in self._STATUS_TERMINAL,
                    "events": [],
                },
            )
            return

        file_size = log_path.stat().st_size

        offset = requested_offset
        if offset > file_size:
            offset = 0
            reset = True
        else:
            reset = False

        read_cap = 262144
        with log_path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(read_cap)

        terminal = job.status.value in self._STATUS_TERMINAL

        if len(chunk) == 0:
            self._send_json(
                200,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "event_format": event_format,
                    "requested_offset": requested_offset,
                    "next_offset": offset,
                    "file_size": file_size,
                    "has_more": False,
                    "at_eof": True,
                    "reset": reset,
                    "terminal": terminal,
                    "events": [],
                },
            )
            return

        read_to_eof = (offset + len(chunk)) >= file_size

        if chunk.endswith(b"\n"):
            complete_len = len(chunk)
        elif read_to_eof and terminal:
            # No more bytes will ever arrive; the trailing unterminated line is
            # the job's final output, so consume it rather than stalling forever.
            complete_len = len(chunk)
        else:
            last_newline = chunk.rfind(b"\n")
            if last_newline != -1:
                complete_len = last_newline + 1
            elif len(chunk) >= read_cap:
                # A single record larger than the read cap and lacking a newline
                # would otherwise be re-read forever. Consume it (possibly
                # truncated) so tailing advances; normalize yields a raw event.
                complete_len = len(chunk)
            else:
                # Incomplete final line still being written — wait for more bytes.
                self._send_json(
                    200,
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "event_format": event_format,
                        "requested_offset": requested_offset,
                        "next_offset": offset,
                        "file_size": file_size,
                        "has_more": offset < file_size,
                        "at_eof": False,
                        "reset": reset,
                        "terminal": terminal,
                        "events": [],
                    },
                )
                return

        complete_bytes = chunk[:complete_len]
        next_offset = offset + complete_len

        # Strip the trailing newline before splitting to avoid an empty
        # final element when the chunk is newline-terminated.
        raw_text = complete_bytes.decode("utf-8", errors="replace")
        lines = raw_text.splitlines()

        events: list[dict[str, object]] = []
        for line in lines:
            events.extend(normalize_stream_line(event_format, line + "\n"))

        at_eof = next_offset >= file_size

        self._send_json(
            200,
            {
                "schema_version": 1,
                "job_id": job_id,
                "event_format": event_format,
                "requested_offset": requested_offset,
                "next_offset": next_offset,
                "file_size": file_size,
                "has_more": not at_eof,
                "at_eof": at_eof,
                "reset": reset,
                "terminal": terminal,
                "events": events,
            },
        )

    def _handle_job_audit(self, job_id: str) -> None:
        job = self._load_job(job_id)
        if job is None:
            self._send_json(404, {"error": "unknown job"})
            return
        log_path = (
            jobs_mod.job_dir_path(self.server.state_root, job_id) / "events.jsonl"
        )
        audit_events: list[dict[str, Any]] = []
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    audit_events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self._send_json(
            200,
            {
                "schema_version": 1,
                "job_id": job_id,
                "events": audit_events,
            },
        )

    def _event_format_for_job(self, job: jobs_mod.Job) -> str:
        try:
            advisor = advisors_mod.resolve(job.advisor)
        except KeyError:
            return "text"
        return resolve_event_format(advisor.result_parser)

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
        self._send(
            code,
            "application/json; charset=utf-8",
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page uses one inline <script>/<style> block and fetches only
        # same-origin JSON; everything else is denied.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self'; base-uri 'none'; form-action 'none'",
        )
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
        print(
            f"[crossagent] WARNING: binding to non-loopback host '{host}' exposes "
            f"job metadata and logs to the network.",
            file=sys.stderr,
        )

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
  main { display: grid; grid-template-columns: minmax(420px, 1fr) 6px 1.2fr;
         gap: 0; height: calc(100vh - 51px); }
  #jobs-pane { overflow-y: auto; }
  #pane-splitter { cursor: col-resize; background: #21262d; }
  #pane-splitter:hover, #pane-splitter.dragging { background: #58a6ff; }
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
  #audit-feed { display: none; background: #010409; border: 1px solid #21262d;
                border-radius: 6px; padding: 12px; overflow-y: auto;
                max-height: 55vh; font-size: 12px; }
  .audit-row { padding: 4px 0; border-bottom: 1px solid #161b22; }
  .audit-row:last-child { border-bottom: none; }
  .audit-ts { color: #7d8590; margin-right: 8px; }
  .audit-actor { display: inline-block; min-width: 130px; font-size: 11px;
                 padding: 1px 6px; border-radius: 3px; margin-right: 8px; }
  .audit-actor-user { background: #1f3a5f; color: #79c0ff; }
  .audit-actor-system { background: #30363d; color: #9da7b3; }
  .audit-transition { color: #e6edf3; }
  .audit-error { color: #ff7b72; margin-left: 8px; }
  .empty { color: #7d8590; padding: 24px; }
  #events-feed { display: none; background: #010409; border: 1px solid #21262d; border-radius: 6px; padding: 12px; overflow-y: auto; max-height: 55vh; font-size: 12px; }
  .ev-init { color: #7d8590; padding: 2px 0; }
  .ev-assistant { color: #e6edf3; border-left: 2px solid #58a6ff; padding: 4px 0 4px 10px; margin: 4px 0; white-space: pre-wrap; }
  .ev-result { color: #56d364; border-left: 2px solid #56d364; padding: 2px 0 2px 10px; margin: 4px 0; font-size: 12px; }
  .ev-output { color: #9da7b3; padding: 2px 0; font-size: 12px; }
  .ev-raw { color: #484f58; padding: 2px 0; font-size: 11px; }
  .ev-raw-label { display: inline-block; color: #7d8590; font-size: 10px; background: #21262d; border-radius: 3px; padding: 0 4px; margin-right: 6px; vertical-align: middle; }
  #events-chip { display: none; position: sticky; bottom: 4px; z-index: 10; text-align: center; padding: 4px 0; cursor: pointer; }
  #events-chip span { display: inline-block; color: #58a6ff; background: #1c2431; border: 1px solid #58a6ff; border-radius: 12px; padding: 4px 14px; font-size: 11px; }
  .ev-thinking { color: #7d8590; padding: 2px 0; }
  .ev-thinking summary { cursor: pointer; color: #7d8590; font-size: 11px; }
  .ev-thinking .ev-body { white-space: pre-wrap; padding: 4px 0 2px 14px; color: #9da7b3; }
  .ev-rate { color: #d29922; border-left: 2px solid #d29922; padding: 2px 0 2px 10px; margin: 4px 0; }
  .ev-error { color: #ff7b72; border-left: 2px solid #ff7b72; padding: 2px 0 2px 10px; margin: 4px 0; }
  .ev-tool { color: #e6edf3; border-left: 2px solid #7d8590; padding: 4px 0 4px 10px; margin: 4px 0; }
  .ev-tool.ev-tool-done { border-left-color: #56d364; }
  .ev-tool.ev-tool-failed { border-left-color: #ff7b72; }
  .ev-tool summary { cursor: pointer; color: #58a6ff; font-size: 12px; }
  .ev-tool .ev-section-label { color: #7d8590; font-size: 11px; margin-top: 4px; }
  .ev-tool .ev-body { white-space: pre-wrap; font-size: 11px; color: #9da7b3; margin: 2px 0 2px 14px; }
  .ev-tool .ev-done-mark { color: #56d364; font-size: 11px; margin-left: 6px; }
  .ev-tool .ev-fail-mark { color: #ff7b72; font-size: 11px; margin-left: 6px; }
  .view-toggle { margin-left: auto; display: flex; gap: 0; }
  .view-toggle button { background: #21262d; color: #e6edf3; border: 1px solid #30363d; padding: 4px 14px; cursor: pointer; font: inherit; font-size: 12px; }
  .view-toggle button:first-child { border-radius: 6px 0 0 6px; }
  .view-toggle button:last-child { border-radius: 0 6px 6px 0; }
  .view-toggle button.active { background: #1c2431; border-color: #58a6ff; }
  #graph-container { display: none; overflow: hidden; width: 100%; height: 100%; background: #010409; position: relative; }
  #list-view { height: 100%; }
</style>
</head>
<body>
<header>
  <h1>crossagent dashboard</h1>
  <span id="refreshed"></span>
  <div class="view-toggle">
    <button id="view-list" class="active">List</button>
    <button id="view-graph">Graph</button>
  </div>
</header>
<main>
  <div id="jobs-pane">
    <div id="list-view">
      <table>
        <thead>
          <tr><th>Job</th><th>Status</th><th>Advisor</th><th>Elapsed</th><th>Idle</th><th>Name</th></tr>
        </thead>
        <tbody id="jobs-body"></tbody>
      </table>
      <div id="jobs-empty" class="empty" hidden>No jobs yet. Start one with <code>crossagent start …</code></div>
    </div>
    <div id="graph-container">
      <canvas id="graph-canvas"></canvas>
      <button id="fit-btn" style="position:absolute;top:8px;right:8px;z-index:5;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 12px;cursor:pointer;font:12px ui-monospace,monospace;">Fit</button>
      <div id="no-nest-hint" style="display:none;position:absolute;top:8px;left:50%;transform:translateX(-50%);color:#7d8590;font-size:12px;pointer-events:none;text-align:center;white-space:nowrap;">No nested orchestration yet — jobs shown standalone.</div>
    </div>
  </div>
  <div id="pane-splitter" title="Drag to resize"></div>
  <div id="detail-pane">
    <div class="empty">Select a job to see its detail and live logs.</div>
  </div>
</main>
<script>
"use strict";
let selectedJobId = null;
let logStream = "stdout";
let detailTab = "events";
const terminal = new Set(["succeeded", "failed", "timed_out", "cancelled", "abandoned"]);
let hasRunningJobs = false;

const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let rafId = null;
let hasRunningNodes = false;

const knownStatuses = new Set(["pending", "running", "succeeded", "failed",
                               "timed_out", "cancelled", "abandoned"]);

const jobRows = new Map();
let lastDetailJobId = null;
// Monotonic generation token — every refreshDetail call bumps it, so an older
// in-flight call (rapid A→B→A reselection, tab switch, or an overlapping poll)
// detects it was superseded after an await and bails without applying stale data.
let detailSeq = 0;

// Events feed state (per selected job)
let eventsOffset = 0;
let eventsFollow = true;
let pendingNew = 0;
let eventsStopped = false;
let toolRowMap = new Map();

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

function setCellText(cell, value) {
  const text = String(value == null ? "" : value);
  if (cell.textContent !== text) cell.textContent = text;
}

function setCellHTML(cell, html) {
  if (cell.innerHTML !== html) cell.innerHTML = html;
}

function buildEventRow(event) {
  var row = document.createElement("div");
  row.className = "ev-row ev-" + event.kind;
  switch (event.kind) {
    case "init":
      row.textContent = "session \u00b7 " + (event.body || "");
      break;
    case "assistant":
      row.textContent = event.body || "";
      break;
    case "result":
      row.textContent = (event.title || "") + (event.title && event.body ? " " : "") + (event.body || "");
      break;
    case "output":
      row.textContent = event.body || "";
      break;
    case "raw":
      var label = document.createElement("span");
      label.className = "ev-raw-label";
      label.textContent = event.raw_type || "raw";
      row.appendChild(label);
      row.appendChild(document.createTextNode(event.body || ""));
      break;
    case "thinking":
      var details = document.createElement("details");
      var summary = document.createElement("summary");
      summary.textContent = "thinking";
      details.appendChild(summary);
      var body = document.createElement("div");
      body.className = "ev-body";
      body.textContent = event.body || "";
      details.appendChild(body);
      row.appendChild(details);
      break;
    case "rate_limit":
      row.textContent = (event.title || "") + ((event.title && event.body) ? " " : "") + (event.body || "");
      break;
    case "error":
      row.textContent = (event.title || "") + ((event.title && event.body) ? " " : "") + (event.body || "");
      break;
    case "tool":
      var nameSpan = document.createElement("span");
      nameSpan.textContent = event.title || "tool";
      row.appendChild(nameSpan);
      var toolDetails = document.createElement("details");
      var toolSummary = document.createElement("summary");
      toolSummary.textContent = "details";
      toolDetails.appendChild(toolSummary);
      if (event.phase === "started") {
        var argsLabel = document.createElement("div");
        argsLabel.className = "ev-section-label";
        argsLabel.textContent = "args";
        toolDetails.appendChild(argsLabel);
        var argsBody = document.createElement("div");
        argsBody.className = "ev-body";
        argsBody.textContent = event.body || "";
        toolDetails.appendChild(argsBody);
      } else {
        var outLabel = document.createElement("div");
        outLabel.className = "ev-section-label";
        outLabel.textContent = "output";
        toolDetails.appendChild(outLabel);
        var outBody = document.createElement("div");
        outBody.className = "ev-body";
        outBody.textContent = event.body || "";
        toolDetails.appendChild(outBody);
      }
      if (event.failed) row.classList.add("ev-tool-failed");
      row.appendChild(toolDetails);
      break;
  }
  return row;
}

function completeToolRow(row, event) {
  var failed = !!(event && event.failed);
  row.classList.add(failed ? "ev-tool-failed" : "ev-tool-done");
  var mark = document.createElement("span");
  mark.className = failed ? "ev-fail-mark" : "ev-done-mark";
  mark.textContent = failed ? "\u2717" : "\u2713";
  var details = row.querySelector("details");
  if (!details) {
    details = document.createElement("details");
    var summary = document.createElement("summary");
    summary.textContent = "details";
    details.appendChild(summary);
    row.appendChild(details);
  }
  row.insertBefore(mark, details);
  var outLabel = document.createElement("div");
  outLabel.className = "ev-section-label";
  outLabel.textContent = "output";
  details.appendChild(outLabel);
  var outBody = document.createElement("div");
  outBody.className = "ev-body";
  outBody.textContent = event.body || "";
  details.appendChild(outBody);
}

function updateChip() {
  var chip = document.getElementById("events-chip");
  if (!chip) return;
  if (pendingNew > 0) {
    chip.style.display = "block";
    document.getElementById("pending-count").textContent = pendingNew;
  } else {
    chip.style.display = "none";
  }
}

async function refreshJobs() {
  const response = await fetch("/api/jobs");
  const payload = await response.json();
  const body = document.getElementById("jobs-body");
  document.getElementById("jobs-empty").hidden = payload.jobs.length > 0;

  const seen = new Set();

  for (const job of payload.jobs) {
    seen.add(job.job_id);
    let row = jobRows.get(job.job_id);

    if (!row) {
      row = document.createElement("tr");
      row.innerHTML =
        "<td></td><td></td><td></td><td></td><td></td><td></td>";
      row.onclick = () => {
        selectedJobId = job.job_id;
        for (const [rowId, existingRow] of jobRows) {
          existingRow.classList.toggle("selected", rowId === selectedJobId);
        }
        refreshDetail();
      };
      jobRows.set(job.job_id, row);
      body.appendChild(row);
    }

    row.classList.toggle("selected", job.job_id === selectedJobId);

    const cells = row.children;
    setCellText(cells[0], job.job_id);
    setCellHTML(cells[1], badge(job.status));
    setCellText(cells[2], job.advisor);
    setCellText(cells[3], fmtSeconds(job.elapsed_seconds));
    const idleText = terminal.has(job.status) ? "-" : fmtSeconds(job.idle_seconds);
    setCellText(cells[4], idleText);
    setCellText(cells[5], job.name || "");
  }

  for (const [id, row] of jobRows) {
    if (!seen.has(id)) {
      jobRows.delete(id);
      row.remove();
    }
  }

  // Reorder only rows that are actually out of position, so a stable order
  // does not thrash the DOM on every poll.
  let orderIndex = 0;
  for (const job of payload.jobs) {
    const row = jobRows.get(job.job_id);
    if (!row) continue;
    if (body.children[orderIndex] !== row) {
      body.insertBefore(row, body.children[orderIndex] || null);
    }
    orderIndex++;
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
  const jobId = selectedJobId;
  const mySeq = ++detailSeq;
  const pane = document.getElementById("detail-pane");
  const logStream = detailTab === "events" ? "stdout" : detailTab;
  const [detailResponse, logsResponse] = await Promise.all([
    fetch("/api/jobs/" + jobId),
    fetch("/api/jobs/" + jobId + "/logs?stream=" + logStream),
  ]);
  // Bail if a different job was selected while these were in flight, so one
  // job's metadata is never rendered against another job's events.
  if (detailSeq !== mySeq) return;
  if (!detailResponse.ok) {
    pane.innerHTML = '<div class="empty">Job not found.</div>';
    lastDetailJobId = null;
    return;
  }
  const job = await detailResponse.json();
  const logs = await logsResponse.text();
  if (detailSeq !== mySeq) return;

  if (jobId !== lastDetailJobId) {
    pane.innerHTML =
      "<h2></h2>" +
      "<dl>" +
      "<dt>status</dt><dd></dd>" +
      "<dt>advisor</dt><dd></dd>" +
      "<dt>name</dt><dd></dd>" +
      "<dt>elapsed</dt><dd></dd>" +
      "<dt>idle</dt><dd></dd>" +
      "<dt>last event</dt><dd></dd>" +
      "<dt>error</dt><dd></dd>" +
      "</dl>" +
      '<div class="tabs">' +
      '<button id="tab-events">events</button>' +
      '<button id="tab-stdout">stdout</button>' +
      '<button id="tab-stderr">stderr</button>' +
      '<button id="tab-audit">audit</button>' +
      "</div>" +
      '<pre id="logs"></pre>' +
      '<div id="events-feed"><div id="events-chip"><span>\u2193 <span id="pending-count">0</span> new events</span></div></div>';
    lastDetailJobId = jobId;
    eventsOffset = 0;
    eventsFollow = true;
    pendingNew = 0;
    eventsStopped = false;
    toolRowMap = new Map();
    for (const tab of ["events", "stdout", "stderr", "audit"]) {
      const btn = document.getElementById("tab-" + tab);
      btn.onclick = () => { detailTab = tab; refreshDetail(); };
    }
    var feed = document.getElementById("events-feed");
    document.getElementById("events-chip").onclick = function () {
      eventsFollow = true;
      pendingNew = 0;
      this.style.display = "none";
      feed.scrollTop = feed.scrollHeight;
    };
    feed.addEventListener("scroll", function () {
      if (feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 48) {
        eventsFollow = true;
        pendingNew = 0;
        document.getElementById("events-chip").style.display = "none";
      }
    });
  }

  const dds = pane.querySelectorAll("dd");
  dds[0].innerHTML = badge(job.status);
  dds[1].textContent = escapeHtml(job.advisor);
  dds[2].textContent = escapeHtml(job.name || "-");
  dds[3].textContent = fmtSeconds(job.elapsed_seconds);
  dds[4].textContent = fmtSeconds(job.idle_seconds);
  dds[5].textContent = escapeHtml(job.last_event || "-");
  dds[6].textContent = escapeHtml(job.error || "-");

  pane.querySelector("h2").textContent = escapeHtml(job.job_id);

  for (const tab of ["events", "stdout", "stderr"]) {
    const btn = document.getElementById("tab-" + tab);
    if (btn) btn.classList.toggle("active", detailTab === tab);
  }

  const logEl = document.getElementById("logs");
  const eventsFeed = document.getElementById("events-feed");
  if (detailTab === "events") {
    logEl.style.display = "none";
    if (eventsFeed) eventsFeed.style.display = "block";
  } else {
    logEl.style.display = "block";
    if (eventsFeed) eventsFeed.style.display = "none";
  }

  const logText = logs || "(no output yet)";
  if (logEl.textContent !== logText) {
    logEl.textContent = logText;
  }

  if (detailTab === "events" && !eventsStopped) {
    try {
      var evResponse = await fetch("/api/jobs/" + jobId + "/events?offset=" + eventsOffset);
      if (detailSeq !== mySeq) return;
      if (!evResponse.ok) return;
      var evPayload = await evResponse.json();
      if (detailSeq !== mySeq) return;
      var feed = document.getElementById("events-feed");
      if (!feed) return;
      if (evPayload.reset) {
        var rows = feed.querySelectorAll(".ev-row");
        for (var i = 0; i < rows.length; i++) rows[i].remove();
        toolRowMap = new Map();
        eventsFollow = true;
        pendingNew = 0;
        updateChip();
      }
      var evList = evPayload.events || [];
      if (evList.length > 0) {
        var wasAtBottom = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 48;
        var frag = document.createDocumentFragment();
        var newRowCount = 0;
        for (var i = 0; i < evList.length; i++) {
          var ev = evList[i];
          if (ev.kind === "tool" && ev.entity_id && ev.phase === "completed") {
            var existingRow = toolRowMap.get(ev.entity_id);
            if (existingRow) {
              completeToolRow(existingRow, ev);
            } else {
              frag.appendChild(buildEventRow(ev));
              newRowCount++;
            }
          } else {
            var r = buildEventRow(ev);
            if (ev.kind === "tool" && ev.entity_id && ev.phase === "started") {
              toolRowMap.set(ev.entity_id, r);
            }
            frag.appendChild(r);
            newRowCount++;
          }
        }
        var chip = document.getElementById("events-chip");
        if (frag.childNodes.length > 0) {
          feed.insertBefore(frag, chip);
        }
        if (wasAtBottom && eventsFollow) {
          feed.scrollTop = feed.scrollHeight;
        } else if (!wasAtBottom) {
          eventsFollow = false;
          pendingNew += newRowCount;
          updateChip();
        }
      }
      // Always advance the cursor — even for an empty/filtered chunk — so the
      // feed can never re-request the same bytes forever (freeze bug).
      eventsOffset = evPayload.next_offset;
      if (evPayload.terminal && evPayload.at_eof) {
        eventsStopped = true;
      }
    } catch (_e) {
      // Don't crash the poll loop
    }
  }
}

async function poll() {
  try {
    if (currentView === "graph") {
      await refreshGraph();
    } else {
      await refreshJobs();
    }
    if (selectedJobId) {
      await refreshDetail();
    }
  } catch (_e) {
    // Swallow transient fetch/JSON errors so a single blip never stops polling.
  } finally {
    setTimeout(poll, hasRunningJobs ? 3000 : 15000);
  }
}

// ---------------------------------------------------------------------------
// Graph view
// ---------------------------------------------------------------------------

let currentView = "list";

document.getElementById("view-list").onclick = function () {
  currentView = "list";
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  document.getElementById("view-list").classList.add("active");
  document.getElementById("view-graph").classList.remove("active");
  document.getElementById("list-view").style.display = "";
  document.getElementById("graph-container").style.display = "none";
};

document.getElementById("view-graph").onclick = function () {
  currentView = "graph";
  document.getElementById("view-graph").classList.add("active");
  document.getElementById("view-list").classList.remove("active");
  document.getElementById("list-view").style.display = "none";
  document.getElementById("graph-container").style.display = "block";
  if (graphFirstShown) {
    graphFirstShown = false;
    fitToView();
  } else {
    paintGraph();
  }
  maybeStartAnimation();
};

let graphData = null;
let graphRects = [];
let lastLaidOutRevision = null;
let panX = 0, panY = 0, scale = 1;
let userAdjustedView = false;
let graphFirstShown = true;

const COL_W = 210, ROW_H = 76, NODE_W = 188, NODE_H = 52, PAD_X = 24, PAD_Y = 24;

function ctxRoundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  if (r > 0) ctx.arcTo(x + w, y, x + w, y + r, r);
  ctx.lineTo(x + w, y + h - r);
  if (r > 0) ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
  ctx.lineTo(x + r, y + h);
  if (r > 0) ctx.arcTo(x, y + h, x, y + h - r, r);
  ctx.lineTo(x, y + r);
  if (r > 0) ctx.arcTo(x, y, x + r, y, r);
  ctx.closePath();
}

function clipText(ctx, text, maxWidth) {
  if (!text) return "";
  if (ctx.measureText(text).width <= maxWidth) return text;
  var lo = 0, hi = text.length;
  while (lo < hi) {
    var mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(text.slice(0, mid)).width <= maxWidth) lo = mid; else hi = mid - 1;
  }
  if (lo < 3) return "";
  return text.slice(0, lo - 1) + "\u2026";
}

function computeGraphLayout(nodes, edges) {
  if (nodes.length === 0) return { rects: [], width: 0, height: 0 };
  if (nodes.length === 1) {
    var n = nodes[0];
    return {
      rects: [{ id: n.id, x: PAD_X, y: PAD_Y, w: NODE_W, h: NODE_H, kind: n.kind }],
      width: PAD_X + NODE_W + PAD_X,
      height: PAD_Y + NODE_H + PAD_Y,
    };
  }

  var byId = {};
  for (var i = 0; i < nodes.length; i++) byId[nodes[i].id] = nodes[i];

  var children = {};
  var hasParent = {};
  for (var i = 0; i < edges.length; i++) {
    var e = edges[i];
    var src = e["from"];
    var tgt = e["to"];
    if (!children[src]) children[src] = [];
    children[src].push(tgt);
    hasParent[tgt] = true;
  }

  var roots = nodes.filter(function (n) { return !hasParent[n.id]; });
  if (roots.length === 0) return { rects: [], width: 0, height: 0 };

  var depth = {};
  var visited = {};
  var queue = [];
  for (var i = 0; i < roots.length; i++) {
    if (!visited[roots[i].id]) {
      visited[roots[i].id] = true;
      depth[roots[i].id] = 0;
      queue.push(roots[i].id);
    }
  }
  var qi = 0;
  while (qi < queue.length) {
    var id = queue[qi]; qi++;
    var nd = depth[id] + 1;
    var kids = children[id] || [];
    for (var j = 0; j < kids.length; j++) {
      var k = kids[j];
      if (!visited[k]) {
        visited[k] = true;
        depth[k] = nd;
        queue.push(k);
      }
    }
  }

  var leafCounter = 0;
  var yPos = {};
  var dfsVisited = {};

  function dfsY(nodeId) {
    if (dfsVisited[nodeId]) return yPos[nodeId] || 0;
    dfsVisited[nodeId] = true;
    var kids = children[nodeId] || [];
    if (kids.length === 0) {
      yPos[nodeId] = leafCounter * ROW_H;
      leafCounter++;
      return yPos[nodeId];
    }
    var sumY = 0;
    for (var j = 0; j < kids.length; j++) sumY += dfsY(kids[j]);
    yPos[nodeId] = sumY / kids.length;
    return yPos[nodeId];
  }

  for (var i = 0; i < roots.length; i++) {
    dfsY(roots[i].id);
    leafCounter++;
  }

  var rects = [];
  var maxRight = 0, maxBottom = 0;
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    var d = depth[n.id] !== undefined ? depth[n.id] : 0;
    var y = yPos[n.id] !== undefined ? yPos[n.id] : 0;
    var x = PAD_X + d * COL_W;
    rects.push({ id: n.id, x: x, y: y, w: NODE_W, h: NODE_H, kind: n.kind });
    maxRight = Math.max(maxRight, x + NODE_W);
    maxBottom = Math.max(maxBottom, y + NODE_H);
  }

  return { rects: rects, width: maxRight + PAD_X, height: maxBottom + PAD_Y };
}

function layoutGraph() {
  if (!graphData || !graphData.nodes || graphData.nodes.length === 0) {
    graphRects = [];
    return;
  }
  var layout = computeGraphLayout(graphData.nodes, graphData.edges);
  graphRects = layout.rects;
}

function paintGraph() {
  var canvas = document.getElementById("graph-canvas");
  var ctx = canvas.getContext("2d");
  var dpr = window.devicePixelRatio || 1;

  var container = canvas.parentElement;
  var cssW = container.clientWidth;
  var cssH = container.clientHeight;
  if (cssW === 0 || cssH === 0) { cssW = 400; cssH = 300; }

  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);

  hasRunningNodes = false;

  var hint = document.getElementById("no-nest-hint");
  if (hint) hint.style.display = (graphData && graphData.meaningful_edge_count === 0) ? "" : "none";

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  if (!graphData || !graphData.nodes || graphData.nodes.length === 0) {
    ctx.fillStyle = "#7d8590";
    ctx.font = "16px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("No jobs yet.", cssW / 2, cssH / 2);
    return;
  }

  if (graphRects.length === 0) {
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = "#7d8590";
    ctx.font = "14px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("Could not lay out graph (possible cycle in data).", cssW / 2, cssH / 2);
    return;
  }

  ctx.clearRect(0, 0, cssW, cssH);
  ctx.save();
  ctx.translate(panX, panY);
  ctx.scale(scale, scale);

  var rectById = {};
  for (var i = 0; i < graphRects.length; i++) rectById[graphRects[i].id] = graphRects[i];

  var orphanIds = new Set();
  if (graphData && graphData.diagnostics) {
    for (var di = 0; di < graphData.diagnostics.length; di++) {
      var d = graphData.diagnostics[di];
      if (d.type === "missing_parent") orphanIds.add(d.job_id);
    }
  }

  ctx.lineWidth = 1.5;
  for (var i = 0; i < graphData.edges.length; i++) {
    var e = graphData.edges[i];
    var srcR = rectById[e["from"]];
    var tgtR = rectById[e["to"]];
    if (!srcR || !tgtR) continue;
    var x1 = srcR.x + srcR.w, y1 = srcR.y + srcR.h / 2;
    var x2 = tgtR.x, y2 = tgtR.y + tgtR.h / 2;
    var cp = Math.min(Math.abs(x2 - x1) * 0.4, 60);
    var orphanEdge = orphanIds.has(e["to"]);
    ctx.strokeStyle = orphanEdge ? "#ff7b72" : "#30363d";
    ctx.setLineDash(orphanEdge ? [4, 3] : []);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.bezierCurveTo(x1 + cp, y1, x2 - cp, y2, x2, y2);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  for (var i = 0; i < graphData.nodes.length; i++) {
    var n = graphData.nodes[i];
    if (n.kind === "orchestrator") continue;
    var r = rectById[n.id];
    if (!r) continue;
    var status = n.status || "pending";
    var borderColor, bgColor;
    switch (status) {
      case "running": borderColor = "#79c0ff"; bgColor = "#1f3a5f"; break;
      case "succeeded": borderColor = "#56d364"; bgColor = "#1b3a2a"; break;
      case "failed": case "timed_out": borderColor = "#ff7b72"; bgColor = "#4a1e24"; break;
      default: borderColor = "#9da7b3"; bgColor = "#30363d";
    }

    ctx.fillStyle = bgColor;
    ctx.strokeStyle = borderColor;
    ctx.lineWidth = 1.5;
    ctxRoundRect(ctx, r.x, r.y, r.w, r.h, 8);
    ctx.fill();
    ctx.stroke();

    if (n.id === selectedJobId) {
      ctx.strokeStyle = "#00ffff";
      ctx.lineWidth = 2;
      ctxRoundRect(ctx, r.x - 1, r.y - 1, r.w + 2, r.h + 2, 10);
      ctx.stroke();
    }

    if (orphanIds.has(n.id)) {
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = "#ff7b72";
      ctx.lineWidth = 1.5;
      ctxRoundRect(ctx, r.x, r.y, r.w, r.h, 8);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    if (n.status === "running") {
      hasRunningNodes = true;
      if (reduceMotion) {
        ctx.strokeStyle = "#58a6ff";
        ctx.lineWidth = 2;
        ctxRoundRect(ctx, r.x - 1, r.y - 1, r.w + 2, r.h + 2, 10);
        ctx.stroke();
      } else {
        var pulse = 0.35 + 0.35 * (0.5 + 0.5 * Math.sin(performance.now() / 500));
        ctx.save();
        ctx.globalAlpha = pulse;
        ctx.strokeStyle = "#58a6ff";
        ctx.lineWidth = 3;
        ctxRoundRect(ctx, r.x - 1.5, r.y - 1.5, r.w + 3, r.h + 3, 10);
        ctx.stroke();
        ctx.restore();
      }
    }

    ctx.fillStyle = "#e6edf3";
    ctx.font = "bold 11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    var line1 = (n.advisor || "") + " " + (n.id ? n.id.slice(-8) : "");
    ctx.fillText(clipText(ctx, line1, r.w * 0.55 - 4), r.x + 7, r.y + 14);

    ctx.fillStyle = borderColor;
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "right";
    ctx.fillText(clipText(ctx, status.replace(/_/g, " "), r.w * 0.4 - 4), r.x + r.w - 7, r.y + 14);

    ctx.fillStyle = "#7d8590";
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.fillText(clipText(ctx, n.name || "", r.w - 14), r.x + 7, r.y + 34);
  }

  for (var i = 0; i < graphData.nodes.length; i++) {
    var n = graphData.nodes[i];
    if (n.kind !== "orchestrator") continue;
    var r = rectById[n.id];
    if (!r) continue;

    ctx.fillStyle = "#2a1f5e";
    ctx.strokeStyle = "#6e40c9";
    ctx.lineWidth = 1.5;
    ctxRoundRect(ctx, r.x, r.y, r.w, r.h, 8);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = "#d2a8ff";
    ctx.font = "13px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(clipText(ctx, "MAIN \u00b7 " + (n.label || ""), r.w - 14), r.x + 7, r.y + r.h / 2);
  }

  ctx.restore();

  maybeStartAnimation();
}

async function refreshGraph() {
  try {
    var response = await fetch("/api/graph");
    var payload = await response.json();
    graphData = payload;
    if (graphData.revision !== lastLaidOutRevision) {
      layoutGraph();
      lastLaidOutRevision = graphData.revision;
      if (!userAdjustedView) {
        fitToView();
        return;
      }
    }
    paintGraph();
  } catch (_e) { /* poll safety */ }
}

(function initGraphInteraction() {
  var canvas = document.getElementById("graph-canvas");
  var startX, startY, startPanX, startPanY;
  var mouseDown = false;
  var dragged = false;

  canvas.addEventListener("mousedown", function (ev) {
    var rect = canvas.getBoundingClientRect();
    startX = ev.clientX - rect.left;
    startY = ev.clientY - rect.top;
    startPanX = panX;
    startPanY = panY;
    mouseDown = true;
    dragged = false;
  });

  window.addEventListener("mousemove", function (ev) {
    if (!mouseDown) return;
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left;
    var sy = ev.clientY - rect.top;
    if (Math.abs(sx - startX) > 4 || Math.abs(sy - startY) > 4) {
      dragged = true;
      userAdjustedView = true;
      panX = startPanX + (sx - startX);
      panY = startPanY + (sy - startY);
      paintGraph();
    }
  });

  window.addEventListener("mouseup", function (ev) {
    if (!mouseDown) return;
    mouseDown = false;
    if (dragged) return;
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left;
    var sy = ev.clientY - rect.top;
    var worldX = (sx - panX) / scale;
    var worldY = (sy - panY) / scale;
    for (var i = graphRects.length - 1; i >= 0; i--) {
      var r = graphRects[i];
      if (worldX >= r.x && worldX < r.x + r.w && worldY >= r.y && worldY < r.y + r.h) {
        if (r.kind === "job") {
          selectedJobId = r.id;
          paintGraph();
          refreshDetail();
        }
        return;
      }
    }
  });

  canvas.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left;
    var sy = ev.clientY - rect.top;
    var factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
    var newScale = Math.min(Math.max(scale * factor, 0.4), 2.0);
    var worldX = (sx - panX) / scale;
    var worldY = (sy - panY) / scale;
    panX = sx - worldX * newScale;
    panY = sy - worldY * newScale;
    scale = newScale;
    userAdjustedView = true;
    paintGraph();
  }, { passive: false });
})();

function fitToView() {
  if (graphRects.length === 0) {
    panX = 0; panY = 0; scale = 1;
    paintGraph();
    return;
  }

  var canvas = document.getElementById("graph-canvas");
  var container = canvas.parentElement;
  var cssW = container.clientWidth;
  var cssH = container.clientHeight;
  if (cssW === 0 || cssH === 0) return;

  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (var i = 0; i < graphRects.length; i++) {
    var r = graphRects[i];
    if (r.x < minX) minX = r.x;
    if (r.y < minY) minY = r.y;
    if (r.x + r.w > maxX) maxX = r.x + r.w;
    if (r.y + r.h > maxY) maxY = r.y + r.h;
  }

  var bboxW = maxX - minX;
  var bboxH = maxY - minY;
  if (bboxW === 0) bboxW = 1;
  if (bboxH === 0) bboxH = 1;

  var margin = 40;
  scale = Math.min((cssW - 2 * margin) / bboxW, (cssH - 2 * margin) / bboxH);
  scale = Math.min(Math.max(scale, 0.4), 2.0);

  panX = (cssW - bboxW * scale) / 2 - minX * scale;
  panY = (cssH - bboxH * scale) / 2 - minY * scale;

  userAdjustedView = false;
  paintGraph();
}

function animationLoop() {
  if (currentView !== "graph") { rafId = null; return; }
  paintGraph();
  if (hasRunningNodes && !reduceMotion) {
    rafId = requestAnimationFrame(animationLoop);
  } else {
    rafId = null;
  }
}

function maybeStartAnimation() {
  if (currentView !== "graph" || reduceMotion || rafId !== null) return;
  if (graphData && graphData.nodes) {
    for (var i = 0; i < graphData.nodes.length; i++) {
      if (graphData.nodes[i].status === "running") {
        rafId = requestAnimationFrame(animationLoop);
        return;
      }
    }
  }
}

document.getElementById("fit-btn").onclick = fitToView;

window.addEventListener("keydown", function (ev) {
  if (currentView !== "graph") return;
  var t = ev.target;
  if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable) return;
  if (ev.key === "f" || ev.key === "F") {
    ev.preventDefault();
    fitToView();
  }
});

window.addEventListener("resize", function () {
  if (currentView === "graph" && document.getElementById("graph-container").style.display !== "none") {
    paintGraph();
  }
});

// Draggable splitter between the left pane and the detail pane.
(function initPaneSplitter() {
  const splitter = document.getElementById("pane-splitter");
  const mainEl = document.querySelector("main");
  let dragging = false;
  splitter.addEventListener("mousedown", function (e) {
    dragging = true;
    splitter.classList.add("dragging");
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    e.preventDefault();
  });
  window.addEventListener("mousemove", function (e) {
    if (!dragging) return;
    const rect = mainEl.getBoundingClientRect();
    let leftWidth = e.clientX - rect.left;
    const minLeft = 320, maxLeft = rect.width - 360 - 6;
    if (leftWidth < minLeft) leftWidth = minLeft;
    if (leftWidth > maxLeft) leftWidth = maxLeft;
    mainEl.style.gridTemplateColumns = leftWidth + "px 6px 1fr";
  });
  window.addEventListener("mouseup", function () {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("dragging");
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    if (currentView === "graph") paintGraph();
  });
})();

// Start the poll loop only after every top-level binding above is initialized
// (currentView, graphData, etc. are `let`-scoped and would be in the temporal
// dead zone if poll() ran before their declarations executed).
poll();
</script>
</body>
</html>
"""
