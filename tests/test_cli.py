import json
import subprocess
import sys
from dataclasses import replace

import pytest

from crossagent import __version__, advisors
from crossagent import parsers as parsers_mod
from crossagent import registry as reg
from crossagent.advisors import Advisor
from crossagent import cli as cli_mod
from crossagent.cli import (
    _dispatch,
    _parse_job_args,
    _redacted_command,
    _write_command_info,
    build_command,
    main,
    parse_args,
)


def _args(**overrides):
    argv = []
    for key, value in overrides.items():
        if key == "_prompt":
            continue
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value not in (False, None):
            argv.extend([flag, str(value)])
    ns = parse_args(argv)
    ns._prompt = overrides.get("_prompt", "hello?")
    return ns


def test_claude_command_defaults_to_streaming_and_dashdash():
    cmd, key = build_command(
        advisors.resolve("claude"), _args(name="topic-a"), {"sessions": {}}
    )
    assert cmd[:2] == ["claude", "-p"]
    assert "--output-format" in cmd and "stream-json" in cmd
    assert cmd[-2:] == ["--", "hello?"]
    assert cmd[cmd.index("--name") + 1] == "topic-a"
    assert key == "claude:topic-a"


def test_claude_resumes_stored_session():
    registry = {"sessions": {"claude:topic-a": {"session_id": "sess-123"}}}
    cmd, _ = build_command(advisors.resolve("claude"), _args(name="topic-a"), registry)
    assert cmd[cmd.index("--resume") + 1] == "sess-123"
    assert "--name" not in cmd


def test_new_session_ignores_stored_id():
    registry = {"sessions": {"claude:topic-a": {"session_id": "sess-123"}}}
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(name="topic-a", new_session=True), registry
    )
    assert "--resume" not in cmd
    assert cmd[cmd.index("--name") + 1] == "topic-a"


def test_codex_uses_positional_prompt():
    cmd, key = build_command(
        advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}}
    )
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-1] == "hello?"
    assert "--" not in cmd
    assert key == ""  # no --name given -> no session key


def test_codex_command_includes_json_flag():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}}
    )
    assert "--json" in cmd


def test_codex_no_stream_still_includes_json_flag():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex", stream=False), {"sessions": {}}
    )
    assert "--json" in cmd


def test_codex_resumes_stored_thread():
    registry = {"sessions": {"codex:topic-a": {"session_id": "thread-123"}}}
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex", name="topic-a"), registry
    )
    assert cmd[:2] == ["codex", "exec"]
    assert "resume" in cmd
    assert "thread-123" in cmd
    assert cmd[-1] == "hello?"
    assert "--json" in cmd


def test_codex_command_includes_skip_git_repo_check():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}}
    )
    # Flag rides in base_args, right after the exec subcommand.
    assert cmd[:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert cmd[-1] == "hello?"


def test_commandcode_command_includes_json_output_format():
    cmd, _ = build_command(
        advisors.resolve("commandcode"), _args(agent="commandcode"), {"sessions": {}}
    )
    assert cmd[:2] == ["commandcode", "-p"]
    assert "--output-format" in cmd and "json" in cmd
    assert cmd[-1] == "hello?"


def test_gemini_uses_flag_delivery():
    cmd, _ = build_command(
        advisors.resolve("gemini"), _args(agent="gemini"), {"sessions": {}}
    )
    assert cmd[-2:] == ["-p", "hello?"]


def test_model_flag_only_added_when_supported_and_requested():
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(model="opus"), {"sessions": {}}
    )
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_codex_fresh_invocation_defaults_to_gpt6_astra():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}}
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"


def test_codex_model_alias_is_expanded():
    for alias in ("gpt6", "ASTRA"):
        cmd, _ = build_command(
            advisors.resolve("codex"),
            _args(agent="codex", model=alias),
            {"sessions": {}},
        )
        assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"


@pytest.mark.parametrize("sentinel", ["default", "DEFAULT", " Default "])
def test_model_default_sentinel_suppresses_the_flag(sentinel):
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", model=sentinel),
        {"sessions": {}},
    )
    assert "--model" not in cmd


