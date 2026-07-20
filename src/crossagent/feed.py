"""Pure event normalizer for advisor stdout log lines.

Converts raw stdout lines into structured "feed event" dicts that the
dashboard event-stream endpoint returns.  Independent of the stateful
parser classes in ``parsers.py`` — zero side effects, zero state.
"""

from __future__ import annotations

import json
from typing import Any


def normalize_stream_line(event_format: str, raw_line: str) -> list[dict[str, Any]]:
    """Convert one raw stdout line into zero or more feed-event dicts.

    Parameters
    ----------
    event_format:
        ``"claude-stream"`` for Claude stream-json output; ``"codex-jsonl"`` for
        Codex JSONL output; anything else (``"text"``, …) is treated as plain text.
    raw_line:
        A single line of advisor stdout, still including its trailing
        newline (or not — the function strips internally).

    Returns
    -------
    A list of event dicts with keys ``kind``, ``title``, ``body``,
    ``meta``, ``raw_type``.  May also include ``entity_id`` (str) and
    ``phase`` (str) for tool-related events.  May be empty.
    """
    stripped = raw_line.strip()
    if isinstance(stripped, bytes):
        stripped = stripped.decode("utf-8")

    if event_format == "claude-stream":
        return _normalize_claude_stream(raw_line, stripped)
    if event_format == "codex-jsonl":
        return _normalize_codex_jsonl(raw_line, stripped)
    return _normalize_text(raw_line, stripped)


def resolve_event_format(result_parser: str) -> str:
    """Map an advisor's ``result_parser`` to an event format string.

    - ``"claude-stream"`` → ``"claude-stream"``
    - ``"codex-jsonl"`` → ``"codex-jsonl"``
    - anything else → ``"text"``
    """
    if result_parser in ("claude-stream", "codex-jsonl"):
        return result_parser
    return "text"


def _raw_event(raw_line: str, raw_type: str) -> list[dict[str, Any]]:
    """Return a single collapsed ``raw`` event so no line is ever dropped."""
    return [
        {
            "kind": "raw",
            "title": "",
            "body": raw_line.rstrip("\n"),
            "meta": {},
            "raw_type": raw_type,
        }
    ]


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _normalize_text(raw_line: str, stripped: str) -> list[dict[str, Any]]:
    if not stripped:
        return []
    return [
        {
            "kind": "output",
            "title": "",
            "body": raw_line.rstrip("\n"),
            "meta": {},
            "raw_type": "text",
        }
    ]


# ---------------------------------------------------------------------------
# Claude stream-json
# ---------------------------------------------------------------------------


def _normalize_claude_stream(raw_line: str, stripped: str) -> list[dict[str, Any]]:
    if not stripped:
        return []
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return _raw_event(raw_line, "unparsed")

    if not isinstance(event, dict):
        return _raw_event(raw_line, "non-object")

    event_type = event.get("type")

    if event_type == "system" and event.get("subtype") == "init":
        return _claude_init(event)

    if event_type == "assistant":
        return _claude_assistant(event)

    if event_type == "user":
        return _claude_user(event)

    if event_type == "rate_limit_event":
        return _claude_rate_limit(event)

    if event_type == "result":
        return _claude_result(event, raw_line)

    return [
        {
            "kind": "raw",
            "title": "",
            "body": raw_line.rstrip("\n"),
            "meta": {},
            "raw_type": str(event_type) if event_type else "unknown",
        }
    ]


def _claude_init(event: dict[str, Any]) -> list[dict[str, Any]]:
    meta: dict[str, Any] = {}
    for key in ("model", "session_id", "cwd"):
        val = event.get(key)
        if val is not None:
            meta[key] = val
    parts = []
    if "model" in meta:
        parts.append(f"model={meta['model']}")
    if "session_id" in meta:
        parts.append(f"session_id={meta['session_id']}")
    if "cwd" in meta:
        parts.append(f"cwd={meta['cwd']}")
    body = " ".join(parts) if parts else "init"
    return [
        {
            "kind": "init",
            "title": "session",
            "body": body,
            "meta": meta,
            "raw_type": "system/init",
        }
    ]


