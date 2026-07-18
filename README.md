# crossagent

**Get a second opinion from another AI agent — without leaving the one you're in.**

`crossagent` lets your coding agent (Claude Code, Codex, Cursor, Cline, OpenCode, CommandCode…) pause and ask a *different* agent to independently evaluate a hard call — an architecture decision, a gnarly bug, a prompt rewrite — then hands you both viewpoints so **you** decide. One agent proposes; another one challenges. You get the disagreement, not just an echo.

```bash
crossagent --agent claude --name payments-retry-design --prompt-file /tmp/decision.md
```

- 🤝 **Cross-agent, by design.** Ask **Claude, Codex, OpenCode, CommandCode, or Gemini** — from inside whichever agent you already use.
- 🧠 **A teammate, not an oracle.** Ships a second-opinion protocol that forces an evidence-backed critique with cited files, risks, and a recommendation — then a synthesis step, so the second opinion sharpens *your* judgment instead of replacing it.
- 🔁 **Named, resumable Claude sessions.** Reuse a name to continue the same decision; fork to explore a branch. No re-explaining context every turn.
- 📡 **Long-running & streamed.** No default timeout, no default cost cap. It waits until the advisor finishes and streams progress as it goes.
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

## How it works

```
your agent ──▶ crossagent CLI ──▶ peer agent's CLI (claude -p / codex exec / …)
     ▲              │                     │
     └── synthesis ─┴──── streamed ◀──────┘
         (you reconcile both views)       result + session id
```

1. You (or your agent) package the decision using the [second-opinion protocol](skills/crossagent/references/second-opinion-protocol.md).
2. `crossagent` builds the right command for the chosen advisor and keeps it alive until it exits.
3. For session-capable advisors, it stores the `session_id` keyed by `advisor:name` so the next turn resumes.
4. You compare the two viewpoints and make the call.

## Advisors

| Advisor | Command | Status | Sessions |
|---|---|---|---|
| `claude` | `claude -p` | ✅ full | resume, name, fork, streamed |
| `codex` | `codex exec` | 🧪 experimental | text output |
| `opencode` | `opencode run` | 🧪 experimental | text output |
| `commandcode` | `commandcode -p` | 🧪 experimental | text output |
| `gemini` | `gemini -p` | 🧪 experimental | text output |

Experimental advisors ship best-effort default flags. If your install differs, fix them without touching code — see below.

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

## Examples

- [Architecture decision](examples/architecture-decision.md)
- [Debugging second opinion](examples/debug-second-opinion.md)

## Security

- Secrets never belong in prompts, CLI args, logs, or copied context — the protocol says so and you should enforce it.
- Second-opinion sessions run read-only by convention; for Claude use `--tools ""` for pure reasoning or restrict with `--allowedTools`.
- Everything runs locally against CLIs you already trust. `crossagent` adds no network calls of its own.

## Contributing

Issues and PRs welcome — especially hardening the experimental advisors against real installs. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT © Dat Nguyen. See [LICENSE](LICENSE).
