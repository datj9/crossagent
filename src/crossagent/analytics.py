"""Rollup analytics over persisted job records (slice S6).

Computes per-advisor and per-model summaries — cost, tokens, duration, verdict
distribution, check-pass rate, escalation rate, and job counts by state — from
the durable :class:`~crossagent.jobs.Job` records the dashboard already reads.

Binding decisions honoured here:

- **Rollups come only from persisted records**, never a re-parse of raw advisor
  logs — persistence is the durable path and re-parsing would drift from it.
- **Tokens are not additive (D2).** ``cache_*`` and ``reasoning_*`` counts are
  subsets of the input/output totals, so a job's token total sums only the
  primary input/output buckets — never every ``usage_details`` key, and never a
  pre-summed field. See :func:`_token_totals`.
- **Unmeasured is never zero (D3/D7).** A metric with no measurement contributes
  nothing to a total and is excluded from the mean's denominator; the payload
  reports ``measured_count`` alongside ``job_count`` so the UI can say how many
  of the group's jobs were actually measured rather than implying zero.
- **Old records (schema 1/2) predate every metric field.** They load with empty
  metric maps and ``cost_source == "unknown"``, so they appear in every count by
  state and verdict but simply do not contribute to a measured total — no rollup
  breaks on them.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .jobs import (
    DelegationVerdict,
    Job,
    delegation_verdict,
    is_terminal,
)

# Payload schema version — bump when the analytics envelope shape changes.
ANALYTICS_SCHEMA_VERSION = 1

# The four verdicts, in the order the UI presents them.
_VERDICTS: tuple[DelegationVerdict, ...] = (
    "verified",
    "failed",
    "unverified",
    "incomplete",
)


def build_analytics(jobs: list[Job]) -> dict[str, Any]:
    """Build the full analytics payload from a flat job listing.

    Returns a dict with ``schema_version``, ``job_count``, an overall
    ``totals`` rollup, and ``by_advisor`` / ``by_model`` lists of rollups. Each
    rollup is the shape produced by :func:`_rollup`.
    """
    escalated_parent_ids = _escalated_parent_ids(jobs)

    by_advisor = [
        _rollup(key, group, escalated_parent_ids)
        for key, group in _group_by(jobs, _advisor_key).items()
    ]
    by_model = [
        _rollup(key, group, escalated_parent_ids)
        for key, group in _group_by(jobs, _model_key).items()
    ]

    return {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "job_count": len(jobs),
        "totals": _rollup(None, jobs, escalated_parent_ids),
        "by_advisor": _sort_rollups(by_advisor),
        "by_model": _sort_rollups(by_model),
    }


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _advisor_key(job: Job) -> Optional[str]:
    """Group key for the per-advisor rollup (empty advisor stays visible)."""
    return job.advisor or None


def _model_key(job: Job) -> Optional[str]:
    """Group key for the per-model rollup.

    ``None`` is a real group: codex reports no model id (S2), so those jobs must
    appear under an explicit "not reported" bucket rather than vanish.
    """
    return job.model_reported


def _group_by(
    jobs: list[Job], key_of: Callable[[Job], Optional[str]]
) -> dict[Optional[str], list[Job]]:
    groups: dict[Optional[str], list[Job]] = {}
    for job in jobs:
        groups.setdefault(key_of(job), []).append(job)
    return groups


def _sort_rollups(rollups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort rollups by job count (desc), then key — ``None`` key last."""
    return sorted(
        rollups,
        key=lambda r: (-r["job_count"], r["key"] is None, str(r["key"] or "")),
    )


# ---------------------------------------------------------------------------
# Escalation detection (lineage-derived)
# ---------------------------------------------------------------------------


def _escalated_parent_ids(jobs: list[Job]) -> set[str]:
    """Return ids of jobs that were re-dispatched (have a child in the same trace).

    An escalation (slice S5) re-dispatches a failed delegation to a larger peer,
    recording the retry as a child under the same ``trace_id``. Until S5 lands no
    such child is written, so this set is empty and every escalation rate reads
    ``0`` over its measured denominator — honest, not fabricated. A parent counts
    as escalated once regardless of how many children it spawned.
    """
    by_id = {job.job_id: job for job in jobs}
    escalated: set[str] = set()
    for job in jobs:
        parent_id = job.parent_job_id
        if parent_id is None or parent_id not in by_id:
            continue
        parent = by_id[parent_id]
        # Only a same-trace child counts — a cross-trace parent link is a data
        # inconsistency (the graph flags it), not a real escalation.
        if parent.trace_id is not None and parent.trace_id == job.trace_id:
            escalated.add(parent_id)
    return escalated


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def _rollup(
    key: Optional[str],
    jobs: list[Job],
    escalated_parent_ids: set[str],
) -> dict[str, Any]:
    """Summarise one group of jobs into a rollup dict."""
    verdicts = {v: 0 for v in _VERDICTS}
    states: dict[str, int] = {}
    for job in jobs:
        verdicts[delegation_verdict(job)] += 1
        states[job.status.value] = states.get(job.status.value, 0) + 1

    return {
        "key": key,
        "job_count": len(jobs),
        "terminal_count": sum(1 for job in jobs if is_terminal(job.status)),
        "states": states,
        "verdicts": verdicts,
        "check": _check_rollup(verdicts),
        "escalation": _escalation_rollup(jobs, verdicts, escalated_parent_ids),
        "cost": _cost_rollup(jobs),
        "tokens": _token_rollup(jobs),
        "duration": _duration_rollup(jobs),
    }


