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
RESULT_PARSERS = frozenset({"claude-stream", "codex-jsonl", "text"})

USER_CONFIG = Path.home() / ".config" / "crossagent" / "advisors.json"


@dataclass(frozen=True)
class Advisor:
    """Declarative recipe for invoking one peer-agent CLI."""

    name: str
    executable: str
    base_args: tuple[str, ...] = ()
    invoke_args: tuple[str, ...] = ()
    prompt_delivery: str = "positional"  # "dashdash" | "positional" | "flag:<flag>"
    model_flag: str | None = None
    stream_args: tuple[str, ...] = ()
    json_args: tuple[str, ...] = ()
    resume_flag: str | None = None
    session_name_flag: str | None = None
    fork_flag: str | None = None
    result_parser: str = "text"
    resume_command: tuple[str, ...] | None = None
    session_event_field: str | None = None
    experimental: bool = False
    notes: str = ""

    @property
    def supports_sessions(self) -> bool:
        return (
            self.resume_flag is not None
            or self.session_name_flag is not None
            or self.resume_command is not None
        )

    @property
    def supports_stream(self) -> bool:
        return self.result_parser in ("claude-stream", "codex-jsonl")


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
    ),
    "codex": Advisor(
        name="codex",
        executable="codex",
        base_args=("exec",),
        prompt_delivery="positional",
        model_flag="--model",
        json_args=("--json",),
        stream_args=("--json",),
        result_parser="codex-jsonl",
        resume_command=("resume",),
        session_event_field="thread_id",
        experimental=True,
        notes="Uses `codex exec --json <prompt>` with JSONL event streaming and resume.",
    ),
    "opencode": Advisor(
        name="opencode",
        executable="opencode",
        base_args=("run",),
        prompt_delivery="positional",
        model_flag="--model",
        result_parser="text",
        experimental=True,
        notes="Uses `opencode run <prompt>` (headless).",
    ),
    "commandcode": Advisor(
        name="commandcode",
        executable="commandcode",
        invoke_args=("-p",),
        prompt_delivery="positional",
        model_flag="--model",
        result_parser="text",
        experimental=True,
        notes="Uses `commandcode -p <prompt>` (non-interactive). Resume not wired by default.",
    ),
    "gemini": Advisor(
        name="gemini",
        executable="gemini",
        prompt_delivery="flag:-p",
        model_flag="--model",
        result_parser="text",
        experimental=True,
        notes="Uses `gemini -p <prompt>` (non-interactive).",
    ),
}

# Friendly aliases callers may type.
_ALIASES = {"cmd": "commandcode", "cc": "claude", "oc": "opencode"}


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