def test_claude_without_model_still_emits_no_flag():
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(name="topic-a"), {"sessions": {}}
    )
    assert "--model" not in cmd


def test_claude_passes_codex_alias_through_verbatim():
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(model="gpt6"), {"sessions": {}}
    )
    assert cmd[cmd.index("--model") + 1] == "gpt6"


def test_codex_resume_does_not_force_the_default_model():
    registry = {"sessions": {"codex:topic-a": {"session_id": "thread-123"}}}
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex", name="topic-a"), registry
    )
    assert "--model" not in cmd
    assert cmd[cmd.index("resume") + 1] == "thread-123"


_RESUMABLE = Advisor(
    name="codex",
    executable="codex",
    model_flag="--model",
    default_model="gpt-6-astra",
    resume_flag="--resume",
)


def test_explicit_resume_flag_suppresses_the_default_model():
    cmd, _ = build_command(_RESUMABLE, _args(resume="sess-9"), {"sessions": {}})
    assert "--model" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-9"


def test_explicit_model_survives_an_explicit_resume_flag():
    cmd, _ = build_command(
        _RESUMABLE, _args(resume="sess-9", model="gpt6"), {"sessions": {}}
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"
    assert cmd.index("--model") < cmd.index("--resume")


def test_explicit_model_survives_resume_in_its_argv_position():
    registry = {"sessions": {"codex:topic-a": {"session_id": "thread-123"}}}
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", name="topic-a", model="gpt-5.6-sol"),
        registry,
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
    assert cmd.index("--model") < cmd.index("resume")


def test_new_session_reinstates_the_codex_default_model():
    registry = {"sessions": {"codex:topic-a": {"session_id": "thread-123"}}}
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", name="topic-a", new_session=True),
        registry,
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"
    assert "resume" not in cmd


def test_job_start_argv_path_also_gets_the_codex_default_model():
    args = _parse_job_args("start", ["--agent", "codex", "--prompt", "hello?"])
    args._prompt = "hello?"
    cmd, _ = build_command(
        advisors.resolve("codex"), args, {"sessions": {}}, include_prompt=False
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"


# ---------------------------------------------------------------------------
# Reasoning effort (codex/GPT-6 defaults to low for cost)
# ---------------------------------------------------------------------------

_LOW = ["-c", "model_reasoning_effort=low"]


def _reasoning_value(cmd):
    return cmd[cmd.index("-c") + 1] if "-c" in cmd else None


def test_codex_fresh_invocation_defaults_to_low_reasoning_effort():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}}
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"
    assert _reasoning_value(cmd) == "model_reasoning_effort=low"


def test_codex_default_ask_builds_the_expected_argv():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}}
    )
    assert cmd == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--model",
        "gpt-6-astra",
        *_LOW,
        "--json",
        "hello?",
    ]


def test_explicit_reasoning_level_overrides_the_default():
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", reasoning="high"),
        {"sessions": {}},
    )
    assert _reasoning_value(cmd) == "model_reasoning_effort=high"


def test_explicit_reasoning_level_is_lowercased():
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", reasoning="HIGH"),
        {"sessions": {}},
    )
    assert _reasoning_value(cmd) == "model_reasoning_effort=high"


def test_invalid_reasoning_level_exits_two(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_command(
            advisors.resolve("codex"),
            _args(agent="codex", reasoning="turbo"),
            {"sessions": {}},
        )
    assert excinfo.value.code == 2
    assert "--reasoning" in capsys.readouterr().err


@pytest.mark.parametrize("sentinel", ["default", "DEFAULT", " Default "])
def test_reasoning_default_sentinel_suppresses_the_override(sentinel):
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", reasoning=sentinel),
        {"sessions": {}},
    )
    assert "-c" not in cmd


def test_reasoning_default_sentinel_leaves_the_model_flag_alone():
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", reasoning="default", model="gpt6"),
        {"sessions": {}},
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"
    assert "-c" not in cmd


def test_a_non_default_model_gets_no_forced_low_reasoning():
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex", model="o3"), {"sessions": {}}
    )
    assert cmd[cmd.index("--model") + 1] == "o3"
    assert "-c" not in cmd


