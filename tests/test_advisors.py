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
    cfg.write_text(
        json.dumps(
            {
                "advisors": {
                    "codex": {"executable": "my-codex", "base_args": ["run", "--fast"]},
                    "myllm": {"executable": "myllm", "prompt_delivery": "flag:-q"},
                }
            }
        )
    )
    registry = advisors.available(cfg)
    assert registry["codex"].executable == "my-codex"
    assert registry["codex"].base_args == ("run", "--fast")
    # Untouched built-in fields survive the layering.
    assert registry["codex"].model_flag == "--model"
    assert registry["myllm"].prompt_delivery == "flag:-q"


def test_codex_defaults_to_gpt6_astra_and_others_have_no_default():
    assert advisors.resolve("codex").default_model == "gpt-6-astra"
    for name in ("claude", "opencode", "commandcode", "gemini"):
        assert advisors.resolve(name).default_model is None


@pytest.mark.parametrize("alias", ["gpt6", "GPT6", "astra", "Astra", " gpt6 "])
def test_resolve_model_expands_codex_aliases_case_insensitively(alias):
    assert advisors.resolve_model(alias, "codex") == "gpt-6-astra"


def test_codex_aliases_do_not_leak_to_other_advisors():
    # The Claude CLI resolves its own shorthands; we must not rewrite them.
    assert advisors.resolve_model("gpt6", "claude") == "gpt6"
    assert advisors.resolve_model("astra", "gemini") == "astra"
    assert advisors.resolve_model("fable", "claude") == "fable"


@pytest.mark.parametrize("value", [None, 0, 1.5, [], {}, object()])
def test_resolve_model_returns_none_for_non_strings(value):
    assert advisors.resolve_model(value, "codex") is None


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_resolve_model_returns_none_for_blank_strings(value):
    assert advisors.resolve_model(value, "codex") is None


def test_resolve_model_passes_unknown_models_through_stripped():
    assert advisors.resolve_model("  gpt-5.6-sol  ", "codex") == "gpt-5.6-sol"
    assert advisors.resolve_model("opus", "claude") == "opus"


def test_user_config_can_clear_codex_default_model(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(json.dumps({"advisors": {"codex": {"default_model": None}}}))
    registry = advisors.available(cfg)
    assert registry["codex"].default_model is None
    # Other built-in fields survive the layering.
    assert registry["codex"].executable == "codex"


def test_user_config_can_override_default_model(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(
        json.dumps({"advisors": {"codex": {"default_model": "gpt-5.6-sol"}}})
    )
    assert advisors.available(cfg)["codex"].default_model == "gpt-5.6-sol"


def test_malformed_user_config_is_ignored(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text("{ not json")
    registry = advisors.available(cfg)
    assert "claude" in registry  # falls back to built-ins
