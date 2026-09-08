"""Advisor registry: how to invoke each peer coding-agent CLI.

An *advisor* is a peer AI agent you ask for a second opinion (Claude, Codex,
OpenCode, CommandCode, Gemini, ...). Each entry is a small, declarative spec that
tells the runner how to build the command line, where the prompt goes, and how to
read the result back out. Built-ins ship sane defaults; users override or add their
own via ~/.config/crossagent/advisors.json without touching code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# Where the prompt string is placed on the argv.
#   "dashdash"   -> [..., "--", prompt]        (claude: everything after -- is the prompt)
#   "positional" -> [..., prompt]              (codex exec / opencode run)
#   "flag:-p"    -> [..., "-p", prompt]        (gemini: prompt is the value of -p)
PROMPT_DELIVERIES = frozenset({"dashdash", "positional"})

# How to read the advisor's answer back out of its stdout.
#   "claude-stream" -> parse newline-delimited stream-json events, take the result event
#   "text"          -> capture raw stdout as the answer
RESULT_PARSERS = frozenset({"claude-stream", "codex-jsonl", "commandcode-json", "text"})

USER_CONFIG = Path.home() / ".config" / "crossagent" / "advisors.json"

# Reasoning-effort ladder. codex owns this vocabulary (GPT-6 Astra accepts every
# rung; codex 0.145 rejects anything else), so the whitelist lives next to the
# advisor specs rather than in the CLI.
VALID_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max"}
)


@dataclass(frozen=True)
class Advisor:
    """Declarative recipe for invoking one peer-agent CLI."""

    name: str
    executable: str
    base_args: tuple[str, ...] = ()
    invoke_args: tuple[str, ...] = ()
    prompt_delivery: str = "positional"  # "dashdash" | "positional" | "flag:<flag>"
    model_flag: str | None = None
    default_model: str | None = None
    # Reasoning effort crossagent asks for when it sends ``default_model`` on a
    # FRESH call, and the advisor-native config key that carries it. ``None``
    # means "this advisor has no reasoning knob crossagent knows how to set", so
    # the advisor CLI's own configuration is left untouched.
    default_reasoning_effort: str | None = None
    reasoning_effort_config_key: str | None = None
    stream_args: tuple[str, ...] = ()
    json_args: tuple[str, ...] = ()
    resume_flag: str | None = None
    session_name_flag: str | None = None
    fork_flag: str | None = None
    result_parser: str = "text"
    resume_command: tuple[str, ...] | None = None
    session_event_field: str | None = None
    # Flag that requests a machine-checkable JSON output contract for the
    # independent verification pass (slice S5). ``None`` means the advisor has no
    # such contract, so a verifier built on it degrades to parsing a JSON verdict
    # out of the answer text (D4 graceful degradation — never a hard failure).
    # Claude exposes ``--json-schema`` (research finding [6]: the payload lands in
    # ``structured_output``); no other built-in advisor has a verified equivalent.
    json_schema_flag: str | None = None
    # Permission-mode expansion for delegation (write vs plan). ``--write`` and
    # ``--plan`` are advisor-agnostic *intents*; each advisor declares the concrete
    # flags that realise them so an orchestrator (or the escalation ladder) never
    # has to know an advisor's native permission syntax. An empty tuple means the
    # advisor has no distinct flags for that intent: ``--plan`` degrades to the
    # advisor's default (already read-only for most), while ``--write`` on an
    # empty ``write_args`` is a hard error (delegating a write to a read-only
    # executor is the exact silent failure this feature exists to prevent).
    write_args: tuple[str, ...] = ()
    plan_args: tuple[str, ...] = ()
    experimental: bool = False
    notes: str = ""

    def mode_args(self, mode: str | None) -> tuple[str, ...]:
        """Return the concrete flags that realise a delegation *mode* intent.

        ``None`` -> no mode requested -> no extra flags (advisor native default).
        ``"write"`` -> ``write_args``; ``"plan"`` -> ``plan_args``.
        """
        if mode == "write":
            return self.write_args
        if mode == "plan":
            return self.plan_args
        return ()

    def reasoning_args(self, level: str | None) -> tuple[str, ...]:
        """Return the flags that pin *level* as this advisor's reasoning effort.

        Empty when no level was requested, or when the advisor exposes no
        reasoning-effort config key (there is nothing to set, so a requested
        level is dropped rather than guessed at).
        """
        if not level or self.reasoning_effort_config_key is None:
            return ()
        return ("-c", f"{self.reasoning_effort_config_key}={level}")

    def supports_mode(self, mode: str | None) -> bool:
        """Whether the advisor declares concrete flags for *mode*.

        ``None`` is always supported (it means "no mode"). ``"plan"`` is always
        supported because read-only is a safe universal fallback. ``"write"`` is
        supported only when ``write_args`` is non-empty.
        """
        if mode is None or mode == "plan":
            return True
        return bool(self.write_args)

    @property
    def supports_sessions(self) -> bool:
        return (
            self.resume_flag is not None
            or self.session_name_flag is not None
            or self.resume_command is not None
        )

    @property
    def supports_stream(self) -> bool:
        return self.result_parser in (
            "claude-stream",
            "codex-jsonl",
            "commandcode-json",
        )


# --- Built-in advisors -------------------------------------------------------
# claude is the reference implementation: fully featured, verified against the
# upstream `claude -p` behaviour. The rest are pragmatic best-effort defaults —
# marked experimental — that users can correct via the JSON override file.

_BUILTINS: dict[str, Advisor] = {
    "claude": Advisor(
        name="claude",
        executable="claude",
        invoke_args=("-p",),
        prompt_delivery="dashdash",
        model_flag="--model",
        stream_args=("--verbose", "--output-format", "stream-json"),
        json_args=("--output-format", "json"),
        resume_flag="--resume",
        session_name_flag="--name",
        fork_flag="--fork-session",
        result_parser="claude-stream",
        json_schema_flag="--json-schema",
        # ``--write`` grants unattended edit+command execution: acceptEdits alone
        # still auto-DENIES every non-allowlisted Bash call in ``-p`` mode, which
        # reproduces the silent read-only failure for any task that runs a
        # command. bypassPermissions is the honest "unattended executor" contract
        # — bound it with ``--allow-path``. ``--plan`` maps to Claude's own plan
        # permission mode (read-only).
        write_args=("--permission-mode", "bypassPermissions"),
        plan_args=("--permission-mode", "plan"),
    ),
    "codex": Advisor(
        name="codex",
        executable="codex",
        # --skip-git-repo-check: `codex exec` refuses to run (exit 1, empty
        # stdout) with "Not inside a trusted directory" whenever the cwd is not
        # a trusted git repo (S2 probe). crossagent runs from arbitrary --cwd
        # paths, so without this a delegation from a non-repo directory fails
        # confusingly with no JSON to parse. Skipping the check only bypasses
        # codex's own trust gate; it grants no extra capability.
        base_args=("exec", "--skip-git-repo-check"),
        prompt_delivery="positional",
        model_flag="--model",
        default_model="gpt-6-astra",
        # GPT-6 Astra bills reasoning effort as a cost multiplier, and codex
        # reads the level from ~/.codex/config.toml — which may well be set to
        # `high` for the user's own interactive work. A second opinion is a
        # reviewer, not an author: `low` is strong enough there at a fraction of
        # the tokens, so crossagent's own default asks pin `low` via a visible
        # `-c` override (the user's config file is never modified).
        default_reasoning_effort="low",
        reasoning_effort_config_key="model_reasoning_effort",
        json_args=("--json",),
        stream_args=("--json",),
        result_parser="codex-jsonl",
        resume_command=("resume",),
        session_event_field="thread_id",
        # Stock ``codex exec`` runs sandbox=read-only, approval=never (probed), so
        # ``--write`` must opt in to workspace-write explicitly; ``--plan`` pins
        # read-only.
        write_args=("--sandbox", "workspace-write"),
        plan_args=("--sandbox", "read-only"),
        experimental=True,
        notes=(
            "Uses `codex exec --skip-git-repo-check --json <prompt>` with JSONL event "
            "streaming and resume. Fresh asks on the default model run at `low` "
            "reasoning effort (-c model_reasoning_effort=low); override with --reasoning."
        ),
    ),
    # opencode stays on the text parser: its SUCCESS telemetry shape is
    # unmeasured (every probe run failed on provider creds, S2). `run --format
    # json` exists, but wiring it without a verified success event shape would
    # risk dropping the answer or inventing field names (D4). Re-probe with
    # working creds before wiring. Telemetry degrades to unknown via text.
    "opencode": Advisor(
        name="opencode",
        executable="opencode",
        base_args=("run",),
        prompt_delivery="positional",
        model_flag="--model",
        result_parser="text",
        # ``--auto`` selects opencode's build agent (edits allowed);
        # ``--agent plan`` is read-only. Both are position-independent yargs
        # options relative to the variadic message.
        write_args=("--auto",),
        plan_args=("--agent", "plan"),
        experimental=True,
        notes="Uses `opencode run <prompt>` (headless). Telemetry unmeasured; stays text-only.",
    ),
    "commandcode": Advisor(
        name="commandcode",
        executable="commandcode",
        invoke_args=("-p",),
        prompt_delivery="positional",
        model_flag="--model",
        # `-p --output-format json` emits an NDJSON event stream ending in a
        # type:"result" line with camelCase usage, durationMs, and finalText
        # (verified live on CommandCode 1.4.1, S2 probe). Plain `-p` text mode
        # emits zero telemetry, so the JSON flag is required to reach analytics.
        json_args=("--output-format", "json"),
        stream_args=("--output-format", "json"),
        result_parser="commandcode-json",
        # ``--write`` maps to commandcode's full permission bypass. In
        # non-interactive ``-p`` mode ``--permission-mode auto-accept`` is NOT
        # enough — the write tools stay blocked and commandcode itself tells you
        # to re-run with the bypass flag — so the honest write contract is the
        # bypass (the same flag the /delegate skill uses). Bound it with
        # ``--allow-path``. ``--plan`` is read-only.
        write_args=("--yolo",),
        plan_args=("--plan",),
        experimental=True,
        notes="Uses `commandcode -p --output-format json <prompt>` (non-interactive). Resume not wired by default.",
    ),
    # gemini is not installed on this machine, so its telemetry shape is
    # unverifiable; it stays text-only until it can be probed live.
    "gemini": Advisor(
        name="gemini",
        executable="gemini",
        prompt_delivery="flag:-p",
        model_flag="--model",
        result_parser="text",
        experimental=True,
        notes="Uses `gemini -p <prompt>` (non-interactive). Not installed here; telemetry unverified.",
    ),
}

# Friendly aliases callers may type.
_ALIASES = {"cmd": "commandcode", "cc": "claude", "oc": "opencode"}

# Short model aliases, scoped per advisor so a name never leaks across CLIs
# (each advisor's own CLI resolves its own shorthands; we only expand ours).
MODEL_ALIASES: dict[str, dict[str, str]] = {
    "codex": {"gpt6": "gpt-6-astra", "astra": "gpt-6-astra"},
}


def resolve_model(name: str | None, advisor_name: str) -> str | None:
    """Expand a per-advisor model alias. Returns None when no model was requested."""
    if not isinstance(name, str):
        return None
    candidate = name.strip()
    if not candidate:
        return None
    return MODEL_ALIASES.get(advisor_name, {}).get(candidate.lower(), candidate)


def default_reasoning_for_model(advisor: Advisor, model: str | None) -> str:
    """The advisor's default reasoning effort, but only when *model* IS its default.

    Crossagent only claims to know the right effort for the model it chose itself,
    so the gate compares *model* against the alias-resolved ``default_model``,
    case-insensitively: a config that sets ``default_model: "gpt6"`` and a caller
    who types ``--model GPT-6-Astra`` both still land on the default effort, while
    any other model is left to the advisor CLI's own configuration.
    """
    if not advisor.default_reasoning_effort or not advisor.default_model:
        return ""
    if not isinstance(model, str) or not model.strip():
        return ""
    resolved = resolve_model(advisor.default_model, advisor.name) or ""
    if resolved.lower() != model.strip().lower():
        return ""
    return advisor.default_reasoning_effort


def _coerce(name: str, raw: dict[str, Any]) -> Advisor:
    """Build an Advisor from a user-config dict, layering onto a built-in if one exists."""
    base = _BUILTINS.get(
        name, Advisor(name=name, executable=raw.get("executable", name))
    )
    tuple_fields = {
        "base_args",
        "invoke_args",
        "stream_args",
        "json_args",
        "resume_command",
        "write_args",
        "plan_args",
    }
    overrides: dict[str, Any] = {}
    for key, value in raw.items():
        if key in tuple_fields and isinstance(value, list):
            overrides[key] = tuple(value)
        elif key != "name":
            overrides[key] = value
    return replace(base, name=name, **overrides)


def _load_user_config(path: Path) -> dict[str, Advisor]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    entries = data.get("advisors", data) if isinstance(data, dict) else {}
    result: dict[str, Advisor] = {}
    if isinstance(entries, dict):
        for name, raw in entries.items():
            if isinstance(raw, dict):
                result[name] = _coerce(name, raw)
    return result


def available(config_path: Path | None = None) -> dict[str, Advisor]:
    """Return the merged advisor registry: built-ins overridden by user config."""
    merged = dict(_BUILTINS)
    merged.update(_load_user_config(config_path or USER_CONFIG))
    return merged


def resolve(name: str, config_path: Path | None = None) -> Advisor:
    """Look up an advisor by name or alias. Raises KeyError with a helpful message."""
    canonical = _ALIASES.get(name.strip().lower(), name.strip().lower())
    registry = available(config_path)
    if canonical not in registry:
        known = ", ".join(sorted(registry))
        raise KeyError(
            f"Unknown advisor '{name}'. Known advisors: {known}. Add your own in {USER_CONFIG}."
        )
    return registry[canonical]
