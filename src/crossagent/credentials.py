"""Credential scrubbing for the advisor child environment (slice S4).

A delegate is a partially-untrusted actor (research finding [10]: two-tier
delegation has a documented path from untrusted repo text to a committed
backdoor). crossagent must therefore not hand the delegate the caller's ambient
secrets — a compromised or prompt-injected delegate with a cloud key in its
environment is a direct exfiltration path.

By default every environment variable whose NAME matches a credential pattern is
withheld from the child. A caller that genuinely needs one passed through (for
example the advisor's own API key) opts in explicitly with ``--pass-env NAME``.

Only variable NAMES are ever recorded (in the audit log or the job record). A
name such as ``AWS_SECRET_ACCESS_KEY`` is not itself a secret, but its VALUE is
and must never appear in ``events.jsonl``, the redacted command, or any job
record field.
"""

from __future__ import annotations

from collections.abc import Mapping

# Substrings (matched case-insensitively against the variable NAME) that mark a
# variable as credential-bearing. Curated to catch the common secret shapes
# without over-matching benign names: ``ACCESS_KEY`` matches
# ``AWS_ACCESS_KEY_ID`` but a bare ``KEY`` (which would also hit
# ``KEYBOARD_LAYOUT``) is deliberately not listed.
_CREDENTIAL_SUBSTRINGS: tuple[str, ...] = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
)


def is_credential_name(name: str) -> bool:
    """Return True when *name* looks like a credential-bearing env var."""
    upper = name.upper()
    return any(marker in upper for marker in _CREDENTIAL_SUBSTRINGS)


def withheld_names(
    env: Mapping[str, str], pass_through: list[str] | None = None
) -> list[str]:
    """Return the sorted NAMES of credential vars that would be withheld.

    A name listed in *pass_through* is never withheld. Values are never
    returned — only names, which are safe to record.
    """
    allow = set(pass_through or [])
    return sorted(
        name for name in env if name not in allow and is_credential_name(name)
    )


def scrub_env(
    env: Mapping[str, str], *, pass_through: list[str] | None = None
) -> dict[str, str]:
    """Return a copy of *env* with credential-bearing variables removed.

    A variable is kept only when its name is explicitly allow-listed in
    *pass_through* or does not match any credential pattern. Uses the same
    :func:`is_credential_name` predicate as :func:`withheld_names`, so the two
    can never disagree about what was scrubbed.
    """
    allow = set(pass_through or [])
    return {
        name: value
        for name, value in env.items()
        if name in allow or not is_credential_name(name)
    }
