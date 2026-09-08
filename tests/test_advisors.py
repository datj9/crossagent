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


def test_commandcode_wires_json_telemetry():
    cc = advisors.resolve("commandcode")
    assert cc.result_parser == "commandcode-json"
    assert cc.json_args == ("--output-format", "json")
    assert cc.stream_args == ("--output-format", "json")
    # supports_stream must be true so build_command appends the JSON flag.
    assert cc.supports_stream


def test_codex_base_args_skip_git_repo_check():
    codex = advisors.resolve("codex")
    assert codex.base_args == ("exec", "--skip-git-repo-check")


def test_commandcode_json_wiring_survives_user_override(tmp_path):
    """A user override that only tweaks the model must not drop the new
    json_args/result_parser layered from the built-in (dataclasses.replace)."""
    cfg = tmp_path / "advisors.json"
    cfg.write_text(json.dumps({"advisors": {"commandcode": {"model_flag": "-m"}}}))
    cc = advisors.available(cfg)["commandcode"]
    assert cc.model_flag == "-m"
    assert cc.result_parser == "commandcode-json"
    assert cc.json_args == ("--output-format", "json")


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


def test_codex_defaults_to_low_reasoning_effort_and_others_have_none():
    codex = advisors.resolve("codex")
    assert codex.default_reasoning_effort == "low"
    assert codex.reasoning_effort_config_key == "model_reasoning_effort"
    for name in ("claude", "opencode", "commandcode", "gemini"):
        adv = advisors.resolve(name)
        assert adv.default_reasoning_effort is None
        assert adv.reasoning_effort_config_key is None


def test_reasoning_args_builds_the_codex_config_override():
    assert advisors.resolve("codex").reasoning_args("low") == (
        "-c",
        "model_reasoning_effort=low",
    )


@pytest.mark.parametrize("level", ["", None])
def test_reasoning_args_is_empty_without_a_level(level):
    assert advisors.resolve("codex").reasoning_args(level) == ()


def test_reasoning_args_is_empty_for_an_advisor_with_no_config_key():
    assert advisors.resolve("claude").reasoning_args("low") == ()


def test_valid_reasoning_efforts_covers_the_gpt6_ladder():
    assert advisors.VALID_REASONING_EFFORTS == frozenset(
        {"minimal", "low", "medium", "high", "xhigh", "max"}
    )


def test_user_config_can_override_default_reasoning_effort(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(
        json.dumps({"advisors": {"codex": {"default_reasoning_effort": "high"}}})
    )
    codex = advisors.available(cfg)["codex"]
    assert codex.default_reasoning_effort == "high"
    assert codex.reasoning_args("high") == ("-c", "model_reasoning_effort=high")


def test_user_config_can_clear_default_reasoning_effort(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(
        json.dumps({"advisors": {"codex": {"default_reasoning_effort": None}}})
    )
    codex = advisors.available(cfg)["codex"]
    assert codex.default_reasoning_effort is None
    # Other built-in fields survive the layering.
    assert codex.reasoning_effort_config_key == "model_reasoning_effort"


def test_default_reasoning_for_model_gates_on_the_default_model():
    codex = advisors.resolve("codex")
    assert advisors.default_reasoning_for_model(codex, "gpt-6-astra") == "low"
    # Case-insensitive: a caller may type the model id in any case.
    assert advisors.default_reasoning_for_model(codex, "GPT-6-Astra") == "low"
    assert advisors.default_reasoning_for_model(codex, "gpt-5.6-sol") == ""
    assert advisors.default_reasoning_for_model(codex, "") == ""
    assert advisors.default_reasoning_for_model(codex, None) == ""
    assert advisors.default_reasoning_for_model(advisors.resolve("claude"), "opus") == ""


def test_default_reasoning_for_model_resolves_an_aliased_default_model(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(json.dumps({"advisors": {"codex": {"default_model": "gpt6"}}}))
    codex = advisors.available(cfg)["codex"]
    assert advisors.default_reasoning_for_model(codex, "gpt-6-astra") == "low"


def test_malformed_user_config_is_ignored(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text("{ not json")
    registry = advisors.available(cfg)
    assert "claude" in registry  # falls back to built-ins


# --- Delegation mode support ------------------------------------------------


def test_mode_args_maps_intent_to_flags():
    cc = advisors.resolve("commandcode")
    assert cc.mode_args("write") == ("--yolo",)
    assert cc.mode_args("plan") == ("--plan",)
    assert cc.mode_args(None) == ()


def test_supports_mode_write_is_false_for_readonly_advisor():
    assert advisors.resolve("gemini").supports_mode("write") is False
    # plan and None are always supported (read-only is a safe fallback).
    assert advisors.resolve("gemini").supports_mode("plan") is True
    assert advisors.resolve("gemini").supports_mode(None) is True


def test_codex_write_mode_opts_into_workspace_write():
    codex = advisors.resolve("codex")
    assert codex.write_args == ("--sandbox", "workspace-write")
    assert codex.plan_args == ("--sandbox", "read-only")
    assert codex.supports_mode("write") is True


def test_mode_args_survive_user_override(tmp_path):
    cfg = tmp_path / "advisors.json"
    cfg.write_text(
        json.dumps(
            {"advisors": {"commandcode": {"write_args": ["--permission-mode", "yolo"]}}}
        )
    )
    cc = advisors.available(cfg)["commandcode"]
    # The list override is coerced to a tuple on the frozen dataclass.
    assert cc.write_args == ("--permission-mode", "yolo")
    assert cc.mode_args("write") == ("--permission-mode", "yolo")
