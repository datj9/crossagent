import json

from crossagent import registry


def test_session_key_slugifies_names():
    assert (
        registry.session_key("claude", " Payment retry / API ")
        == "claude:payment-retry-api"
    )
    assert registry.session_key("claude", None) == ""


def test_record_persists_and_loads_session(tmp_path):
    path = tmp_path / "sessions.json"
    initial = {"sessions": {}}

    updated = registry.record(
        path,
        initial,
        "claude:payment-retry",
        session_id="session-123",
        name="payment-retry",
        cwd=str(tmp_path),
        advisor="claude",
        model="sonnet",
    )

    assert initial == {"sessions": {}}
    assert registry.stored_session_id(updated, "claude:payment-retry") == "session-123"
    assert registry.load(path) == updated


def test_record_persists_the_reasoning_effort(tmp_path):
    path = tmp_path / "sessions.json"

    updated = registry.record(
        path,
        {"sessions": {}},
        "codex:payment-retry",
        session_id="thread-123",
        name="payment-retry",
        cwd=str(tmp_path),
        advisor="codex",
        model="gpt-6-astra",
        reasoning="low",
    )

    assert updated["sessions"]["codex:payment-retry"]["reasoning"] == "low"
    assert registry.load(path) == updated


def test_record_defaults_reasoning_to_empty(tmp_path):
    path = tmp_path / "sessions.json"

    updated = registry.record(
        path,
        {"sessions": {}},
        "claude:payment-retry",
        session_id="session-123",
        name="payment-retry",
        cwd=str(tmp_path),
        advisor="claude",
        model="sonnet",
    )

    assert updated["sessions"]["claude:payment-retry"]["reasoning"] == ""


def test_worker_load_command_tolerates_a_reasoning_key(tmp_path):
    from crossagent import worker as worker_mod

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "command.json").write_text(
        json.dumps(
            {
                "command": ["codex", "exec"],
                "prompt_delivery": "positional",
                "cwd": str(tmp_path),
                "result_parser": "codex-jsonl",
                "registry_path": str(tmp_path / "sessions.json"),
                "key": "codex:topic-a",
                "name": "topic-a",
                "model": "gpt-6-astra",
                "reasoning": "low",
                "advisor": "codex",
            }
        ),
        encoding="utf-8",
    )

    command = worker_mod._load_command(job_dir)

    assert command.model == "gpt-6-astra"
    assert not hasattr(command, "reasoning")


def test_load_recovers_from_corrupt_registry(tmp_path, capsys):
    path = tmp_path / "sessions.json"
    path.write_text("{invalid json", encoding="utf-8")

    loaded = registry.load(path)

    assert loaded == {"sessions": {}}
    assert not path.exists()
    assert (
        path.with_suffix(".json.corrupt").read_text(encoding="utf-8") == "{invalid json"
    )
    assert "Registry was invalid JSON" in capsys.readouterr().err


def test_load_normalizes_non_mapping_sessions(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"sessions": []}), encoding="utf-8")

    assert registry.load(path) == {"sessions": {}}
