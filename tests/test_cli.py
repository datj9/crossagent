import json

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
