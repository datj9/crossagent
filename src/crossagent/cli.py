"""Run a named, resumable second-opinion session with a peer coding-agent CLI.

`crossagent` lets one AI coding agent get a second opinion from another. It builds
the right command for the chosen advisor (Claude by default), keeps the process
alive until the advisor finishes, streams progress to stderr, prints the final
answer to stdout, and remembers the session so a follow-up can resume it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from . import advisors as advisors_mod
from . import jobs as jobs_mod
from . import parsers as parsers_mod
from . import registry as reg
from . import runner as runner_mod
from . import worker as worker_mod
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


def build_command(
    advisor: Advisor,
    args: argparse.Namespace,
    registry: dict[str, Any],
    *,
    include_prompt: bool = True,
) -> tuple[list[str], str]:
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
        elif stored_id and not args.new_session:
            if advisor.resume_command:
                cmd.extend(advisor.resume_command)
                cmd.append(stored_id)
            elif advisor.resume_flag:
                cmd.extend([advisor.resume_flag, stored_id])
        elif args.name and advisor.session_name_flag:
            cmd.extend([advisor.session_name_flag, args.name])
        if args.fork_session and advisor.fork_flag:
            cmd.append(advisor.fork_flag)

    if include_prompt:
        _append_prompt(cmd, advisor, prompt=args._prompt)
    return cmd, key


def _run_advisor(
    cmd: list[str], cwd: str | None, parser_name: str
) -> tuple[int, parsers_mod.ParsedResult]:
    parser = parsers_mod.get_parser(parser_name)
    outcome = runner_mod.run(cmd, cwd=cwd, consumer=parser, max_runtime_seconds=None)
    parsed = (
        outcome.result
        if isinstance(outcome.result, parsers_mod.ParsedResult)
        else parsers_mod.ParsedResult()
    )
    return outcome.exit_code, parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="crossagent", description=__doc__)
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--agent",
        "--advisor",
        dest="agent",
        default="claude",
        help="Peer agent to ask: claude, codex, opencode, commandcode, gemini, or a custom advisor. Default: claude.",
    )
    parser.add_argument(
        "--name",
        help="Stable session name. Reused names auto-resume the stored session.",
    )
    parser.add_argument(
        "--resume", help="Force a --resume target: session id or search term."
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Ignore any stored session for --name and start fresh.",
    )
    parser.add_argument(
        "--fork-session",
        action="store_true",
        help="Fork a new session from the existing conversation.",
    )
    parser.add_argument("--prompt-file", help="Path to the second-opinion prompt.")
    parser.add_argument(
        "--prompt", help="Prompt text. Prefer --prompt-file for long prompts."
    )
    parser.add_argument(
        "--cwd",
        help="Working directory for the advisor. Defaults to the current directory.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Advisor model or alias (advisor-specific). Empty = advisor default.",
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="Claude: run with --safe-mode (skip repo config/skills/hooks).",
    )
    parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="Claude: use single JSON output instead of streaming.",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="Claude: include partial stream messages.",
    )
    parser.add_argument(
        "--tools", help='Claude: pass --tools, e.g. "" for none or "Read,Bash".'
    )
    parser.add_argument(
        "--allowed-tools",
        action="append",
        default=[],
        help="Claude: repeatable --allowedTools value.",
    )
    parser.add_argument("--permission-mode", help="Claude: --permission-mode value.")
    parser.add_argument("--system-prompt", help="Claude: --system-prompt value.")
    parser.add_argument(
        "--raw-arg",
        action="append",
        default=[],
        help="Repeatable raw argument passed straight to the advisor CLI.",
    )
    parser.add_argument(
        "--registry", default=str(reg.DEFAULT_REGISTRY), help="Session registry path."
    )
    parser.add_argument(
        "--list-advisors", action="store_true", help="Print known advisors and exit."
    )
    parser.set_defaults(stream=True)
    return parser.parse_args(argv)


def _print_advisors() -> int:
    for name, adv in sorted(advisors_mod.available().items()):
        tag = " (experimental)" if adv.experimental else ""
        print(f"{name:14} -> {adv.executable}{tag}")
        if adv.notes:
            print(f"{'':14}    {adv.notes}")
    return 0


_JOB_SUBCOMMANDS = {
    "start",
    "wait",
    "status",
    "result",
    "logs",
    "cancel",
    "list",
    "dashboard",
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in _JOB_SUBCOMMANDS:
        return _job_subcommand(argv[0], argv[1:])
    return _foreground_main(argv)


def _foreground_main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.list_advisors:
        return _print_advisors()

    try:
        advisor = advisors_mod.resolve(args.agent)
    except KeyError as exc:
        print(f"[crossagent] {exc}", file=sys.stderr)
        return 2

    if advisor.experimental:
        print(
            f"[crossagent] advisor '{advisor.name}' is experimental — verify flags for your install.",
            file=sys.stderr,
        )

    args._prompt = read_prompt(args)
    registry_path = Path(args.registry).expanduser()
    registry = reg.load(registry_path)
    cmd, key = build_command(advisor, args, registry)

    print(f"[crossagent] running: {_redacted_command(cmd)}", file=sys.stderr)

    try:
        return _dispatch(advisor, args, cmd, key, registry, registry_path)
    except FileNotFoundError:
        print(
            f"[crossagent] advisor CLI not found on PATH: '{advisor.executable}'. "
            f"Install it, or point '{advisor.name}' at the right executable in "
            f"{advisors_mod.USER_CONFIG}.",
            file=sys.stderr,
        )
        return 127


def _dispatch(
    advisor: Advisor,
    args: argparse.Namespace,
    cmd: list[str],
    key: str,
    registry: dict[str, Any],
    registry_path: Path,
) -> int:
    code, parsed = _run_advisor(cmd, args.cwd, advisor.result_parser)

    if parsed.failure:
        error = parsed.error or f"{advisor.name} exited with code {code}"
        print(f"[crossagent] {advisor.name} returned error: {error}", file=sys.stderr)
        return code if code != 0 else 1

    if parsed.result is not None:
        print(parsed.result, end="" if parsed.result.endswith("\n") else "\n")

    if parsed.session_id and key:
        reg.record(
            registry_path,
            registry,
            key,
            session_id=parsed.session_id,
            name=args.name,
            cwd=args.cwd or os.getcwd(),
            advisor=advisor.name,
            model=args.model,
        )
        print(
            f"[crossagent] saved session name={key} id={parsed.session_id}",
            file=sys.stderr,
        )

    return code


# ---------------------------------------------------------------------------
# Durable job subcommands
# ---------------------------------------------------------------------------


def _job_subcommand(subcommand: str, argv: list[str]) -> int:
    args = _parse_job_args(subcommand, argv)
    if subcommand == "start":
        return _cmd_start(args)
    if subcommand == "wait":
        return _cmd_wait(args)
    if subcommand == "status":
        return _cmd_status(args)
    if subcommand == "result":
        return _cmd_result(args)
    if subcommand == "logs":
        return _cmd_logs(args)
    if subcommand == "cancel":
        return _cmd_cancel(args)
    if subcommand == "list":
        return _cmd_list(args)
    if subcommand == "dashboard":
        return _cmd_dashboard(args)
    return 2


def _parse_job_args(subcommand: str, argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=f"crossagent {subcommand}")
    if subcommand == "start":
        _add_advisor_args(parser)
        parser.add_argument(
            "--max-runtime",
            type=float,
            default=1800.0,
            help="Maximum seconds the advisor may run (default 1800).",
        )
        parser.add_argument("--termination-grace", type=float, default=10.0)
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(stream=True)
    elif subcommand == "wait":
        parser.add_argument("job_id")
        parser.add_argument("--timeout", type=float, default=45.0)
        parser.add_argument("--require-complete", action="store_true")
        parser.add_argument("--json", action="store_true")
    elif subcommand == "status":
        parser.add_argument("job_id")
        parser.add_argument("--json", action="store_true")
    elif subcommand == "result":
        parser.add_argument("job_id")
    elif subcommand == "logs":
        parser.add_argument("job_id")
        parser.add_argument("--follow", action="store_true")
        parser.add_argument("--stream", choices=["stdout", "stderr"], default="stdout")
    elif subcommand == "cancel":
        parser.add_argument("job_id")
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--timeout", type=float, default=45.0)
    elif subcommand == "list":
        parser.add_argument(
            "--status",
            choices=[s.value for s in jobs_mod.JobState],
            help="Only show jobs in this state.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Show at most N jobs, newest first (0 = all).",
        )
        parser.add_argument("--json", action="store_true")
    elif subcommand == "dashboard":
        parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Bind address (default 127.0.0.1 — loopback only).",
        )
        parser.add_argument(
            "--port", type=int, default=8642, help="Port to listen on (default 8642)."
        )
        parser.add_argument(
            "--no-open",
            dest="open_browser",
            action="store_false",
            help="Do not open the browser automatically.",
        )
        parser.set_defaults(open_browser=True)
    return parser.parse_args(argv)


def _add_advisor_args(parser: argparse.ArgumentParser) -> None:
    """Add the same advisor/invocation flags used by the foreground CLI."""
    parser.add_argument("--agent", "--advisor", dest="agent", default="claude")
    parser.add_argument("--name")
    parser.add_argument("--resume")
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--fork-session", action="store_true")
    parser.add_argument("--prompt-file")
    parser.add_argument("--prompt")
    parser.add_argument("--cwd")
    parser.add_argument("--model", default="")
    parser.add_argument("--safe-mode", action="store_true")
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--tools")
    parser.add_argument("--allowed-tools", action="append", default=[])
    parser.add_argument("--permission-mode")
    parser.add_argument("--system-prompt")
    parser.add_argument("--raw-arg", action="append", default=[])
    parser.add_argument("--registry", default=str(reg.DEFAULT_REGISTRY))
    parser.add_argument("--parent", help="Declare an explicit parent job ID.")
    parser.add_argument(
        "--no-parent",
        action="store_true",
        help="Force top-level; ignore any inherited parent.",
    )
    parser.add_argument("--trace-id", help="Declare the tree identity (trace ID).")
    parser.add_argument(
        "--orchestrator-label", help="Display label for the tree's root."
    )


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        advisor = advisors_mod.resolve(args.agent)
    except KeyError as exc:
        print(f"[crossagent] {exc}", file=sys.stderr)
        return 2

    args._prompt = read_prompt(args)
    registry_path = Path(args.registry).expanduser()
    registry = reg.load(registry_path)
    cmd, key = build_command(advisor, args, registry, include_prompt=False)

    state_root = jobs_mod.default_state_root()
    job_id = jobs_mod.generate_job_id()
    # Resolve and validate lineage BEFORE creating the job directory, so a
    # rejected delegation never leaves a prompt/command.json artifact behind.
    try:
        parent_id, trace_id, label, depth = jobs_mod.resolve_lineage(
            no_parent=args.no_parent,
            parent_flag=args.parent,
            parent_env=os.environ.get("CROSSAGENT_PARENT_JOB_ID"),
            trace_flag=args.trace_id,
            trace_env=os.environ.get("CROSSAGENT_TRACE_ID"),
            label_flag=args.orchestrator_label,
            label_env=os.environ.get("CROSSAGENT_ORCHESTRATOR_LABEL"),
            depth_env=os.environ.get("CROSSAGENT_NESTING_DEPTH"),
            state_root=state_root,
            new_job_id=job_id,
        )
    except jobs_mod.LineageError as exc:
        print(f"[crossagent] {exc}", file=sys.stderr)
        return 1

    job_dir = jobs_mod.create_job_dir(state_root, job_id)
    _write_job_prompt(job_dir, args._prompt)
    _write_command_info(job_dir, advisor, args, cmd, key, registry_path)

    now = datetime.now(timezone.utc).isoformat()
    job = jobs_mod.Job(
        job_id=job_id,
        status=jobs_mod.JobState.PENDING,
        advisor=advisor.name,
        name=args.name or "",
        cwd=args.cwd or os.getcwd(),
        redacted_command=_redacted_command([*cmd, "<prompt>"]),
        started_at=now,
        updated_at=now,
        last_activity_at=now,
        last_event="start.created",
        max_runtime_seconds=int(args.max_runtime),
        termination_grace_seconds=int(args.termination_grace),
        parent_job_id=parent_id,
        trace_id=trace_id,
        orchestrator_label=label,
        nesting_depth=depth,
    )
    jobs_mod.save_state(job_dir, job)

    try:
        worker_proc = worker_mod.start_worker(job_id, state_root)
    except OSError as exc:
        print(f"[crossagent] failed to launch worker: {exc}", file=sys.stderr)
        return 1

    # Wait briefly for the worker to confirm it has taken over.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            job = jobs_mod.load_state(job_dir)
            if job.status != jobs_mod.JobState.PENDING:
                break
        except Exception:
            pass
        if worker_proc.poll() is not None:
            break
        time.sleep(0.05)

    try:
        job = jobs_mod.load_state(job_dir)
    except Exception:
        pass

    if args.json:
        _json_print(
            {
                "schema_version": job.schema_version,
                "job_id": job.job_id,
                "status": job.status.value,
                "advisor": job.advisor,
                "started_at": job.started_at,
            }
        )
    else:
        print(
            f"[crossagent] started {job.job_id} ({job.status.value})", file=sys.stderr
        )
        print(job.job_id)
    return (
        0
        if worker_proc.poll() is None or job.status != jobs_mod.JobState.PENDING
        else 1
    )


def _cmd_wait(args: argparse.Namespace) -> int:
    state_root = jobs_mod.default_state_root()
    job_dir = jobs_mod.job_dir_path(state_root, args.job_id)
    try:
        job = jobs_mod.load_state(job_dir)
    except FileNotFoundError:
        print(f"[crossagent] unknown job: {args.job_id}", file=sys.stderr)
        return 2

    job = _load_and_reconcile(job_dir, job)
    deadline = time.monotonic() + args.timeout
    while not jobs_mod.is_terminal(job.status) and time.monotonic() < deadline:
        time.sleep(0.1)
        job = _load_and_reconcile(job_dir, job)

    if args.json:
        _json_print(_format_status(job))
    else:
        print(f"[crossagent] {job.job_id} status={job.status.value}", file=sys.stderr)

    if args.require_complete and job.status != jobs_mod.JobState.SUCCEEDED:
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    state_root = jobs_mod.default_state_root()
    job_dir = jobs_mod.job_dir_path(state_root, args.job_id)
    try:
        job = jobs_mod.load_state(job_dir)
    except FileNotFoundError:
        print(f"[crossagent] unknown job: {args.job_id}", file=sys.stderr)
        return 2

    job = _load_and_reconcile(job_dir, job)
    if args.json:
        _json_print(_format_status(job))
    else:
        print(f"[crossagent] {job.job_id} status={job.status.value}", file=sys.stderr)
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    state_root = jobs_mod.default_state_root()
    job_dir = jobs_mod.job_dir_path(state_root, args.job_id)
    try:
        job = jobs_mod.load_state(job_dir)
    except FileNotFoundError:
        print(f"[crossagent] unknown job: {args.job_id}", file=sys.stderr)
        return 2

    if job.status != jobs_mod.JobState.SUCCEEDED:
        print(
            f"[crossagent] job {job.job_id} is {job.status.value} — no result available",
            file=sys.stderr,
        )
        return 1

    result_path = job_dir / "result.md"
    if not result_path.exists():
        print(f"[crossagent] result file missing for {job.job_id}", file=sys.stderr)
        return 1

    print(result_path.read_text(encoding="utf-8"), end="")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    state_root = jobs_mod.default_state_root()
    job_dir = jobs_mod.job_dir_path(state_root, args.job_id)
    try:
        jobs_mod.load_state(job_dir)
    except FileNotFoundError:
        print(f"[crossagent] unknown job: {args.job_id}", file=sys.stderr)
        return 2

    log_path = job_dir / f"{args.stream}.log"
    if not log_path.exists():
        return 0

    if args.follow:
        return _follow_log(log_path, job_dir)

    print(log_path.read_text(encoding="utf-8"), end="")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    state_root = jobs_mod.default_state_root()
    job_dir = jobs_mod.job_dir_path(state_root, args.job_id)
    try:
        job = jobs_mod.load_state(job_dir)
    except FileNotFoundError:
        print(f"[crossagent] unknown job: {args.job_id}", file=sys.stderr)
        return 2

    if jobs_mod.is_terminal(job.status):
        print(
            f"[crossagent] job {job.job_id} is already {job.status.value}",
            file=sys.stderr,
        )
        return 0

    jobs_mod.create_cancel_request(job_dir)
    print(f"[crossagent] cancellation requested for {job.job_id}", file=sys.stderr)

    if args.wait:
        deadline = time.monotonic() + args.timeout
        while not jobs_mod.is_terminal(job.status) and time.monotonic() < deadline:
            time.sleep(0.1)
            job = _load_and_reconcile(job_dir, job)
        if not jobs_mod.is_terminal(job.status):
            print(
                f"[crossagent] job did not terminate within {args.timeout}s",
                file=sys.stderr,
            )
            return 1
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    state_root = jobs_mod.default_state_root()
    listed_jobs = _collect_jobs(state_root)

    if args.status:
        listed_jobs = [job for job in listed_jobs if job.status.value == args.status]

    listed_jobs.sort(key=lambda job: (job.started_at, job.job_id), reverse=True)
    if args.limit > 0:
        listed_jobs = listed_jobs[: args.limit]

    if args.json:
        _json_print(
            {
                "schema_version": 1,
                "jobs": [jobs_mod.list_entry(job) for job in listed_jobs],
            }
        )
        return 0

    _print_job_table(listed_jobs)
    return 0


def _collect_jobs(state_root: Path) -> list[jobs_mod.Job]:
    """Load every job under *state_root*, warning on stderr about skipped dirs."""

    def warn_skipped(dir_name: str, exc: Exception) -> None:
        print(
            f"[crossagent] skipping unreadable job dir {dir_name}: {exc}",
            file=sys.stderr,
        )

    return jobs_mod.collect_jobs(state_root, on_skip=warn_skipped)


def _print_job_table(listed_jobs: list[jobs_mod.Job]) -> None:
    if not listed_jobs:
        print("[crossagent] no jobs found", file=sys.stderr)
        return
    header = f"{'JOB ID':<34} {'STATUS':<10} {'ADVISOR':<12} {'ELAPSED':>8} {'IDLE':>6}  NAME"
    print(header)
    for job in listed_jobs:
        entry = _format_status(job)
        elapsed = _format_duration(entry["elapsed_seconds"], job)
        idle = (
            "-"
            if jobs_mod.is_terminal(job.status)
            else _format_seconds(entry["idle_seconds"])
        )
        print(
            f"{job.job_id:<34} {job.status.value:<10} {job.advisor:<12} "
            f"{elapsed:>8} {idle:>6}  {job.name}"
        )


def _format_duration(elapsed_seconds: int, job: jobs_mod.Job) -> str:
    if job.duration_seconds is not None:
        return _format_seconds(int(job.duration_seconds))
    return _format_seconds(elapsed_seconds)


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from . import dashboard as dashboard_mod

    return dashboard_mod.serve(
        args.host,
        args.port,
        jobs_mod.default_state_root(),
        open_browser=args.open_browser,
    )


def _format_seconds(total_seconds: int) -> str:
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------------------
# Subcommand helpers
# ---------------------------------------------------------------------------


def _write_job_prompt(job_dir: Path, prompt: str) -> None:
    prompt_path = job_dir / "prompt"
    prompt_path.write_text(prompt, encoding="utf-8")
    try:
        prompt_path.chmod(0o600)
    except OSError:
        pass


def _write_command_info(
    job_dir: Path,
    advisor: Advisor,
    args: argparse.Namespace,
    cmd: list[str],
    key: str,
    registry_path: Path,
) -> None:
    info = {
        "command": cmd,
        "prompt_delivery": advisor.prompt_delivery,
        "cwd": args.cwd or os.getcwd(),
        "result_parser": advisor.result_parser,
        "registry_path": str(registry_path),
        "key": key,
        "name": args.name,
        "model": args.model,
        "advisor": advisor.name,
    }
    jobs_mod.atomic_json_write(info, job_dir / "command.json")


def _json_print(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _format_status(job: jobs_mod.Job) -> dict[str, Any]:
    return jobs_mod.runtime_status(job)


def _load_and_reconcile(job_dir: Path, job: jobs_mod.Job) -> jobs_mod.Job:
    try:
        fresh = jobs_mod.load_state(job_dir)
    except FileNotFoundError:
        return job
    return jobs_mod.reconcile_stale(fresh, job_dir)


def _follow_log(path: Path, job_dir: Path) -> int:
    with open(path, "r", encoding="utf-8") as f:
        while True:
            line = f.readline()
            if line:
                print(line, end="")
                continue
            if not _job_active(job_dir):
                break
            time.sleep(0.1)
    return 0


def _job_active(job_dir: Path) -> bool:
    try:
        job = jobs_mod.load_state(job_dir)
    except Exception:
        return False
    return not jobs_mod.is_terminal(job.status)


if __name__ == "__main__":
    raise SystemExit(main())
