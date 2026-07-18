"""Tests for advisor output parsers (Phase 4).

These tests cover the Phase 4 Codex adapter exit criteria:
  1. Codex activity (turn.started/item.*) updates last-activity.
  2. Codex ``turn.failed``/``error`` produce explicit failure; empty output + failure
     event is NOT a successful empty result.
  3. Codex stores the thread ID from ``thread.started``.
  4. Malformed JSONL lines don't crash the parser and are preserved in diagnostics.
  5. Claude stream behavior unchanged; generic text advisors unchanged.

All inputs are synthetic JSONL fixtures; no real model CLI is invoked.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from crossagent import parsers


def _emit(*events: dict[str, Any]) -> list[str]:
    return [json.dumps(e) for e in events]


# =========================================================================
# Text parser
# =========================================================================


def test_text_parser_returns_stdout():
    parser = parsers.TextParser()
    parser.consume_stdout("line one\n")
    parser.consume_stdout("line two\n")
    parser.consume_stderr("err\n")
    result = parser.finish(0)
    assert result.result == "line one\nline two\n"
    assert result.failure is False
    assert result.session_id is None


# =========================================================================
# Claude stream-json parser
# =========================================================================


def test_claude_parser_extracts_result_and_session():
    parser = parsers.ClaudeStreamParser()
    for line in _emit(
        {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "sonnet", "cwd": "/tmp"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}},
        {"type": "result", "result": "final answer", "session_id": "sess-1", "subtype": "final"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.result == "final answer"
    assert result.session_id == "sess-1"
    assert result.failure is False


def test_claude_parser_detects_error():
    parser = parsers.ClaudeStreamParser()
    for line in _emit(
        {"type": "result", "is_error": True, "errors": "something went wrong"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.failure is True
    assert "something went wrong" in result.error


def test_claude_parser_no_result_is_failure():
    parser = parsers.ClaudeStreamParser()
    parser.consume_stdout("not a json line\n")
    result = parser.finish(0)
    assert result.failure is True
    assert "No result event" in result.error


# =========================================================================
# Codex JSONL parser
# =========================================================================


def test_codex_parser_extracts_thread_id():
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "content": "hello"}},
        {"type": "turn.completed"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.session_id == "thread_abc"
    assert result.result == "hello"
    assert result.failure is False


def test_codex_parser_extracts_agent_message_blocks():
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "content": [
            {"type": "text", "text": "part1 "},
            {"type": "text", "text": "part2"},
        ]}},
        {"type": "turn.completed"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.result == "part1 part2"


def test_codex_parser_extracts_agent_message_text_field():
    """Real `codex exec --json` puts the answer in item.text, not item.content."""
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "item_4", "type": "agent_message", "text": "the final answer",
        }},
        {"type": "turn.completed"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.result == "the final answer"
    assert result.failure is False


def test_codex_parser_turn_failed_is_failure():
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "turn.failed", "error": "The model refused"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.failure is True
    assert "The model refused" in result.error
    assert result.session_id == "thread_abc"


def test_codex_parser_error_event_is_failure():
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "error", "message": "bad request"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.failure is True
    assert "bad request" in result.error


def test_codex_parser_empty_success_is_not_failure():
    """No agent_message but turn.completed + exit 0 should be success with None result."""
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "turn.completed"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.failure is False
    assert result.result is None
    assert result.session_id == "thread_abc"


def test_codex_parser_empty_with_failure_event_is_failure():
    """Empty output plus a failure event must not look like a successful empty result."""
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.failed", "error": "internal error"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.failure is True


def test_codex_parser_malformed_lines_are_ignored():
    parser = parsers.CodexJsonlParser()
    for line in [
        '{"type": "thread.started", "thread_id": "thread_abc"}',
        'not valid json',
        '{"type": "turn.completed"}',
    ]:
        parser.consume_stdout(line + "\n")
    result = parser.finish(0)
    assert result.failure is False
    assert result.session_id == "thread_abc"


def test_codex_parser_activity_callback():
    activities = []

    def on_activity(stream: str) -> None:
        activities.append(stream)

    parser = parsers.CodexJsonlParser(on_activity=on_activity)
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "content": "hello"}},
        {"type": "item.started", "item": {"type": "command", "command": "ls"}},
        {"type": "turn.completed"},
    ):
        parser.consume_stdout(line + "\n")
    parser.finish(0)
    assert activities == ["stdout", "stdout", "stdout"]


def test_codex_parser_summaries_do_not_leak_content():
    import io
    import sys

    stderr_capture = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = stderr_capture
    try:
        parser = parsers.CodexJsonlParser()
        for line in _emit(
            {"type": "thread.started", "thread_id": "thread_abc"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "content": "SECRET_PROMPT_DATA"}},
            {"type": "turn.completed"},
        ):
            parser.consume_stdout(line + "\n")
        result = parser.finish(0)
    finally:
        sys.stderr = old_stderr

    assert result.result == "SECRET_PROMPT_DATA"
    summary = stderr_capture.getvalue()
    assert "SECRET_PROMPT_DATA" not in summary
    assert "agent_message" in summary


def test_codex_parser_nonzero_exit_is_failure():
    parser = parsers.CodexJsonlParser()
    for line in _emit(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.completed"},
    ):
        parser.consume_stdout(line + "\n")
    result = parser.finish(1)
    assert result.failure is True
    assert "exited with code 1" in result.error


# =========================================================================
# Factory
# =========================================================================


def test_get_parser_known_names():
    assert isinstance(parsers.get_parser("text"), parsers.TextParser)
    assert isinstance(parsers.get_parser("claude-stream"), parsers.ClaudeStreamParser)
    assert isinstance(parsers.get_parser("codex-jsonl"), parsers.CodexJsonlParser)


def test_get_parser_unknown_raises():
    with pytest.raises(ValueError):
        parsers.get_parser("unknown")
