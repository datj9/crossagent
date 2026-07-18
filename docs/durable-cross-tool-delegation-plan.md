# Durable Cross-Tool Delegation Implementation Plan

Status: Proposed  
Date: 2026-07-18  
Target: `crossagent` CLI and installed `crossagent` Agent Skill

## Summary

Cross-tool delegation currently assumes that the calling agent can keep one
foreground process open until the delegated advisor exits. That assumption does
not hold reliably when, for example, Claude Code invokes `crossagent`, which then
invokes Codex. The parent tool can reach its own wall-clock timeout while Codex is
still running, and the current wrapper has no durable job ID, persisted state, or
recovery command. The parent receives no reliable indication of whether the
advisor is healthy, stalled, failed, or completed after the timeout.

The recommended solution is a durable job protocol:

1. `crossagent start` launches the advisor in a detached worker and immediately
   returns a job ID.
2. `crossagent wait` waits for a bounded period and returns either a terminal
   result state or an explicit `running` state.
3. `crossagent status`, `result`, `logs`, and `cancel` remain available if the
   calling agent times out, restarts, or loses its original shell invocation.
4. A shared process supervisor drains both output streams, emits heartbeats,
   applies runtime limits, persists results, and terminates the complete process
   tree when required.

The existing synchronous command remains available for backward compatibility.
The installed skill should use durable jobs by default because agent callers have
bounded tool-call lifetimes.

## Goals

- Never leave a parent agent waiting forever without a state signal.
- Distinguish a healthy long-running job from a silent or stalled process.
- Preserve the delegated result when the original parent invocation ends.
- Make cancellation and timeout behavior explicit and process-tree safe.
- Stream progress from Codex and other advisors when their CLIs support it.
- Preserve the current zero-runtime-dependency and Python 3.9 requirements.
- Keep the current foreground CLI behavior working during migration.
- Avoid invoking real paid advisor CLIs in the automated test suite.

## Non-goals

- Building a distributed scheduler or remote execution service.
- Adding a permanently running daemon in the first release.
- Normalizing every advisor's internal event model into one exhaustive schema.
- Guaranteeing that an advisor can resume unless its native CLI supports resume.
- Treating a lack of model output as proof that the process is unhealthy.
- Adding a default monetary cost cap.

## Current-State Findings

### Text advisors are opaque until exit

`_run_text` uses `subprocess.run(..., capture_output=True)`. Codex, OpenCode,
CommandCode, and Gemini therefore provide no visible progress until the process
finishes. A caller cannot distinguish active work from a hang.

### Claude streaming can deadlock

`_run_stream` reads stdout to EOF and only then drains stderr. If the advisor
writes enough stderr to fill its pipe while stdout remains open, the child blocks
on stderr and the parent blocks waiting for stdout.

### There is no durable lifecycle

The current process has no persisted job state, heartbeat, timeout model,
cancellation request, or result spool. If the parent tool kills the wrapper, the
result is no longer addressable even if the advisor continues running.

### Caller and worker timeouts are conflated

The system needs four independent controls:

- **Wait timeout:** how long one parent command waits before returning control.
- **Idle threshold:** how long the advisor has produced no observable activity.
- **Maximum runtime:** how long the delegated job may run overall.
- **Termination grace:** how long the child has to exit before forced cleanup.

A wait timeout must not terminate the job. It exists specifically to return a
recoverable `running` signal to the parent.

### Codex capabilities are not being used

Current Codex CLI versions support `codex exec --json`, which emits JSONL events
such as thread start, turn start/completion/failure, item updates, and errors.
Codex also supports non-interactive session resume. The current adapter treats
Codex as raw text and does not store its thread ID.

## Proposed User Experience

### Start a durable delegation

```bash
crossagent start \
  --agent codex \
  --name payments-retry-design \
  --cwd "$PWD" \
  --max-runtime 1800 \
  --prompt-file /tmp/decision.md \
  --json
```

Expected stdout:

