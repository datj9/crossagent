"""Advisor output parsers for the shared process runner.

Phase 4 of the durable cross-tool delegation plan: unifies text, Claude stream-json,
and Codex JSONL parsing behind a single ``EventParser`` interface that is compatible
with ``runner.LineConsumer``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedResult:
    """Final payload returned by a parser after the advisor exits."""

    result: Optional[str] = None
    session_id: Optional[str] = None
    failure: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Parser interface
# ---------------------------------------------------------------------------

class EventParser:
    """Base class for advisor output parsers.

    Parsers implement the ``runner.LineConsumer`` protocol: ``consume_stdout``,
    ``consume_stderr``, and ``finish`` are called by the runner.  They may also
    call *on_activity* when output indicates the advisor is alive.
    """

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        self._on_activity = on_activity

    def consume_stdout(self, line: str) -> None:
        raise NotImplementedError

    def consume_stderr(self, line: str) -> None:
        print(line, file=sys.stderr, end="")

    def finish(self, exit_code: int) -> ParsedResult:
        raise NotImplementedError

    def _activity(self, stream: str) -> None:
        if self._on_activity is not None:
            self._on_activity(stream)


# ---------------------------------------------------------------------------
# Text parser
# ---------------------------------------------------------------------------

class TextParser(EventParser):
    """Capture raw stdout as the answer; echo stderr as it arrives."""

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_activity)
        self._stdout_parts: list[str] = []

    def consume_stdout(self, line: str) -> None:
        self._stdout_parts.append(line)
        self._activity("stdout")

    def consume_stderr(self, line: str) -> None:
        super().consume_stderr(line)
        self._activity("stderr")

    def finish(self, exit_code: int) -> ParsedResult:
        return ParsedResult(result="".join(self._stdout_parts))


# ---------------------------------------------------------------------------
# Claude stream-json parser
# ---------------------------------------------------------------------------

class ClaudeStreamParser(EventParser):
    """Parse Claude's newline-delimited stream-json events."""

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_activity)
        self._final: dict[str, Any] | None = None

    def consume_stdout(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            print(stripped, file=sys.stderr)
            return
        self._summarize(event)
        if event.get("type") == "result":
            self._final = event
            self._activity("stdout")

    def finish(self, exit_code: int) -> ParsedResult:
        if self._final is None:
            return ParsedResult(
                failure=True,
                error="No result event received from Claude",
            )
        if self._final.get("is_error"):
            errors = self._final.get("errors") or self._final.get("api_error_status") or "unknown error"
            return ParsedResult(
                failure=True,
                error=str(errors),
            )
        result = self._final.get("result")
        if result is not None:
            return ParsedResult(result=result, session_id=self._final.get("session_id"))
        structured = self._final.get("structured_output")
        if structured is not None:
            return ParsedResult(
                result=json.dumps(structured, indent=2, sort_keys=True),
                session_id=self._final.get("session_id"),
            )
        return ParsedResult(
            failure=True,
            error="Result event contained no answer",
        )

    def _summarize(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            print(
                f"[crossagent] init session={event.get('session_id')} model={event.get('model')} "
                f"cwd={event.get('cwd')}",
                file=sys.stderr,
            )
        elif kind == "assistant":
            message = event.get("message", {})
            blocks = message.get("content", []) if isinstance(message, dict) else []
            text = "".join(
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                print(
                    f"[crossagent] assistant: {text.replace(chr(10), ' ')[:240]}",
                    file=sys.stderr,
                )
        elif kind == "result":
            print(
                f"[crossagent] result subtype={event.get('subtype')} session={event.get('session_id')} "
                f"cost={event.get('total_cost_usd')}",
                file=sys.stderr,
            )
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            print(
                f"[crossagent] rate_limit status={info.get('status')} resetsAt={info.get('resetsAt')}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Codex JSONL parser
# ---------------------------------------------------------------------------

class CodexJsonlParser(EventParser):
    """Parse Codex ``exec --json`` JSONL events and extract the final answer.

    Summaries are emitted to stderr but never include prompt text.  Malformed or
    unknown lines are preserved in stderr diagnostics and ignored safely.
    """

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_activity)
        self._thread_id: Optional[str] = None
        self._last_agent_message: Optional[str] = None
        self._failure_error: Optional[str] = None

    def consume_stdout(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            print(f"[crossagent] codex malformed line: {line.rstrip()[:240]}", file=sys.stderr)
            return

        event_type = event.get("type")
        self._summarize(event)

        if event_type == "thread.started":
            self._thread_id = event.get("thread_id") or event.get("id")
        elif event_type == "turn.started":
            self._activity("stdout")
        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                self._last_agent_message = self._extract_message_text(item)
            self._activity("stdout")
        elif event_type == "item.started":
            self._activity("stdout")
        elif event_type in ("turn.failed", "error"):
            self._failure_error = event.get("error") or event.get("message") or event_type
            self._activity("stdout")

    def finish(self, exit_code: int) -> ParsedResult:
        if self._failure_error is not None:
            return ParsedResult(
                failure=True,
                error=self._failure_error,
                session_id=self._thread_id,
            )
        if exit_code != 0:
            return ParsedResult(
                failure=True,
                error=f"Codex exited with code {exit_code}",
                session_id=self._thread_id,
            )
        return ParsedResult(
            result=self._last_agent_message,
            session_id=self._thread_id,
        )

    def _summarize(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or event.get("id")
            print(f"[crossagent] codex thread={thread_id}", file=sys.stderr)
        elif event_type == "turn.started":
            print("[crossagent] codex turn started", file=sys.stderr)
        elif event_type == "turn.completed":
            print("[crossagent] codex turn completed", file=sys.stderr)
        elif event_type == "turn.failed":
            print("[crossagent] codex turn failed", file=sys.stderr)
        elif event_type == "error":
            msg = event.get("message") or event.get("error") or "unknown error"
            print(f"[crossagent] codex error: {msg[:200]}", file=sys.stderr)
        elif event_type in ("item.completed", "item.started"):
            item_type = event.get("item", {}).get("type", "unknown")
            print(f"[crossagent] codex {event_type} type={item_type}", file=sys.stderr)

    @staticmethod
    def _extract_message_text(item: dict[str, Any]) -> Optional[str]:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts) if parts else None
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

PARSER_NAMES = {"text", "claude-stream", "codex-jsonl"}


def get_parser(
    name: str,
    *,
    on_activity: Optional[Callable[[str], None]] = None,
) -> EventParser:
    """Return a parser instance by *name*."""
    if name == "text":
        return TextParser(on_activity=on_activity)
    if name == "claude-stream":
        return ClaudeStreamParser(on_activity=on_activity)
    if name == "codex-jsonl":
        return CodexJsonlParser(on_activity=on_activity)
    raise ValueError(f"Unknown parser '{name}'. Known parsers: {', '.join(sorted(PARSER_NAMES))}")
