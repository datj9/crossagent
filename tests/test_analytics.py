"""Tests for the rollup analytics module (slice S6).

These assert the measurement contract that makes the dashboard a trustworthy
instrument rather than a pretty lie:

  1. Tokens are summed non-additively (D2) — cache/reasoning subsets never
     inflate a total.
  2. Unmeasured metrics are excluded from denominators and stay distinguishable
     from a measured zero (D3/D7).
  3. Legacy schema 1/2 records appear in counts without contributing to any
     measured total and without breaking a rollup.
  4. Check-pass and escalation rates derive from the verdict, not process exit.
  5. Rollups are per advisor AND per resolved model, codex's absent model id
     landing in an explicit "not reported" bucket.
"""

from __future__ import annotations

import pytest

from crossagent.analytics import build_analytics
from crossagent.jobs import Job, JobState


def _job(job_id: str, advisor: str, status: JobState, **overrides) -> Job:
    return Job(job_id=job_id, advisor=advisor, status=status, **overrides)


def _find(rollups: list[dict], key) -> dict:
    for rollup in rollups:
        if rollup["key"] == key:
            return rollup
    raise AssertionError(f"no rollup for key {key!r}")


# ---------------------------------------------------------------------------
# Token non-additivity (D2)
# ---------------------------------------------------------------------------


def test_token_total_excludes_claude_cache_subsets():
    job = _job(
        "job_1",
        "claude",
        JobState.SUCCEEDED,
        usage_details={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 200,
        },
    )
    tokens = build_analytics([job])["totals"]["tokens"]
    # Only input + output — cache_* are subsets, not additions.
    assert tokens["input"] == 100
    assert tokens["output"] == 20
    assert tokens["total"] == 120


def test_token_total_excludes_codex_reasoning_subset():
    job = _job(
        "job_1",
        "codex",
        JobState.SUCCEEDED,
        usage_details={
            "input_tokens": 50,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 5,
            "output_tokens": 8,
            "reasoning_output_tokens": 30,
        },
    )
    tokens = build_analytics([job])["totals"]["tokens"]
    assert tokens["input"] == 50
    assert tokens["output"] == 8
    assert tokens["total"] == 58


def test_token_total_handles_commandcode_camelcase():
    job = _job(
        "job_1",
        "commandcode",
        JobState.SUCCEEDED,
        usage_details={
            "inputTokens": 70,
            "outputTokens": 12,
            "cacheReadTokens": 90,
            "cacheWriteTokens": 3,
        },
    )
    tokens = build_analytics([job])["totals"]["tokens"]
    assert tokens["input"] == 70
    assert tokens["output"] == 12
    assert tokens["total"] == 82


# ---------------------------------------------------------------------------
# Unmeasured is never zero (D3/D7)
# ---------------------------------------------------------------------------


def test_unmeasured_cost_is_none_not_zero():
    job = _job("job_1", "codex", JobState.SUCCEEDED)  # codex emits no cost
    cost = build_analytics([job])["totals"]["cost"]
    assert cost["measured_count"] == 0
    assert cost["total"] is None
    assert cost["mean"] is None
    assert cost["source"] is None


def test_measured_zero_cost_stays_zero_not_none():
    # A genuinely measured $0 is distinct from unmeasured — it must survive.
    job = _job(
        "job_1",
        "claude",
        JobState.SUCCEEDED,
        cost_details={"total": 0.0},
        cost_source="advisor",
    )
    cost = build_analytics([job])["totals"]["cost"]
    assert cost["measured_count"] == 1
    assert cost["total"] == 0.0
    assert cost["mean"] == 0.0


def test_mean_cost_excludes_unmeasured_from_denominator():
    jobs = [
        _job(
            "job_1",
            "claude",
            JobState.SUCCEEDED,
            cost_details={"total": 1.0},
            cost_source="advisor",
        ),
        _job("job_2", "codex", JobState.SUCCEEDED),  # unmeasured
        _job("job_3", "gemini", JobState.SUCCEEDED),  # unmeasured
    ]
    cost = build_analytics(jobs)["totals"]["cost"]
    # Mean is over the ONE measured job, not diluted to 1/3 by treating the
    # unmeasured pair as $0.
    assert cost["measured_count"] == 1
    assert cost["total"] == 1.0
    assert cost["mean"] == 1.0


def test_unmeasured_duration_and_tokens_are_none():
    job = _job("job_1", "gemini", JobState.SUCCEEDED)
    totals = build_analytics([job])["totals"]
    assert totals["duration"]["total_ms"] is None
    assert totals["duration"]["mean_ms"] is None
    assert totals["tokens"]["total"] is None
    assert totals["tokens"]["measured_count"] == 0


def test_cost_source_mixed_label():
    jobs = [
        _job(
            "job_1",
            "claude",
            JobState.SUCCEEDED,
            cost_details={"total": 1.0},
            cost_source="advisor",
        ),
        _job(
            "job_2",
            "codex",
            JobState.SUCCEEDED,
            cost_details={"total": 0.2},
            cost_source="computed",
        ),
    ]
    cost = build_analytics(jobs)["totals"]["cost"]
    assert cost["source"] == "mixed"
    assert cost["total"] == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# Legacy schema records (D7)
# ---------------------------------------------------------------------------


def test_legacy_v1_v2_records_appear_without_breaking_rollups():
    jobs = [
        _job("job_v1", "claude", JobState.SUCCEEDED, schema_version=1),
        _job("job_v2", "codex", JobState.FAILED, schema_version=2),
    ]
    analytics = build_analytics(jobs)
    assert analytics["job_count"] == 2
    totals = analytics["totals"]
    # They count by state/verdict but contribute nothing measured.
    assert totals["states"]["succeeded"] == 1
    assert totals["states"]["failed"] == 1
    assert totals["cost"]["total"] is None
    assert totals["tokens"]["total"] is None


