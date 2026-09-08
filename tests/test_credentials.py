"""Tests for credential env scrubbing (slice S4).

A delegate is partially untrusted, so the caller's ambient secrets must not be
handed to it. ``scrub_env`` withholds credential-bearing variables by default;
``withheld_names`` reports the withheld NAMES (never values) for the audit
trail; ``--pass-env`` is the explicit opt-in exception.
"""

from __future__ import annotations

from crossagent import credentials as credentials_mod
from crossagent.credentials import is_credential_name, scrub_env, withheld_names


def test_is_credential_name_matches_common_secret_shapes():
    for name in (
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
        "DB_PASSWORD",
        "MYSQL_PASSWD",
        "STRIPE_API_KEY",
        "OPENAI_APIKEY",
        "GCP_CREDENTIALS",
        "SSH_PRIVATE_KEY",
        "SESSION_SECRET",
    ):
        assert is_credential_name(name), name


def test_is_credential_name_does_not_over_match_benign_names():
    for name in ("PATH", "HOME", "LANG", "KEYBOARD_LAYOUT", "SHELL", "TERM"):
        assert not is_credential_name(name), name


def test_is_credential_name_is_case_insensitive():
    assert is_credential_name("aws_secret_access_key")
    assert is_credential_name("Github_Token")


def test_scrub_env_removes_secrets_but_keeps_benign_vars():
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/dat",
        "AWS_SECRET_ACCESS_KEY": "super-secret-value",
        "GITHUB_TOKEN": "ghp_secret",
    }
    scrubbed = scrub_env(env)
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/home/dat"}


def test_scrub_env_secret_value_never_appears_in_output():
    env = {"DB_PASSWORD": "hunter2", "PATH": "/usr/bin"}
    scrubbed = scrub_env(env)
    assert "hunter2" not in scrubbed.values()
    assert "DB_PASSWORD" not in scrubbed


def test_scrub_env_pass_through_allowlist_keeps_named_var():
    env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "AWS_SECRET_ACCESS_KEY": "aws"}
    scrubbed = scrub_env(env, pass_through=["ANTHROPIC_API_KEY"])
    assert scrubbed == {"ANTHROPIC_API_KEY": "sk-ant-xxx"}


def test_scrub_env_does_not_mutate_input():
    env = {"GITHUB_TOKEN": "x", "PATH": "/usr/bin"}
    scrub_env(env)
    assert "GITHUB_TOKEN" in env  # original untouched


def test_withheld_names_returns_sorted_names_only():
    env = {
        "GITHUB_TOKEN": "x",
        "AWS_SECRET_ACCESS_KEY": "y",
        "PATH": "/usr/bin",
    }
    assert withheld_names(env) == ["AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"]


def test_withheld_names_excludes_pass_through():
    env = {"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "y"}
    assert withheld_names(env, pass_through=["ANTHROPIC_API_KEY"]) == ["GITHUB_TOKEN"]


def test_withheld_names_empty_when_nothing_secret():
    assert withheld_names({"PATH": "/usr/bin", "HOME": "/home/dat"}) == []


def test_scrub_and_withheld_agree_on_the_same_predicate():
    """The launch env and the audit record must never disagree about what was
    scrubbed — both go through ``is_credential_name``."""
    env = {"PATH": "/usr/bin", "API_KEY": "k", "SESSION_TOKEN": "t"}
    scrubbed = set(scrub_env(env))
    withheld = set(withheld_names(env))
    # Every name is either kept or withheld, never both, never neither.
    assert scrubbed | withheld == set(env)
    assert scrubbed & withheld == set()


def test_credential_substrings_are_uppercase_constants():
    # Guard against a lowercase entry silently never matching (predicate uppers).
    assert all(
        marker == marker.upper() for marker in credentials_mod._CREDENTIAL_SUBSTRINGS
    )
