"""Tests for the diff-scope assertion (slice S4).

The assertion answers: did the delegate modify only paths inside the declared
allowlist? It determines "modified paths" from git, diffed against a baseline
captured before the delegate ran, and fails CLOSED when it cannot tell.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from crossagent import scope as scope_mod
from crossagent.scope import ScopeBaseline, assert_scope, capture_baseline


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init"], path)
    # Identity so a commit can be made without touching global config.
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _commit_file(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(["add", rel], repo)
    _git(["commit", "-m", f"add {rel}"], repo)


def _write(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path + violation
# ---------------------------------------------------------------------------


def test_declared_only_edits_pass(repo: Path):
    _commit_file(repo, "src/app.py", "clean\n")
    baseline = capture_baseline(str(repo))
    _write(repo, "src/app.py", "edited by delegate\n")
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "ok"
    assert outcome.violating_paths == ()


def test_touching_undeclared_path_fails_and_lists_offenders(repo: Path):
    _commit_file(repo, "src/app.py", "clean\n")
    _commit_file(repo, "secret/keys.txt", "clean\n")
    baseline = capture_baseline(str(repo))
    _write(repo, "src/app.py", "ok edit\n")
    _write(repo, "secret/keys.txt", "delegate tampered\n")
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "violated"
    assert outcome.violating_paths == ("secret/keys.txt",)


def test_new_untracked_file_outside_scope_is_a_violation(repo: Path):
    baseline = capture_baseline(str(repo))
    _write(repo, "src/new.py", "in scope\n")
    _write(repo, "evil.py", "out of scope\n")
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "violated"
    assert outcome.violating_paths == ("evil.py",)


def test_delegate_that_changed_nothing_is_ok(repo: Path):
    _commit_file(repo, "src/app.py", "clean\n")
    baseline = capture_baseline(str(repo))
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "ok"
    assert outcome.violating_paths == ()


# ---------------------------------------------------------------------------
# Pre-existing dirt is not misattributed
# ---------------------------------------------------------------------------


def test_pre_existing_dirty_tree_is_not_misattributed(repo: Path):
    """A file already dirty BEFORE the delegate ran must not be blamed on it."""
    _commit_file(repo, "src/app.py", "clean\n")
    _commit_file(repo, "notes.txt", "clean\n")
    # notes.txt is dirty before the delegate runs, outside the declared scope.
    _write(repo, "notes.txt", "user's own uncommitted edit\n")
    baseline = capture_baseline(str(repo))
    # The delegate only touches an in-scope file.
    _write(repo, "src/app.py", "delegate edit\n")
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "ok", outcome.detail


def test_further_modifying_an_already_dirty_file_is_attributed(repo: Path):
    """A file dirty before AND further modified by the delegate IS attributed."""
    _commit_file(repo, "app.py", "clean\n")
    _write(repo, "app.py", "user edit\n")  # dirty before
    baseline = capture_baseline(str(repo))
    _write(repo, "app.py", "delegate changed it further\n")  # content differs
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "violated"
    assert outcome.violating_paths == ("app.py",)


def test_reverting_a_pre_existing_change_is_attributed(repo: Path):
    """If the delegate reverts a user's dirty file back to HEAD, that is a change
    it made and must be attributed."""
    _commit_file(repo, "app.py", "committed\n")
    _write(repo, "app.py", "user's uncommitted edit\n")  # dirty before
    baseline = capture_baseline(str(repo))
    _write(repo, "app.py", "committed\n")  # reverted to HEAD -> clean after
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "violated"
    assert outcome.violating_paths == ("app.py",)


# ---------------------------------------------------------------------------
# Traversal / symlink escape must not defeat the matcher
# ---------------------------------------------------------------------------


def test_dotdot_in_declared_scope_does_not_widen_it(repo: Path):
    """`--allow-path allowed/../allowed` resolves to `allowed`, not the repo
    root, so an out-of-scope sibling is still a violation."""
    _commit_file(repo, "allowed/a.py", "clean\n")
    _commit_file(repo, "other/b.py", "clean\n")
    baseline = capture_baseline(str(repo))
    _write(repo, "other/b.py", "delegate edit\n")
    outcome = assert_scope(baseline, ["allowed/../allowed"], str(repo))
    assert outcome.status == "violated"
    assert outcome.violating_paths == ("other/b.py",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_symlinked_write_target_is_resolved_before_matching(repo: Path):
    """A write that lands (via a symlink) in a real location outside the declared
    scope is flagged: git reports the real path, which we resolve and match."""
    (repo / "allowed").mkdir()
    (repo / "secret").mkdir()
    # A symlink inside the allowed dir pointing at the out-of-scope secret dir.
    (repo / "allowed" / "link").symlink_to(repo / "secret")
    baseline = capture_baseline(str(repo))
    # Writing "through" the symlink creates secret/leak.txt — git reports it at
    # its real repo-relative path, which resolves outside `allowed`.
    _write(repo, "secret/leak.txt", "exfiltrated\n")
    outcome = assert_scope(baseline, ["allowed"], str(repo))
    assert outcome.status == "violated"
    assert "secret/leak.txt" in outcome.violating_paths


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_symlinked_declared_root_still_contains_its_real_subtree(repo: Path):
    """A declared root that is itself a symlink resolves to its target, and a
    write inside that target is correctly in scope."""
    (repo / "real").mkdir()
    (repo / "alias").symlink_to(repo / "real")
    baseline = capture_baseline(str(repo))
    _write(repo, "real/x.py", "edit\n")
    outcome = assert_scope(baseline, ["alias"], str(repo))
    assert outcome.status == "ok", outcome.detail


# ---------------------------------------------------------------------------
# Fail closed: non-git cwd, git unavailable, identity shift
# ---------------------------------------------------------------------------


def test_non_git_cwd_is_undetermined_not_a_pass(tmp_path: Path):
    baseline = capture_baseline(str(tmp_path))
    assert baseline.repo_root is None
    outcome = assert_scope(baseline, ["src"], str(tmp_path))
    assert outcome.status == "undetermined"
    assert outcome.violating_paths == ()
    assert "git" in outcome.detail.lower()


def test_repo_identity_shift_mid_run_is_undetermined(repo: Path, tmp_path_factory):
    """If the baseline was captured in one repo but the after-check sees a
    different worktree root, fail closed."""
    _commit_file(repo, "src/app.py", "clean\n")
    baseline = capture_baseline(str(repo))
    other = tmp_path_factory.mktemp("other_repo")
    _init_repo(other)
    outcome = assert_scope(baseline, ["src"], str(other))
    assert outcome.status == "undetermined"


def test_unexpected_error_is_undetermined_not_a_crash():
    """assert_scope must never raise: a malformed baseline yields undetermined,
    fail closed, rather than propagating an exception into the worker."""
    bogus = ScopeBaseline(repo_root=12345)  # type: ignore[arg-type]
    outcome = assert_scope(bogus, ["src"], "/nonexistent")
    assert outcome.status == "undetermined"
    assert outcome.violating_paths == ()


def test_to_dict_shape_is_stable(repo: Path):
    baseline = capture_baseline(str(repo))
    outcome = assert_scope(baseline, ["src"], str(repo))
    as_dict = outcome.to_dict()
    assert set(as_dict) == {"declared", "status", "violating_paths", "detail"}
    assert as_dict["declared"] == ["src"]
    assert isinstance(as_dict["violating_paths"], list)


def test_git_unavailable_is_undetermined(repo: Path, monkeypatch):
    """If git cannot be executed at all, scope is undetermined (fail closed)."""

    def _boom(*_args, **_kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(scope_mod.subprocess, "run", _boom)
    baseline = capture_baseline(str(repo))
    assert baseline.repo_root is None
    outcome = assert_scope(baseline, ["src"], str(repo))
    assert outcome.status == "undetermined"