def test_the_default_model_typed_verbatim_still_gets_low_reasoning():
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", model="GPT-6-Astra"),
        {"sessions": {}},
    )
    assert _reasoning_value(cmd) == "model_reasoning_effort=low"


def test_low_reasoning_applies_to_an_aliased_configured_default_model():
    aliased = replace(advisors.resolve("codex"), default_model="gpt6")
    cmd, _ = build_command(aliased, _args(agent="codex"), {"sessions": {}})
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"
    assert _reasoning_value(cmd) == "model_reasoning_effort=low"


def test_codex_resume_does_not_force_low_reasoning():
    registry = {"sessions": {"codex:topic-a": {"session_id": "thread-123"}}}
    cmd, _ = build_command(
        advisors.resolve("codex"), _args(agent="codex", name="topic-a"), registry
    )
    assert "-c" not in cmd


def test_explicit_reasoning_survives_resume_and_precedes_the_resume_token():
    registry = {"sessions": {"codex:topic-a": {"session_id": "thread-123"}}}
    cmd, _ = build_command(
        advisors.resolve("codex"),
        _args(agent="codex", name="topic-a", reasoning="high"),
        registry,
    )
    assert _reasoning_value(cmd) == "model_reasoning_effort=high"
    assert cmd.index("-c") < cmd.index("resume")


def test_claude_fresh_invocation_gets_no_reasoning_override():
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(name="topic-a"), {"sessions": {}}
    )
    assert "-c" not in cmd


def test_reasoning_on_an_advisor_without_a_config_key_warns_and_emits_nothing(capsys):
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(reasoning="low"), {"sessions": {}}
    )
    assert "-c" not in cmd
    assert "no reasoning-effort setting" in capsys.readouterr().err


def test_job_start_argv_path_also_gets_low_reasoning():
    args = _parse_job_args("start", ["--agent", "codex", "--prompt", "hello?"])
    args._prompt = "hello?"
    cmd, _ = build_command(
        advisors.resolve("codex"), args, {"sessions": {}}, include_prompt=False
    )
    assert _reasoning_value(cmd) == "model_reasoning_effort=low"


def test_job_start_accepts_an_explicit_reasoning_flag():
    args = _parse_job_args(
        "start", ["--agent", "codex", "--prompt", "hello?", "--reasoning", "xhigh"]
    )
    args._prompt = "hello?"
    cmd, _ = build_command(
        advisors.resolve("codex"), args, {"sessions": {}}, include_prompt=False
    )
    assert _reasoning_value(cmd) == "model_reasoning_effort=xhigh"


def _fake_run_advisor(*_args, **_kwargs):
    return 0, parsers_mod.ParsedResult(result="ok", session_id="thread-7")


def test_registry_record_persists_the_resolved_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_run_advisor", _fake_run_advisor)
    registry_path = tmp_path / "sessions.json"
    args = _args(agent="codex", name="topic-a", model="gpt6")

    code = _dispatch(
        advisors.resolve("codex"),
        args,
        ["codex"],
        "codex:topic-a",
        {"sessions": {}},
        registry_path,
    )
    capsys.readouterr()

    assert code == 0
    saved = reg.load(registry_path)
    assert saved["sessions"]["codex:topic-a"]["model"] == "gpt-6-astra"


def test_registry_record_persists_the_default_model_on_a_fresh_session(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli_mod, "_run_advisor", _fake_run_advisor)
    registry_path = tmp_path / "sessions.json"
    args = _args(agent="codex", name="topic-a")

    _dispatch(
        advisors.resolve("codex"),
        args,
        ["codex"],
        "codex:topic-a",
        {"sessions": {}},
        registry_path,
    )
    capsys.readouterr()

    saved = reg.load(registry_path)
    assert saved["sessions"]["codex:topic-a"]["model"] == "gpt-6-astra"


