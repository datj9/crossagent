import json

from consult import registry


def test_session_key_slugifies_names():
    assert registry.session_key("claude", " Payment retry / API ") == "claude:payment-retry-api"
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


def test_load_recovers_from_corrupt_registry(tmp_path, capsys):
    path = tmp_path / "sessions.json"
    path.write_text("{invalid json", encoding="utf-8")

    loaded = registry.load(path)

    assert loaded == {"sessions": {}}
    assert not path.exists()
    assert path.with_suffix(".json.corrupt").read_text(encoding="utf-8") == "{invalid json"
    assert "Registry was invalid JSON" in capsys.readouterr().err


def test_load_normalizes_non_mapping_sessions(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"sessions": []}), encoding="utf-8")

    assert registry.load(path) == {"sessions": {}}
