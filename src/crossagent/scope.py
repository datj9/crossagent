"""Diff-scope assertion for delegated work (slice S4).

A delegate has write authority over the user's repo that a second-opinion run
never had (research finding [10]: the two-tier delegation pattern crossagent
generalizes has a documented path from untrusted repo text to a committed
backdoor). The caller declares an allowlist of paths the delegate may modify via
``--allow-path``; after the delegate finishes, crossagent asserts that every
file it changed lies inside that allowlist and records a *failed delegation*
otherwise.

How "modified paths" is determined
-----------------------------------
git, run as an argument list with ``shell=False`` — never through a shell, and
no delegate output is ever interpolated into a command line (matching the
standard set by ``check.py``). A baseline of the working tree's dirty set is
captured *before* the delegate runs; after it finishes the dirty set is
recomputed and diffed against the baseline, so a tree that was *already* dirty is
not misattributed to the delegate. For a path that was dirty both before and
after, the file's content hash is compared so a further modification (or a
revert to HEAD) is still attributed. Paths are compared by their RESOLVED real
location — symlinks and ``..`` eliminated — against the resolved declared roots,
so neither traversal nor a symlink can smuggle a write outside the allowlist.

Blind spots (documented, never hidden):
- ``.gitignore``-d paths are invisible to ``git status`` and therefore to this
  check. A delegate writing to an ignored path (build output, a ``.env`` file)
  is not detected. Catching these would require hashing every ignored file
  (e.g. all of ``node_modules``) and is left to a follow-up.
- A file the delegate creates and then deletes leaves no trace in the dirty set
  and is not detected.
- Content changed inside an already-ignored directory is not detected.

Fail closed
-----------
If crossagent cannot establish what changed — the cwd is not a git repository,
git is unavailable, or the repo identity shifts mid-run — the outcome is
``undetermined``, NOT a pass. A scope check that silently passes when it cannot
see the changes is worse than no check: it grants false assurance.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .jobs import ScopeResultDict, ScopeStatus

_GIT_TIMEOUT_SECONDS = 30.0
# porcelain -z entries are "XY <path>": two status chars, a space, then the path.
_STATUS_ENTRY_PREFIX = 3
_HASH_CHUNK_BYTES = 65536


@dataclass(frozen=True)
class ScopeBaseline:
    """The working-tree state captured *before* the delegate runs.

    ``repo_root`` is ``None`` when no baseline could be established (not a git
    repo, git unavailable), in which case *error* explains why and the scope
    outcome is ``undetermined`` (fail closed). ``dirty_hashes`` maps each
    already-dirty repo-relative path to its content hash (or ``None`` when the
    file could not be read), so pre-existing dirt is not later misattributed.
    """

    repo_root: Optional[str]
    dirty_hashes: dict[str, Optional[str]] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True)
class ScopeOutcome:
    """The result of asserting the delegate's writes against the allowlist."""

    declared: tuple[str, ...]
    status: ScopeStatus
    violating_paths: tuple[str, ...]
    detail: str

    def to_dict(self) -> ScopeResultDict:
        """Return the persisted-record shape for ``Job.scope_result``."""
        return {
            "declared": list(self.declared),
            "status": self.status,
            "violating_paths": list(self.violating_paths),
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# git helpers (argument lists, shell=False — matches check.py)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: str) -> Optional[subprocess.CompletedProcess]:
    """Run ``git <args>`` in *cwd* as an argument list, never through a shell.

    Returns the completed process, or ``None`` when git could not be executed at
    all (not installed, timed out, OS error). An un-runnable git means scope
    cannot be determined — callers surface that as ``undetermined``, never a
    pass.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_root(cwd: str) -> Optional[str]:
    """Return the git worktree root for *cwd*, or ``None`` when it is not one."""
    completed = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if completed is None or completed.returncode != 0:
        return None
    root = completed.stdout.strip()
    return root or None


def _dirty_paths(cwd: str) -> Optional[list[str]]:
    """Return repo-relative paths of every dirty file, or ``None`` on git error.

    Covers tracked-modified, staged, and untracked files. ``--no-renames`` keeps
    the output a flat path list (a rename surfaces as delete + add, both
    "touched") so there is no rename-arrow parsing to get wrong. Ignored files
    are intentionally excluded — see the module docstring's blind spots.
    """
    completed = _run_git(
        ["status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"],
        cwd,
    )
    if completed is None or completed.returncode != 0:
        return None
    paths: list[str] = []
    for entry in completed.stdout.split("\0"):
        if len(entry) <= _STATUS_ENTRY_PREFIX:
            continue
        paths.append(entry[_STATUS_ENTRY_PREFIX:])
    return paths


def _hash_file(path: Path) -> Optional[str]:
    """Return the sha256 of *path*'s bytes, or ``None`` if it cannot be read."""
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Path matching (resolve real locations so symlink / .. cannot escape)
# ---------------------------------------------------------------------------


def _resolve(path: Path) -> Path:
    """Resolve *path* to a real absolute location (symlinks and ``..`` gone).

    ``Path.resolve`` is non-strict on Python 3.9+, so a not-yet-existing path
    (e.g. a file the delegate deleted) still normalizes lexically. A resolution
    error (symlink loop) falls back to a lexical normalization so matching never
    crashes.
    """
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.normpath(str(path)))