def _claude_assistant(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message", {})
    blocks = message.get("content", []) if isinstance(message, dict) else []
    if not blocks:
        return []
    events: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                events.append(
                    {
                        "kind": "assistant",
                        "title": "",
                        "body": text,
                        "meta": {},
                        "raw_type": "assistant",
                    }
                )
        elif block_type in ("thinking", "redacted_thinking"):
            events.append(
                {
                    "kind": "thinking",
                    "title": "",
                    "body": block.get("thinking", ""),
                    "meta": {},
                    "raw_type": "assistant/thinking",
                }
            )
        elif block_type == "tool_use":
            block_input = block.get("input", {})
            body = (
                json.dumps(block_input, sort_keys=True)
                if isinstance(block_input, dict)
                else str(block_input)
            )
            events.append(
                {
                    "kind": "tool",
                    "title": block.get("name", ""),
                    "body": body,
                    "meta": {},
                    "raw_type": "assistant/tool_use",
                    "entity_id": block.get("id", ""),
                    "phase": "started",
                }
            )
    return events


def _claude_user(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message", {})
    blocks = message.get("content", []) if isinstance(message, dict) else []
    events: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            content = block.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = str(content) if content is not None else ""
            events.append(
                {
                    "kind": "tool",
                    "title": "",
                    "body": text,
                    "meta": {},
                    "raw_type": "user/tool_result",
                    "entity_id": block.get("tool_use_id", ""),
                    "phase": "completed",
                    "failed": bool(block.get("is_error")),
                }
            )
    return events


def _claude_rate_limit(event: dict[str, Any]) -> list[dict[str, Any]]:
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        info = {}
    parts = []
    status = info.get("status")
    if status is not None:
        parts.append(f"status={status}")
    resets_at = info.get("resetsAt")
    if resets_at is not None:
        parts.append(f"resetsAt={resets_at}")
    body = " ".join(parts) if parts else "rate limit"
    return [
        {
            "kind": "rate_limit",
            "title": "rate limit",
            "body": body,
            "meta": info,
            "raw_type": "rate_limit_event",
        }
    ]


def _claude_result(event: dict[str, Any], raw_line: str) -> list[dict[str, Any]]:
    is_error = event.get("is_error")
    subtype = event.get("subtype", "")
    if is_error or subtype == "error":
        meta: dict[str, Any] = {}
        for key in ("session_id",):
            val = event.get(key)
            if val is not None:
                meta[key] = val
        return [
            {
                "kind": "error",
                "title": str(subtype) if subtype else "error",
                "body": f"Error: {subtype if subtype else 'unknown'}",
                "meta": meta,
                "raw_type": "result",
            }
        ]
    meta = {}
    for key in (
        "session_id",
        "total_input_tokens",
        "total_output_tokens",
        "total_cost_usd",
        "duration_ms",
    ):
        val = event.get(key)
        if val is not None:
            meta[key] = val
    parts = []
    if subtype:
        parts.append(str(subtype))
    if "duration_ms" in meta:
        parts.append(f"duration={meta['duration_ms']}ms")
    if "total_cost_usd" in meta:
        parts.append(f"cost=${meta['total_cost_usd']}")
    body = " ".join(parts) if parts else "result"
    return [
        {
            "kind": "result",
            "title": str(subtype) if subtype else "",
            "body": body,
            "meta": meta,
            "raw_type": "result",
        }
    ]


# ---------------------------------------------------------------------------
# Codex JSONL
# ---------------------------------------------------------------------------


def _normalize_codex_jsonl(raw_line: str, stripped: str) -> list[dict[str, Any]]:
    if not stripped:
        return []
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return _raw_event(raw_line, "unparsed")

    if not isinstance(event, dict):
        return _raw_event(raw_line, "non-object")

    event_type = event.get("type", "")

    if event_type == "thread.started":
        return _codex_thread_started(event)

    if event_type == "turn.started":
        return [
            {
                "kind": "thinking",
                "title": "",
                "body": "turn started",
                "meta": {},
                "raw_type": "codex/turn.started",
            }
        ]

    if event_type == "turn.completed":
        return [
            {
                "kind": "result",
                "title": "turn completed",
                "body": "turn completed",
                "meta": {},
                "raw_type": "codex/turn.completed",
            }
        ]

    if event_type == "turn.failed":
        error_msg = event.get("error") or event.get("message") or "turn failed"
        return [
            {
                "kind": "error",
                "title": "turn failed",
                "body": str(error_msg),
                "meta": {},
                "raw_type": "codex/turn.failed",
            }
        ]

    if event_type == "error":
        error_msg = event.get("error") or event.get("message") or "unknown error"
        return [
            {
                "kind": "error",
                "title": "error",
                "body": str(error_msg),
                "meta": {},
                "raw_type": "codex/error",
            }
        ]

    if event_type == "item.started":
        return _codex_item_started(event, raw_line)

    if event_type == "item.completed":
        return _codex_item_completed(event, raw_line)

    if event_type == "item.updated":
        # Intermediate streaming delta (e.g. partial command output). The final
        # state arrives on item.completed, so we coalesce updates to avoid
        # duplicate rows; no final data is lost.
        return []

    return _raw_event(raw_line, f"codex/{event_type}")


# Codex item types that represent a tool/command invocation (started+completed).
# The variant names track OpenAI's Codex exec event schema.
_CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "tool_call",
        "command_execution",
        "mcp_tool_call",
        "file_change",
        "web_search",
        "patch_apply",
    }
)


def _codex_tool_title(item: dict[str, Any], item_type: str) -> str:
    for key in ("name", "command", "tool", "server"):
        val = item.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list):
            return " ".join(str(part) for part in val)
    return item_type


