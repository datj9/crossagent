"""Run a named, resumable consultation with a peer coding-agent CLI.

`consult` lets one AI coding agent get a second opinion from another. It builds
the right command for the chosen advisor (Claude by default), keeps the process
alive until the advisor finishes, streams progress to stderr, prints the final
answer to stdout, and remembers the session so a follow-up can resume it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import advisors as advisors_mod
from . import registry as reg
from .advisors import Advisor


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --prompt-file, --prompt, or pipe the prompt on stdin.")


def _append_prompt(cmd: list[str], advisor: Advisor, prompt: str) -> None:
    delivery = advisor.prompt_delivery
    if delivery == "dashdash":
        cmd.extend(["--", prompt])
    elif delivery.startswith("flag:"):
        cmd.extend([delivery.split(":", 1)[1], prompt])
    else:  # "positional"
        cmd.append(prompt)


def _redacted_command(cmd: list[str]) -> str:
    """Format an advisor command without exposing its final prompt argument."""
    if not cmd:
        return "<prompt>"
    return shlex.join([*cmd[:-1], "<prompt>"])


def build_command(advisor: Advisor, args: argparse.Namespace, registry: dict[str, Any]) -> tuple[list[str], str]:
    cmd = [advisor.executable, *advisor.base_args, *advisor.invoke_args]

    if args.model and advisor.model_flag:
        cmd.extend([advisor.model_flag, args.model])

    if advisor.supports_stream:
        cmd.extend(args.stream and advisor.stream_args or advisor.json_args)
        if args.stream and args.partial:
            cmd.append("--include-partial-messages")

    if args.safe_mode:
        cmd.append("--safe-mode")
    if args.permission_mode:
        cmd.extend(["--permission-mode", args.permission_mode])
    if args.tools is not None:
        cmd.extend(["--tools", args.tools])
    for allowed in args.allowed_tools:
        cmd.extend(["--allowedTools", allowed])
    if args.system_prompt:
        cmd.extend(["--system-prompt", args.system_prompt])
    for extra in args.raw_arg:
        cmd.append(extra)

    key = reg.session_key(advisor.name, args.name)
    stored_id = reg.stored_session_id(registry, key)

    if advisor.supports_sessions:
        if args.resume and advisor.resume_flag:
            cmd.extend([advisor.resume_flag, args.resume])
        elif stored_id and not args.new_session and advisor.resume_flag:
            cmd.extend([advisor.resume_flag, stored_id])
        elif args.name and advisor.session_name_flag:
            cmd.extend([advisor.session_name_flag, args.name])
        if args.fork_session and advisor.fork_flag:
            cmd.append(advisor.fork_flag)

    _append_prompt(cmd, advisor, prompt=args._prompt)
    return cmd, key


# --- Claude stream-json handling --------------------------------------------

def summarize_event(event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        print(f"[consult] init session={event.get('session_id')} model={event.get('model')} "
              f"cwd={event.get('cwd')}", file=sys.stderr)
    elif kind == "assistant":
        message = event.get("message", {})
        blocks = message.get("content", []) if isinstance(message, dict) else []
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        if text:
            print(f"[consult] assistant: {text.replace(chr(10), ' ')[:240]}", file=sys.stderr)
    elif kind == "result":
        print(f"[consult] result subtype={event.get('subtype')} session={event.get('session_id')} "
              f"cost={event.get('total_cost_usd')}", file=sys.stderr)
    elif kind == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        print(f"[consult] rate_limit status={info.get('status')} resetsAt={info.get('resetsAt')}",
              file=sys.stderr)


def _run_stream(cmd: list[str], cwd: str | None) -> tuple[int, dict[str, Any] | None]:
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    assert proc.stdout is not None and proc.stderr is not None
    final: dict[str, Any] | None = None
    for line in proc.stdout:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            print(stripped, file=sys.stderr)
            continue
        summarize_event(event)
        if event.get("type") == "result":
            final = event
    err = proc.stderr.read()
    if err:
        print(err, file=sys.stderr, end="")
    return proc.wait(), final


def _run_text(cmd: list[str], cwd: str | None) -> tuple[int, str]:
    completed = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return completed.returncode, completed.stdout


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="consult", description=__doc__)
    parser.add_argument("--agent", "--advisor", dest="agent", default="claude",
                        help="Peer agent to consult: claude, codex, opencode, commandcode, gemini, or a custom advisor. Default: claude.")
    parser.add_argument("--name", help="Stable consultation name. Reused names auto-resume the stored session.")
    parser.add_argument("--resume", help="Force a --resume target: session id or search term.")
    parser.add_argument("--new-session", action="store_true", help="Ignore any stored session for --name and start fresh.")
    parser.add_argument("--fork-session", action="store_true", help="Fork a new session from the existing conversation.")
    parser.add_argument("--prompt-file", help="Path to the consultation prompt.")
    parser.add_argument("--prompt", help="Prompt text. Prefer --prompt-file for long prompts.")
    parser.add_argument("--cwd", help="Working directory for the advisor. Defaults to the current directory.")
    parser.add_argument("--model", default="", help="Advisor model or alias (advisor-specific). Empty = advisor default.")
    parser.add_argument("--safe-mode", action="store_true", help="Claude: run with --safe-mode (skip repo config/skills/hooks).")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Claude: use single JSON output instead of streaming.")
    parser.add_argument("--partial", action="store_true", help="Claude: include partial stream messages.")
    parser.add_argument("--tools", help='Claude: pass --tools, e.g. "" for none or "Read,Bash".')
    parser.add_argument("--allowed-tools", action="append", default=[], help="Claude: repeatable --allowedTools value.")
    parser.add_argument("--permission-mode", help="Claude: --permission-mode value.")
    parser.add_argument("--system-prompt", help="Claude: --system-prompt value.")
    parser.add_argument("--raw-arg", action="append", default=[], help="Repeatable raw argument passed straight to the advisor CLI.")
    parser.add_argument("--registry", default=str(reg.DEFAULT_REGISTRY), help="Session registry path.")
    parser.add_argument("--list-advisors", action="store_true", help="Print known advisors and exit.")
    parser.set_defaults(stream=True)
    return parser.parse_args(argv)


def _print_advisors() -> int:
    for name, adv in sorted(advisors_mod.available().items()):
        tag = " (experimental)" if adv.experimental else ""
        print(f"{name:14} -> {adv.executable}{tag}")
        if adv.notes:
            print(f"{'':14}    {adv.notes}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_advisors:
        return _print_advisors()

    try:
        advisor = advisors_mod.resolve(args.agent)
    except KeyError as exc:
        print(f"[consult] {exc}", file=sys.stderr)
        return 2

    if advisor.experimental:
        print(f"[consult] advisor '{advisor.name}' is experimental — verify flags for your install.",
              file=sys.stderr)

    args._prompt = read_prompt(args)
    registry_path = Path(args.registry).expanduser()
    registry = reg.load(registry_path)
    cmd, key = build_command(advisor, args, registry)

    print(f"[consult] running: {_redacted_command(cmd)}", file=sys.stderr)

    try:
        return _dispatch(advisor, args, cmd, key, registry, registry_path)
    except FileNotFoundError:
        print(f"[consult] advisor CLI not found on PATH: '{advisor.executable}'. "
              f"Install it, or point '{advisor.name}' at the right executable in "
              f"{advisors_mod.USER_CONFIG}.", file=sys.stderr)
        return 127


def _dispatch(advisor: Advisor, args: argparse.Namespace, cmd: list[str], key: str,
              registry: dict[str, Any], registry_path: Path) -> int:
    if advisor.supports_stream and args.stream:
        code, final = _run_stream(cmd, args.cwd)
        if final:
            if final.get("is_error"):
                errors = final.get("errors") or final.get("api_error_status") or "unknown error"
                print(f"[consult] {advisor.name} returned error: {errors}", file=sys.stderr)
            result = final.get("result")
            structured = final.get("structured_output")
            if result:
                print(result)
            elif structured is not None:
                print(json.dumps(structured, indent=2, sort_keys=True))
            session_id = final.get("session_id")
            if key and session_id:
                reg.record(registry_path, registry, key, session_id=session_id, name=args.name,
                           cwd=args.cwd or os.getcwd(), advisor=advisor.name, model=args.model)
                print(f"[consult] saved session name={key} id={session_id}", file=sys.stderr)
        return code

    code, out = _run_text(cmd, args.cwd)
    if out.strip():
        print(out, end="" if out.endswith("\n") else "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
