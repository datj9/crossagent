import json

import pytest

from crossagent import advisors


def test_builtin_claude_is_fully_featured():
    claude = advisors.resolve("claude")
    assert claude.executable == "claude"
    assert claude.prompt_delivery == "dashdash"
    assert claude.supports_sessions
    assert claude.supports_stream
    assert not claude.experimental


def test_aliases_resolve():
    assert advisors.resolve("cmd").name == "commandcode"
    assert advisors.resolve("cc").name == "claude"
    assert advisors.resolve("oc").name == "opencode"


def test_unknown_advisor_raises_with_hint():
    with pytest.raises(KeyError) as exc:
        advisors.resolve("does-not-exist")
    assert "Known advisors" in str(exc.value)


def test_experimental_advisors_are_flagged():
    for name in ("codex", "opencode", "commandcode", "gemini"):
        assert advisors.resolve(name).experimental


def test_user_config_overrides_builtin(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(json.dumps({
        "advisors": {
            "codex": {"executable": "my-codex", "base_args": ["run", "--fast"]},
            "myllm": {"executable": "myllm", "prompt_delivery": "flag:-q"},
        }
    }))
    registry = advisors.available(cfg)
    assert registry["codex"].executable == "my-codex"
    assert registry["codex"].base_args == ("run", "--fast")
    # Untouched built-in fields survive the layering.
    assert registry["codex"].model_flag == "--model"
    assert registry["myllm"].prompt_delivery == "flag:-q"


def test_malformed_user_config_is_ignored(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text("{ not json")
    registry = advisors.available(cfg)
    assert "claude" in registry  # falls back to built-ins
