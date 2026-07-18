# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [Unreleased]

### Added
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
- Codex JSONL support is experimental per the rollout strategy. The built-in
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