def _codex_tool_body(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = item.get(key)
        if val in (None, ""):
            continue
        if isinstance(val, str):
            return val
        if isinstance(val, (dict, list)):
            return json.dumps(val, sort_keys=True)
        return str(val)
    return ""


def _codex_completed_tool_event(
    item: dict[str, Any], item_type: str, event: dict[str, Any]
) -> dict[str, Any]:
    """Build a completed-tool event that preserves failure signals.

    Codex command/tool completions carry status/exit_code/error; without these a
    failed command would render as an empty successful row.
    """
    meta: dict[str, Any] = {}
    for key in ("status", "exit_code"):
        val = item.get(key)
        if val is not None:
            meta[key] = val
    error = item.get("error")
    if error:
        meta["error"] = error
    status = item.get("status")
    exit_code = item.get("exit_code")
    failed = (
        bool(error)
        or (exit_code is not None and exit_code != 0)
        or (
            isinstance(status, str)
            and status.lower() in ("failed", "error", "declined", "cancelled")
        )
    )
    body = _codex_tool_body(
        item, ("aggregated_output", "output", "result", "diff", "changes", "content")
    )
    if not body and error:
        body = str(error)
    return {
        "kind": "tool",
        "title": _codex_tool_title(item, item_type),
        "body": body,
        "meta": meta,
        "raw_type": f"codex/item.completed/{item_type}",
        "entity_id": item.get("id", "") or event.get("id", ""),
        "phase": "completed",
        "failed": failed,
    }


def _codex_thread_started(event: dict[str, Any]) -> list[dict[str, Any]]:
    thread_id = event.get("thread_id") or event.get("id") or ""
    meta: dict[str, Any] = {}
    if thread_id:
        meta["thread_id"] = thread_id
    body = f"thread_id={thread_id}" if thread_id else "thread started"
    return [
        {
            "kind": "init",
            "title": "thread",
            "body": body,
            "meta": meta,
            "raw_type": "codex/thread.started",
        }
    ]


def _codex_item_started(event: dict[str, Any], raw_line: str) -> list[dict[str, Any]]:
    item = event.get("item", {})
    if not isinstance(item, dict):
        return [
            {
                "kind": "raw",
                "title": "",
                "body": raw_line.rstrip("\n"),
                "meta": {},
                "raw_type": "codex/item.started",
            }
        ]
    item_type = item.get("type", "")
    if not isinstance(item_type, str):
        return _raw_event(raw_line, "codex/item.started")
    if item_type in _CODEX_TOOL_ITEM_TYPES:
        return [
            {
                "kind": "tool",
                "title": _codex_tool_title(item, item_type),
                "body": _codex_tool_body(
                    item, ("command", "input", "query", "changes", "arguments")
                ),
                "meta": {},
                "raw_type": f"codex/item.started/{item_type}",
                "entity_id": item.get("id", "") or event.get("id", ""),
                "phase": "started",
            }
        ]
    if item_type == "reasoning":
        thinking_text = item.get("text") or item.get("content") or ""
        body = (
            str(thinking_text) if not isinstance(thinking_text, str) else thinking_text
        )
        return [
            {
                "kind": "thinking",
                "title": "",
                "body": body,
                "meta": {},
                "raw_type": "codex/item.started",
            }
        ]
    return [
        {
            "kind": "raw",
            "title": item_type,
            "body": raw_line.rstrip("\n"),
            "meta": {},
            "raw_type": "codex/item.started",
        }
    ]


def _codex_item_completed(event: dict[str, Any], raw_line: str) -> list[dict[str, Any]]:
    item = event.get("item", {})
    if not isinstance(item, dict):
        return [
            {
                "kind": "raw",
                "title": "",
                "body": raw_line.rstrip("\n"),
                "meta": {},
                "raw_type": "codex/item.completed",
            }
        ]
    item_type = item.get("type", "")
    if not isinstance(item_type, str):
        return _raw_event(raw_line, "codex/item.completed")
    if item_type == "agent_message":
        text = _extract_codex_message_text(item)
        if text:
            return [
                {
                    "kind": "assistant",
                    "title": "",
                    "body": text,
                    "meta": {},
                    "raw_type": "codex/agent_message",
                }
            ]
        return []
    if item_type == "text":
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, str) and text:
            return [
                {
                    "kind": "output",
                    "title": "",
                    "body": text,
                    "meta": {},
                    "raw_type": "codex/item.completed",
                }
            ]
        return []
    if item_type in _CODEX_TOOL_ITEM_TYPES:
        return [_codex_completed_tool_event(item, item_type, event)]
    return [
        {
            "kind": "raw",
            "title": item_type,
            "body": raw_line.rstrip("\n"),
            "meta": {},
            "raw_type": "codex/item.completed",
        }
    ]


def _extract_codex_message_text(item: dict[str, Any]) -> str | None:
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts) if parts else None
    return None
