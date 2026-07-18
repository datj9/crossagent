# crossagent

**Get a second opinion from another AI agent — without leaving the one you're in.**

`crossagent` lets your coding agent (Claude Code, Codex, Cursor, Cline, OpenCode, CommandCode…) pause and ask a *different* agent to independently evaluate a hard call — an architecture decision, a gnarly bug, a prompt rewrite — then hands you both viewpoints so **you** decide. One agent proposes; another one challenges. You get the disagreement, not just an echo.

```bash
crossagent --agent claude --name payments-retry-design --prompt-file /tmp/decision.md
```

- 🤝 **Cross-agent, by design.** Ask **Claude, Codex, OpenCode, CommandCode, or Gemini** — from inside whichever agent you already use.
- 🧠 **A teammate, not an oracle.** Ships a second-opinion protocol that forces an evidence-backed critique with cited files, risks, and a recommendation — then a synthesis step, so the second opinion sharpens *your* judgment instead of replacing it.
- 🔁 **Named, resumable sessions.** Claude sessions and Codex threads resume by `advisor:name`. Fork to explore a branch.
- 📡 **No delegated job is ever silently dropped.** Every `start` persists durable state on disk; if the parent times out or the worker dies, the job resolves to an explicit terminal state (`succeeded`/`failed`/`timed_out`/`cancelled`/`abandoned`) and stays recoverable by job ID — you never lose the answer, or the fact that there was a job at all.
- 📊 **Full observability on every delegation.** `crossagent dashboard` opens a live localhost web UI over all jobs; `crossagent list` is the same dashboard in the terminal; `status --json` reports progress; `logs --follow` tails the advisor's raw output in real time.
- 🧩 **Agent Skill + CLI.** Installs as an [Agent Skill](https://agentskills.dev) *and* a standalone `crossagent` command. Zero runtime dependencies.
- 🔓 **Local & open.** Runs entirely on your machine against CLIs you already have. MIT licensed.

---

## Why

Coding agents are confident. That's the problem. The same model that writes the code also reviews it, so a plausible-but-wrong call sails straight through. The fix engineers already use with each other — *"let me get a second pair of eyes"* — works for agents too, but only if the second agent is **actually different** and is briefed well enough to disagree on the merits.

`crossagent` makes that a one-liner. Package the decision, hand it to a peer agent, get back a structured critique — Position / Evidence / Risks / Recommendation / Unresolved — and reconcile it with your own view before you commit.

## Install

Install the CLI from PyPI (using `pipx` keeps command-line applications isolated):

```bash
pipx install crossagent
# or: pip install crossagent

crossagent --list-advisors
```

You also need at least one advisor CLI on your PATH — e.g. [`claude`](https://docs.claude.com/claude-code), `codex`, `opencode`, `commandcode`, or `gemini`.

To install both the CLI and the Agent Skill from the repository:

```bash
git clone https://github.com/datj9/crossagent.git
cd crossagent
./install.sh
```

The installer checks the supported agent config dirs (`~/.claude`, `~/.codex`, `~/.config/opencode`, `~/.commandcode`, `~/.cursor`), installs the skill into those present, and installs the CLI via `pipx`/`pip`.

## Quick start

Write a compact decision brief, then ask a peer:

```bash
cat > /tmp/decision.md <<'EOF'
<decision>Should the payment retry live in the worker or the API layer?</decision>
<current_state>Retries currently inline in the API handler; p99 latency regressed 40%.</current_state>
<constraints>No new infra this quarter. Must stay idempotent.</constraints>
<evidence>src/api/payments.ts:88, worker/queue.ts:12, load test in bench/2026-07.md</evidence>
<questions>1. Which layer, and why? 2. Strongest counterargument? 3. Cheapest validation?</questions>
EOF

crossagent --agent claude --name payments-retry-design --cwd "$PWD" --prompt-file /tmp/decision.md
```

The advisor's answer prints to **stdout**; progress and session metadata go to **stderr**. Continue the same decision later — context is remembered:

```bash
crossagent --name payments-retry-design --prompt-file /tmp/followup.md   # auto-resumes
crossagent --name payments-retry-design --fork-session --prompt-file /tmp/alt.md  # branch it
```

Or let your agent do it for you — just say *"ask Claude about this"* / *"hỏi ý với Claude"* / *"get a second opinion from Codex"* and the skill fires.

## Durable jobs (survives parent timeout)

When the calling agent has a wall-clock limit (Claude Code, Codex, etc.), use the
durable job workflow. The job continues even after the parent tool call ends.

```bash
# Start a durable job.
crossagent start \
  --agent codex \
  --name payments-retry-design \
  --cwd "$PWD" \
  --prompt-file /tmp/decision.md \
  --json

# Returns {"schema_version":1,"job_id":"job_...","status":"running",...}
```

Then poll with bounded waits, never indefinitely:

```bash
crossagent wait job_20260718T100000_a1b2c3d4 --timeout 45 --json
```

If the parent times out, recover by job ID — **do not start a duplicate**:

```bash
crossagent status job_20260718T100000_a1b2c3d4 --json
crossagent result job_20260718T100000_a1b2c3d4
```

### No silent drops

A delegated job can never disappear without a trace:

- Job state is persisted to disk (`~/.local/state/crossagent/jobs/<id>/`) before
  the worker even launches, and every transition is written atomically.
- If the worker process dies, the next `status`, `wait`, or `list` reconciles the
  stale `running` state to an explicit `abandoned` — with the reason in `error`.
- Every outcome is one of seven explicit states: `pending`, `running`,
  `succeeded`, `failed`, `timed_out`, `cancelled`, `abandoned`. There is no
  "gone" state.
- Unreadable job directories are reported on stderr during `list`, never
  skipped silently.

### Observability: see every job's progress and output

Open the **web dashboard** — a live view of all jobs with per-job status and
logs, served from the Python standard library (zero dependencies, loopback
only by default):

```bash
crossagent dashboard                 # serves http://127.0.0.1:8642/ and opens your browser
crossagent dashboard --port 9000 --no-open
```

The page auto-refreshes every 3 s; click a job to see its detail (status,
elapsed, idle, last event, error) and its live stdout/stderr. The prompt is
never served — only status metadata and advisor output logs.

Prefer the terminal? `crossagent list` is the same dashboard as a table —
including jobs whose ID you lost:

```bash
crossagent list                      # table: job id, status, advisor, elapsed, idle, name
crossagent list --status running     # only active jobs
crossagent list --limit 10 --json    # newest 10, machine-readable
```

```
JOB ID                             STATUS     ADVISOR       ELAPSED   IDLE  NAME
job_20260718T104500_ee55ff66       running    codex           3m07s     8s  order-workflow-design
job_20260718T100000_aa11bb22       succeeded  claude          2m41s      -  payments-retry-design
```

Per-job progress and output:

```bash
crossagent status <job-id> --json    # elapsed_seconds, idle_seconds, last_event
crossagent logs <job-id> --follow    # tail the advisor's live output
crossagent logs <job-id> --stream stderr   # advisor diagnostics
```

`idle_seconds` distinguishes a healthy long-running advisor (recent output) from
a silent one; the worker also emits heartbeats every 15 s and an idle warning at
120 s to the job's log.

Full reference: [`skills/crossagent/SKILL.md`](skills/crossagent/SKILL.md) and
[examples/durable-job-recovery.md](examples/durable-job-recovery.md).

### Timer defaults

| Control | `crossagent start` | Foreground CLI |
|---|---|---|
| Maximum runtime | 1,800 s (30 min) | Unlimited |
| Heartbeat interval | 15 s | 15 s |
| Idle warning | 120 s | 120 s |
| Termination grace | 10 s | 10 s |
| Bounded wait default | 45 s | N/A |

The idle threshold warns via stderr but never kills the advisor. Only
`--max-runtime` or explicit cancellation (`crossagent cancel`) terminates.

## How it works

```
your agent ──▶ crossagent CLI ──▶ peer agent's CLI (claude -p / codex exec / …)
     ▲              │                     │
     └── synthesis ─┴──── streamed ◀──────┘
         (you reconcile both views)       result + session id
```

1. You (or your agent) package the decision using the [second-opinion protocol](skills/crossagent/references/second-opinion-protocol.md).
2. **Foreground:** `crossagent` runs the advisor synchronously, streams progress to stderr, and prints the answer to stdout.
3. **Durable:** `crossagent start` spawns a detached worker that runs the advisor, persists state to disk, and returns a `job_id` immediately. Then `crossagent wait`/`status`/`result` retrieve the outcome, and `crossagent list` shows all jobs at a glance.
4. For session-capable advisors (Claude with `stream-json`, Codex with `--json` JSONL), it stores the `session_id`/`thread_id` keyed by `advisor:name` so the next turn resumes.

## Advisors

| Advisor | Command | Output | Sessions |
|---|---|---|---|
| `claude` | `claude -p` | stream-json (JSON events) | resume, name, fork, streamed |
| `codex` | `codex exec --json` | codex-jsonl (JSONL events) | resume, thread-id storage |
| `opencode` | `opencode run` | text | — |
| `commandcode` | `commandcode -p` | text | — |
| `gemini` | `gemini -p` | text | — |

Experimental advisors (codex, opencode, commandcode, gemini) ship best-effort
default flags.  Codex is experimental but has full `start`/`wait`/`result` and
resume support.  If your install differs, fix them without touching code — see
below.

### Add or fix an advisor

Create `~/.config/crossagent/advisors.json`:

```json
{
  "advisors": {
    "codex": { "executable": "codex", "base_args": ["exec", "--full-auto"] },
    "myllm": { "executable": "myllm", "prompt_delivery": "flag:-q", "model_flag": "--model" }
  }
}
```

Fields layer onto the built-ins, so you only specify what differs. `prompt_delivery` is `dashdash` (prompt after `--`), `positional` (prompt as last arg), or `flag:<flag>` (prompt is the value of a flag).

## Skill usage inside an agent

Once installed, the skill auto-triggers on phrases like *"ask Claude"*, *"debate with Claude"*, *"ask Codex"*, *"second opinion"*, *"hỏi ý với Claude"*. The agent packages context, runs `crossagent`, and reports both views. See [`skills/crossagent/SKILL.md`](skills/crossagent/SKILL.md).

The skill uses the **durable job workflow** by default for agent-to-agent calls. See the "Durable Job Workflow" section in the skill file for the complete 9-step flow. The synchronous foreground shortcut is documented there for quick interactive use.

## Examples

- [Architecture decision](examples/architecture-decision.md)
- [Debugging second opinion](examples/debug-second-opinion.md)
- [Durable job recovery](examples/durable-job-recovery.md)

## Security

- Secrets never belong in prompts, CLI args, logs, or copied context — the protocol says so and you should enforce it.
- Second-opinion sessions run read-only by convention; for Claude use `--tools ""` for pure reasoning or restrict with `--allowedTools`.
- Everything runs locally against CLIs you already trust. `crossagent` adds no network calls of its own.
- Job state files use private permissions (0700 directories, 0600 files).
- The web dashboard binds to loopback (`127.0.0.1`) by default, validates job IDs against a strict pattern (no path traversal), and never serves the prompt file — only status metadata and advisor output logs.

## Contributing

Issues and PRs welcome — especially hardening the experimental advisors against real installs. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT © Dat Nguyen. See [LICENSE](LICENSE).
