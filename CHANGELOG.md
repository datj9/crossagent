# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [0.1.0] - 2026-07-18

### Added
- Initial release: `crossagent` CLI and Agent Skill for getting a second opinion from a peer AI coding agent.
- Cross-agent advisor registry with built-ins for **claude** (full), **codex**, **opencode**, **commandcode**, and **gemini** (experimental).
- User-overridable advisors via `~/.config/crossagent/advisors.json` — add or fix an advisor with no code change.
- Named, resumable sessions keyed by `advisor:name`, with `--new-session`, `--resume`, and `--fork-session`.
- Streamed progress and final-answer capture for Claude's `stream-json`; text capture for other advisors.
- `install.sh` that places the skill into detected agent config dirs and installs the CLI.
- Dependency-free test suite covering command building, advisor config layering, and the session registry.

### Fixed
- Redact second-opinion prompts from command progress logs for every advisor.
- Match CommandCode's verified `--model` flag and non-interactive invocation.
