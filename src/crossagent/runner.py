"""Unified process supervisor for cross-tool delegation.

Phase 2 of the durable cross-tool delegation plan: runs an advisor CLI with
concurrent stdout/stderr draining, heartbeat emission, cancellation and runtime
limits, and process-tree cleanup.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, TextIO, Tuple


# ---------------------------------------------------------------------------
# Consumer protocol
# ---------------------------------------------------------------------------

class LineConsumer(Protocol):
    """Pluggable consumer of stdout/stderr lines."""

    def consume_stdout(self, line: str) -> None:
        """Handle one line of advisor stdout."""

    def consume_stderr(self, line: str) -> None:
        """Handle one line of advisor stderr."""

    def finish(self, exit_code: int) -> Any:
        """Return the final result/payload after the process exits."""


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunOutcome:
    """Structured result of running one advisor."""

    exit_code: int
    result: Any
    failure_category: str
    timed_out: bool
    cancelled: bool
    forced: bool
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    cmd: list[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    consumer: Optional[LineConsumer] = None,
    heartbeat_interval: float = 15.0,
    idle_warning_threshold: float = 120.0,
    max_runtime_seconds: Optional[float] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    termination_grace_seconds: float = 10.0,
) -> RunOutcome:
    """Run *cmd* under the shared process supervisor and return a ``RunOutcome``.

    The advisor is launched in its own process group/session so the entire tree
    can be terminated together.  stdout and stderr are drained concurrently via
    reader threads and a queue.  Output is fed to *consumer* incrementally.

    *heartbeat_interval* controls how often a liveness line is printed to stderr
    when the advisor produces no output.  *idle_warning_threshold* is the
    no-activity duration that triggers a warning (but never a kill).  The
    *max_runtime_seconds* boundary is the terminal safety limit; ``None``
    means unlimited (the foreground CLI default).  *should_cancel* is polled
    every loop iteration; when it returns ``True`` graceful termination begins.
    """
    start_mono = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, **_popen_kwargs())

    assert proc.stdout is not None
    assert proc.stderr is not None

    q: queue.Queue[Tuple[str, Optional[str]]] = queue.Queue()
    stdout_thread = threading.Thread(
        target=_reader, args=(proc.stdout, "stdout", q), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_reader, args=(proc.stderr, "stderr", q), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    active_readers = 2
    last_activity_mono = start_mono
    last_heartbeat_mono = start_mono
    idle_warning_emitted = False
    termination_started = False
    termination_start_mono = 0.0
    timed_out = False
    cancelled = False
    forced = False

    try:
        while True:
            now = time.monotonic()

            # Timeout / cancellation checks
            if not termination_started:
                if max_runtime_seconds is not None and (now - start_mono) >= max_runtime_seconds:
                    timed_out = True
                    termination_started = True
                    termination_start_mono = time.monotonic()
                    _graceful_terminate(proc)

                if should_cancel is not None and should_cancel():
                    cancelled = True
                    termination_started = True
                    termination_start_mono = time.monotonic()
                    _graceful_terminate(proc)

            # Drain the queue
            try:
                stream, line = q.get(timeout=0.1)
            except queue.Empty:
                stream, line = None, None

            if line is not None:
                last_activity_mono = now
                idle_warning_emitted = False
                if consumer is not None:
                    if stream == "stdout":
                        consumer.consume_stdout(line)
                    elif stream == "stderr":
                        consumer.consume_stderr(line)
            elif stream is not None:
                # sentinel from a reader thread
                active_readers -= 1

            # Heartbeat and idle warning
            if not termination_started:
                if now - last_heartbeat_mono >= heartbeat_interval:
                    elapsed = now - start_mono
                    idle = now - last_activity_mono
                    print(_heartbeat_line(elapsed, idle), file=sys.stderr)
                    last_heartbeat_mono = now
                    last_activity_mono = now
                    idle_warning_emitted = False

                if now - last_activity_mono >= idle_warning_threshold and not idle_warning_emitted:
                    elapsed = now - start_mono
                    idle = now - last_activity_mono
                    print(_idle_warning_line(elapsed, idle), file=sys.stderr)
                    idle_warning_emitted = True

            # Exit / grace-period handling
            if termination_started:
                if proc.poll() is not None:
                    break
                if now - termination_start_mono >= termination_grace_seconds:
                    forced = True
                    _force_terminate(proc)
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
                    break
            else:
                if proc.poll() is not None and active_readers == 0:
                    break

        # Drain any remaining queued lines
        while True:
            try:
                stream, line = q.get(block=False)
            except queue.Empty:
                break
            if line is not None and consumer is not None:
                if stream == "stdout":
                    consumer.consume_stdout(line)
                else:
                    consumer.consume_stderr(line)

        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)

        exit_code = proc.poll()
        if exit_code is None:
            _force_terminate(proc)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            exit_code = proc.poll() or -1
            forced = True

        result = consumer.finish(exit_code) if consumer is not None else None

        return RunOutcome(
            exit_code=exit_code,
            result=result,
            failure_category=_failure_category(exit_code, timed_out, cancelled),
            timed_out=timed_out,
            cancelled=cancelled,
            forced=forced,
        )
    finally:
        _close_pipe(proc.stdout)
        _close_pipe(proc.stderr)


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

def _popen_kwargs() -> dict[str, Any]:
    """Return Popen kwargs that put the child in its own process group/session."""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _graceful_terminate(proc: subprocess.Popen[str]) -> None:
    """Ask the advisor process group to exit gracefully."""
    if sys.platform == "win32":
        _windows_graceful_terminate(proc)
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                pass


def _force_terminate(proc: subprocess.Popen[str]) -> None:
    """Force-kill the advisor process group."""
    if sys.platform == "win32":
        _windows_kill_tree(proc.pid)
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass


def _windows_graceful_terminate(proc: subprocess.Popen[str]) -> None:
    """Send a graceful break to a Windows process group, falling back to terminate."""
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    except (OSError, ValueError):
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass


def _windows_kill_tree(pid: int) -> None:
    """Terminate a Windows process tree without adding a dependency."""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        _windows_terminate_process(pid)


def _windows_terminate_process(pid: int) -> None:
    """Last-resort TerminateProcess call for a single Windows PID."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0001, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Reader thread
# ---------------------------------------------------------------------------

def _reader(
    stream: TextIO,
    name: str,
    q: queue.Queue[Tuple[str, Optional[str]]],
) -> None:
    """Read lines from *stream* and push them onto *q*; push a sentinel at EOF."""
    try:
        for line in iter(stream.readline, ""):
            q.put((name, line))
    finally:
        q.put((name, None))


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _close_pipe(pipe: Optional[TextIO]) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except Exception:
        pass


def _failure_category(exit_code: int, timed_out: bool, cancelled: bool) -> str:
    if timed_out:
        return "timeout"
    if cancelled:
        return "cancelled"
    if exit_code == 0:
        return "ok"
    return "nonzero_exit"


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    mins, secs = divmod(total, 60)
    return f"{mins:02d}:{secs:02d}"


def _heartbeat_line(elapsed: float, idle: float) -> str:
    return f"[crossagent] running elapsed={_format_duration(elapsed)} idle={_format_duration(idle)}"


def _idle_warning_line(elapsed: float, idle: float) -> str:
    return f"[crossagent] idle warning: no output for {_format_duration(idle)}"
