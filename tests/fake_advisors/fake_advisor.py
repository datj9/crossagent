"""Fake advisor for runner tests.

Launched via ``sys.executable`` with controllable behavior: exit code,
stdout/stderr volume, delays, SIGTERM handling, and grandchild spawning.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def _write_pid(role: str, pid: int | None = None) -> None:
    marker = os.environ.get("FAKE_ADVISOR_PID_FILE")
    if not marker:
        return
    with open(marker, "a", encoding="utf-8") as f:
        f.write(f"{role}={pid or os.getpid()}\n")


def _spawn_grandchild(sleep_seconds: float) -> None:
    if hasattr(os, "fork"):
        pid = os.fork()
        if pid == 0:
            _write_pid("grandchild")
            time.sleep(sleep_seconds)
            sys.exit(0)
        else:
            _write_pid("child_fork", pid)
    else:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--sleep", str(sleep_seconds), "--write-pid"],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        _write_pid("grandchild", proc.pid)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stdout-count", type=int, default=0)
    parser.add_argument("--stderr-count", type=int, default=0)
    parser.add_argument("--stdout-line-size", type=int, default=80)
    parser.add_argument("--stderr-line-size", type=int, default=80)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--ignore-sigterm", action="store_true")
    parser.add_argument("--spawn-grandchild", action="store_true")
    parser.add_argument("--grandchild-sleep", type=float, default=600.0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--write-pid", action="store_true")
    args = parser.parse_args()

    if args.write_pid:
        _write_pid("self")

    if args.ignore_sigterm:
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

    if args.spawn_grandchild:
        _spawn_grandchild(args.grandchild_sleep)

    if args.sleep:
        time.sleep(args.sleep)

    for i in range(args.stdout_count):
        payload_size = max(0, args.stdout_line_size - 14)
        sys.stdout.write(f"stdout {i:05d} " + "x" * payload_size + "\n")
        sys.stdout.flush()
        if args.delay:
            time.sleep(args.delay)

    for i in range(args.stderr_count):
        payload_size = max(0, args.stderr_line_size - 14)
        sys.stderr.write(f"stderr {i:05d} " + "x" * payload_size + "\n")
        sys.stderr.flush()
        if args.delay:
            time.sleep(args.delay)

    sys.exit(args.exit_code)


if __name__ == "__main__":
    main()
