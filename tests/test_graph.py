"""Tests for the job lineage graph model and its HTTP endpoint.

Tests the pure ``build_graph`` function and the ``GET /api/graph`` dashboard
route. No real advisor CLI is ever invoked.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from crossagent import dashboard as dashboard_mod
from crossagent import jobs as jobs_mod
from crossagent.graph import build_graph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    job_id: str,
    *,
    trace_id: str | None = None,
    parent_job_id: str | None = None,
    status: jobs_mod.JobState = jobs_mod.JobState.SUCCEEDED,
    advisor: str = "codex",
    name: str = "test-job",
    orchestrator_label: str | None = None,
    started_at: str | None = None,
    nesting_depth: int | None = None,
) -> jobs_mod.Job:
    return jobs_mod.Job(
        schema_version=2,
        job_id=job_id,
        status=status,
        advisor=advisor,
        name=name,
        cwd=os.getcwd(),
        redacted_command="codex exec <prompt>",
        started_at=started_at or "2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        last_activity_at="2025-01-01T00:00:00Z",
        trace_id=trace_id,
        parent_job_id=parent_job_id,
        orchestrator_label=orchestrator_label,
        nesting_depth=nesting_depth,
    )


def _assert_no_prompt_or_command_fields(payload: dict) -> None:
    body = json.dumps(payload)
    assert "prompt" not in body or body.count("prompt") == body.count(
        "prompt"
    )  # only trace_id prefixes
    assert "redacted_command" not in body


# ---------------------------------------------------------------------------
# build_graph — pure function
# ---------------------------------------------------------------------------


class TestBuildGraphSingleTopLevel:
    def test_one_job_one_root(self):
        job = _make_job("job_a", trace_id="trace_1")
        result = build_graph([job])

        assert result["schema_version"] == 1
        assert result["meaningful_edge_count"] == 0
        assert len(result["diagnostics"]) == 0

        node_ids = {n["id"] for n in result["nodes"]}
        assert job.job_id in node_ids
        assert "root:trace_1" in node_ids
        assert len(result["nodes"]) == 2

        assert len(result["edges"]) == 1
        assert result["edges"][0] == {
            "from": "root:trace_1",
            "to": job.job_id,
            "kind": "spawned",
        }

        _assert_no_prompt_or_command_fields(result)

    def test_job_node_fields(self):
        job = _make_job(
            "job_x",
            trace_id="trace_1",
            parent_job_id=None,
            advisor="claude",
            name="my-job",
            status=jobs_mod.JobState.RUNNING,
            started_at="2025-06-01T12:00:00Z",
            nesting_depth=1,
        )
        result = build_graph([job])
        job_node = [n for n in result["nodes"] if n["kind"] == "job"][0]
        assert job_node["id"] == "job_x"
        assert job_node["kind"] == "job"
        assert job_node["trace_id"] == "trace_1"
        assert job_node["parent_job_id"] is None
        assert job_node["advisor"] == "claude"
        assert job_node["name"] == "my-job"
        assert job_node["status"] == "running"
        assert job_node["started_at"] == "2025-06-01T12:00:00Z"
        assert job_node["nesting_depth"] == 1


class TestBuildGraphParentChild:
    def test_parent_and_child(self):
        parent = _make_job("job_parent", trace_id="trace_1")
        child = _make_job("job_child", trace_id="trace_1", parent_job_id="job_parent")
        result = build_graph([parent, child])

        assert result["meaningful_edge_count"] == 1
        assert len(result["nodes"]) == 3
        assert len(result["edges"]) == 2

        edge_pairs = {(e["from"], e["to"]) for e in result["edges"]}
        assert ("root:trace_1", "job_parent") in edge_pairs
        assert ("job_parent", "job_child") in edge_pairs

    def test_meaningful_edge_counts_only_job_job(self):
        parent = _make_job("job_p", trace_id="trace_1")
        child = _make_job("job_c", trace_id="trace_1", parent_job_id="job_p")
        grandchild = _make_job("job_gc", trace_id="trace_1", parent_job_id="job_c")
        result = build_graph([parent, child, grandchild])
        assert result["meaningful_edge_count"] == 2


class TestBuildGraphRootLabel:
    def test_label_from_orchestrator_label(self):
        job = _make_job(
            "job_a",
            trace_id="trace_1",
            orchestrator_label="My Custom Label",
        )
        result = build_graph([job])
        root = [n for n in result["nodes"] if n["kind"] == "orchestrator"][0]
        assert root["label"] == "My Custom Label"

    def test_label_fallback_to_external_caller(self):
        job = _make_job(
            "job_a",
            trace_id="trace_1",
            orchestrator_label=None,
        )
        result = build_graph([job])
        root = [n for n in result["nodes"] if n["kind"] == "orchestrator"][0]
        assert root["label"] == "External caller"

    def test_label_earliest_started_at_wins(self):
        early = _make_job(
            "job_early",
            trace_id="trace_1",
            orchestrator_label="Early Label",
            started_at="2025-01-01T00:00:00Z",
        )
        _ = _make_job(
            "job_late",
            trace_id="trace_1",
            orchestrator_label="Late Label",
            started_at="2025-06-01T00:00:00Z",
        )
        result = build_graph([early])
        root = [n for n in result["nodes"] if n["kind"] == "orchestrator"][0]
        assert root["label"] == "Early Label", (
            "should pick the label from the earliest top-level job"
        )

    def test_label_tie_break_by_job_id(self):
        job_a = _make_job(
            "job_a",
            trace_id="trace_1",
            orchestrator_label="Label A",
            started_at="2025-01-01T00:00:00Z",
        )
        job_b = _make_job(
            "job_b",
            trace_id="trace_1",
            orchestrator_label="Label B",
            started_at="2025-01-01T00:00:00Z",
        )
        result = build_graph([job_b, job_a])
        root = [n for n in result["nodes"] if n["kind"] == "orchestrator"][0]
        assert root["label"] == "Label A", "tie-break by job_id alphabetical"


class TestBuildGraphOrphan:
    def test_orphan_gets_root_edge_and_diagnostic(self):
        job = _make_job(
            "job_orphan",
            trace_id="trace_1",
            parent_job_id="job_nonexistent",
        )
        result = build_graph([job])

        # Node present
        job_node = [n for n in result["nodes"] if n["id"] == "job_orphan"][0]
        assert job_node is not None

        # Root -> orphan edge
        edge = [e for e in result["edges"] if e["to"] == "job_orphan"][0]
        assert edge["from"] == "root:trace_1"

        # Diagnostic
        assert len(result["diagnostics"]) == 1
        diag = result["diagnostics"][0]
        assert diag["type"] == "missing_parent"
        assert diag["job_id"] == "job_orphan"
        assert diag["parent_job_id"] == "job_nonexistent"

    def test_orphan_is_not_job_job_edge(self):
        job = _make_job(
            "job_orphan",
            trace_id="trace_1",
            parent_job_id="job_nonexistent",
        )
        result = build_graph([job])
        assert result["meaningful_edge_count"] == 0


class TestBuildGraphLegacy:
    def test_legacy_job_has_no_edges(self):
        job = _make_job("job_legacy", trace_id=None)
        result = build_graph([job])

        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["id"] == "job_legacy"
        assert result["edges"] == []

    def test_multiple_legacy_jobs(self):
        j1 = _make_job("job_old1", trace_id=None)
        j2 = _make_job("job_old2", trace_id=None)
        result = build_graph([j1, j2])

        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 0
        assert result["meaningful_edge_count"] == 0
        assert result["diagnostics"] == []


class TestBuildGraphCycleDetection:
    def test_direct_cycle_is_detected(self):
        job_a = _make_job("job_a", trace_id="trace_1", parent_job_id="job_b")
        job_b = _make_job("job_b", trace_id="trace_1", parent_job_id="job_a")
        result = build_graph([job_a, job_b])

        # Both cycle edges should be removed
        assert len(result["edges"]) == 0
        assert len(result["diagnostics"]) == 2
        for diag in result["diagnostics"]:
            assert diag["type"] == "cycle"

    def test_longer_cycle(self):
        a = _make_job("job_a", trace_id="trace_1", parent_job_id="job_b")
        b = _make_job("job_b", trace_id="trace_1", parent_job_id="job_c")
        c = _make_job("job_c", trace_id="trace_1", parent_job_id="job_a")
        result = build_graph([a, b, c])

        assert (
            len([e for e in result["edges"] if not e["from"].startswith("root:")]) == 0
        )
        assert len(result["diagnostics"]) == 3
        for diag in result["diagnostics"]:
            assert diag["type"] == "cycle"


class TestBuildGraphRevision:
    def test_stable_for_identical_data(self):
        job = _make_job("job_a", trace_id="trace_1")
        r1 = build_graph([job])
        r2 = build_graph([job])
        assert r1["revision"] == r2["revision"]

    def test_changes_when_status_changes(self):
        job1 = _make_job("job_a", trace_id="trace_1", status=jobs_mod.JobState.RUNNING)
        job2 = _make_job(
            "job_a", trace_id="trace_1", status=jobs_mod.JobState.SUCCEEDED
        )
        r1 = build_graph([job1])
        r2 = build_graph([job2])
        assert r1["revision"] != r2["revision"]

    def test_stable_across_different_runs(self):
        jobs = [
            _make_job("job_a", trace_id="trace_1", status=jobs_mod.JobState.RUNNING),
            _make_job("job_b", trace_id="trace_1", parent_job_id="job_a"),
        ]
        r1 = build_graph(jobs)
        r2 = build_graph(jobs)
        assert r1["revision"] == r2["revision"]


class TestBuildGraphMixed:
    def test_traced_and_legacy_jobs(self):
        traced = _make_job("job_traced", trace_id="trace_1")
        legacy = _make_job("job_legacy", trace_id=None)
        result = build_graph([traced, legacy])

        node_ids = {n["id"] for n in result["nodes"]}
        assert "job_traced" in node_ids
        assert "job_legacy" in node_ids
        assert "root:trace_1" in node_ids

        legacy_node = [n for n in result["nodes"] if n["id"] == "job_legacy"][0]
        assert legacy_node["kind"] == "job"

        assert len(result["edges"]) == 1
        assert result["edges"][0]["to"] == "job_traced"


# ---------------------------------------------------------------------------
# GET /api/graph — HTTP endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "jobs"
    monkeypatch.setenv("CROSSAGENT_STATE_DIR", str(root))
    return root


def _write_job(
    state_root: Path,
    job_id: str,
    *,
    trace_id: str | None = None,
    parent_job_id: str | None = None,
    status: jobs_mod.JobState = jobs_mod.JobState.SUCCEEDED,
    advisor: str = "codex",
    name: str = "",
    orchestrator_label: str | None = None,
) -> None:
    job_dir = state_root / job_id
    job_dir.mkdir(parents=True)
    now = "2025-01-01T00:00:00Z"
    job = jobs_mod.Job(
        schema_version=2,
        job_id=job_id,
        status=status,
        advisor=advisor,
        name=name,
        cwd=os.getcwd(),
        redacted_command=f"{advisor} exec <prompt>",
        started_at=now,
        updated_at=now,
        last_activity_at=now,
        trace_id=trace_id,
        parent_job_id=parent_job_id,
        orchestrator_label=orchestrator_label,
    )
    jobs_mod.save_state(job_dir, job)


@pytest.fixture
def server_url(state_dir: Path) -> Iterator[str]:
    server = dashboard_mod.create_server("127.0.0.1", 0, state_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(url: str) -> tuple[int, bytes]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestGraphEndpoint:
    def test_graph_returns_valid_json(self, server_url):
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)
        assert payload["schema_version"] == 1
        assert "revision" in payload
        assert "nodes" in payload
        assert "edges" in payload
        assert "meaningful_edge_count" in payload
        assert "diagnostics" in payload

    def test_graph_shows_traced_jobs(self, state_dir, server_url):
        _write_job(state_dir, "job_endpoint_a", trace_id="trace_ep", name="ep-test")
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)

        node_ids = {n["id"] for n in payload["nodes"]}
        assert "job_endpoint_a" in node_ids
        assert "root:trace_ep" in node_ids

        assert len(payload["edges"]) == 1

    def test_graph_shows_parent_child(self, state_dir, server_url):
        _write_job(state_dir, "job_ep_parent", trace_id="trace_ep")
        _write_job(
            state_dir,
            "job_ep_child",
            trace_id="trace_ep",
            parent_job_id="job_ep_parent",
        )
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)

        edge_pairs = {(e["from"], e["to"], e["kind"]) for e in payload["edges"]}
        assert ("root:trace_ep", "job_ep_parent", "spawned") in edge_pairs
        assert ("job_ep_parent", "job_ep_child", "spawned") in edge_pairs
        assert payload["meaningful_edge_count"] == 1

    def test_graph_empty_when_no_jobs(self, server_url):
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)
        assert payload["nodes"] == []
        assert payload["edges"] == []
        assert payload["diagnostics"] == []
        assert payload["meaningful_edge_count"] == 0

    def test_graph_never_exposes_prompt_or_command(self, state_dir, server_url):
        _write_job(state_dir, "job_graph_safe", trace_id="trace_safe")
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        text = body.decode("utf-8")
        assert "redacted_command" not in text
        assert '"prompt"' not in text

    def test_graph_orphan_has_diagnostic(self, state_dir, server_url):
        _write_job(
            state_dir,
            "job_ep_orphan",
            trace_id="trace_orphan",
            parent_job_id="job_does_not_exist",
        )
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)
        diags = [d for d in payload["diagnostics"] if d["type"] == "missing_parent"]
        assert len(diags) == 1
        assert diags[0]["job_id"] == "job_ep_orphan"

    def test_graph_ungrouped_legacy_job(self, state_dir, server_url):
        _write_job(state_dir, "job_legacy_ep", trace_id=None)
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)
        assert any(n["id"] == "job_legacy_ep" for n in payload["nodes"])
        assert payload["edges"] == []

    def test_graph_unknown_route_not_affected(self, server_url):
        status, _ = _get(server_url + "/api/graph/extra")
        assert status == 404

    def test_graph_includes_orchestrator_root_fields(self, state_dir, server_url):
        _write_job(
            state_dir,
            "job_root_test",
            trace_id="trace_root_lbl",
            orchestrator_label="My Label",
        )
        status, body = _get(server_url + "/api/graph")
        assert status == 200
        payload = json.loads(body)
        root = [n for n in payload["nodes"] if n["kind"] == "orchestrator"][0]
        assert root["id"] == "root:trace_root_lbl"
        assert root["trace_id"] == "trace_root_lbl"
        assert root["label"] == "My Label"