# ---------------------------------------------------------------------------
# Verdict-derived check-pass rate (D5)
# ---------------------------------------------------------------------------


def test_check_pass_rate_counts_only_definitive_verdicts():
    jobs = [
        _job(
            "job_pass",
            "claude",
            JobState.SUCCEEDED,
            check_result={
                "command": "pytest",
                "exit_code": 0,
                "stdout_tail": "",
                "stderr_tail": "",
            },
        ),
        _job(
            "job_fail_check",
            "claude",
            JobState.SUCCEEDED,
            check_result={
                "command": "pytest",
                "exit_code": 1,
                "stdout_tail": "",
                "stderr_tail": "",
            },
        ),
        _job("job_unverified", "claude", JobState.SUCCEEDED),  # no check
        _job("job_running", "claude", JobState.RUNNING),  # incomplete
    ]
    check = build_analytics(jobs)["totals"]["check"]
    # verified=1, failed=1 → measured 2; unverified/incomplete excluded.
    assert check["measured"] == 2
    assert check["passed"] == 1
    assert check["pass_rate"] == 0.5


def test_process_failure_is_a_failed_delegation():
    # A delegate that crashed (non-SUCCEEDED terminal) is a failed delegation
    # regardless of any check — it drags the pass rate down, as it should.
    jobs = [
        _job(
            "job_pass",
            "claude",
            JobState.SUCCEEDED,
            check_result={
                "command": "pytest",
                "exit_code": 0,
                "stdout_tail": "",
                "stderr_tail": "",
            },
        ),
        _job("job_crashed", "claude", JobState.FAILED),
    ]
    check = build_analytics(jobs)["totals"]["check"]
    assert check["measured"] == 2
    assert check["passed"] == 1
    assert check["pass_rate"] == 0.5


def test_pass_rate_is_none_when_nothing_checked():
    jobs = [
        _job("job_1", "claude", JobState.SUCCEEDED),  # unverified
        _job("job_2", "claude", JobState.RUNNING),  # incomplete
    ]
    check = build_analytics(jobs)["totals"]["check"]
    assert check["measured"] == 0
    assert check["pass_rate"] is None


# ---------------------------------------------------------------------------
# Escalation rate (lineage-derived)
# ---------------------------------------------------------------------------


def test_escalation_rate_none_when_no_failures():
    job = _job(
        "job_1",
        "claude",
        JobState.SUCCEEDED,
        check_result={
            "command": "c",
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )
    escalation = build_analytics([job])["totals"]["escalation"]
    assert escalation["failed"] == 0
    assert escalation["rate"] is None


def test_escalation_counts_redispatched_failed_parent():
    parent = _job(
        "job_parent",
        "codex",
        JobState.FAILED,
        trace_id="trace_x",
    )
    # A same-trace child re-dispatched after the parent failed = an escalation.
    child = _job(
        "job_child",
        "claude",
        JobState.SUCCEEDED,
        trace_id="trace_x",
        parent_job_id="job_parent",
        check_result={
            "command": "c",
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )
    escalation = build_analytics([parent, child])["totals"]["escalation"]
    assert escalation["failed"] == 1
    assert escalation["escalated"] == 1
    assert escalation["rate"] == 1.0


def test_cross_trace_child_is_not_an_escalation():
    parent = _job("job_parent", "codex", JobState.FAILED, trace_id="trace_a")
    child = _job(
        "job_child",
        "claude",
        JobState.SUCCEEDED,
        trace_id="trace_b",  # different trace — a data inconsistency, not a retry
        parent_job_id="job_parent",
    )
    escalation = build_analytics([parent, child])["totals"]["escalation"]
    assert escalation["escalated"] == 0
    assert escalation["rate"] == 0.0


# ---------------------------------------------------------------------------
# Grouping: per advisor and per resolved model
# ---------------------------------------------------------------------------


def test_per_advisor_and_per_model_grouping():
    jobs = [
        _job(
            "job_1",
            "claude",
            JobState.SUCCEEDED,
            model_reported="claude-opus-5",
            usage_details={"input_tokens": 10, "output_tokens": 2},
        ),
        _job(
            "job_2",
            "claude",
            JobState.SUCCEEDED,
            model_reported="claude-sonnet-5",
            usage_details={"input_tokens": 20, "output_tokens": 4},
        ),
        _job("job_3", "codex", JobState.SUCCEEDED),  # no model reported
    ]
    analytics = build_analytics(jobs)

    claude = _find(analytics["by_advisor"], "claude")
    assert claude["job_count"] == 2
    assert claude["tokens"]["total"] == 36  # (10+2) + (20+4)

    opus = _find(analytics["by_model"], "claude-opus-5")
    assert opus["tokens"]["total"] == 12
    # codex reports no model id — it lands in an explicit None bucket, visible.
    none_bucket = _find(analytics["by_model"], None)
    assert none_bucket["job_count"] == 1


def test_by_model_none_group_sorts_last():
    jobs = [
        _job("job_1", "codex", JobState.SUCCEEDED),  # None model
        _job("job_2", "claude", JobState.SUCCEEDED, model_reported="claude-opus-5"),
        _job("job_3", "claude", JobState.SUCCEEDED, model_reported="claude-opus-5"),
    ]
    by_model = build_analytics(jobs)["by_model"]
    assert by_model[0]["key"] == "claude-opus-5"  # highest count first
    assert by_model[-1]["key"] is None  # None bucket always last


def test_empty_jobs_yields_empty_rollups():
    analytics = build_analytics([])
    assert analytics["job_count"] == 0
    assert analytics["by_advisor"] == []
    assert analytics["by_model"] == []
    assert analytics["totals"]["cost"]["total"] is None
