"""Independent check-gate for delegated work (slice S3).

D5: a delegate's verdict is a deterministic exit code, never the agent's
self-report. After a delegate finishes, crossagent itself runs the caller's
``--check`` command and records the real outcome, so a delegate that exits 0
while the project's tests fail is recorded as a *failed delegation*.

Security: the check command is supplied by the (trusted) caller that invoked
``crossagent start`` — the same trust level as the invocation itself — not by
the delegate or any untrusted repository content. Even so it is never run
through a shell: it is split with :func:`shlex.split` and executed as an
argument list with ``shell=False``, so no shell metacharacters are honoured and
no delegate output is ever interpolated into a command line. The check inherits
the ambient environment minus crossagent's own ``CROSSAGENT_*`` lineage
variables; broader credential scrubbing is slice S4's remit.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional

from .jobs import CheckResultDict

# Keep only a bounded tail of the check's output — a full test run can emit
# megabytes, and the whole Job record is loaded into memory (and the dashboard).
CHECK_OUTPUT_TAIL_CHARS = 4000
# A check that never terminates must not hang the worker forever.
CHECK_DEFAULT_TIMEOUT_SECONDS = 600.0
# crossagent's own lineage variables are internals, not project config — never
# hand them to the check subprocess.
_LINEAGE_ENV_PREFIX = "CROSSAGENT_"
_TRUNCATION_MARKER = "...[truncated]\n"

# Exit codes for cases where the check could not actually run. All are non-zero
# so an un-runnable check is treated as *not verified*, never as a pass.
_EXIT_TIMEOUT = 124
_EXIT_NOT_EXECUTABLE = 126
_EXIT_NOT_FOUND = 127


@dataclass(frozen=True)
class CheckOutcome:
    """The real outcome of running one check command."""

    command: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> CheckResultDict:
        """Return the persisted-record shape for ``Job.check_result``."""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def sanitized_check_env() -> dict[str, str]:
    """Return the process environment with crossagent's lineage vars removed.

    The check inherits the ambient environment so project tooling (PATH, the
    active virtualenv, language runtimes) keeps working, but crossagent's own
    ``CROSSAGENT_*`` lineage variables are stripped so delegation internals do
    not leak into the check subprocess or anything it spawns.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_LINEAGE_ENV_PREFIX)
    }


def run_check(
    command: str,
    *,
    cwd: str,
    timeout: float = CHECK_DEFAULT_TIMEOUT_SECONDS,
    env: Optional[dict[str, str]] = None,
) -> CheckOutcome:
    """Run the caller's *command* in *cwd* and return its outcome, never raising.

    Any failure to launch (bad quoting, missing executable, non-executable,
    timeout) is captured as a non-zero ``exit_code`` — an un-runnable check is
    *not verified*, never silently a pass — and never propagates as an exception
    that could crash the worker.
    """
    check_env = env if env is not None else sanitized_check_env()

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return CheckOutcome(
            command,
            _EXIT_NOT_FOUND,
            "",
            f"check command could not be parsed: {exc}\n",
        )
    if not argv:
        return CheckOutcome(command, _EXIT_NOT_FOUND, "", "check command is empty\n")

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=check_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = _as_text(exc.stderr) + f"check command timed out after {timeout:g}s\n"
        return CheckOutcome(
            command, _EXIT_TIMEOUT, _tail(_as_text(exc.stdout)), _tail(stderr)
        )
    except FileNotFoundError:
        return CheckOutcome(
            command, _EXIT_NOT_FOUND, "", f"check command not found: {argv[0]}\n"
        )
    except PermissionError:
        return CheckOutcome(
            command,
            _EXIT_NOT_EXECUTABLE,
            "",
            f"check command not executable: {argv[0]}\n",
        )
    except OSError as exc:
        return CheckOutcome(
            command, _EXIT_NOT_FOUND, "", f"check command failed to launch: {exc}\n"
        )

    return CheckOutcome(
        command,
        completed.returncode,
        _tail(completed.stdout),
        _tail(completed.stderr),
    )


def _tail(text: Optional[str]) -> str:
    """Return the last :data:`CHECK_OUTPUT_TAIL_CHARS` characters of *text*.

    Failures surface at the end of a test run, so the tail is the useful part.
    A truncation marker is prepended so a reader can tell output was dropped.
    """
    if not text:
        return ""
    if len(text) <= CHECK_OUTPUT_TAIL_CHARS:
        return text
    return _TRUNCATION_MARKER + text[-CHECK_OUTPUT_TAIL_CHARS:]


def _as_text(value: object) -> str:
    """Coerce captured subprocess output to text, tolerating bytes/None."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""