def _resolve_declared_roots(declared: tuple[str, ...], cwd: str) -> list[Path]:
    """Resolve each declared allowlist entry, relative to *cwd*, to a real path."""
    base = Path(cwd)
    roots: list[Path] = []
    for pattern in declared:
        candidate = Path(pattern)
        if not candidate.is_absolute():
            candidate = base / candidate
        roots.append(_resolve(candidate))
    return roots


def _is_in_scope(modified: Path, roots: list[Path]) -> bool:
    """Return True when *modified* is equal to or under one declared root.

    Both sides are resolved first, so a ``..`` segment or a symlinked directory
    cannot make an out-of-scope write appear in scope: the comparison is on real
    locations, not on the paths as typed.
    """
    resolved = _resolve(modified)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def capture_baseline(cwd: str) -> ScopeBaseline:
    """Capture the pre-delegation working-tree state for a later scope check."""
    root = _repo_root(cwd)
    if root is None:
        return ScopeBaseline(
            repo_root=None,
            error=(f"cwd is not a git repository (or git is unavailable): {cwd}"),
        )
    entries = _dirty_paths(cwd)
    if entries is None:
        return ScopeBaseline(
            repo_root=None,
            error="git status failed while capturing the pre-delegation baseline",
        )
    hashes = {rel: _hash_file(Path(root) / rel) for rel in entries}
    return ScopeBaseline(repo_root=root, dirty_hashes=hashes)


def assert_scope(
    baseline: ScopeBaseline, declared_paths: list[str], cwd: str
) -> ScopeOutcome:
    """Assert the delegate's writes stayed within *declared_paths*.

    Never raises: any unexpected error is captured as ``undetermined`` (fail
    closed and surfaced), so the security check can never crash the worker and
    can never silently degrade into a pass.
    """
    declared = tuple(declared_paths)
    try:
        return _assert_scope(baseline, declared, cwd)
    except Exception as exc:  # fail closed; surface, never crash the worker
        return ScopeOutcome(
            declared=declared,
            status="undetermined",
            violating_paths=(),
            detail=f"scope check errored unexpectedly: {exc!r}",
        )


def _assert_scope(
    baseline: ScopeBaseline, declared: tuple[str, ...], cwd: str
) -> ScopeOutcome:
    if baseline.repo_root is None:
        return ScopeOutcome(
            declared,
            "undetermined",
            (),
            baseline.error or "could not determine the pre-delegation baseline",
        )

    # The repo identity must be stable across the run; a cwd that stopped being
    # the same worktree means we can no longer attribute changes. Fail closed.
    after_root = _repo_root(cwd)
    if after_root is None or after_root != baseline.repo_root:
        return ScopeOutcome(
            declared,
            "undetermined",
            (),
            "git repository became unavailable or changed identity during the run",
        )

    after_paths = _dirty_paths(cwd)
    if after_paths is None:
        return ScopeOutcome(
            declared,
            "undetermined",
            (),
            "git status failed while evaluating post-delegation changes",
        )

    touched = _delegate_touched(baseline, after_paths)
    repo_root = Path(baseline.repo_root)
    roots = _resolve_declared_roots(declared, cwd)
    violating = tuple(
        sorted(rel for rel in touched if not _is_in_scope(repo_root / rel, roots))
    )
    if violating:
        return ScopeOutcome(
            declared,
            "violated",
            violating,
            f"delegate modified {len(violating)} path(s) outside the declared scope",
        )
    return ScopeOutcome(
        declared,
        "ok",
        (),
        f"all {len(touched)} modified path(s) were inside the declared scope",
    )


def _delegate_touched(baseline: ScopeBaseline, after_paths: list[str]) -> set[str]:
    """Return repo-relative paths the delegate actually changed.

    Pre-existing dirt (dirty before AND after with identical content) is
    excluded so it is never misattributed; a path further modified, newly
    dirtied, or reverted to HEAD by the delegate is included.
    """
    repo_root = Path(baseline.repo_root or "")
    before = baseline.dirty_hashes
    before_set = set(before)
    after_set = set(after_paths)
    touched: set[str] = set()
    # Clean before, dirty after -> the delegate created or modified it.
    touched |= after_set - before_set
    # Dirty before but clean after -> the delegate reverted it to HEAD.
    touched |= before_set - after_set
    # Dirty before and after -> attribute only if the content actually changed.
    for rel in after_set & before_set:
        if _hash_file(repo_root / rel) != before[rel]:
            touched.add(rel)
    return touched
