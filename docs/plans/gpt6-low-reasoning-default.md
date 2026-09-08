# Plan: default GPT-6 Astra to `low` reasoning effort (rev2, post fable-5.1 review)

## Context (verified 2026-09-08)
- GPT-6 Astra reasoning ladder (codex 0.145 accepts): `minimal | low | medium | high | xhigh | max`.
- **This machine's `~/.codex/config.toml` sets `model_reasoning_effort = "high"`.** So when crossagent asks codex for a GPT-6 second opinion it currently runs at *high* — expensive. Defaulting crossagent's own GPT-6 asks to `low` saves cost; low is strong for a reviewer (AA Coding Index 67 > Sol, ~1/3 tokens). The `-c model_reasoning_effort=low` override is visible in the `[crossagent] running:` stderr line, so it's not a hidden change of the user's config.
- codex CLI reads reasoning from config.toml; override with the GLOBAL flag `-c model_reasoning_effort=<level>` (must precede the `resume` subcommand — confirmed: build_command's model block at cli.py:122 precedes the resume block at :147, and both `codex exec` and `codex exec resume` accept `-c`).
- crossagent state (post PR #24 on `main`): `codex` advisor has `default_model="gpt-6-astra"`. `effective_model(advisor, args, *, is_resume)` (cli.py:92-107) injects the default only on a FRESH call and only when the user didn't override; **explicit `--model` survives resume** (locked by two tests). `reg.record` (registry.py:61-70) is keyword-only with a fixed field list. `--model` is defined TWICE independently: cli.py:222 (ask) and cli.py:678 (job start). Escalation (`escalate._build_child_argv`) and verification (`verify.build_verifier_command`) build argv independently and both skip `default_model` today.

## Goal
crossagent's default GPT-6 Astra second opinions run at `low` reasoning effort for cost, everywhere crossagent sends that default model (ask, job start, escalation, verification), while staying trivially overridable and never switching effort mid-thread.

## Design
1. `Advisor.default_reasoning_effort: str | None = None` (codex: `"low"`; others `None`).
2. `Advisor.reasoning_effort_config_key: str | None = None` (codex: `"model_reasoning_effort"`).
3. `Advisor.reasoning_args(level) -> tuple[str,...]`: `()` when `level` falsy or `reasoning_effort_config_key is None`; else `("-c", f"{key}={level}")`.
4. **Valid levels:** `VALID_REASONING_EFFORTS = frozenset({"minimal","low","medium","high","xhigh","max"})` (module const in advisors.py, since codex owns the vocabulary).
5. CLI `--reasoning` flag (default `""`): a level, or `default` sentinel (case-insensitive) to suppress and let codex config.toml win.
6. `effective_reasoning(advisor, args, *, is_resume, chosen_model) -> str` in cli.py — single source of truth:
   - non-str/blank handling like `effective_model`; **lowercase** the level.
   - `--reasoning default` → `""` (suppress).
   - explicit `--reasoning X` → validate ∈ VALID (else `argparse`-style error, exit 2); return X **on both fresh AND resume** (mirrors how explicit `--model` survives resume — FINDING 1 fix).
   - no flag → `advisor.default_reasoning_effort` only when `not is_resume` AND the default model is actually in play: compare `resolve_model(advisor.default_model, advisor.name)` (case-insensitively) against `chosen_model` (FINDING 4 fix — handles `default_model="gpt6"` config and `--model GPT-6-Astra`). Else `""`.
   - explicit level on an advisor whose `reasoning_effort_config_key is None` → keep the level (so `reasoning_args` no-ops) BUT emit a stderr warning that it was dropped (FINDING 7). Warning lives at the build site, mirroring `_apply_mode`.

## Tasks

### T1 — `advisors.py`
- Add `default_reasoning_effort`, `reasoning_effort_config_key` fields (after `default_model`).
- Add `reasoning_args` method + `VALID_REASONING_EFFORTS` const.
- codex builtin: `default_reasoning_effort="low"`, `reasoning_effort_config_key="model_reasoning_effort"`; update `notes`.
- `_coerce` unchanged (verified: `replace` accepts any real field). No special-casing.

### T2 — `cli.py`
- `_REASONING_SENTINEL_DEFAULT = "default"`.
- `effective_reasoning(...)` per design (validation, lowercase, gates, resume symmetry).
- In `build_command`: after model block, reuse the already-computed `chosen_model` + `is_resume`; compute level; if level set and advisor has no config key → stderr warn; else `cmd.extend(advisor.reasoning_args(level))`. Ordering: emitted before stream/resume args (so `-c` precedes `resume`).
- Add `--reasoning` (default `""`) at BOTH definition sites (cli.py:222 ask, cli.py:678 job start — FINDING 8) with help: "codex/GPT-6 reasoning effort: minimal|low|medium|high|xhigh|max. Empty = low when sending the default gpt-6-astra model. 'default' sends no override so codex config.toml wins."
- `_print_advisors`: print `default_reasoning_effort` next to default model when set.
- **Persistence:** thread the effective level into `_dispatch`'s `reg.record(...)` and `_write_command_info`. On resume `effective_reasoning` returns `""` (no default) — document that, matching the existing model quirk.

### T2b — `registry.py` (FINDING 2)
- Add `reasoning: str = ""` keyword to `record(...)`; write `"reasoning"` into the session dict. Keep additive (default `""`), so existing readers/`worker._load_command` tolerate it. Preserve-prior-value-on-resume is out of scope (inherits the existing model-overwrite quirk; note it).

### T2c — `escalate.py` + `verify.py` (FINDING 3)
- `_build_child_argv` and `build_verifier_command` are FRESH GPT-6 delegations → apply the same low default. Add: after the model line, `cmd.extend(advisor.reasoning_args(<level>))` where level = `advisor.default_reasoning_effort` iff `resolve_model(advisor.default_model)==model` (reuse a shared helper `advisors`-level or a tiny local). Both call sites pass `is_resume=False` semantics (always fresh). No `--reasoning` plumbing into these paths (they don't take user flags today) — default only.

### T3 — Tests
`tests/test_advisors.py`:
- `reasoning_args`: codex+"low" → `("-c","model_reasoning_effort=low")`; codex+""/None → `()`; claude+"low" → `()`.
- codex `default_reasoning_effort=="low"`, `reasoning_effort_config_key=="model_reasoning_effort"`; other builtins both `None`.
- `_coerce` override to `"high"` AND to `null` (FINDING 10).

`tests/test_cli.py`:
- fresh codex, no flags → `-c model_reasoning_effort=low` AND `--model gpt-6-astra`.
- `--reasoning high` → `=high`; `--reasoning HIGH` → `=high` (lowercased); invalid `--reasoning turbo` → exit 2 (FINDING 6).
- `--reasoning default` → no `-c`; `--reasoning default` + `--model gpt6` → `--model` present, no `-c` (missing-test).
- `--model o3` + no `--reasoning` → no forced low (gate); `--model GPT-6-Astra` verbatim → still low (case-insensitive gate, FINDING 4); user config `default_model:"gpt6"` → low still applies (FINDING 4).
- Resume (stored codex thread) + no flags → no forced low; **explicit `--reasoning high` on resume → `=high` present** and `-c` precedes `resume` token (FINDING 1 + 9).
- claude fresh → no `-c` (unchanged).
- explicit `--reasoning low` on an advisor with no config key → warns on stderr, no `-c` (FINDING 7).
- Job `start` path via `_add_advisor_args` sees `--reasoning`.

`tests/test_registry.py`: `record(..., reasoning="low")` persists it; default `""` when omitted; `worker._load_command` tolerates the key.

`tests/test_escalate.py` / `tests/test_verify.py`: child/verifier argv for codex+default model contains `-c model_reasoning_effort=low`; for a non-default model, none.

### T4 — Docs
- `README.md`: extend model-selection — reasoning effort, codex/GPT-6 defaults to `low` for cost, ladder incl. `minimal`, `--reasoning` + `--reasoning default`, advisors.json opt-out. One example. Note it overrides a config.toml effort setting for crossagent's own asks only.
- `CHANGELOG.md`: unreleased `feat(models): crossagent's default gpt-6-astra second opinions run at low reasoning effort (cheaper; override with --reasoning or advisors.json)`.
- No version bump.

## Constraints
- Backward compatible for all advisors except codex-with-default-model (documented). No new deps. Python 3.9+. Surgical diffs. No secrets. No AI/tool attribution in commits/MR.

## Verification
- `python -m pytest -q` green.
- Ruff: no NEW findings vs `main`.
- Sanity unit test: fresh default codex ask builds `codex exec --skip-git-repo-check --model gpt-6-astra -c model_reasoning_effort=low --json <prompt>`.

## Out of scope
- Per-task-type reasoning presets, token/cost accounting, wiring GPT-6 into Ringkas repos, preserving prior stored model/reasoning on resume (pre-existing quirk).
