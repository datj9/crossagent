#!/usr/bin/env bash
#
# consult installer — puts the `consult` skill into the agent skill dirs it
# finds, and installs the `consult` CLI helper.
#
# Usage:
#   ./install.sh                      # auto-detect agents, install skill + CLI
#   ./install.sh --skill-only         # copy the skill, skip the pip install
#   ./install.sh --cli-only           # install the CLI, skip the skill copy
#   ./install.sh --target DIR         # also install the skill into DIR/consult
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="$REPO_DIR/skills/consult"

DO_SKILL=1
DO_CLI=1
EXTRA_TARGETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skill-only) DO_CLI=0; shift ;;
    --cli-only)   DO_SKILL=0; shift ;;
    --target)     EXTRA_TARGETS+=("$2"); shift 2 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[1;36m[consult]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[consult]\033[0m %s\n' "$*" >&2; }

# Agent -> skills directory. Only the ones that exist get the skill.
declare -a SKILL_DIRS=(
  "$HOME/.claude/skills"                 # Claude Code
  "$HOME/.codex/skills"                  # Codex
  "$HOME/.config/opencode/skills"        # OpenCode
  "$HOME/.commandcode/skills"            # CommandCode
  "$HOME/.cursor/skills"                 # Cursor
)

install_skill_into() {
  local dest_root="$1"
  mkdir -p "$dest_root"
  rm -rf "$dest_root/consult"
  cp -R "$SKILL_SRC" "$dest_root/consult"
  info "installed skill -> $dest_root/consult"
}

if [[ "$DO_SKILL" == 1 ]]; then
  installed_any=0
  for base in "${SKILL_DIRS[@]}"; do
    parent="$(dirname "$base")"          # e.g. ~/.claude
    if [[ -d "$parent" ]]; then
      install_skill_into "$base"
      installed_any=1
    fi
  done
  for t in "${EXTRA_TARGETS[@]:-}"; do
    [[ -n "$t" ]] && install_skill_into "$t"
  done
  if [[ "$installed_any" == 0 && ${#EXTRA_TARGETS[@]} -eq 0 ]]; then
    warn "no agent config dirs found. Re-run with --target <dir> to place the skill manually."
  fi
fi

if [[ "$DO_CLI" == 1 ]]; then
  if command -v pipx >/dev/null 2>&1; then
    info "installing CLI with pipx"
    pipx install --force "$REPO_DIR"
  elif command -v pip3 >/dev/null 2>&1; then
    info "installing CLI with pip3 (--user)"
    pip3 install --user --upgrade "$REPO_DIR"
  else
    warn "no pip/pipx found — install the CLI manually: pip install ."
  fi
  if command -v consult >/dev/null 2>&1; then
    info "CLI ready: $(command -v consult)"
    consult --list-advisors || true
  else
    warn "'consult' is not on PATH yet. Ensure your user bin dir is on PATH."
  fi
fi

info "done."
