"""Tests for the independent verification pass (slice S5)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from crossagent import verify as verify_mod
from crossagent.advisors import Advisor, resolve
from crossagent.verify import (
    VerifyOutcome,
    _parse_verdict_object,
    _verdict_from_object,
    build_artifact,
    build_verifier_command,
    run_verification,
    verifier_env,
)

# ---------------------------------------------------------------------------
# Freshness contract: the verifier command must carry no session attachment.
# ---------------------------------------------------------------------------

_SESSION_FLAGS = ("--resume", "--fork-session", "--name")


def test_verifier_command_has_no_session_flags():
    """A fresh session is the whole point (D6): no resume/fork/name flag may
    appear, so no prior conversation can be attached to the verifier."""
    claude = resolve("claude")
    cmd = build_verifier_command(claude, "sonnet", schema_path="/tmp/schema.json")
    for flag in _SESSION_FLAGS:
        assert flag not in cmd, flag


def test_verifier_command_never_grants_write_mode():
    """The verifier only reads the delegate's artifact; the delegate's --write
    mode must never leak into the grading session. build_verifier_command takes
    no mode, so no write flag can appear regardless of the delegate's mode."""
    for name in ("claude", "commandcode", "opencode"):
        cmd = build_verifier_command(resolve(name), None)
        assert "bypassPermissions" not in cmd
        assert "auto-accept" not in cmd
        assert "--auto" not in cmd


def test_verifier_command_pins_low_reasoning_on_the_codex_default_model():
    """A verifier session is a fresh default-model ask, so it inherits the same
    cost-saving `low` effort the ask/start paths use."""
    cmd = build_verifier_command(resolve("codex"), "gpt-6-astra")
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=low"


def test_verifier_command_omits_reasoning_for_a_non_default_model():
    for model in ("gpt-5.6-sol", None):
        cmd = build_verifier_command(resolve("codex"), model)
        assert "-c" not in cmd


def test_verifier_command_omits_reasoning_for_advisors_without_the_knob():
    cmd = build_verifier_command(resolve("claude"), "opus")
    assert "-c" not in cmd


def test_verifier_command_requests_structured_output_when_supported():
    claude = resolve("claude")
    cmd = build_verifier_command(claude, None, schema_path="/tmp/schema.json")
    assert "--json-schema" in cmd
    assert "/tmp/schema.json" in cmd


def test_verifier_command_omits_schema_flag_for_unsupported_advisor():
    """An advisor with no json_schema_flag never gets a schema flag — it will
    degrade to prose parsing, not crash."""
    codex = resolve("codex")
    cmd = build_verifier_command(codex, None, schema_path="/tmp/schema.json")
    assert "--json-schema" not in cmd


def test_artifact_is_delivered_as_the_prompt_argument():
    """The artifact must arrive as the advisor's user-turn prompt, never as
    assistant/system context. For claude (dashdash delivery) it is the final
    argument after ``--``."""
    claude = resolve("claude")
    cmd = build_verifier_command(claude, None)
    verify_mod._append_prompt(cmd, claude, "THE-ARTIFACT")
    assert cmd[-1] == "THE-ARTIFACT"
    assert cmd[-2] == "--"


# ---------------------------------------------------------------------------
# Verdict extraction
# ---------------------------------------------------------------------------


def test_parse_verdict_object_from_raw_json():
    obj = _parse_verdict_object('{"verdict": "pass", "reason": "ok"}')
    assert obj == {"verdict": "pass", "reason": "ok"}


def test_parse_verdict_object_extracts_from_surrounding_prose():
    text = 'Here is my review.\n\n{"verdict": "fail", "reason": "bug"}\n\nThanks!'
    obj = _parse_verdict_object(text)
    assert obj["verdict"] == "fail"


def test_parse_verdict_object_returns_none_for_prose():
    assert _parse_verdict_object("The code looks fine to me.") is None


def test_verdict_from_object_maps_tokens():
    assert _verdict_from_object({"verdict": "pass"})[0] == "pass"
    assert _verdict_from_object({"verdict": "FAILED"})[0] == "fail"
    assert _verdict_from_object({"verdict": True})[0] == "pass"
    assert _verdict_from_object({"verdict": False})[0] == "fail"
    # An unrecognised token is inconclusive, never a pass.
    assert _verdict_from_object({"verdict": "maybe"})[0] == "unverified"


# ---------------------------------------------------------------------------
# run_verification end-to-end via a fake advisor script
# ---------------------------------------------------------------------------


def _fake_advisor(
    tmp_path: Path,
    body: str,
    *,
    result_parser: str = "claude-stream",
    json_schema_flag: str | None = "--json-schema",
) -> Advisor:
    script = tmp_path / "fake_verifier.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return Advisor(
        name="fakeverify",
        executable=sys.executable,
        base_args=(str(script),),
        prompt_delivery="positional",
        result_parser=result_parser,
        json_args=(),
        json_schema_flag=json_schema_flag,
    )


# A fake claude-style advisor: emits a structured_output verdict and records the
# prompt (its last argv) so a test can prove the artifact arrived as user input.
def _structured_advisor(tmp_path: Path, verdict: str) -> Advisor:
    return _fake_advisor(
        tmp_path,
        f"""
        import json, sys
        with open('prompt_seen.txt', 'w') as handle:
            handle.write(sys.argv[-1])
        print(json.dumps({{
            "type": "result", "subtype": "success",
            "structured_output": {{"verdict": {verdict!r}, "reason": "because"}},
        }}))
        """,
    )