def _check_rollup(verdicts: dict[DelegationVerdict, int]) -> dict[str, Any]:
    """Check-pass rate over jobs with a definitive verdict (verified/failed).

    ``unverified`` (finished, no gate) and ``incomplete`` (still running) carry
    no pass/fail signal, so they are excluded from the denominator — a missing
    gate is never counted as a pass. ``pass_rate`` is ``None`` when nothing was
    checked, which the UI renders "not measured", never ``0``.
    """
    passed = verdicts["verified"]
    measured = passed + verdicts["failed"]
    return {
        "measured": measured,
        "passed": passed,
        "pass_rate": (passed / measured) if measured else None,
    }


def _escalation_rollup(
    jobs: list[Job],
    verdicts: dict[DelegationVerdict, int],
    escalated_parent_ids: set[str],
) -> dict[str, Any]:
    """Escalation rate over failed delegations in this group.

    Denominator is the group's failed delegations; numerator is those that were
    re-dispatched (:func:`_escalated_parent_ids`). ``rate`` is ``None`` when
    nothing failed — again "not measured" rather than a misleading ``0``.
    """
    failed = verdicts["failed"]
    escalated = sum(
        1
        for job in jobs
        if delegation_verdict(job) == "failed" and job.job_id in escalated_parent_ids
    )
    return {
        "failed": failed,
        "escalated": escalated,
        "rate": (escalated / failed) if failed else None,
    }


def _cost_rollup(jobs: list[Job]) -> dict[str, Any]:
    """Total and mean USD cost over jobs that actually measured a cost.

    Only ``advisor``/``computed`` sources with a non-empty ``cost_details``
    count; ``unknown`` jobs are excluded from both the sum and the mean's
    denominator. ``source`` reports provenance ("advisor", "computed", "mixed",
    or ``None`` when nothing was measured) so cost is never mistaken for billing
    truth (D3).
    """
    measured = 0
    total = 0.0
    sources: set[str] = set()
    for job in jobs:
        if job.cost_source == "unknown" or not job.cost_details:
            continue
        measured += 1
        total += _job_cost(job)
        sources.add(job.cost_source)
    return {
        "measured_count": measured,
        "total": total if measured else None,
        "mean": (total / measured) if measured else None,
        "source": _source_label(sources),
    }


def _token_rollup(jobs: list[Job]) -> dict[str, Any]:
    """Token totals over jobs that reported any token telemetry.

    A job counts as measured when ``usage_details`` is non-empty. Totals sum only
    the primary input/output buckets (D2) — never cache/reasoning subsets and
    never every key — so cache reads are not double-counted.
    """
    measured = 0
    input_total = 0
    output_total = 0
    for job in jobs:
        if not job.usage_details:
            continue
        measured += 1
        job_input, job_output = _token_totals(job.usage_details)
        input_total += job_input
        output_total += job_output
    total = input_total + output_total
    return {
        "measured_count": measured,
        "input": input_total if measured else None,
        "output": output_total if measured else None,
        "total": total if measured else None,
        "mean": (total / measured) if measured else None,
    }


def _duration_rollup(jobs: list[Job]) -> dict[str, Any]:
    """Total and mean wall-clock duration over jobs that measured one."""
    measured = 0
    total_ms = 0
    for job in jobs:
        if job.duration_ms is None:
            continue
        measured += 1
        total_ms += job.duration_ms
    return {
        "measured_count": measured,
        "total_ms": total_ms if measured else None,
        "mean_ms": (total_ms / measured) if measured else None,
    }


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def _job_cost(job: Job) -> float:
    """Return a single job's cost from its ``cost_details`` map.

    The parsers store a scalar cost under a derived ``"total"`` key (D3); prefer
    it when present, else sum the map's values.
    """
    details = job.cost_details
    if "total" in details:
        return float(details["total"])
    return float(sum(details.values()))


def _token_totals(usage_details: dict[str, int]) -> tuple[int, int]:
    """Return ``(input_total, output_total)`` from an open-keyed usage map.

    Only *primary* buckets contribute: a key is a subset (excluded) when its name
    mentions ``cache`` or ``reasoning`` — those are portions of the input/output
    totals, not additions (D2). Among the remaining primary keys, "input" and
    "output" buckets are summed separately; any primary key mentioning neither is
    ignored rather than risk folding in a pre-summed total. Case-insensitive so
    the rule holds for snake_case (claude/codex) and camelCase (commandcode)
    alike.
    """
    input_total = 0
    output_total = 0
    for key, count in usage_details.items():
        lowered = key.lower()
        if "cache" in lowered or "reasoning" in lowered:
            continue
        if "input" in lowered:
            input_total += count
        elif "output" in lowered:
            output_total += count
    return input_total, output_total


def _source_label(sources: set[str]) -> Optional[str]:
    """Collapse a set of cost sources into a single provenance label."""
    if not sources:
        return None
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


__all__ = ["build_analytics", "ANALYTICS_SCHEMA_VERSION"]