```json
{
  "schema_version": 1,
  "job_id": "job_01J...",
  "status": "running",
  "advisor": "codex",
  "started_at": "2026-07-18T10:00:00Z"
}
```

The command returns after the worker and advisor have launched successfully. It
does not wait for the advisor's answer.

### Wait for a bounded period

```bash
crossagent wait job_01J... --timeout 45 --json
```

If the job remains active:

```json
{
  "schema_version": 1,
  "job_id": "job_01J...",
  "status": "running",
  "elapsed_seconds": 91,
  "idle_seconds": 7,
  "last_event": "item.completed",
  "updated_at": "2026-07-18T10:01:31Z"
}
```

Reaching the wait timeout is not a job failure. In JSON mode, `wait` should exit
successfully when it can report a valid state, including `running`. Scripts that
want a nonzero exit when the job is incomplete can opt into
`--require-complete`.

### Retrieve and manage the job

```bash
crossagent status job_01J... --json
crossagent logs job_01J... --follow
crossagent result job_01J...
crossagent cancel job_01J...
```

`result` prints only the final advisor answer to stdout for pipeline
compatibility. Diagnostics and metadata continue to go to stderr unless JSON
mode explicitly requests a machine-readable envelope.

### Preserve synchronous compatibility

The existing form continues to work:

```bash
crossagent --agent codex --prompt-file /tmp/decision.md
```

It should use the new process supervisor internally, gaining concurrent output
draining, heartbeats, clean signal handling, and optional timeout flags without
requiring callers to adopt durable jobs immediately.

## Job State Model

### States

| State | Terminal | Meaning |
|---|---:|---|
| `pending` | No | Job metadata exists, but the advisor is not confirmed running. |
| `running` | No | Worker and advisor are active. |
| `succeeded` | Yes | Advisor exited successfully and a result was captured. |
| `failed` | Yes | Launch, protocol, advisor, or worker failure prevented success. |
| `timed_out` | Yes | Maximum job runtime expired and cleanup completed. |
| `cancelled` | Yes | A user or parent agent requested cancellation. |
| `abandoned` | Yes | Persisted state said running, but no matching live worker can be found. |

Only the detached worker writes lifecycle state. Other commands communicate by
reading state or creating request files, preventing competing state writers.

### Required state fields

```json
{
  "schema_version": 1,
  "job_id": "job_01J...",
  "status": "running",
  "advisor": "codex",
  "name": "payments-retry-design",
  "cwd": "/absolute/project/path",
  "redacted_command": "codex exec --json <prompt>",
  "worker_pid": 1234,
  "advisor_pid": 1235,
  "process_group_id": 1235,
  "started_at": "2026-07-18T10:00:00Z",
  "updated_at": "2026-07-18T10:01:31Z",
  "last_activity_at": "2026-07-18T10:01:24Z",
  "last_event": "item.completed",
  "max_runtime_seconds": 1800,
  "termination_grace_seconds": 10,
  "advisor_session_id": null,
  "advisor_exit_code": null,
  "error": null
}
```

Terminal state adds `finished_at`, `duration_seconds`, and applicable exit,
result, timeout, or cancellation information.

### Storage layout

Default state root:

```text
~/.local/state/crossagent/jobs/<job-id>/
├── state.json
├── stdout.log
├── stderr.log
├── result.md
├── prompt
└── cancel.request
```

On platforms without `~/.local/state`, resolve the path through a small
platform-aware helper and allow `--state-dir`/configuration overrides.

Storage requirements:

- Job directories use mode `0700` where supported.
- Prompt, logs, state, and result files use mode `0600` where supported.
- State writes use a temporary sibling file followed by `os.replace`.
- Metadata stores only redacted commands.
- The prompt must not appear in progress messages or job listings.
- Retention cleanup is explicit and must never delete active jobs.

## Process Supervisor Design

Create `src/crossagent/runner.py` and replace both current runner functions with
one lifecycle implementation.

### Responsibilities

