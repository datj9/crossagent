"""Tests for advisor metric extraction and durable persistence (slice S1).

Covers the S1 acceptance criteria and the binding architectural decisions:

  * D1 — metrics are two open-keyed maps (``usage_details``, ``cost_details``).
  * D2 — token counts are NOT summed; each token lives under exactly one key.
  * D3 — advisor-emitted cost is labelled ``cost_source == "advisor"``.
  * D4 — malformed/absent telemetry degrades to unknown, never raises.
  * D7 — schema 2 -> 3 migration; an unmeasured metric is distinguishable from
    a measured zero (unknown, never ``0.0``).

All inputs are synthetic; no real advisor CLI is invoked.
"""

from __future__ import annotations

from crossagent import parsers
from crossagent.cli import _format_cost, _metrics_summary, _print_job_table
from crossagent.jobs import (
    Job,
    JobState,
    atomic_json_write,
    create_job_dir,
    load_state,
    runtime_status,
    save_state,
)


def _claude_result_event() -> dict[str, object]:
    """A realistic Claude stream-json ``result`` event carrying telemetry."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "sess-1",
        "duration_ms": 5000,
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 200,
        },
        "modelUsage": {"claude-opus-4": {"inputTokens": 100}},
        "result": "the answer",
    }


# =========================================================================
# Claude metric extraction
# =========================================================================


def test_extract_claude_metrics_full_payload():
    metrics = parsers.extract_claude_metrics(_claude_result_event())
    assert metrics.usage_details == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 200,
    }
    assert metrics.cost_details == {"total": 0.0123}
    assert metrics.cost_source == "advisor"
    assert metrics.duration_ms == 5000
    assert metrics.model_reported == "claude-opus-4"


def test_extract_claude_metrics_tokens_are_not_summed():
    """D2: no fabricated aggregate token key; each category stands alone."""
    metrics = parsers.extract_claude_metrics(_claude_result_event())
    # A summed total (370) must NOT appear under any key.
    assert 370 not in metrics.usage_details.values()
    assert "total" not in metrics.usage_details
    assert "total_tokens" not in metrics.usage_details


def test_extract_claude_metrics_no_telemetry_is_unknown_not_zero():
    """D7: absent telemetry -> unknown, distinguishable from a measured zero."""
    metrics = parsers.extract_claude_metrics({"type": "result", "subtype": "success"})
    assert metrics.usage_details == {}
    assert metrics.cost_details == {}
    assert metrics.cost_source == "unknown"
    assert metrics.duration_ms is None
    assert metrics.model_reported is None
    # The measured-zero shape must be different from the unknown shape.
    assert metrics.cost_details != {"total": 0.0}


def test_extract_claude_metrics_zero_cost_is_measured_advisor():
    """A real ``total_cost_usd: 0.0`` is a measured zero, not unknown."""
    metrics = parsers.extract_claude_metrics(
        {"type": "result", "subtype": "success", "total_cost_usd": 0.0}
    )
    assert metrics.cost_details == {"total": 0.0}
    assert metrics.cost_source == "advisor"


def test_extract_claude_metrics_malformed_payload_does_not_raise():
    """D4: version-fragile fields that arrive with the wrong type degrade
    to unknown instead of raising."""
    metrics = parsers.extract_claude_metrics(
        {
            "type": "result",
            "usage": ["not", "a", "dict"],
            "total_cost_usd": "not-a-number",
            "duration_ms": {"nested": "garbage"},
            "modelUsage": 42,
        }
    )
    assert metrics.usage_details == {}
    assert metrics.cost_details == {}
    assert metrics.cost_source == "unknown"
    assert metrics.duration_ms is None
    assert metrics.model_reported is None


def test_extract_claude_metrics_non_dict_event_is_unknown():
    metrics = parsers.extract_claude_metrics("not an event")  # type: ignore[arg-type]
    assert metrics.cost_source == "unknown"
    assert metrics.usage_details == {}


def test_extract_claude_metrics_top_level_token_totals_fallback():
    """Older stream shapes expose totals at the top level, no ``usage`` object."""
    metrics = parsers.extract_claude_metrics(
        {
            "type": "result",
            "total_input_tokens": 300,
            "total_output_tokens": 120,
        }
    )
    assert metrics.usage_details == {
        "total_input_tokens": 300,
        "total_output_tokens": 120,
    }


def test_extract_claude_metrics_model_from_init_fallback():
    """When the result event has no model, the init-event model is used."""
    metrics = parsers.extract_claude_metrics(
        {"type": "result", "subtype": "success"}, model="claude-sonnet-4"
    )
    assert metrics.model_reported == "claude-sonnet-4"


def test_extract_claude_metrics_ignores_non_int_token_values():
    metrics = parsers.extract_claude_metrics(
        {
            "type": "result",
            "usage": {
                "input_tokens": 100,
                "output_tokens": True,  # bool is not a token count
                "server_tool_use": {"web_search_requests": 3},  # nested dict skipped
                "output_text": "unexpected",
            },
        }
    )
    assert metrics.usage_details == {"input_tokens": 100}


# =========================================================================
# Codex metric extraction (defensive; usually unknown per S2)
# =========================================================================


def test_extract_codex_metrics_absent_is_unknown():
    assert parsers.extract_codex_metrics(None).cost_source == "unknown"
    assert parsers.extract_codex_metrics({}).usage_details == {}
    assert parsers.extract_codex_metrics({}).cost_source == "unknown"


def test_extract_codex_metrics_reads_usage_when_present():
    metrics = parsers.extract_codex_metrics(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }
    )
    assert metrics.usage_details == {"input_tokens": 12, "output_tokens": 8}


# =========================================================================
# Parser finish() carries metrics into ParsedResult
# =========================================================================


def test_claude_parser_finish_carries_metrics():
    import json

    parser = parsers.ClaudeStreamParser()
    for event in (
        {"type": "system", "subtype": "init", "model": "claude-opus-4"},
        _claude_result_event(),
    ):
        parser.consume_stdout(json.dumps(event) + "\n")
    result = parser.finish(0)
    assert result.result == "the answer"
    assert result.cost_source == "advisor"
    assert result.cost_details == {"total": 0.0123}
    assert result.usage_details["input_tokens"] == 100
    assert result.duration_ms == 5000
    assert result.model_reported == "claude-opus-4"


def test_claude_parser_no_result_event_metrics_unknown():
    parser = parsers.ClaudeStreamParser()
    parser.consume_stdout("garbage line\n")
    result = parser.finish(0)
    assert result.failure is True
    assert result.cost_source == "unknown"
    assert result.usage_details == {}
    assert result.duration_ms is None


def test_parsed_result_defaults_are_unknown():
    result = parsers.ParsedResult()
    assert result.usage_details == {}
    assert result.cost_details == {}
    assert result.cost_source == "unknown"
    assert result.duration_ms is None
    assert result.model_reported is None


# =========================================================================
# Durable persistence on the Job record (schema v3)
# =========================================================================


def test_new_job_default_schema_version_is_3():
    assert Job().schema_version == 3


def test_job_metrics_round_trip(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_metrics")
    job = Job(
        job_id="job_metrics",
        status=JobState.SUCCEEDED,
        advisor="claude",
        usage_details={"input_tokens": 100, "output_tokens": 50},
        cost_details={"total": 0.0123},
        cost_source="advisor",
        duration_ms=5000,
        model_reported="claude-opus-4",
    )
    save_state(job_dir, job)
    loaded = load_state(job_dir)
    assert loaded == job
    assert loaded.usage_details == {"input_tokens": 100, "output_tokens": 50}
    assert loaded.cost_details == {"total": 0.0123}
    assert loaded.cost_source == "advisor"
    assert loaded.duration_ms == 5000
    assert loaded.model_reported == "claude-opus-4"


def test_v2_record_loads_with_unknown_metrics(tmp_path):
    """D7: a v2 record on disk loads and reports metrics as unknown, not zero."""
    job_dir = create_job_dir(tmp_path, "job_v2_metrics")
    atomic_json_write(
        {
            "schema_version": 2,
            "job_id": "job_v2_metrics",
            "status": "succeeded",
            "advisor": "claude",
            "trace_id": "trace_old",
        },
        job_dir / "state.json",
    )
    loaded = load_state(job_dir)
    assert loaded.schema_version == 2
    assert loaded.usage_details == {}
    assert loaded.cost_details == {}
    assert loaded.cost_source == "unknown"
    assert loaded.duration_ms is None
    assert loaded.model_reported is None
    # Distinguishable from a measured zero.
    assert loaded.cost_details != {"total": 0.0}


def test_load_state_accepts_v3(tmp_path):
    job_dir = create_job_dir(tmp_path, "job_v3")
    atomic_json_write(
        {
            "schema_version": 3,
            "job_id": "job_v3",
            "status": "succeeded",
            "cost_source": "advisor",
            "cost_details": {"total": 0.5},
            "usage_details": {"input_tokens": 10},
        },
        job_dir / "state.json",
    )
    loaded = load_state(job_dir)
    assert loaded.schema_version == 3
    assert loaded.cost_details == {"total": 0.5}
    assert loaded.usage_details == {"input_tokens": 10}
    assert loaded.cost_source == "advisor"


def test_runtime_status_surfaces_metrics():
    job = Job(
        job_id="job_rt_metrics",
        status=JobState.SUCCEEDED,
        started_at="2026-07-19T10:00:00Z",
        usage_details={"input_tokens": 100},
        cost_details={"total": 0.02},
        cost_source="advisor",
        duration_ms=1234,
        model_reported="claude-opus-4",
    )
    status = runtime_status(job)
    assert status["usage_details"] == {"input_tokens": 100}
    assert status["cost_details"] == {"total": 0.02}
    assert status["cost_source"] == "advisor"
    assert status["duration_ms"] == 1234
    assert status["model_reported"] == "claude-opus-4"


def test_runtime_status_metrics_default_unknown():
    job = Job(
        job_id="job_rt_unknown",
        status=JobState.RUNNING,
        started_at="2026-07-19T10:00:00Z",
    )
    status = runtime_status(job)
    assert status["cost_source"] == "unknown"
    assert status["usage_details"] == {}
    assert status["cost_details"] == {}
    assert status["duration_ms"] is None


# =========================================================================
# CLI surfacing (list column, result footer)
# =========================================================================


def test_format_cost_measured_and_unmeasured():
    measured = Job(
        job_id="j",
        cost_details={"total": 0.0123},
        cost_source="advisor",
    )
    assert _format_cost(measured) == "$0.0123"
    # A measured zero still renders as an amount, not "-".
    zero = Job(job_id="j", cost_details={"total": 0.0}, cost_source="advisor")
    assert _format_cost(zero) == "$0.0000"
    # Unmeasured shows a dash, never "$0".
    unknown = Job(job_id="j")
    assert _format_cost(unknown) == "-"


def test_list_table_has_cost_column(capsys):
    job = Job(
        job_id="job_table",
        status=JobState.SUCCEEDED,
        advisor="claude",
        started_at="2026-07-19T10:00:00Z",
        finished_at="2026-07-19T10:00:05Z",
        duration_seconds=5.0,
        cost_details={"total": 0.0123},
        cost_source="advisor",
    )
    _print_job_table([job])
    out = capsys.readouterr().out
    assert "COST" in out
    assert "$0.0123" in out


def test_metrics_summary_measured_and_unmeasured():
    measured = Job(
        job_id="j",
        cost_details={"total": 0.02},
        cost_source="advisor",
        usage_details={"input_tokens": 100, "output_tokens": 50},
        duration_ms=1234,
        model_reported="claude-opus-4",
    )
    summary = _metrics_summary(measured)
    assert "cost=$0.0200 (advisor)" in summary
    assert "tokens=100 in / 50 out" in summary
    assert "duration=1234ms" in summary
    assert "model=claude-opus-4" in summary
    # Nothing measured -> empty summary (caller prints nothing).
    assert _metrics_summary(Job(job_id="j")) == ""
