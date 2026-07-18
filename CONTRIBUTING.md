# Contributing to crossagent

Thanks for helping make agent-to-agent second opinions better.

## Ways to contribute

- **Harden an experimental advisor.** `codex`, `opencode`, `commandcode`, and `gemini` ship best-effort default flags. If you run one of these, confirm the invocation and session behavior against a real install and send a PR to `src/crossagent/advisors.py` (plus a test).
- **Add a new advisor.** Any CLI agent that takes a prompt and prints an answer can be an advisor. Add a built-in entry or document it as a user-config recipe in the README.
- **Improve the second-opinion protocol.** The value is in the prompt quality — see `skills/crossagent/references/second-opinion-protocol.md`.
- **Harden the process supervisor or parsers.** The supervisor in `src/crossagent/runner.py` handles concurrent output draining, heartbeat emission, and process-tree cleanup. Parsers in `src/crossagent/parsers.py` convert advisor output (text, Claude stream-json, Codex JSONL) to structured results. Contributions should include tests for the added parser and a matching advisor entry.
- **Durable job subcommands.** `start`, `wait`, `status`, `result`, `logs`, `cancel` live in `src/crossagent/cli.py` (subcommand dispatch) and `src/crossagent/worker.py` (detached worker). They reuse `src/crossagent/jobs.py` for state persistence. Tests go in `tests/test_job_cli.py`.

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

Include your advisor CLI + version, the `crossagent` command you ran (redact secrets), and the `[crossagent] running: …` line from stderr.