- Launch the advisor with stdout and stderr pipes.
- Put the advisor in a separately controllable process group/session.
- Drain stdout and stderr concurrently using two reader threads and a queue.
- Forward or persist output incrementally without unbounded in-memory capture.
- Feed lines to the selected advisor parser.
- Update activity timestamps and summarized progress.
- Emit periodic heartbeats even if the advisor produces no output.
- Observe cancellation requests and maximum runtime.
- Terminate gracefully, then force-kill the complete process tree if necessary.
- Always return a structured `RunOutcome`.

Reader threads are preferred over `selectors` because the project supports
Python 3.9, has no dependencies, and should work on Windows as well as POSIX.

### Timer behavior

Use `time.monotonic()` for elapsed-time decisions and UTC timestamps only for
persistence and display.

Recommended initial defaults:

| Control | Agent-skill default | Foreground CLI default |
|---|---:|---:|
| Heartbeat interval | 15 seconds | 15 seconds |
| Bounded wait | 45 seconds | Not applicable |
| Idle warning | 120 seconds | 120 seconds |
| Maximum runtime | 1,800 seconds | Unlimited for compatibility |
| Termination grace | 10 seconds | 10 seconds |

The idle threshold initially emits warnings but does not kill the advisor. A
model can legitimately spend time without producing output. Maximum runtime is
the terminal safety boundary for agent-started jobs.

### Cancellation and cleanup

On POSIX:

1. Launch the advisor with `start_new_session=True`.
2. Send `SIGTERM` to its process group.
3. Wait for the configured grace period.
4. Send `SIGKILL` to the process group if anything remains.

On Windows:

1. Launch with `CREATE_NEW_PROCESS_GROUP`.
2. Attempt a graceful control signal/termination.
3. After the grace period, terminate the process tree using the safest
   platform-supported mechanism available without a new dependency.

Cancellation is complete only after process cleanup and terminal state
persistence. A signal sent to only the immediate advisor PID is insufficient
because agent CLIs can launch tool subprocesses.

### Crash reconciliation

`status` should reconcile a stale nonterminal state when the recorded worker no
longer exists. PID existence alone is not sufficient because PIDs can be reused;
store and compare a process identity token when the platform exposes one. If
identity cannot be confirmed safely, report `abandoned` with a diagnostic rather
than signaling an unrelated process.

## Advisor Event Architecture

Refactor `Advisor` so output format and session behavior are declarative without
equating streaming exclusively with Claude.

Suggested additions:

```python
result_parser: str                 # text, claude-stream, codex-jsonl
stream_args: tuple[str, ...]
resume_command: tuple[str, ...]    # or an advisor-specific command builder
session_event_field: str | None
```

Parser interface:

```python
class EventParser(Protocol):
    def consume_stdout(self, line: str) -> list[RunnerEvent]: ...
    def consume_stderr(self, line: str) -> list[RunnerEvent]: ...
    def finish(self, exit_code: int) -> ParsedResult: ...
```

The supervisor remains advisor-agnostic. Parsers may emit normalized events:

- `session.started`
- `advisor.progress`
- `advisor.message`
- `advisor.warning`
- `advisor.error`
- `advisor.completed`

Unknown native events should be retained in logs and ignored safely rather than
turning a compatible advisor upgrade into a wrapper crash.

### Codex adapter

Change the built-in invocation to include `--json` and add a `codex-jsonl`
parser. It should:

- Capture `thread_id` from `thread.started`.
- Treat `turn.started` and `item.*` as activity.
- Summarize command, MCP, plan, and message events without leaking prompt data.
- Extract the last completed `agent_message` as the final result.
- Treat `turn.failed` and `error` as explicit failure evidence.
- Treat `turn.completed` plus exit code zero as successful completion.
- Preserve malformed or unknown lines in diagnostic logs.
- Store the thread ID in the session registry/job state.
- Build follow-ups with `codex exec resume <SESSION_ID>`.

Do not adopt the Codex app-server as the core integration in this phase. Its
richer bidirectional protocol would add advisor-specific operational complexity,
while the CLI JSONL stream already supplies the events needed for liveness and
result capture.

### Claude adapter

