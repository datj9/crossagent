from consult import advisors
from consult.advisors import Advisor
from consult.cli import _redacted_command, build_command, main, parse_args


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
    cmd, key = build_command(advisors.resolve("claude"), _args(name="topic-a"), {"sessions": {}})
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
    cmd, _ = build_command(advisors.resolve("claude"), _args(name="topic-a", new_session=True), registry)
    assert "--resume" not in cmd
    assert cmd[cmd.index("--name") + 1] == "topic-a"


def test_codex_uses_positional_prompt():
    cmd, key = build_command(advisors.resolve("codex"), _args(agent="codex"), {"sessions": {}})
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-1] == "hello?"
    assert "--" not in cmd
    assert key == ""  # no --name given -> no session key


def test_gemini_uses_flag_delivery():
    cmd, _ = build_command(advisors.resolve("gemini"), _args(agent="gemini"), {"sessions": {}})
    assert cmd[-2:] == ["-p", "hello?"]


def test_model_flag_only_added_when_supported_and_requested():
    cmd, _ = build_command(advisors.resolve("claude"), _args(model="opus"), {"sessions": {}})
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
    missing = Advisor(name="missing", executable="consult-definitely-missing-cli")
    monkeypatch.setattr(advisors, "resolve", lambda _name: missing)

    code = main(["--agent", "missing", "--prompt", "sensitive prompt"])

    captured = capsys.readouterr()
    assert code == 127
    assert "advisor CLI not found on PATH" in captured.err
    assert "sensitive prompt" not in captured.err
    assert captured.out == ""
