"""Tests for the pure event normalizer (``crossagent.feed``)."""

from __future__ import annotations

import json

from crossagent.feed import normalize_stream_line, resolve_event_format


class TestNormalizeStreamLineClaudeStream:
    def test_init_line(self):
        raw = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-opus-4",
                "session_id": "sess_abc123",
                "cwd": "/home/user",
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "init"
        assert ev["title"] == "session"
        assert "model=claude-opus-4" in ev["body"]
        assert "session_id=sess_abc123" in ev["body"]
        assert ev["meta"] == {
            "model": "claude-opus-4",
            "session_id": "sess_abc123",
            "cwd": "/home/user",
        }
        assert ev["raw_type"] == "system/init"

    def test_assistant_text_only(self):
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello, world!"},
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "assistant"
        assert ev["body"] == "Hello, world!"
        assert ev["raw_type"] == "assistant"

    def test_assistant_text_and_tool_use(self):
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello, world!"},
                        {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "bash",
                            "input": {"cmd": "ls -la"},
                        },
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 2
        assert result[0]["kind"] == "assistant"
        assert result[0]["body"] == "Hello, world!"
        assert result[1]["kind"] == "tool"
        assert result[1]["phase"] == "started"
        assert result[1]["entity_id"] == "toolu_abc"
        assert result[1]["title"] == "bash"
        assert result[1]["raw_type"] == "assistant/tool_use"

    def test_assistant_only_tool_use(self):
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_xyz",
                            "name": "read_file",
                            "input": {"path": "/tmp/test.txt"},
                        }
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "started"
        assert ev["entity_id"] == "toolu_xyz"
        assert ev["title"] == "read_file"
        assert ev["raw_type"] == "assistant/tool_use"

    def test_assistant_no_blocks(self):
        raw = json.dumps({"type": "assistant", "message": {"content": []}})
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert result == []

    def test_assistant_thinking_block(self):
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "Let me think about this..."}
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "thinking"
        assert ev["body"] == "Let me think about this..."
        assert ev["raw_type"] == "assistant/thinking"

    def test_assistant_redacted_thinking_block(self):
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "redacted_thinking",
                            "thinking": "[redacted reasoning]",
                        }
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "thinking"
        assert ev["body"] == "[redacted reasoning]"

    def test_assistant_mixed_blocks(self):
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check..."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {"cmd": "ls"},
                        },
                        {"type": "text", "text": "Done!"},
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 3
        assert result[0]["kind"] == "assistant"
        assert result[0]["body"] == "Let me check..."
        assert result[1]["kind"] == "tool"
        assert result[1]["phase"] == "started"
        assert result[1]["entity_id"] == "toolu_1"
        assert result[1]["title"] == "bash"
        assert result[2]["kind"] == "assistant"
        assert result[2]["body"] == "Done!"

    def test_user_tool_result_string_content(self):
        raw = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "file1.txt\nfile2.txt",
                        }
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "completed"
        assert ev["entity_id"] == "toolu_abc"
        assert ev["body"] == "file1.txt\nfile2.txt"
        assert ev["raw_type"] == "user/tool_result"

    def test_user_tool_result_list_content(self):
        raw = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_xyz",
                            "content": [
                                {"type": "text", "text": "Output line 1"},
                                {"type": "text", "text": "Output line 2"},
                            ],
                        }
                    ]
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "completed"
        assert ev["entity_id"] == "toolu_xyz"
        assert ev["body"] == "Output line 1Output line 2"

    def test_user_no_tool_result(self):
        raw = json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert result == []

    def test_result_line(self):
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "message_stop",
                "session_id": "sess_abc",
                "total_input_tokens": 150,
                "total_output_tokens": 200,
                "total_cost_usd": 0.015,
                "duration_ms": 5000,
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "result"
        assert ev["title"] == "message_stop"
        assert "duration=5000ms" in ev["body"]
        assert "cost=$0.015" in ev["body"]
        assert ev["meta"]["total_cost_usd"] == 0.015
        assert ev["raw_type"] == "result"

    def test_result_error_with_is_error(self):
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "error"
        assert ev["title"] == "error"
        assert ev["raw_type"] == "result"

    def test_result_error_with_subtype_error(self):
        raw = json.dumps({"type": "result", "subtype": "error"})
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "error"
        assert ev["title"] == "error"
        assert ev["raw_type"] == "result"

    def test_rate_limit_event(self):
        raw = json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "ok",
                    "resetsAt": "2026-01-01T00:00:00Z",
                },
            }
        )
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "rate_limit"
        assert ev["title"] == "rate limit"
        assert ev["raw_type"] == "rate_limit_event"
        assert "status=ok" in ev["body"]
        assert "resetsAt=2026-01-01T00:00:00Z" in ev["body"]
        assert ev["meta"]["status"] == "ok"

    def test_rate_limit_event_minimal(self):
        raw = json.dumps({"type": "rate_limit_event", "rate_limit_info": {}})
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "rate_limit"
        assert ev["body"] == "rate limit"

    def test_unknown_type_line(self):
        raw = json.dumps({"type": "ping", "ts": 12345})
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["body"] == raw
        assert ev["raw_type"] == "ping"

    def test_non_json_line(self):
        raw = "some raw output here"
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["body"] == raw
        assert ev["raw_type"] == "unparsed"

    def test_blank_line(self):
        assert normalize_stream_line("claude-stream", "") == []
        assert normalize_stream_line("claude-stream", "   ") == []
        assert normalize_stream_line("claude-stream", "\n") == []


