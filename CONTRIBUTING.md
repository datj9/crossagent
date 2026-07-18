# Contributing to consult

Thanks for helping make agent-to-agent second opinions better.

## Ways to contribute

- **Harden an experimental advisor.** `codex`, `opencode`, `commandcode`, and `gemini` ship best-effort default flags. If you run one of these, confirm the invocation and session behavior against a real install and send a PR to `src/consult/advisors.py` (plus a test).
- **Add a new advisor.** Any CLI agent that takes a prompt and prints an answer can be an advisor. Add a built-in entry or document it as a user-config recipe in the README.
- **Improve the consultation protocol.** The value is in the prompt quality — see `skills/consult/references/consultation-protocol.md`.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

The runtime has **zero Python dependencies**. Tests use `pytest` but never invoke a real agent CLI — they assert on the *constructed* command line and on config/registry behavior. Please keep it that way: new advisors should be covered by command-building tests, not live calls.

## Guidelines

- Small, focused files; explicit error handling; no hidden mutation (see the immutable `registry.record`).
- Keep the CLI dependency-free (standard library only).
- Every behavior change gets a test and a `CHANGELOG.md` entry.
- Conventional-commit style messages: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.

## Reporting issues

Include your advisor CLI + version, the `consult` command you ran (redact secrets), and the `[consult] running: …` line from stderr.