def test_run_verification_structured_pass(tmp_path):
    advisor = _structured_advisor(tmp_path, "pass")
    outcome = run_verification(advisor, None, "ARTIFACT-TEXT", cwd=str(tmp_path))
    assert outcome.verdict == "pass"
    assert outcome.structured is True
    # The artifact reached the advisor as its user-turn prompt argument.
    assert (tmp_path / "prompt_seen.txt").read_text() == "ARTIFACT-TEXT"


def test_run_verification_structured_fail(tmp_path):
    advisor = _structured_advisor(tmp_path, "fail")
    outcome = run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))
    assert outcome.verdict == "fail"


def test_run_verification_prose_degrades_to_unverified(tmp_path):
    """An advisor with no structured-output contract that returns prose degrades
    to 'unverified' — never a pass, never a crash (D4)."""
    advisor = _fake_advisor(
        tmp_path,
        """
        print("The delegate's work looks reasonable to me.")
        """,
        result_parser="text",
        json_schema_flag=None,
    )
    outcome = run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))
    assert outcome.verdict == "unverified"
    assert outcome.structured is False


def test_run_verification_prose_advisor_can_still_parse_json_answer(tmp_path):
    """A text advisor that happens to answer in clean JSON yields a usable
    verdict (structured=False but decisive)."""
    advisor = _fake_advisor(
        tmp_path,
        """
        print('{"verdict": "pass", "reason": "all good"}')
        """,
        result_parser="text",
        json_schema_flag=None,
    )
    outcome = run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))
    assert outcome.verdict == "pass"
    assert outcome.structured is False


def test_run_verification_no_result_is_error(tmp_path):
    """A verifier that emits no result event is recorded as 'error', which is
    inconclusive — it must not green or hard-fail the delegation."""
    advisor = _fake_advisor(tmp_path, "pass\n")
    outcome = run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))
    assert outcome.verdict == "error"


def test_run_verification_survives_schema_temp_file_failure(tmp_path, monkeypatch):
    """HIGH 2: a read-only /tmp, a full disk, or a restricted TMPDIR makes
    ``tempfile.mkstemp`` raise OSError. That must degrade to non-structured mode,
    never propagate out of ``run_verification`` (which promises never to raise)
    and crash the worker after the delegate already did its work."""

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(verify_mod.tempfile, "mkstemp", _boom)
    advisor = _structured_advisor(tmp_path, "pass")

    # Must not raise despite mkstemp failing:
    outcome = run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))

    assert isinstance(outcome, VerifyOutcome)
    # Degraded gracefully: without the schema flag the fake advisor still emitted
    # a JSON verdict, which is parsed out of the answer.
    assert outcome.verdict == "pass"


def test_run_verification_never_raises_on_missing_executable(tmp_path):
    advisor = Advisor(
        name="ghost",
        executable="/nonexistent/verifier-binary",
        result_parser="text",
    )
    outcome = run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))
    assert isinstance(outcome, VerifyOutcome)
    assert outcome.verdict == "error"


# ---------------------------------------------------------------------------
# Credential + lineage scrubbing for the verifier (it is a delegate too)
# ---------------------------------------------------------------------------


def test_verifier_env_scrubs_credentials(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")
    env = verifier_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_verifier_env_honours_pass_env_opt_in(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")
    env = verifier_env(["ANTHROPIC_API_KEY"])
    assert env["ANTHROPIC_API_KEY"] == "sk-xxx"


def test_verifier_env_strips_lineage_vars(monkeypatch):
    monkeypatch.setenv("CROSSAGENT_PARENT_JOB_ID", "job_parent")
    monkeypatch.setenv("CROSSAGENT_TRACE_ID", "trace_x")
    env = verifier_env()
    assert "CROSSAGENT_PARENT_JOB_ID" not in env
    assert "CROSSAGENT_TRACE_ID" not in env


def test_run_verification_secret_does_not_reach_verifier(tmp_path, monkeypatch):
    """End-to-end: a credential env var is withheld from the verifier child."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    advisor = _fake_advisor(
        tmp_path,
        """
        import json, os
        with open('env_probe.txt', 'w') as handle:
            handle.write(os.environ.get('AWS_SECRET_ACCESS_KEY', 'ABSENT'))
        print(json.dumps({"type": "result", "subtype": "success",
                          "structured_output": {"verdict": "pass"}}))
        """,
    )
    run_verification(advisor, None, "ARTIFACT", cwd=str(tmp_path))
    assert (tmp_path / "env_probe.txt").read_text() == "ABSENT"


# ---------------------------------------------------------------------------
# Artifact assembly includes a git diff when the cwd is a repo
# ---------------------------------------------------------------------------


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def test_build_artifact_includes_git_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "app.py").write_text("original\n", encoding="utf-8")
    _git(["add", "app.py"], repo)
    _git(["commit", "-m", "init"], repo)
    (repo / "app.py").write_text("delegate changed this\n", encoding="utf-8")

    artifact = build_artifact("do the task", "I edited app.py", str(repo))
    assert "TASK GIVEN TO THE DELEGATE" in artifact
    assert "I edited app.py" in artifact
    assert "delegate changed this" in artifact  # the diff is present


def test_build_artifact_tolerates_non_git_cwd(tmp_path):
    artifact = build_artifact("do the task", "the answer", str(tmp_path))
    assert "the answer" in artifact
    assert "WORKING-TREE DIFF" not in artifact
