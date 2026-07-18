# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [0.1.3] - 2026-07-18

### Fixed
- Stop the dashboard reconciler from corrupting job state: after observing a
  worker PID die, `reconcile_stale` re-reads `state.json` and re-runs the full
  liveness decision (terminal state, pending grace, live worker PID) on the
  fresh copy before abandoning — closing the TOCTOU race that could flip
  succeeded jobs to `abandoned`.
- Treat `PermissionError` from `kill(pid, 0)` as "process alive" (EPERM means
  the PID exists) instead of presuming the worker dead.
- Allow a slow-booting worker to reclaim a job the reconciler marked
  `abandoned` during the startup grace window, clearing stale abandonment
  artefacts instead of crashing on an illegal state transition.

## [0.1.2] - 2026-07-18

### Fixed
- Extract the final Codex response from the `item.text` field emitted by current
  `codex exec --json` versions, while retaining support for older content shapes.
- Keep newly launched jobs visible on the dashboard by allowing the detached
  worker a short startup grace period to record its PID. Stale running jobs and
  genuinely orphaned pending jobs are still reconciled to `abandoned`.

## [0.1.1] - 2026-07-18

### Added
- **Web dashboard** (`crossagent dashboard [--host H] [--port N] [--no-open]`):
  local web UI at `http://127.0.0.1:8642/` served entirely from the Python
  standard library — live auto-refreshing job table, per-job detail (status,
  elapsed, idle, last event, error), and stdout/stderr log viewer. Loopback
  bind by default with a warning on non-loopback hosts; job IDs are validated
  against a strict pattern (no path traversal); the prompt file is never
  routable.
- **Job listing** (`crossagent list [--status STATE] [--limit N] [--json]`):
  lists every job newest-first with status, advisor, elapsed, idle, and name.
  Stale `running` jobs reconcile to `abandoned` during listing and unreadable
  job directories are reported on stderr — no delegation is ever silently
  dropped from view. Recovers job IDs lost to a parent timeout or context reset.
- **Durable job lifecycle** (`crossagent start`/`wait`/`status`/`result`/`logs`/`cancel`):
  Detached worker runs the advisor in the background; job state persists to
  `~/.local/state/crossagent/jobs/<job-id>/`. The calling agent can poll with
  bounded waits (`--timeout`) and recover by job ID after a parent timeout.
  States: `pending`, `running`, `succeeded`, `failed`, `timed_out`, `cancelled`,
  `abandoned`. Terminal states are final and immutable; stale `running` state
  reconciles to `abandoned` on status.
- **Unified process supervisor** (`src/crossagent/runner.py`): concurrent stdout/stderr
  reader threads + queue, per-process-group isolation, heartbeat emission, idle
  warning, max-runtime enforcement, and graceful then forced process-tree cleanup.
- **Codex JSONL event streaming** (`result_parser = "codex-jsonl"`): the built-in
  Codex adapter now uses `codex exec --json` and the `CodexJsonlParser` extracts
  `agent_message` as the final answer, captures `thread_id` from `thread.started`,
  detects `turn.failed`/`error` as failure, and builds `codex exec resume <id>` for
  follow-ups. Malformed lines are preserved in diagnostic logs and ignored safely.
- **Advisor parser interface** (`src/crossagent/parsers.py`): all advisor output
  (text, Claude stream-json, Codex JSONL) goes through a common `EventParser`
  protocol with `consume_stdout`/`consume_stderr`/`finish`, wired into both the
  foreground CLI and the durable worker.

### Changed
- The installed agent skill (`skills/crossagent/SKILL.md`) now defaults to the
  durable job workflow for agent-to-agent calls (start → bounded wait → recover
  by job ID). The synchronous foreground shortcut is documented as an alternative.
- README and skill documentation now specify precise lifecycle semantics
  (heartbeat 15 s, idle warning 120 s, termination grace 10 s; durable start
  defaults to 1800 s max runtime; foreground CLI remains unlimited for
  compatibility).
- All CLI examples in documentation have been verified against the actual
  `argparse` surface of each subcommand.

### Experimental
- Codex JSONL support is experimental. The built-in
  flags match `codex exec --json`. If your install differs, override in
  `~/.config/crossagent/advisors.json`.

## [0.1.0] - 2026-07-18

### Added
- Initial release: `crossagent` CLI and Agent Skill for getting a second opinion from a peer AI coding agent.
- Cross-agent advisor registry with built-ins for **claude** (full), **codex**, **opencode**, **commandcode**, and **gemini** (experimental).
- User-overridable advisors via `~/.config/crossagent/advisors.json` — add or fix an advisor with no code change.
- Named, resumable sessions keyed by `advisor:name`, with `--new-session`, `--resume`, and `--fork-session`.
- Streamed progress and final-answer capture for Claude's `stream-json`; text capture for other advisors.
- `install.sh` that places the skill into detected agent config dirs and installs the CLI.
- Dependency-free test suite covering command building, advisor config layering, and the session registry.

### Fixed
- Redact second-opinion prompts from command progress logs for every advisor.
- Match CommandCode's verified `--model` flag and non-interactive invocation.
