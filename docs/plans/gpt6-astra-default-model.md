# Plan: GPT-6 Astra model support in crossagent (rev2, post fable-5.1 review)

## Context (verified 2026-09-08)
- OpenAI **GPT-6 Astra** released 2026-09-03. API model id: `gpt-6-astra`. Codex-native (`codex exec --model gpt-6-astra`). 1.05M ctx / 128K out, cutoff Apr 30 2026, $10/$50 per M. Successor to GPT-5.6 Sol.
- crossagent: `Advisor` frozen dataclass (`advisors.py`). Model flows only via `--model` at `cli.py:67-68`, placed on argv BEFORE the resume subcommand (`cli.py:91-101`). `args.model` is also persisted raw to the session registry (`cli.py:299`) and `command.json` (`cli.py:767`). `_coerce` (`advisors.py:146-151`) forwards any non-name key straight into `replace` — a `default_model` JSON key needs no special-casing (confirmed by review).

## Goal
Make GPT-6 Astra the default second-opinion model for the **codex** advisor, with a clean escape hatch and per-advisor aliases. Backward-compatible for every other advisor. Codex behaviour DOES change (documented).

## Design (revised per review)
1. `Advisor.default_model: str | None = None` (add after `model_flag`; all later fields have defaults and all constructions are keyword — safe).
2. **Per-advisor** aliases: `MODEL_ALIASES: dict[str, dict[str, str]]` keyed by advisor name. `{"codex": {"gpt6": "gpt-6-astra", "astra": "gpt-6-astra"}}`. **Drop any `fable` alias** — the Claude CLI resolves `fable` itself; pinning it is wrong.
3. `resolve_model(name: str | None, advisor_name: str) -> str | None`: guard `isinstance(name, str)`; `strip()`; return `None` if empty; else return per-advisor alias hit (case-insensitive) or the stripped value verbatim.
4. `codex` built-in gets `default_model="gpt-6-astra"`. All others stay `None`.
5. **Escape hatch:** the literal `--model default` (case-insensitive) suppresses the model flag entirely, so a user falls back to their own codex `config.toml`. `--model ""` (empty) also falls through to `default_model` (today's fall-through), while `--model default` means "no flag".
6. **Single source of truth:** `effective_model(advisor, args) -> str` in cli.py, used by `build_command`, the registry `record` call (`cli.py:299`), and `_write_command_info` (`cli.py:767`) — so the RESOLVED id (`gpt-6-astra`), not `""`/`gpt6`, is persisted everywhere.
7. **Do not switch model mid-thread on resume:** apply `default_model` only on a FRESH invocation. When a resume is being emitted (`args.resume` set, or stored_id used without `--new-session`), skip the default (explicit `--model` still applies exactly as today, unchanged position). This avoids `codex exec --model gpt-6-astra ... resume <thread>` forcing a switch on every stored thread.

## Tasks (implement as one coherent unit — coupled files)

### T1 — `advisors.py`
- Add `default_model` field.
- Add `MODEL_ALIASES` (per-advisor, codex only) + `resolve_model(name, advisor_name)` with the robustness guards above.
- Set codex `default_model="gpt-6-astra"`.
- Export `resolve_model`, `MODEL_ALIASES` (module-level; no `__all__` gymnastics needed).

### T2 — `cli.py`
- Add `effective_model(advisor, args, *, is_resume) -> str`:
  - `SENTINEL "default"` → return `""` (suppress).
  - `raw = args.model or ("" if is_resume else advisor.default_model or "")`.
  - return `advisors.resolve_model(raw, advisor.name) or ""`.
- In `build_command` (line ~67): compute `is_resume` (mirror the resume conditions at 91-101), call `effective_model`, emit `[model_flag, chosen]` only when `chosen` and `advisor.model_flag`.
- At `cli.py:299` and `cli.py:767`, persist `effective_model(advisor, args, is_resume=...)` instead of raw `args.model`. (Both are post-build; reuse a value computed once and thread it, or recompute — recompute is fine, pure function.)
- Update `--model` help (line ~163): mention aliases, that empty falls back to advisor `default_model` then the CLI default, and that `default` suppresses the flag.
- `_print_advisors` (line ~208): print each advisor's `default_model` when set, so users see codex → gpt-6-astra.

### T3 — Tests (`tests/test_advisors.py`, `tests/test_cli.py`)
- `resolve_model`: codex alias hit (case-insensitive) → `gpt-6-astra`; same alias under `claude` → passthrough verbatim (no cross-advisor leak); non-str input → `None`; whitespace-only → `None`; unknown → verbatim stripped.
- codex `default_model == "gpt-6-astra"`; every other built-in `None`.
- `build_command`: codex + no `--model` (fresh) → includes `--model gpt-6-astra`; codex + `--model gpt6` → `gpt-6-astra`; codex + `--model default` → NO `--model` flag; claude + no model → no flag (unchanged); claude + `--model gpt6` → passes `gpt6` verbatim.
- Resume: codex with a stored thread (resume path) + no `--model` → NO forced `--model` (default skipped); explicit `--model X` on resume → `--model X` still present before `resume`.
- Persistence: registry `record` and `command.json` receive `gpt-6-astra` (resolved), not `""`/`gpt6` — assert via the `start`/dispatch path and `_write_command_info`.
- `"default_model": null` user-config override clears the codex default.
- Job `start` path via `_add_advisor_args` exercises `build_command` too — one test through that entry point.

### T4 — Docs
- `README.md`: model-selection subsection — per-advisor aliases, `default_model`, codex defaults to `gpt-6-astra`, `--model default` escape hatch, one JSON override example (`"codex": {"default_model": null}` to opt out). Update the line-282 example to show `default_model`.
- `CHANGELOG.md`: unreleased `feat(models): per-advisor default_model + aliases; codex second opinions default to gpt-6-astra (override via advisors.json or --model default)`.
- No version bump / release.

## Constraints
- Backward compatible for all advisors EXCEPT codex (documented behaviour change).
- No new deps. Python 3.9+ (`from __future__ import annotations` already present).
- Surgical diffs; no unrelated refactors/formatting. No secrets. No AI/tool attribution in commits/MR.

## Verification
- `python -m pytest -q` green (was 298 passed).
- Manual: `python -m crossagent --list` shows codex default; a dry command build (via test) shows `--model gpt-6-astra`.

## Out of scope (follow-up)
- Reasoning-effort passthrough (`--reasoning low|medium|high|xhigh|max`), cost/pricing tracking, wiring GPT-6 into Ringkas repos.
