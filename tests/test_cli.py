import sys

import pytest

from crossagent import __version__, advisors
from crossagent.advisors import Advisor
from crossagent.cli import _redacted_command, build_command, main, parse_args


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