Preserve the existing stream-json behavior while moving pipe management into the
shared supervisor. The parser should no longer own process reads or waits.

### Generic text adapter

Forward both stdout and stderr incrementally. The last complete stdout content
is the result on exit code zero. Heartbeats and lifecycle state remain available
even when the advisor has no structured output mode.

## CLI Contract

### Commands

| Command | Purpose |
|---|---|
| `crossagent start` | Create a job, launch a detached worker, and return its ID. |
| `crossagent wait` | Wait for at most N seconds and report current or terminal state. |
| `crossagent status` | Return current state without waiting. |
| `crossagent result` | Print the completed result. |
| `crossagent logs` | Read or follow advisor logs. |
| `crossagent cancel` | Request cancellation and optionally wait for cleanup. |
| Existing invocation | Run synchronously through the shared supervisor. |

### Exit behavior

- `start` returns zero only after durable metadata exists and the worker launch is
  confirmed.
- `status --json` returns zero whenever the job exists and a valid state can be
  reported, regardless of whether the job succeeded or failed.
- `wait --json` returns zero for `running` and terminal states unless
  `--require-complete` is supplied.
- `result` returns zero only when a successful result is available.
- Unknown job IDs, invalid state, and unreadable job storage are CLI errors.
- Job failure category and advisor exit code are carried in state instead of
  overloading a single shell code with all semantics.

### Machine-readable output

Every JSON response includes `schema_version`, `job_id`, and `status`. New fields
may be added compatibly; existing field meanings must not change without a schema
version increment.

Human progress continues on stderr. Commands that promise JSON must never mix
human text into stdout.

## Installed Skill Workflow

Replace the instruction to wait indefinitely with this flow:

1. Package the second-opinion prompt normally.
2. Run `crossagent start ... --json` and retain `job_id`.
3. Tell the user that the advisor started when the surrounding agent surface
   benefits from a progress update.
4. Run `crossagent wait JOB_ID --timeout 45 --json`.
5. If the state is `running`, report concise elapsed/idle status and perform
   independent useful work before polling again.
6. If the parent tool call itself times out, recover with
   `crossagent status JOB_ID --json`; do not start a duplicate advisor.
7. On `succeeded`, fetch `crossagent result JOB_ID` and synthesize both views.
8. On `failed`, `timed_out`, `cancelled`, or `abandoned`, report the terminal
   state and the actionable diagnostic.
9. Cancel only when the user requests it, the overall task is abandoned, or the
   configured maximum runtime expires.

The skill must keep the job ID visible in its own working context. A stable
session name is not a substitute for a unique job ID because multiple attempts
can use the same decision session.

## Implementation Phases

### Phase 1: Contract and state model

Files:

- Add `src/crossagent/jobs.py`.
- Add focused state tests in `tests/test_jobs.py`.
- Add this document's state schema and CLI examples to user documentation as
  behavior becomes available.

Work:

- Define enums/data classes for job state and terminal outcomes.
- Implement state-root resolution, private directories, and atomic writes.
- Implement state loading, schema validation, cancellation request creation,
  and stale-state reconciliation interfaces.
- Define JSON serialization and stable status responses.

Exit criteria:

- State transitions reject invalid regressions.
- Concurrent readers never observe partially written JSON.
- Prompt and unredacted command data do not appear in list/status output.

### Phase 2: Unified process supervisor

Files:

- Add `src/crossagent/runner.py`.
- Add `tests/test_runner.py` and fake-advisor fixtures.
- Refactor `src/crossagent/cli.py` to use the supervisor.

Work:

- Implement concurrent stdout/stderr readers.
- Implement incremental logging, activity timestamps, and heartbeats.
- Implement runtime, cancellation, signal, and process-tree cleanup behavior.
- Convert all outcomes into a structured result.
- Migrate existing synchronous Claude and text execution.

Exit criteria:

- A child that floods stderr while keeping stdout open cannot deadlock.
- Large outputs are streamed to disk/output rather than retained without bound.
- Interrupt and timeout paths clean up the process tree and return a terminal
  outcome.
