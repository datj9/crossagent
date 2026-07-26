"""Shared persisted-record shapes for the delegation gates.

These ``TypedDict``/``Literal`` definitions describe how the check (S3), scope
(S4), and verification (S5) gate outcomes are serialized into a ``Job`` record.
They live in their own dependency-free module so the gate modules (``check.py``,
``scope.py``, ``verify.py``) can name the persisted shape they produce WITHOUT
importing from ``jobs.py`` — their consumer — which would be an inverted
dependency. ``jobs.py`` imports these for its ``Job`` fields; this module imports
nothing from the package, so no import cycle is possible.
"""

from __future__ import annotations

from typing import Literal, Optional, TypedDict


class CheckResultDict(TypedDict):
    """Persisted outcome of the independent check-gate (slice S3).

    ``command`` is the caller-supplied check string; ``exit_code`` is the
    deterministic delegation verdict (D5) — ``0`` means the work verified, any
    non-zero value means the delegation *failed* regardless of whether the
    delegate process itself exited 0. ``stdout_tail``/``stderr_tail`` are
    bounded tails of the check's output.

    A ``Job.check_result`` of ``None`` means *no check ran* (unverified) — that
    stays structurally distinct from a check that ran and failed (D7): absent is
    never the same as false.
    """

    command: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str


# Outcome of the diff-scope assertion (slice S4). ``ok`` — every path the
# delegate modified was inside the declared allowlist; ``violated`` — it wrote
# outside its declared scope; ``undetermined`` — crossagent could not establish
# what changed (e.g. the cwd is not a git repo). ``undetermined`` is FAIL-CLOSED,
# never a pass: a scope check that silently passes when it cannot see the changes
# grants false assurance.
ScopeStatus = Literal["ok", "violated", "undetermined"]


class ScopeResultDict(TypedDict):
    """Persisted outcome of the diff-scope assertion (slice S4).

    ``declared`` is the caller-supplied allowlist; ``violating_paths`` lists the
    repo-relative paths the delegate modified outside it (empty unless
    ``status == "violated"``); ``detail`` is a human-readable summary.

    A ``Job.scope_result`` of ``None`` means *no scope was declared* — scope
    enforcement was off — which stays structurally distinct from a scope that
    was declared and satisfied (D7: absent is never the same as ``ok``).
    """

    declared: list[str]
    status: ScopeStatus
    violating_paths: list[str]
    detail: str


# Outcome of the independent verification pass (slice S5). A FRESH peer session
# grades the delegate's artifact supplied as user-turn input (D6), which removes
# the implicit-authorship channel that weakens self-grading — it does NOT claim
# to eliminate self-preference bias, which is a separate documented effect.
#   ``pass``       — the verifier returned a machine-checkable verdict of correct.
#   ``fail``       — the verifier returned a machine-checkable verdict of wrong.
#   ``unverified`` — the verifier ran but produced no machine-checkable verdict
#                    (free prose, or the advisor lacks a structured-output
#                    contract). Treated as inconclusive, never as a pass (D4).
#   ``error``      — the verifier could not run (no artifact, launch failure).
# ``unverified``/``error`` are inconclusive: they never green a delegation and
# never hard-fail it. Only ``fail`` blocks the green path.
VerifyVerdict = Literal["pass", "fail", "unverified", "error"]


class VerifyResultDict(TypedDict):
    """Persisted outcome of the independent verification pass (slice S5).

    ``advisor``/``model`` identify the FRESH peer session that graded the work.
    ``verdict`` is the deterministic pass/fail/inconclusive outcome; ``structured``
    records whether a machine-checkable contract (e.g. Claude ``--json-schema`` →
    ``structured_output``) produced it, versus a JSON object parsed out of a prose
    answer. ``detail`` is a human-readable summary (never the raw artifact).

    A ``Job.verify_result`` of ``None`` means *no verification was requested* —
    structurally distinct from a verification that ran and failed, or ran and
    could not decide (D7: absent is never the same as ``fail`` or ``unverified``).
    """

    advisor: str
    model: Optional[str]
    verdict: VerifyVerdict
    structured: bool
    detail: str
