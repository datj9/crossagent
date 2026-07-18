---
name: crossagent
description: Use when the user wants a second opinion from another AI coding agent before deciding — "crossagent", "ask Claude", "hoi y voi Claude", "ask Codex", "debate with Claude", "get a second opinion", "what would Claude/Codex say". Runs crossagent to ask a peer agent (Claude, Codex, OpenCode, CommandCode, Gemini) via its CLI with named, resumable Claude sessions, streamed progress, a packaged context prompt, and a final synthesis comparing both viewpoints.
version: 0.2.0
author: Dat Nguyen
---

# crossagent

## Overview

Treat a peer AI agent as a teammate for decision-quality work, not as an oracle.
Call the peer's CLI with enough context for it to evaluate the problem
independently, wait for the answer, then compare its view with your own before
recommending a path. The default advisor is **Claude** (`claude -p`); Codex,
OpenCode, CommandCode, and Gemini are also supported.

## Decision Flow

1. Confirm the second-opinion target: decision to make, current state, constraints, options considered, and what evidence would change the answer.
2. Decide whether to ask a peer. Do so when the user asks directly, when architecture or prompt decisions benefit from an adversarial second view, or when the problem has credible tradeoffs. Skip for trivial commands, purely mechanical edits, or anything requiring secrets.
3. Build a compact context package: absolute paths, relevant files, current findings, known unknowns, and safe read-only commands the advisor may run.
4. Choose a stable session name before the first call. Reuse it for the same problem so later turns resume. Use a new name for a different decision; use `--fork-session` for an alternative branch of the same history.
5. **Start a durable job** (recommended — survives parent timeout). See "Durable Job Workflow" below.
6. Compare outputs. Separate confirmed facts, claims needing verification, disagreements, unresolved questions, and the recommended synthesis. Do not outsource final judgment.
7. If material conflict remains, run a follow-up in the same session with the conflict framed explicitly. Stop when the remaining uncertainty is named and decision-useful.

## Durable Job Workflow

Use this for all agent-to-agent calls. It survives the parent tool call's wall-clock
limit — the job continues in the background and you can recover it later by job ID.

```bash
# 1. Start the job. Retain the returned job_id.
crossagent start \
  --agent claude \
  --name "payments-retry-design" \
  --cwd "$PWD" \
  --prompt-file /tmp/crossagent.md \
  --json

# Output (stdout):
# {
#   "schema_version": 1,
#   "job_id": "job_20260718T100000_a1b2c3d4",
#   "status": "running",
#   "advisor": "claude",
#   "started_at": "2026-07-18T10:00:00Z"
# }
```

2. **Do not block.** Tell the user the advisor started and do other work while the
   job runs. If you must wait, use a bounded poll:

```bash
# 3. Wait up to 45 seconds, return control either way.
crossagent wait job_20260718T100000_a1b2c3d4 --timeout 45 --json

# If still running:
# {"schema_version":1,"job_id":"job_20260718T100000_a1b2c3d4","status":"running","elapsed_seconds":91,"idle_seconds":7,"last_event":"item.completed","updated_at":"2026-07-18T10:01:31Z"}
```

4. If the job is still `running`, do more useful work and poll again. **Never wait
   indefinitely in a single tool call.** Use multiple bounded waits instead.

5. **If the parent tool call times out**, recover by job ID — do **not** start a
   duplicate:

```bash
crossagent status job_20260718T100000_a1b2c3d4 --json

# On success:
# {"schema_version":1,"job_id":"job_20260718T100000_a1b2c3d4","status":"succeeded","elapsed_seconds":187,"idle_seconds":3, ...
```

6. On `succeeded`, fetch the result:

```bash
crossagent result job_20260718T100000_a1b2c3d4
# Prints only the advisor's answer to stdout, for pipelining.
```

7. On `failed`, `timed_out`, `cancelled`, or `abandoned`, report the terminal state
   and the diagnostic from `status --json` (the `error` field explains why). Do not
   retry automatically — ask the user first.

8. **Cancel** only when the user requests it, the overall task is abandoned, or the
   configured `--max-runtime` expires:

```bash
crossagent cancel job_20260718T100000_a1b2c3d4 --wait
```

9. Keep the job ID in your working context. The same session name may appear in
   multiple job IDs if the user retries; the job ID is the unique handle. If the
   ID is ever lost (e.g. after a context reset), recover it with
   `crossagent list --json` — every job stays visible there until you delete its
   state directory.

### Foreground shortcut (synchronous, no durability)

For quick, interactive second opinions where a parent timeout is not a concern:

```bash
crossagent --agent claude \
  --name "project-topic-decision" \
  --cwd "$PWD" \
  --prompt-file /tmp/crossagent.md
```

This runs the advisor to completion synchronously and prints the answer to stdout.
It is a shortcut around `start` + `wait` + `result` but does **not** survive a
parent time-out. Use the durable workflow above for agent-to-agent calls.

## Timer Defaults

| Control | Durable start | Foreground CLI |
|---|---|---|
| Heartbeat interval | 15 s | 15 s |
| Idle warning | 120 s | 120 s |
| Maximum runtime | 1,800 s (30 min) | Unlimited |
| Termination grace | 10 s | 10 s |
| Bounded wait default | 45 s | N/A |

The idle threshold emits warnings to stderr but does **not** kill the advisor.
Only `--max-runtime` or explicit cancellation terminates the job.

## Job Subcommands

| Command | Purpose |
|---|---|
| `crossagent start ...` | Create a job, launch a detached worker, return its ID. |
| `crossagent wait JOB_ID [--timeout N] [--require-complete] [--json]` | Wait up to N seconds for a terminal state. |
| `crossagent status JOB_ID [--json]` | Report current state without waiting. Reconciles stale `running` → `abandoned` when the worker is gone. |
| `crossagent result JOB_ID` | Print the completed advisor answer to stdout. |
| `crossagent logs JOB_ID [--follow] [--stream stdout\|stderr]` | Read or follow an advisor's log stream. |
| `crossagent cancel JOB_ID [--wait] [--timeout N]` | Request cancellation and optionally wait for cleanup. |
| `crossagent list [--status STATE] [--limit N] [--json]` | Dashboard of all jobs, newest first — status, elapsed, idle, name. Reconciles stale jobs to `abandoned` so nothing is silently dropped. Use it to rediscover a job when the ID was lost. |
| `crossagent dashboard [--host H] [--port N] [--no-open]` | Local web dashboard (default `http://127.0.0.1:8642/`): live job table, per-job detail, and stdout/stderr logs. For the human user — an agent should use `list`/`status --json` instead. |

### `start` flags

Everything from the foreground CLI is available:
`--agent`, `--name`, `--resume`, `--new-session`, `--fork-session`,
`--prompt-file`, `--prompt`, `--cwd`, `--model`, `--safe-mode`, `--no-stream`,
`--partial`, `--tools`, `--allowed-tools`, `--permission-mode`, `--system-prompt`,
`--raw-arg`, `--registry`.

Plus:
- `--max-runtime SECONDS` (default 1800 — 30 minutes, safe for agent-started jobs)
- `--termination-grace SECONDS` (default 10)
- `--json` (machine-readable output)

## CLI Helper

The bundled helper builds the right command for the chosen advisor:

- Invokes the chosen advisor's CLI.
- Streams progress (Claude `stream-json`; Codex `--json` JSONL events; others capture text).
- Stores `session_id` by `advisor:name` in `~/.config/crossagent/sessions.json` and auto-resumes when the same `--name` is reused.
- Writes the advisor's final answer to stdout and progress/metadata to stderr.

Switch advisor with `--agent codex|opencode|commandcode|gemini`. List what's available
with `crossagent --list-advisors`. Add or fix an advisor in
`~/.config/crossagent/advisors.json` (no code change needed). Use `--new-session` to
ignore a stored session, `--resume ID_OR_SEARCH` to force a resume target,
`--fork-session` to branch, and `--model` to raise the model only when the decision
justifies it.

## Prompting The Advisor

Read [references/second-opinion-protocol.md](references/second-opinion-protocol.md) when preparing a substantive prompt (architecture, prompt, eval, or debugging decisions).

Use XML-like sections. Ask the advisor to evaluate, inspect, compare, or trace; avoid "think step by step". Give it permission to gather more context with read-only commands, and ask it to cite file paths, commands, or assumptions behind claims.

## Operating Rules

- Keep secrets out of prompts, CLI args, logs, and copied context.
- Default to a routine model; raise `--model` only when the decision justifies it.
- No cost cap by default. Add one only when the user explicitly asks for a spend limit.
- For Claude: use `--safe-mode` when the advisor does not need repo config/skills/hooks; `--tools ""` for pure reasoning; allow read/search tools only when it must inspect files.
- Report both the advisor's answer and your own synthesis. Name where the two views agree, diverge, or leave uncertainty.
- **Never wait indefinitely in one tool call.** Use the durable job workflow and bounded waits. Recover by job ID if the parent times out.
