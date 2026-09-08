"""Independent verification pass for delegated work (slice S5).

Research finding [5]: asking the producing agent to grade its own work is a
measurably weaker gate than an independent check, and the degradation is
triggered by *implicit* authorship — the artifact sitting in the model's own
prior/current turn — not by being told it authored the work. Decision D6 follows
from that mechanism: the verifier must be a **FRESH peer session** with the
artifact supplied as **user-turn input**. A resumed session, or one where the
artifact arrives as prior assistant context, reintroduces exactly the
implicit-authorship channel this pass exists to remove.

Scope honesty: supplying the artifact as user-turn input to a fresh session
*removes the implicit-authorship channel*. It does **not** eliminate
self-preference bias — residual self-recognition preference is a separate
documented effect — and the paper behind this design explicitly did not study
many-turn agentic settings, which is precisely crossagent's setting. Treat the
verdict as a stronger-but-not-infallible gate, not a proof of correctness.

How freshness is guaranteed
---------------------------
:func:`build_verifier_command` builds the advisor argv itself and never adds a
resume, fork, or session-name flag and never consults the session registry, so
there is no channel by which a prior conversation (the delegate's own, or any
other) can be attached. The artifact is appended as the advisor's ordinary
prompt argument — which every supported CLI treats as a user turn — so it can
never arrive as assistant context. Both properties are asserted by the tests.

Machine-checkable contract, with graceful degradation (D4)
----------------------------------------------------------
Where the advisor exposes a JSON-schema contract (Claude ``--json-schema`` →
``structured_output``), the verifier requests it and reads the structured
verdict. Where it does not, the verifier still asks for a JSON verdict in the
prompt and parses one out of the answer; an answer with no parseable verdict is
recorded as ``unverified`` (free prose), never as a pass. A verifier that cannot
run at all is ``error``. Neither ``unverified`` nor ``error`` hard-fails the
delegation — only a machine-readable ``fail`` blocks the green path.

Security: the verifier is a delegate too. It runs with credential-bearing env
vars scrubbed (the caller's ``--pass-env`` opt-ins excepted) and with
crossagent's own ``CROSSAGENT_*`` lineage variables stripped, so a spawned
verifier can neither inherit ambient secrets nor silently attach to a job tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

from . import credentials as credentials_mod
from . import parsers as parsers_mod
from . import runner as runner_mod
from .advisors import Advisor, default_reasoning_for_model
from .types import VerifyResultDict, VerifyVerdict

# A verification that never terminates must not hang the worker forever.
VERIFY_DEFAULT_TIMEOUT_SECONDS = 600.0
# Keep only a bounded head of the delegate's answer/diff in the artifact — an
# LLM prompt does not need (and should not pay for) an unbounded transcript.
_ARTIFACT_MAX_CHARS = 24000
_DIFF_MAX_CHARS = 16000
_GIT_TIMEOUT_SECONDS = 30.0
_LINEAGE_ENV_PREFIX = "CROSSAGENT_"

# The machine-checkable contract we ask the verifier to satisfy. Kept tiny on
# purpose: a single verdict token plus a short reason.
_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

_PROMPT_INSTRUCTIONS = (
    "You are an INDEPENDENT reviewer. You did not write the work below; judge it "
    "on its merits. Decide whether the delegated work correctly and completely "
    "satisfies the stated task. Do not modify any files. Respond with a single "
    'JSON object and nothing else: {"verdict": "pass" | "fail", "reason": '
    '"<one sentence>"}. Use "pass" only if the work is correct and complete; '
    'otherwise "fail".'
)


@dataclass(frozen=True)
class VerifyOutcome:
    """The result of one independent verification pass."""

    advisor: str
    model: Optional[str]
    verdict: VerifyVerdict
    structured: bool
    detail: str

    def to_dict(self) -> VerifyResultDict:
        """Return the persisted-record shape for ``Job.verify_result``."""
        return {
            "advisor": self.advisor,
            "model": self.model,
            "verdict": self.verdict,
            "structured": self.structured,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Fresh command construction (no session flags — this is the freshness contract)
# ---------------------------------------------------------------------------


def build_verifier_command(
    advisor: Advisor, model: Optional[str], *, schema_path: Optional[str] = None
) -> list[str]:
    """Build the verifier argv for *advisor*, WITHOUT the prompt.

    Deliberately omits every session-attachment flag (resume, fork, name) and
    never reads the session registry: a fresh session is the whole point (D6).
    The prompt is appended separately by :func:`_append_prompt` as a user turn.
    When the advisor supports a JSON-schema contract and *schema_path* is given,
    single-shot JSON output plus the schema flag are added so the structured
    verdict lands in ``structured_output``.
    """
    cmd = [advisor.executable, *advisor.base_args, *advisor.invoke_args]
    if model and advisor.model_flag:
        cmd.extend([advisor.model_flag, model])
    # A verifier session is always fresh, so the advisor's default reasoning
    # effort applies whenever *model* is its own default model (the same cost
    # rule the ask/start paths use). No user-facing --reasoning here.
    cmd.extend(advisor.reasoning_args(default_reasoning_for_model(advisor, model)))
    if advisor.supports_stream:
        # Single-shot JSON (not streaming): the terminal event is the whole
        # payload, which is the cleanest carrier for a structured verdict.
        cmd.extend(advisor.json_args)
    if advisor.json_schema_flag and schema_path is not None:
        cmd.extend([advisor.json_schema_flag, schema_path])
    return cmd


def _append_prompt(cmd: list[str], advisor: Advisor, prompt: str) -> None:
    """Append *prompt* as the advisor's user-turn argument.

    Mirrors the delivery rule the runner uses so the artifact is delivered
    exactly as a normal user prompt — never as assistant/system context.
    """
    delivery = advisor.prompt_delivery
    if delivery == "dashdash":
        cmd.extend(["--", prompt])
    elif delivery.startswith("flag:"):
        cmd.extend([delivery.split(":", 1)[1], prompt])
    else:  # "positional"
        cmd.append(prompt)


# ---------------------------------------------------------------------------
# Artifact assembly (what the verifier grades — supplied as user-turn input)
# ---------------------------------------------------------------------------


def build_artifact(task_prompt: str, result_text: Optional[str], cwd: str) -> str:
    """Assemble the artifact the verifier grades, as a user-turn string.

    Combines the original task, the delegate's answer, and — when *cwd* is a git
    repo — a best-effort working-tree diff of what the delegate changed. Bounded
    so the verification prompt stays a reasonable size.
    """
    sections = [
        _PROMPT_INSTRUCTIONS,
        "\n\n=== TASK GIVEN TO THE DELEGATE ===\n" + task_prompt.strip(),
    ]
    answer = (result_text or "").strip()
    sections.append(
        "\n\n=== DELEGATE'S ANSWER ===\n"
        + (answer or "(the delegate produced no answer)")
    )
    diff = _git_diff(cwd)
    if diff:
        sections.append("\n\n=== WORKING-TREE DIFF (git diff HEAD) ===\n" + diff)
    artifact = "".join(sections)
    if len(artifact) > _ARTIFACT_MAX_CHARS:
        artifact = artifact[:_ARTIFACT_MAX_CHARS] + "\n...[artifact truncated]"
    return artifact


def _git_diff(cwd: str) -> Optional[str]:
    """Return a bounded ``git diff HEAD`` for *cwd*, or ``None`` on any failure.

    Best-effort only (never raises): the answer is still gradable without a diff,
    and a non-repo cwd or a git error must not break verification. git is run as
    an argument list with ``shell=False`` (matching ``check.py``/``scope.py``);
    no delegate output is interpolated into the command line.
    """
    try:
        completed = subprocess.run(
            ["git", "-c", "core.pager=cat", "diff", "HEAD", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    diff = completed.stdout
    if not diff.strip():
        return None
    if len(diff) > _DIFF_MAX_CHARS:
        diff = diff[:_DIFF_MAX_CHARS] + "\n...[diff truncated]"
    return diff


# ---------------------------------------------------------------------------
# Verdict extraction
# ---------------------------------------------------------------------------


def _parse_verdict_object(text: str) -> Optional[dict[str, Any]]:
    """Extract the verdict JSON object from *text*, tolerant of surrounding prose.

    Tries the whole string first, then the widest ``{...}`` span. Returns ``None``
    when nothing parses to a mapping — i.e. the answer was free prose.
    """
    stripped = text.strip()
    for candidate in _json_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    return candidates


def _verdict_from_object(verdict_object: dict[str, Any]) -> tuple[VerifyVerdict, str]:
    """Map a parsed verdict object to a ``(verdict, detail)`` pair."""
    raw = verdict_object.get("verdict")
    reason = verdict_object.get("reason")
    detail = str(reason) if isinstance(reason, str) and reason else ""
    if isinstance(raw, bool):
        return ("pass" if raw else "fail"), detail
    token = str(raw).strip().lower() if raw is not None else ""
    if token in ("pass", "passed", "true", "ok", "correct"):
        return "pass", detail
    if token in ("fail", "failed", "false", "incorrect", "reject", "rejected"):
        return "fail", detail
    # A JSON object with an unrecognised verdict token is inconclusive, not a
    # pass — fall back to prose semantics.
    return "unverified", detail or f"unrecognised verdict token: {raw!r}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verifier_env(pass_env: Optional[list[str]] = None) -> dict[str, str]:
    """Return the environment for the verifier subprocess.

    Credential-bearing vars are scrubbed (the caller's ``--pass-env`` opt-ins
    excepted), and crossagent's own ``CROSSAGENT_*`` lineage vars are stripped so
    a nested crossagent inside the verifier cannot attach to a stale job tree.
    """
    scrubbed = credentials_mod.scrub_env(os.environ, pass_through=pass_env or [])
    return {
        key: value
        for key, value in scrubbed.items()
        if not key.startswith(_LINEAGE_ENV_PREFIX)
    }


@contextmanager
def _schema_file(structured: bool) -> Iterator[Optional[str]]:
    """Yield a path to a temp file holding the verdict schema, or ``None``.

    Exception-safe by contract (the verifier "never raises", see
    :func:`run_verification`): if the schema file cannot be created or written —
    a read-only ``/tmp``, a full disk, a restricted ``TMPDIR`` in a sandbox/CI
    container — this yields ``None`` so verification degrades to non-structured
    mode instead of raising ``OSError`` out into the worker. The file is always
    unlinked on exit. When *structured* is False no file is created.

    The mkstemp/write is in its own try/except (setup), separate from the
    try/finally around the yield (cleanup), so an exception the caller raises
    while the file is in use propagates untouched and is never mistaken for a
    setup failure.
    """
    if not structured:
        yield None
        return
    schema_path: Optional[str] = None
    try:
        schema_fd, created_path = tempfile.mkstemp(
            prefix="crossagent-verify-", suffix=".json"
        )
        with os.fdopen(schema_fd, "w", encoding="utf-8") as handle:
            json.dump(_VERDICT_SCHEMA, handle)
        schema_path = created_path
    except OSError:
        schema_path = None
    try:
        yield schema_path
    finally:
        if schema_path is not None:
            try:
                os.unlink(schema_path)
            except OSError:
                pass


def run_verification(
    advisor: Advisor,
    model: Optional[str],
    artifact: str,
    *,
    cwd: str,
    pass_env: Optional[list[str]] = None,
    timeout: float = VERIFY_DEFAULT_TIMEOUT_SECONDS,
) -> VerifyOutcome:
    """Run one fresh, independent verification pass and return its outcome.

    Never raises: any launch or parse failure degrades to an ``error``/
    ``unverified`` verdict, and an inability to create the temp schema file
    degrades to non-structured mode — a broken verifier must never crash the
    worker nor silently green a delegation.
    """
    structured = advisor.json_schema_flag is not None
    with _schema_file(structured) as schema_path:
        cmd = build_verifier_command(advisor, model, schema_path=schema_path)
        _append_prompt(cmd, advisor, artifact)
        parser = parsers_mod.get_parser(advisor.result_parser)
        try:
            outcome = runner_mod.run(
                cmd,
                cwd=cwd,
                env=verifier_env(pass_env),
                consumer=parser,
                max_runtime_seconds=timeout,
            )
        except (OSError, ValueError) as exc:
            return VerifyOutcome(
                advisor.name,
                model,
                "error",
                structured,
                f"verifier failed to launch: {exc}",
            )
    return _interpret_run_outcome(advisor.name, model, structured, outcome, timeout)


def _interpret_run_outcome(
    advisor_name: str,
    model: Optional[str],
    structured: bool,
    outcome: runner_mod.RunOutcome,
    timeout: float,
) -> VerifyOutcome:
    """Map a completed runner outcome to a ``VerifyOutcome``.

    A timeout or a launch/parse failure degrades to ``error``; free prose with no
    parseable verdict is ``unverified`` (inconclusive, never a pass); only a
    machine-checkable object yields the graded ``pass``/``fail`` verdict.
    """
    parsed = (
        outcome.result
        if isinstance(outcome.result, parsers_mod.ParsedResult)
        else parsers_mod.ParsedResult()
    )
    if outcome.timed_out:
        return VerifyOutcome(
            advisor_name,
            model,
            "error",
            structured,
            f"verifier timed out after {timeout:g}s",
        )
    if parsed.failure or parsed.result is None:
        return VerifyOutcome(
            advisor_name,
            model,
            "error",
            structured,
            parsed.error or "verifier produced no answer",
        )

    verdict_object = _parse_verdict_object(parsed.result)
    if verdict_object is None:
        # Free prose with no machine-checkable verdict: inconclusive, not a pass.
        return VerifyOutcome(
            advisor_name,
            model,
            "unverified",
            structured,
            "verifier returned no machine-checkable verdict (free prose)",
        )
    verdict, detail = _verdict_from_object(verdict_object)
    return VerifyOutcome(advisor_name, model, verdict, structured, detail)