- Existing synchronous tests remain compatible.

### Phase 3: Detached worker and job commands

Files:

- Add `src/crossagent/worker.py` or an internal worker entry point.
- Extend `src/crossagent/cli.py` with job subcommands.
- Extend `src/crossagent/__main__.py` only if worker dispatch requires it.
- Add `tests/test_job_cli.py`.

Work:

- Implement detached worker launch without putting prompt text in the worker's
  command line.
- Implement `start`, `wait`, `status`, `result`, `logs`, and `cancel`.
- Persist stdout, stderr, final result, and terminal diagnostics.
- Reconcile jobs after worker crashes or machine restarts.

Exit criteria:

- The process that ran `start` can exit while the worker continues.
- A separate process can retrieve status and result.
- Bounded wait returns on time with an explicit `running` state.

### Phase 4: Codex JSONL and resume

Files:

- Refactor `src/crossagent/advisors.py`.
- Add parser code in `src/crossagent/parsers.py` or a small parser package.
- Extend `src/crossagent/registry.py` if session metadata needs generalization.
- Add Codex parser and command tests.

Work:

- Enable `codex exec --json`.
- Parse progress, failure, final message, and thread ID events.
- Wire `codex exec resume` using stored thread IDs.
- Keep unknown event compatibility and diagnostic preservation.

Exit criteria:

- Codex activity updates `last_activity_at` while it runs.
- Codex failures cannot be mistaken for an empty successful result.
- A named Codex session can build a correct resume command.

### Phase 5: Skill migration and documentation

Files:

- Update `skills/crossagent/SKILL.md`.
- Update `skills/crossagent/references/second-opinion-protocol.md`.
- Update `README.md`, `CONTRIBUTING.md`, and `CHANGELOG.md`.
- Add a recovery-focused example under `examples/`.

Work:

- Make durable jobs the default workflow for agent-to-agent calls.
- Document direct foreground versus agent-mode timeout defaults.
- Document recovery after a parent timeout and cancellation behavior.
- Replace the blanket "no default timeout" claim with precise lifecycle
  semantics.

Exit criteria:

- Installed agents are instructed never to wait indefinitely in one tool call.
- Users can recover a job using only its ID and documented commands.

### Phase 6: Compatibility and release hardening

Work:

- Test macOS, Linux, and Windows process behavior where CI is available.
- Confirm user-config advisor overrides still layer correctly.
- Add state-schema forward-compatibility tests.
- Add retention/cleanup commands only after active-job safety is proven.
- Release behind an opt-in `start` workflow first, then switch the installed
  skill after one compatibility release.

Exit criteria:

- No regression in current foreground behavior.
- Job behavior is consistent across supported platforms or platform limitations
  are documented explicitly.

## Test Matrix

Tests must use local fake advisor processes and never call real model services.

| Scenario | Expected result |
|---|---|
| Immediate success | `succeeded`; exact result retrievable. |
| Nonzero advisor exit | `failed`; stderr and exit code preserved. |
| Periodic stdout | Activity advances; no false idle warning. |
| Periodic stderr only | Activity advances and progress is visible. |
| Completely silent advisor | Heartbeats continue; idle warning appears. |
| Runtime expires | Process tree ends; state becomes `timed_out`. |
| Cancellation request | Graceful cleanup; state becomes `cancelled`. |
| Child ignores termination | Forced cleanup after grace period. |
| Child spawns a grandchild | Entire process group/tree is removed. |
| More than 1 MB stderr | No pipe deadlock. |
| More than 1 MB stdout | No unbounded capture or truncation of persisted result. |
| Malformed JSONL | Diagnostic preserved; parser continues safely. |
| Codex `thread.started` | Session/thread ID stored. |
| Codex `turn.failed` | Explicit job failure. |
| Parent exits after `start` | Worker completes and result remains retrievable. |
| Worker crashes | Later status reconciles to `abandoned` or `failed`. |
| Concurrent status reads | Every reader observes valid JSON. |
| Duplicate cancel calls | Idempotent terminal outcome. |
| Unknown job ID | Clear CLI error without creating files. |
| Prompt contains secret-like text | Not present in status, command preview, or listing. |

