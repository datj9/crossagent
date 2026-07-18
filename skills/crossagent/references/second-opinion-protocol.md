# Second-Opinion Protocol

Use this reference when a second opinion affects architecture, prompt behavior,
debugging strategy, eval interpretation, or another decision where the second
opinion must be evidence-backed.

## Context Package

Provide enough context for the advisor to form an independent view:

- Target decision: the answer the two agents are trying to settle.
- Current state: what exists, what has been observed, and what already works.
- Constraints: time, scope, ownership boundaries, safety rules, explicit cost caps if any, and anti-scope.
- Evidence: file paths, command outputs, logs, test failures, screenshots, artifacts, or prior conclusions.
- Candidate options: the realistic options you see so far, including the boring/default option when relevant.
- Read-only collection plan: commands or files the advisor may inspect to verify claims.
- Output contract: the exact comparison format you need to synthesize a decision.

Keep the package compact. Do not paste large files when file paths and targeted read instructions are enough.

## Session Naming

Use names that are stable across turns and narrow enough to avoid cross-topic contamination:

- `brandmind-slide-service-architecture`
- `atlas-kb-ingest-wakeup-boundary`
- `prompt-eval-rubric-format-drift`

Reuse a name when the same decision continues. Send a short delta summary when resuming:

```text
We are resuming the same decision. New evidence since the last turn:
- ...
- ...
Please update or revise your prior recommendation.
```

Start a new name when the user changes the problem, project, or decision criterion. Use `--fork-session` when exploring an alternative line while preserving prior context.

## Prompt Template

```xml
<role>
You are a senior teammate in a joint decision. Evaluate the problem
independently, inspect additional context when useful, and return a
decision-useful critique. Do not edit files unless explicitly asked. Treat this
as a read-only review.
</role>

<decision>
[The decision to settle.]
</decision>

<current_state>
[What is already known. Include exact dates for time-sensitive state.]
</current_state>

<constraints>
[Scope, anti-scope, safety, ownership, deadlines, and any explicit cost cap.]
</constraints>

<evidence>
[File paths, logs, command outputs, prior findings. Mark unverified claims.]
</evidence>

<context_collection>
You may gather more context using read-only inspection such as pwd, ls, find,
rg, sed, awk, wc, git status, git diff, git log, git show, and opening relevant
files. Avoid commands that modify files, start services, install packages, call
paid external services, or expose secrets. If needed context is unavailable,
name the missing evidence and proceed with caveats.
</context_collection>

<questions>
1. What is your independent diagnosis or architecture framing?
2. Which option do you recommend, and why?
3. What are the strongest risks, failure modes, or counterarguments?
4. What evidence supports your claims? Cite files, commands, or assumptions.
5. What remains unresolved, and what is the cheapest next validation step?
</questions>

<response_format>
Return concise markdown with these sections:
- Position
- Evidence
- Risks And Counterarguments
- Recommendation
- Unresolved Questions
</response_format>
```

## Running The Helper

Prepare the prompt in a temporary file, then call:

```bash
crossagent --agent claude \
  --name "stable-topic-name" \
  --cwd "$PWD" \
  --model sonnet \
  --prompt-file /tmp/crossagent.md
```

Useful variants:

```bash
# Pure reasoning, no tools (Claude).
crossagent --name "stable-topic-name" --tools "" --prompt-file /tmp/crossagent.md

# Ask a different advisor.
crossagent --agent codex --name "stable-topic-name" --prompt-file /tmp/crossagent.md

# Resume a known session directly.
crossagent --resume "SESSION_ID_OR_SEARCH_TERM" --prompt-file /tmp/followup.md

# Branch the same second-opinion history.
crossagent --name "stable-topic-name" --fork-session --prompt-file /tmp/variant.md

# Add a spend cap only when the user explicitly asks (Claude via --raw-arg).
crossagent --name "stable-topic-name" --raw-arg --max-budget-usd --raw-arg 0.50 \
  --prompt-file /tmp/capped.md
```

## Synthesizing The Answer

1. Verify file/path/command claims when they matter and are cheap to check.
2. Separate agreement, disagreement, and new information.
3. Prefer the recommendation that best satisfies the goal, constraints, and evidence — not the louder or more recent answer.
4. Run a follow-up only when the conflict changes the decision or a missing fact is cheap to inspect.
5. Finalize with the chosen path, rationale, risks, and next action.

## Failure Handling

- If a spend cap makes the advisor stop before the decision settles, remove or raise it deliberately and retry with a tighter context package.
- For Claude streaming, `stream-json` requires `--verbose` (the helper adds it automatically).
- If a variadic option like `--tools` precedes the prompt, the helper inserts `--` so the prompt is not swallowed.
- If a session was created without persistence, do not expect it to resume even if a `session_id` was returned.
- Experimental advisors (codex, opencode, commandcode, gemini) use best-effort default flags. If your install differs, correct them in `~/.config/crossagent/advisors.json`.
