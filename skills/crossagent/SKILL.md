---
name: crossagent
description: Use when the user wants a second opinion from another AI coding agent before deciding — "crossagent", "ask Claude", "hoi y voi Claude", "ask Codex", "debate with Claude", "get a second opinion", "what would Claude/Codex say". Runs crossagent to ask a peer agent (Claude, Codex, OpenCode, CommandCode, Gemini) via its CLI with named, resumable Claude sessions, streamed progress, a packaged context prompt, and a final synthesis comparing both viewpoints.
version: 0.1.0
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
5. Call the advisor and keep the process alive until it exits. Do independent local work while it runs; if the next step depends on the answer, wait and monitor progress instead of interrupting.
6. Compare outputs. Separate confirmed facts, claims needing verification, disagreements, unresolved questions, and the recommended synthesis. Do not outsource final judgment.
7. If material conflict remains, run a follow-up in the same session with the conflict framed explicitly. Stop when the remaining uncertainty is named and decision-useful.

## CLI Helper

Prefer the bundled helper for non-trivial second-opinion sessions:

```bash
crossagent --agent claude \
  --name "project-topic-decision" \
  --cwd "$PWD" \
  --prompt-file /tmp/crossagent.md
```

The helper:

- Invokes the chosen advisor's CLI (default `claude -p`).
- Streams progress by default (Claude `stream-json`); other advisors capture text output.
- Stores `session_id` by `advisor:name` in `~/.config/crossagent/sessions.json` and auto-resumes when the same `--name` is reused.
- Has no default timeout and no default cost cap; it waits until the advisor exits.
- Writes the advisor's final answer to stdout and progress/metadata to stderr.

Switch advisor with `--agent codex|opencode|commandcode|gemini`. List what's available with `crossagent --list-advisors`. Add or fix an advisor in `~/.config/crossagent/advisors.json` (no code change needed). Use `--new-session` to ignore a stored session, `--resume ID_OR_SEARCH` to force a resume target, `--fork-session` to branch, and `--model` to raise the model only when the decision justifies it.

## Prompting The Advisor

Read [references/second-opinion-protocol.md](references/second-opinion-protocol.md) when preparing a substantive prompt (architecture, prompt, eval, or debugging decisions).

Use XML-like sections. Ask the advisor to evaluate, inspect, compare, or trace; avoid "think step by step". Give it permission to gather more context with read-only commands, and ask it to cite file paths, commands, or assumptions behind claims.

## Operating Rules

- Keep secrets out of prompts, CLI args, logs, and copied context.
- Default to a routine model; raise `--model` only when the decision justifies it.
- No cost cap by default. Add one only when the user explicitly asks for a spend limit.
- For Claude: use `--safe-mode` when the advisor does not need repo config/skills/hooks; `--tools ""` for pure reasoning; allow read/search tools only when it must inspect files.
- Report both the advisor's answer and your own synthesis. Name where the two views agree, diverge, or leave uncertainty.
- Preserve long-running calls. Do not abandon a started second-opinion session unless the user cancels it or the process fails.