Use short test durations with injected clocks for unit tests and sub-second
timeouts only in focused process integration tests. Avoid a suite dominated by
real sleeps.

## Rollout Strategy

1. **Compatibility release:** ship the supervisor and job commands while keeping
   the skill on the existing foreground invocation.
2. **Opt-in validation:** document `crossagent start` for early users and collect
   advisor/version-specific failure reports.
3. **Skill migration:** switch the installed skill to start-plus-bounded-wait
   after process cleanup and recovery pass on supported platforms.
4. **Codex hardening:** remove Codex's experimental label only after JSONL,
   failure, and resume behavior have compatibility coverage against supported
   Codex CLI versions.
5. **Default review:** evaluate whether direct foreground mode should retain an
   unlimited runtime after durable mode is established. Do not silently change
   it in the initial rollout.

## Observability and Diagnostics

Each job should make the following available without exposing prompt content:

- Advisor name and detected/version-reported executable when available.
- Start, last activity, update, and finish timestamps.
- Elapsed and idle durations.
- Last normalized event type.
- Worker/advisor lifecycle state.
- Advisor exit code and normalized failure category.
- Whether termination was graceful or forced.
- Session/thread ID where supported.
- Paths to private logs and result artifacts.

Human heartbeat example:

```text
[crossagent] job=job_01J... advisor=codex status=running elapsed=01:31 idle=00:07 last=item.completed
```

This line confirms liveness but does not solve parent wall-clock limits by
itself; durable job recovery remains mandatory.

## Risks and Mitigations

### Detached-process portability

Process groups and tree cleanup differ by platform. Keep platform code isolated,
test it directly, and mark a job `abandoned` rather than risking termination of
an unrelated reused PID.

### Job state contains sensitive advisor output

Use private permissions, avoid global temporary directories, redact command
metadata, document retention, and never include prompt/result text in status
listings by default.

### Advisor output formats change

Treat unknown events as compatible diagnostics, test parsers with recorded
synthetic fixtures, and separate lifecycle supervision from semantic parsing.

### Too-aggressive timeout defaults

Apply the finite 30-minute default to agent-started durable jobs first. Preserve
unlimited foreground behavior during the compatibility period and allow explicit
overrides.

### Duplicate jobs after a parent timeout

The skill must persist and reuse the returned job ID. A stable decision/session
name alone must not trigger a second run automatically.

### State accumulation

Defer automatic cleanup until retention behavior is safe. Later add explicit
`crossagent jobs prune --older-than ...` behavior that skips all nonterminal jobs.

## Acceptance Criteria

- `crossagent start` returns a durable job ID within one second after a successful
  local launch under normal conditions.
- `crossagent wait JOB --timeout 45` returns within approximately 47 seconds.
- A running job records a heartbeat at least every 15 seconds.
- The original parent process can exit without losing the advisor result.
- Another process can inspect status, follow logs, cancel, and retrieve results.
- Runtime expiry and cancellation remove the complete advisor process tree.
- Large simultaneous stdout and stderr streams cannot deadlock the runner.
- Every created job eventually reaches a terminal or reconciled `abandoned`
  state.
- Codex structured events produce progress, failures, a final answer, and a
  reusable thread ID.
- JSON commands never mix human progress into stdout.
- Prompt text and unredacted commands never appear in job listings or status.
- Existing synchronous CLI usage remains compatible.
- The full test suite uses no real advisor/model invocations and passes on all
  supported CI platforms.

## Definition of Done

The pain point is resolved when a Claude Code-to-Codex delegation can outlive the
call that started it, the parent always regains control after a bounded wait, and
the user can see one of two reliable outcomes:

1. A concrete running signal with a recoverable job ID; or
2. An explicit terminal result: succeeded, failed, timed out, cancelled, or
   abandoned.

At no point should "wait forever and hope the child eventually prints" remain
the required recovery strategy.