class TestNormalizeStreamLineCodexJsonl:
    def test_thread_started(self):
        raw = json.dumps({"type": "thread.started", "thread_id": "thread_abc"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "init"
        assert ev["title"] == "thread"
        assert "thread_id=thread_abc" in ev["body"]
        assert ev["meta"]["thread_id"] == "thread_abc"
        assert ev["raw_type"] == "codex/thread.started"

    def test_thread_started_no_id(self):
        raw = json.dumps({"type": "thread.started"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "init"
        assert ev["body"] == "thread started"

    def test_turn_started(self):
        raw = json.dumps({"type": "turn.started"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "thinking"
        assert ev["body"] == "turn started"
        assert ev["raw_type"] == "codex/turn.started"

    def test_turn_completed(self):
        raw = json.dumps({"type": "turn.completed"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "result"
        assert ev["raw_type"] == "codex/turn.completed"

    def test_turn_failed(self):
        raw = json.dumps({"type": "turn.failed", "error": "something went wrong"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "error"
        assert ev["title"] == "turn failed"
        assert "something went wrong" in ev["body"]
        assert ev["raw_type"] == "codex/turn.failed"

    def test_error_event(self):
        raw = json.dumps({"type": "error", "message": "API error"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "error"
        assert ev["title"] == "error"
        assert ev["body"] == "API error"
        assert ev["raw_type"] == "codex/error"

    def test_error_event_with_error_field(self):
        raw = json.dumps({"type": "error", "error": "connection refused"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "error"
        assert ev["body"] == "connection refused"

    def test_agent_message(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Hello from Codex!"},
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "assistant"
        assert ev["body"] == "Hello from Codex!"
        assert ev["raw_type"] == "codex/agent_message"

    def test_agent_message_empty(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message"},
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert result == []

    def test_text_item(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "text", "text": "some output"},
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "output"
        assert ev["body"] == "some output"
        assert ev["raw_type"] == "codex/item.completed"

    def test_tool_call_item_started(self):
        raw = json.dumps(
            {
                "type": "item.started",
                "id": "call_123",
                "item": {
                    "type": "tool_call",
                    "id": "call_123",
                    "name": "bash",
                    "input": {"cmd": "ls"},
                },
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "started"
        assert ev["entity_id"] == "call_123"
        assert ev["title"] == "bash"
        assert ev["raw_type"] == "codex/item.started/tool_call"

    def test_tool_call_item_completed(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "id": "call_123",
                "item": {
                    "type": "tool_call",
                    "id": "call_123",
                    "name": "bash",
                    "result": "file1.txt\nfile2.txt",
                },
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "completed"
        assert ev["entity_id"] == "call_123"
        assert ev["title"] == "bash"
        assert ev["body"] == "file1.txt\nfile2.txt"
        assert ev["raw_type"] == "codex/item.completed/tool_call"

    def test_command_execution_item_completed(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "c1",
                    "command": "pytest -q",
                    "aggregated_output": "142 passed",
                },
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "completed"
        assert ev["title"] == "pytest -q"
        assert ev["body"] == "142 passed"
        assert ev["entity_id"] == "c1"

    def test_item_updated_is_coalesced(self):
        raw = json.dumps(
            {"type": "item.updated", "item": {"type": "command_execution"}}
        )
        assert normalize_stream_line("codex-jsonl", raw + "\n") == []

    def test_non_object_json_is_raw(self):
        for fmt in ("codex-jsonl", "claude-stream"):
            result = normalize_stream_line(fmt, "[]\n")
            assert len(result) == 1
            assert result[0]["kind"] == "raw"
            assert result[0]["raw_type"] == "non-object"

    def test_failed_command_execution_marks_failed(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "c9",
                    "command": "pytest",
                    "exit_code": 1,
                    "aggregated_output": "1 failed",
                },
            }
        )
        ev = normalize_stream_line("codex-jsonl", raw + "\n")[0]
        assert ev["kind"] == "tool"
        assert ev["phase"] == "completed"
        assert ev["failed"] is True
        assert ev["meta"]["exit_code"] == 1

    def test_successful_command_execution_not_failed(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "id": "c0", "exit_code": 0},
            }
        )
        assert normalize_stream_line("codex-jsonl", raw + "\n")[0]["failed"] is False

    def test_non_string_item_type_is_raw(self):
        for etype in ("item.started", "item.completed"):
            raw = json.dumps({"type": etype, "item": {"type": ["weird"]}})
            result = normalize_stream_line("codex-jsonl", raw + "\n")
            assert len(result) == 1
            assert result[0]["kind"] == "raw"

    def test_reasoning_item(self):
        raw = json.dumps(
            {
                "type": "item.started",
                "item": {
                    "type": "reasoning",
                    "text": "reasoning trace...",
                },
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "thinking"
        assert ev["body"] == "reasoning trace..."
        assert ev["raw_type"] == "codex/item.started"

    def test_unknown_event_type(self):
        raw = json.dumps({"type": "some.weird.event", "data": {}})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["raw_type"] == "codex/some.weird.event"
        assert ev["body"] == raw

    def test_unknown_item_type_completed(self):
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "custom_thing", "data": 42},
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["title"] == "custom_thing"
        assert ev["raw_type"] == "codex/item.completed"
        assert ev["body"] == raw

    def test_unknown_item_type_started(self):
        raw = json.dumps(
            {
                "type": "item.started",
                "item": {"type": "custom_action", "data": 42},
            }
        )
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["title"] == "custom_action"
        assert ev["raw_type"] == "codex/item.started"

    def test_item_started_no_item_dict(self):
        raw = json.dumps({"type": "item.started", "item": "just a string"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["raw_type"] == "codex/item.started"

    def test_item_completed_no_item_dict(self):
        raw = json.dumps({"type": "item.completed", "item": "just a string"})
        result = normalize_stream_line("codex-jsonl", raw + "\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "raw"
        assert ev["raw_type"] == "codex/item.completed"

    def test_non_json_line(self):
        result = normalize_stream_line("codex-jsonl", "hello\n")
        assert len(result) == 1
        assert result[0]["kind"] == "raw"
        assert result[0]["raw_type"] == "unparsed"
        assert result[0]["body"] == "hello"

    def test_blank_line(self):
        assert normalize_stream_line("codex-jsonl", "") == []
        assert normalize_stream_line("codex-jsonl", "   ") == []
        assert normalize_stream_line("codex-jsonl", "\n") == []


class TestNormalizeStreamLineText:
    def test_line_yields_output(self):
        result = normalize_stream_line("text", "hello world\n")
        assert len(result) == 1
        ev = result[0]
        assert ev["kind"] == "output"
        assert ev["body"] == "hello world"
        assert ev["raw_type"] == "text"

    def test_blank_line(self):
        assert normalize_stream_line("text", "") == []
        assert normalize_stream_line("text", "   ") == []
        assert normalize_stream_line("text", "\n") == []

    def test_unknown_format_treated_as_text(self):
        result = normalize_stream_line("bogus-format", "hello\n")
        assert len(result) == 1
        assert result[0]["kind"] == "output"


class TestNormalizeStreamLineEdgeCases:
    def test_line_without_trailing_newline(self):
        result = normalize_stream_line("text", "no newline")
        assert len(result) == 1
        assert result[0]["body"] == "no newline"

    def test_init_missing_optional_fields(self):
        raw = json.dumps({"type": "system", "subtype": "init"})
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        assert result[0]["body"] == "init"
        assert result[0]["meta"] == {}

    def test_result_minimal(self):
        raw = json.dumps({"type": "result"})
        result = normalize_stream_line("claude-stream", raw + "\n")
        assert len(result) == 1
        assert result[0]["kind"] == "result"
        assert result[0]["body"] == "result"


class TestResolveEventFormat:
    def test_codex_advisor(self):
        assert resolve_event_format("codex-jsonl") == "codex-jsonl"

    def test_claude_advisor(self):
        assert resolve_event_format("claude-stream") == "claude-stream"

    def test_text_advisor(self):
        assert resolve_event_format("text") == "text"

    def test_unknown_advisor(self):
        assert resolve_event_format("bogus") == "text"
