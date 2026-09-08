"""Graph model builder for job lineage visualization.

Produces a directed graph of job nodes and synthesized orchestrator-root
nodes from a flat job listing, with cycle-safe edge linking and diagnostics
for any data-integrity issues found.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .jobs import Job


def build_graph(jobs: list[Job]) -> dict[str, Any]:
    """Build a full graph model from *jobs*.

    Returns a dict with ``schema_version``, ``revision``,
    ``meaningful_edge_count``, ``nodes``, ``edges``, and ``diagnostics``.
    """
    by_id: dict[str, Job] = {j.job_id: j for j in jobs}

    trace_to_jobs: dict[str, list[Job]] = {}
    for j in jobs:
        if j.trace_id is not None:
            trace_to_jobs.setdefault(j.trace_id, []).append(j)

    nodes: list[dict[str, Any]] = []
    for j in jobs:
        nodes.append(_job_node(j))

    trace_roots: dict[str, str] = _build_trace_roots(trace_to_jobs, nodes)

    edges, diagnostics, meaningful_edge_count = _build_edges(jobs, by_id, trace_roots)

    _remove_cycle_edges(jobs, by_id, edges, diagnostics)
    meaningful_edge_count = _recount_job_job_edges(edges)

    revision = _compute_revision(nodes, edges, diagnostics)

    return {
        "schema_version": 1,
        "revision": revision,
        "meaningful_edge_count": meaningful_edge_count,
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# Node construction
# ---------------------------------------------------------------------------


def _job_node(job: Job) -> dict[str, Any]:
    return {
        "id": job.job_id,
        "kind": "job",
        "trace_id": job.trace_id,
        "parent_job_id": job.parent_job_id,
        "advisor": job.advisor,
        "name": job.name,
        "status": job.status.value,
        "started_at": job.started_at,
        "nesting_depth": job.nesting_depth,
    }


def _build_trace_roots(
    trace_to_jobs: dict[str, list[Job]],
    nodes: list[dict[str, Any]],
) -> dict[str, str]:
    trace_roots: dict[str, str] = {}
    for tid, tjobs in trace_to_jobs.items():
        top_level = [j for j in tjobs if j.parent_job_id is None]
        if top_level:
            top_level.sort(key=lambda j: (j.started_at or "", j.job_id))
            chosen = top_level[0]
            label = chosen.orchestrator_label or "External caller"
        else:
            label = "External caller"
        root_id = f"root:{tid}"
        trace_roots[tid] = root_id
        nodes.append(
            {
                "id": root_id,
                "kind": "orchestrator",
                "trace_id": tid,
                "label": label,
            }
        )
    return trace_roots


# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------


def _build_edges(
    jobs: list[Job],
    by_id: dict[str, Job],
    trace_roots: dict[str, str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], int]:
    edges: list[dict[str, str]] = []
    diagnostics: list[dict[str, Any]] = []
    meaningful_edge_count = 0

    for j in jobs:
        if j.trace_id is None:
            continue
        root_id = trace_roots[j.trace_id]

        if j.parent_job_id is None:
            edges.append({"from": root_id, "to": j.job_id, "kind": "spawned"})
        elif j.parent_job_id in by_id:
            parent = by_id[j.parent_job_id]
            if parent.trace_id == j.trace_id:
                edges.append(
                    {"from": j.parent_job_id, "to": j.job_id, "kind": "spawned"}
                )
                meaningful_edge_count += 1
            else:
                # Parent belongs to a different trace — do not merge the two
                # trees; attach the child to its own root and flag the mismatch.
                edges.append({"from": root_id, "to": j.job_id, "kind": "spawned"})
                diagnostics.append(
                    {
                        "type": "trace_mismatch",
                        "job_id": j.job_id,
                        "parent_job_id": j.parent_job_id,
                    }
                )
        else:
            edges.append({"from": root_id, "to": j.job_id, "kind": "spawned"})
            diagnostics.append(
                {
                    "type": "missing_parent",
                    "job_id": j.job_id,
                    "parent_job_id": j.parent_job_id,
                }
            )

    return edges, diagnostics, meaningful_edge_count


def _remove_cycle_edges(
    jobs: list[Job],
    by_id: dict[str, Job],
    edges: list[dict[str, str]],
    diagnostics: list[dict[str, Any]],
) -> None:
    for j in jobs:
        if j.trace_id is None or j.parent_job_id is None:
            continue
        if j.parent_job_id not in by_id:
            continue

        # Unbounded walk over the finite in-memory node set: the visited set
        # guarantees termination (at most one hop per known job), so cycles of
        # any length are detected — no arbitrary hop cap that could miss one.
        visited: set[str] = {j.job_id}
        current: str | None = j.parent_job_id
        is_cycle = False
        while current is not None:
            if current in visited:
                is_cycle = True
                break
            visited.add(current)
            parent = by_id.get(current)
            if parent is None:
                break
            current = parent.parent_job_id

        if is_cycle:
            _pop_edge(edges, j.parent_job_id, j.job_id)
            diagnostics.append({"type": "cycle", "job_id": j.job_id})


def _pop_edge(edges: list[dict[str, str]], from_id: str, to_id: str) -> None:
    for i in range(len(edges) - 1, -1, -1):
        if edges[i]["from"] == from_id and edges[i]["to"] == to_id:
            edges.pop(i)
            return


def _recount_job_job_edges(edges: list[dict[str, str]]) -> int:
    count = 0
    for e in edges:
        if not e["from"].startswith("root:"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Revision hash
# ---------------------------------------------------------------------------


def _compute_revision(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    diagnostics: list[dict[str, Any]],
) -> str:
    """Hash the topology + per-node status + diagnostics.

    Deliberately excludes volatile/display-only fields (elapsed/idle, name,
    advisor, started_at, root label) so the client only relayouts on a change
    that affects the graph's shape or a job's status. Diagnostics ARE included
    so that, e.g., an orphan whose missing-parent id changes yields a new
    revision even when the edge set is unchanged.
    """
    sorted_nodes = sorted(nodes, key=lambda n: n["id"])
    sorted_edges = sorted(edges, key=lambda e: (e["from"], e["to"]))
    sorted_diags = sorted(
        diagnostics,
        key=lambda d: (
            d.get("type", ""),
            d.get("job_id", ""),
            d.get("parent_job_id", ""),
        ),
    )

    parts: list[str] = []
    for n in sorted_nodes:
        if n.get("kind") == "job":
            parts.append(f"{n['id']}:job:{n.get('status', '')}")
        else:
            parts.append(f"{n['id']}:{n.get('kind', '')}")
    for e in sorted_edges:
        parts.append(f"{e['from']}->{e['to']}:{e['kind']}")
    for d in sorted_diags:
        parts.append(
            f"diag:{d.get('type', '')}:{d.get('job_id', '')}:{d.get('parent_job_id', '')}"
        )

    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