def test_command_info_persists_the_resolved_model(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _write_command_info(
        job_dir,
        advisors.resolve("codex"),
        _args(agent="codex", model="astra"),
        ["codex", "exec"],
        "",
        tmp_path / "sessions.json",
        is_resume=False,
    )
    info = json.loads((job_dir / "command.json").read_text(encoding="utf-8"))
    assert info["model"] == "gpt-6-astra"


def test_command_info_persists_the_default_model_and_skips_it_on_resume(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    codex = advisors.resolve("codex")
    args = _args(agent="codex", name="topic-a")

    _write_command_info(
        job_dir, codex, args, ["codex"], "", tmp_path / "s.json", is_resume=False
    )
    fresh = json.loads((job_dir / "command.json").read_text(encoding="utf-8"))
    assert fresh["model"] == "gpt-6-astra"

    _write_command_info(
        job_dir, codex, args, ["codex"], "", tmp_path / "s.json", is_resume=True
    )
    resumed = json.loads((job_dir / "command.json").read_text(encoding="utf-8"))
    assert resumed["model"] == ""


def test_registry_record_persists_the_reasoning_effort(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_run_advisor", _fake_run_advisor)
    registry_path = tmp_path / "sessions.json"

    _dispatch(
        advisors.resolve("codex"),
        _args(agent="codex", name="topic-a"),
        ["codex"],
        "codex:topic-a",
        {"sessions": {}},
        registry_path,
    )
    capsys.readouterr()
    assert reg.load(registry_path)["sessions"]["codex:topic-a"]["reasoning"] == "low"

    _dispatch(
        advisors.resolve("codex"),
        _args(agent="codex", name="topic-b", reasoning="xhigh"),
        ["codex"],
        "codex:topic-b",
        {"sessions": {}},
        registry_path,
    )
    capsys.readouterr()
    assert reg.load(registry_path)["sessions"]["codex:topic-b"]["reasoning"] == "xhigh"


def test_registry_record_persists_no_reasoning_for_claude(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli_mod, "_run_advisor", _fake_run_advisor)
    registry_path = tmp_path / "sessions.json"

    _dispatch(
        advisors.resolve("claude"),
        _args(name="topic-a"),
        ["claude"],
        "claude:topic-a",
        {"sessions": {}},
        registry_path,
    )
    capsys.readouterr()
    assert reg.load(registry_path)["sessions"]["claude:topic-a"]["reasoning"] == ""


def test_command_info_persists_the_reasoning_effort_and_skips_it_on_resume(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    codex = advisors.resolve("codex")
    args = _args(agent="codex", name="topic-a")

    _write_command_info(
        job_dir, codex, args, ["codex"], "", tmp_path / "s.json", is_resume=False
    )
    fresh = json.loads((job_dir / "command.json").read_text(encoding="utf-8"))
    assert fresh["reasoning"] == "low"

    _write_command_info(
        job_dir, codex, args, ["codex"], "", tmp_path / "s.json", is_resume=True
    )
    resumed = json.loads((job_dir / "command.json").read_text(encoding="utf-8"))
    assert resumed["reasoning"] == ""


def test_list_advisors_shows_the_default_reasoning_effort(monkeypatch, capsys):
    builtin_codex = advisors._BUILTINS["codex"]
    monkeypatch.setattr(advisors, "available", lambda *a, **k: {"codex": builtin_codex})
    assert main(["--list-advisors"]) == 0
    out = capsys.readouterr().out
    assert "default model: gpt-6-astra (reasoning effort: low)" in out


def test_list_advisors_shows_the_default_model(monkeypatch, capsys):
    listing = {"codex": _RESUMABLE, "claude": advisors.resolve("claude")}
    monkeypatch.setattr(advisors, "available", lambda *a, **k: listing)
    assert main(["--list-advisors"]) == 0
    out = capsys.readouterr().out
    assert "default model: gpt-6-astra" in out
    assert "default model" not in out.split("codex")[0]  # claude has none


def test_command_preview_redacts_every_prompt_delivery():
    secret = "do-not-log-this-prompt"
    for name in ("claude", "codex", "opencode", "commandcode", "gemini"):
        args = _args(agent=name, _prompt=secret)
        cmd, _ = build_command(advisors.resolve(name), args, {"sessions": {}})
        preview = _redacted_command(cmd)
        assert secret not in preview
        assert "<prompt>" in preview


def test_missing_advisor_cli_exits_cleanly_without_logging_prompt(monkeypatch, capsys):
    missing = Advisor(name="missing", executable="crossagent-definitely-missing-cli")
    monkeypatch.setattr(advisors, "resolve", lambda _name: missing)

    code = main(["--agent", "missing", "--prompt", "sensitive prompt"])

    captured = capsys.readouterr()
    assert code == 127
    assert "advisor CLI not found on PATH" in captured.err
    assert "sensitive prompt" not in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# HIGH 3: the foreground (default) dispatch path scrubs credentials from the
# advisor env too — not only the durable-job path — with the same --pass-env
# escape hatch, so both dispatch modes share one policy.
# ---------------------------------------------------------------------------

_FG_SECRET_NAME = "AWS_SECRET_ACCESS_KEY"
_FG_SECRET_VALUE = "fg-super-secret-value"


def _env_probe_advisor(tmp_path, probe_name):
    """A fake claude-stream advisor that records one env var it was given."""
    script = tmp_path / "fake_fg_advisor.py"
    script.write_text(
        "import json, os\n"
        f"open('fg_env_probe.txt', 'w').write("
        f"os.environ.get({probe_name!r}, 'ABSENT'))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', "
        "'result': 'ok'}))\n",
        encoding="utf-8",
    )
    return Advisor(
        name="fakefg",
        executable=sys.executable,
        base_args=(str(script),),
        prompt_delivery="positional",
        result_parser="claude-stream",
    )


def test_foreground_advisor_env_is_scrubbed(tmp_path, monkeypatch):
    """The default `crossagent --agent ... --prompt ...` invocation must withhold
    the caller's ambient credentials from the advisor, matching the durable-job
    path (HIGH 3)."""
    monkeypatch.setenv(_FG_SECRET_NAME, _FG_SECRET_VALUE)
    advisor = _env_probe_advisor(tmp_path, _FG_SECRET_NAME)
    monkeypatch.setattr(advisors, "resolve", lambda _name: advisor)

    code = main(["--agent", "fakefg", "--prompt", "hi", "--cwd", str(tmp_path)])

    assert code == 0
    assert (tmp_path / "fg_env_probe.txt").read_text() == "ABSENT"


def test_foreground_pass_env_opts_a_named_var_back_in(tmp_path, monkeypatch):
    """--pass-env NAME is the foreground escape hatch: the named credential var
    reaches the advisor despite matching a credential pattern (HIGH 3)."""
    monkeypatch.setenv(_FG_SECRET_NAME, _FG_SECRET_VALUE)
    advisor = _env_probe_advisor(tmp_path, _FG_SECRET_NAME)
    monkeypatch.setattr(advisors, "resolve", lambda _name: advisor)

    code = main(
        [
            "--agent",
            "fakefg",
            "--prompt",
            "hi",
            "--cwd",
            str(tmp_path),
            "--pass-env",
            _FG_SECRET_NAME,
        ]
    )

    assert code == 0
    assert (tmp_path / "fg_env_probe.txt").read_text() == _FG_SECRET_VALUE


def test_foreground_secret_value_absent_from_output(tmp_path, monkeypatch, capsys):
    """No credential VALUE appears in the foreground path's stdout/stderr — its
    only output surface (the session registry stores no env) (HIGH 3)."""
    monkeypatch.setenv(_FG_SECRET_NAME, _FG_SECRET_VALUE)
    advisor = _env_probe_advisor(tmp_path, _FG_SECRET_NAME)
    monkeypatch.setattr(advisors, "resolve", lambda _name: advisor)

    main(["--agent", "fakefg", "--prompt", "hi", "--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert _FG_SECRET_VALUE not in captured.out
    assert _FG_SECRET_VALUE not in captured.err


def test_version_flag_prints_version_and_exits(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"crossagent {__version__}"


def test_short_version_flag_prints_version_and_exits(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["-v"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"crossagent {__version__}"


# ---------------------------------------------------------------------------
# Delegation permission mode (--write / --plan) expansion
# ---------------------------------------------------------------------------

from crossagent.cli import ModeError  # noqa: E402


def _reg():
    return {"sessions": {}}


def test_no_mode_adds_no_permission_flags():
    cmd, _ = build_command(advisors.resolve("commandcode"), _args(), _reg())
    assert "--permission-mode" not in cmd
    assert "--plan" not in cmd


def test_write_mode_expands_commandcode_to_bypass():
    # auto-accept is NOT enough in commandcode -p mode (write tools stay blocked);
    # the write contract is the full permission bypass.
    cmd, _ = build_command(advisors.resolve("commandcode"), _args(write=True), _reg())
    assert "--yolo" in cmd


def test_plan_mode_expands_commandcode():
    cmd, _ = build_command(advisors.resolve("commandcode"), _args(plan=True), _reg())
    assert "--plan" in cmd


def test_write_mode_expands_opencode_to_auto():
    cmd, _ = build_command(advisors.resolve("opencode"), _args(write=True), _reg())
    assert "--auto" in cmd


def test_plan_mode_expands_opencode_to_agent_plan():
    cmd, _ = build_command(advisors.resolve("opencode"), _args(plan=True), _reg())
    assert cmd[cmd.index("--agent") + 1] == "plan"


def test_write_mode_expands_claude_to_bypass():
    cmd, _ = build_command(advisors.resolve("claude"), _args(write=True), _reg())
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"


def test_write_mode_on_codex_opts_into_workspace_write():
    # Stock codex exec is read-only; --write must opt into workspace-write.
    cmd, _ = build_command(advisors.resolve("codex"), _args(write=True), _reg())
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


def test_plan_mode_on_codex_pins_read_only():
    cmd, _ = build_command(advisors.resolve("codex"), _args(plan=True), _reg())
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_write_mode_on_readonly_advisor_raises():
    with pytest.raises(ModeError):
        build_command(advisors.resolve("gemini"), _args(write=True), _reg())


def test_plan_mode_on_advisor_without_plan_flags_warns_not_raises(capsys):
    cmd, _ = build_command(advisors.resolve("gemini"), _args(plan=True), _reg())
    # No plan flags to add, but no error: read-only is the safe default.
    assert "--plan" not in cmd
    assert "no distinct plan mode" in capsys.readouterr().err


def test_write_and_permission_mode_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse_args(["--write", "--permission-mode", "plan"])


def test_write_and_plan_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse_args(["--write", "--plan"])


def test_explicit_permission_mode_still_works_for_claude():
    cmd, _ = build_command(
        advisors.resolve("claude"), _args(permission_mode="acceptEdits"), _reg()
    )
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_foreground_write_without_allow_path_warns(capsys):
    build_command(advisors.resolve("commandcode"), _args(write=True), _reg())
    assert "unbounded filesystem access" in capsys.readouterr().err


def _git_init(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_foreground_write_outside_git_repo_exits_2_before_dispatch(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    code = main(["--agent", "commandcode", "--write", "--prompt", "hi", "--cwd", str(plain)])
    captured = capsys.readouterr()
    assert code == 2
    assert "--write requires a git repository" in captured.err
    assert str(plain) in captured.err
    assert "running:" not in captured.err
    assert captured.out == ""


def test_foreground_write_inside_git_repo_reaches_dispatch(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _git_init(repo)
    missing = Advisor(
        name="missing", executable="crossagent-definitely-missing-cli", write_args=["--yolo"]
    )
    monkeypatch.setattr(advisors, "resolve", lambda _name: missing)
    code = main(["--agent", "missing", "--write", "--prompt", "hi", "--cwd", str(repo)])
    captured = capsys.readouterr()
    assert code == 127
    assert "--write requires a git repository" not in captured.err
    assert "unbounded filesystem access" in captured.err


def test_foreground_plan_outside_git_repo_is_not_gated(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    missing = Advisor(name="missing", executable="crossagent-definitely-missing-cli")
    monkeypatch.setattr(advisors, "resolve", lambda _name: missing)
    code = main(["--agent", "missing", "--plan", "--prompt", "hi", "--cwd", str(plain)])
    assert code == 127
    assert "--write requires a git repository" not in capsys.readouterr().err
