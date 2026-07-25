"""Advisor output parsers for the shared process runner.

Phase 4 of the durable cross-tool delegation plan: unifies text, Claude stream-json,
and Codex JSONL parsing behind a single ``EventParser`` interface that is compatible
with ``runner.LineConsumer``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

# Where a cost figure came from. ``advisor`` is the vendor-declared estimate;
# ``computed`` is derived from a local price table; ``unknown`` means nothing
# was measured — never conflate that with a measured ``0.0`` (D3/D7).
CostSource = Literal["advisor", "computed", "unknown"]


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdvisorMetrics:
    """Structured telemetry pulled from an advisor's terminal event.

    ``usage_details`` and ``cost_details`` are open-keyed maps (D1). Empty maps
    with ``cost_source == "unknown"`` mean *unmeasured* and must stay
    distinguishable from a measured zero (D7). Token counts are NOT summed
    (D2): each token appears under exactly one ``usage_details`` key.
    """

    usage_details: dict[str, int] = field(default_factory=dict)
    cost_details: dict[str, float] = field(default_factory=dict)
    duration_ms: Optional[int] = None
    cost_source: CostSource = "unknown"
    model_reported: Optional[str] = None


def _coerce_token_count(value: object) -> Optional[int]:
    """Return *value* as an int token count, or ``None`` if it is not one.

    ``bool`` is rejected (it is an ``int`` subclass but never a token count).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_cost(value: object) -> Optional[float]:
    """Return *value* as a float USD amount, or ``None`` if it is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_duration_ms(value: object) -> Optional[int]:
    """Return *value* as an int millisecond duration, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _usage_from_mapping(usage: dict[str, Any]) -> dict[str, int]:
    """Collect the scalar int token counts from a *usage* mapping.

    Non-int values (nested breakdowns like ``server_tool_use``, strings, bools)
    are skipped rather than guessed at (D4). Nothing is summed (D2).
    """
    details: dict[str, int] = {}
    for key, value in usage.items():
        coerced = _coerce_token_count(value)
        if coerced is not None:
            details[str(key)] = coerced
    return details


def _extract_model(event: dict[str, Any], fallback: Optional[str]) -> Optional[str]:
    model = event.get("model")
    if isinstance(model, str) and model:
        return model
    model_usage = event.get("modelUsage")
    if isinstance(model_usage, dict):
        for name in model_usage:
            if isinstance(name, str) and name:
                return name
    if isinstance(fallback, str) and fallback:
        return fallback
    return None


def extract_claude_metrics(
    event: Any, *, model: Optional[str] = None
) -> AdvisorMetrics:
    """Extract token/cost/duration telemetry from a Claude ``result`` event.

    Parses defensively (D4): a missing, renamed, or wrong-typed field degrades
    to *unknown* and never raises. *model* is the model reported on the init
    event, used as a fallback when the result event omits it.
    """
    if not isinstance(event, dict):
        return AdvisorMetrics()

    usage = event.get("usage")
    if isinstance(usage, dict):
        usage_details = _usage_from_mapping(usage)
    else:
        # Older stream shapes expose the totals at the top level instead.
        usage_details = {}
        for key in ("total_input_tokens", "total_output_tokens"):
            coerced = _coerce_token_count(event.get(key))
            if coerced is not None:
                usage_details[key] = coerced

    cost = _coerce_cost(event.get("total_cost_usd"))
    if cost is not None:
        # A single scalar cost is deliberately stored under a derived "total"
        # key (D3) and labelled as the vendor's own estimate.
        cost_details: dict[str, float] = {"total": cost}
        cost_source: CostSource = "advisor"
    else:
        cost_details = {}
        cost_source = "unknown"

    return AdvisorMetrics(
        usage_details=usage_details,
        cost_details=cost_details,
        duration_ms=_coerce_duration_ms(event.get("duration_ms")),
        cost_source=cost_source,
        model_reported=_extract_model(event, model),
    )


def extract_codex_metrics(event: Any) -> AdvisorMetrics:
    """Extract telemetry from a Codex ``turn.completed`` event, if any.

    Codex's telemetry shape is unverified (see slice S2), so this reads only a
    plainly-named ``usage`` mapping and cost field when present and degrades to
    *unknown* otherwise — it never guesses at or requires a schema (D4).
    """
    if not isinstance(event, dict):
        return AdvisorMetrics()

    usage_details: dict[str, int] = {}
    for container_key in ("usage", "token_usage"):
        container = event.get(container_key)
        if isinstance(container, dict):
            usage_details = _usage_from_mapping(container)
            if usage_details:
                break

    cost_details: dict[str, float] = {}
    cost_source: CostSource = "unknown"
    for cost_key in ("total_cost_usd", "cost_usd"):
        cost = _coerce_cost(event.get(cost_key))
        if cost is not None:
            cost_details = {"total": cost}
            cost_source = "advisor"
            break

    return AdvisorMetrics(
        usage_details=usage_details,
        cost_details=cost_details,
        duration_ms=_coerce_duration_ms(event.get("duration_ms")),
        cost_source=cost_source,
        model_reported=_extract_model(event, None),
    )


def _parsed_with_metrics(metrics: AdvisorMetrics, **fields: Any) -> "ParsedResult":
    """Build a ``ParsedResult`` carrying *fields* plus the extracted *metrics*."""
    return ParsedResult(
        usage_details=metrics.usage_details,
        cost_details=metrics.cost_details,
        duration_ms=metrics.duration_ms,
        cost_source=metrics.cost_source,
        model_reported=metrics.model_reported,
        **fields,
    )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedResult:
    """Final payload returned by a parser after the advisor exits."""

    result: Optional[str] = None
    session_id: Optional[str] = None
    failure: bool = False
    error: Optional[str] = None
    usage_details: dict[str, int] = field(default_factory=dict)
    cost_details: dict[str, float] = field(default_factory=dict)
    duration_ms: Optional[int] = None
    cost_source: CostSource = "unknown"
    model_reported: Optional[str] = None


# ---------------------------------------------------------------------------
# Parser interface
# ---------------------------------------------------------------------------


class EventParser:
    """Base class for advisor output parsers.

    Parsers implement the ``runner.LineConsumer`` protocol: ``consume_stdout``,
    ``consume_stderr``, and ``finish`` are called by the runner.  They may also
    call *on_activity* when output indicates the advisor is alive.
    """

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        self._on_activity = on_activity

    def consume_stdout(self, line: str) -> None:
        raise NotImplementedError

    def consume_stderr(self, line: str) -> None:
        print(line, file=sys.stderr, end="")

    def finish(self, exit_code: int) -> ParsedResult:
        raise NotImplementedError

    def _activity(self, stream: str) -> None:
        if self._on_activity is not None:
            self._on_activity(stream)


# ---------------------------------------------------------------------------
# Text parser
# ---------------------------------------------------------------------------


class TextParser(EventParser):
    """Capture raw stdout as the answer; echo stderr as it arrives."""

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_activity)
        self._stdout_parts: list[str] = []

    def consume_stdout(self, line: str) -> None:
        self._stdout_parts.append(line)
        self._activity("stdout")

    def consume_stderr(self, line: str) -> None:
        super().consume_stderr(line)
        self._activity("stderr")

    def finish(self, exit_code: int) -> ParsedResult:
        return ParsedResult(result="".join(self._stdout_parts))


# ---------------------------------------------------------------------------
# Claude stream-json parser
# ---------------------------------------------------------------------------


class ClaudeStreamParser(EventParser):
    """Parse Claude's newline-delimited stream-json events."""

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_activity)
        self._final: dict[str, Any] | None = None
        self._model: Optional[str] = None

    def consume_stdout(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            print(stripped, file=sys.stderr)
            return
        self._summarize(event)
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str) and model:
                self._model = model
        if event.get("type") == "result":
            self._final = event
            self._activity("stdout")

    def finish(self, exit_code: int) -> ParsedResult:
        if self._final is None:
            return ParsedResult(
                failure=True,
                error="No result event received from Claude",
            )
        metrics = extract_claude_metrics(self._final, model=self._model)
        if self._final.get("is_error"):
            errors = (
                self._final.get("errors")
                or self._final.get("api_error_status")
                or "unknown error"
            )
            return _parsed_with_metrics(metrics, failure=True, error=str(errors))
        result = self._final.get("result")
        if result is not None:
            return _parsed_with_metrics(
                metrics, result=result, session_id=self._final.get("session_id")
            )
        structured = self._final.get("structured_output")
        if structured is not None:
            return _parsed_with_metrics(
                metrics,
                result=json.dumps(structured, indent=2, sort_keys=True),
                session_id=self._final.get("session_id"),
            )
        return _parsed_with_metrics(
            metrics, failure=True, error="Result event contained no answer"
        )

    def _summarize(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            print(
                f"[crossagent] init session={event.get('session_id')} model={event.get('model')} "
                f"cwd={event.get('cwd')}",
                file=sys.stderr,
            )
        elif kind == "assistant":
            message = event.get("message", {})
            blocks = message.get("content", []) if isinstance(message, dict) else []
            text = "".join(
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                print(
                    f"[crossagent] assistant: {text.replace(chr(10), ' ')[:240]}",
                    file=sys.stderr,
                )
        elif kind == "result":
            print(
                f"[crossagent] result subtype={event.get('subtype')} session={event.get('session_id')} "
                f"cost={event.get('total_cost_usd')}",
                file=sys.stderr,
            )
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            print(
                f"[crossagent] rate_limit status={info.get('status')} resetsAt={info.get('resetsAt')}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Codex JSONL parser
# ---------------------------------------------------------------------------


class CodexJsonlParser(EventParser):
    """Parse Codex ``exec --json`` JSONL events and extract the final answer.

    Summaries are emitted to stderr but never include prompt text.  Malformed or
    unknown lines are preserved in stderr diagnostics and ignored safely.
    """

    def __init__(self, on_activity: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_activity)
        self._thread_id: Optional[str] = None
        self._last_agent_message: Optional[str] = None
        self._failure_error: Optional[str] = None
        self._turn_completed: Optional[dict[str, Any]] = None

    def consume_stdout(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            print(
                f"[crossagent] codex malformed line: {line.rstrip()[:240]}",
                file=sys.stderr,
            )
            return

        event_type = event.get("type")
        self._summarize(event)

        if event_type == "thread.started":
            self._thread_id = event.get("thread_id") or event.get("id")
        elif event_type == "turn.completed":
            self._turn_completed = event
        elif event_type == "turn.started":
            self._activity("stdout")
        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                self._last_agent_message = self._extract_message_text(item)
            self._activity("stdout")
        elif event_type == "item.started":
            self._activity("stdout")
        elif event_type in ("turn.failed", "error"):
            self._failure_error = (
                event.get("error") or event.get("message") or event_type
            )
            self._activity("stdout")

    def finish(self, exit_code: int) -> ParsedResult:
        metrics = extract_codex_metrics(self._turn_completed)
        if self._failure_error is not None:
            return _parsed_with_metrics(
                metrics,
                failure=True,
                error=self._failure_error,
                session_id=self._thread_id,
            )
        if exit_code != 0:
            return _parsed_with_metrics(
                metrics,
                failure=True,
                error=f"Codex exited with code {exit_code}",
                session_id=self._thread_id,
            )
        return _parsed_with_metrics(
            metrics,
            result=self._last_agent_message,
            session_id=self._thread_id,
        )

    def _summarize(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or event.get("id")
            print(f"[crossagent] codex thread={thread_id}", file=sys.stderr)
        elif event_type == "turn.started":
            print("[crossagent] codex turn started", file=sys.stderr)
        elif event_type == "turn.completed":
            print("[crossagent] codex turn completed", file=sys.stderr)
        elif event_type == "turn.failed":
            print("[crossagent] codex turn failed", file=sys.stderr)
        elif event_type == "error":
            msg = event.get("message") or event.get("error") or "unknown error"
            print(f"[crossagent] codex error: {msg[:200]}", file=sys.stderr)
        elif event_type in ("item.completed", "item.started"):
            item_type = event.get("item", {}).get("type", "unknown")
            print(f"[crossagent] codex {event_type} type={item_type}", file=sys.stderr)

    @staticmethod
    def _extract_message_text(item: dict[str, Any]) -> Optional[str]:
        # `codex exec --json` emits the answer as item.text; older/alternate
        # shapes carry item.content as a string or a list of text blocks.
        text = item.get("text")
        if isinstance(text, str):
            return text
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts) if parts else None
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

PARSER_NAMES = {"text", "claude-stream", "codex-jsonl"}


def get_parser(
    name: str,
    *,
    on_activity: Optional[Callable[[str], None]] = None,
) -> EventParser:
    """Return a parser instance by *name*."""
    if name == "text":
        return TextParser(on_activity=on_activity)
    if name == "claude-stream":
        return ClaudeStreamParser(on_activity=on_activity)
    if name == "codex-jsonl":
        return CodexJsonlParser(on_activity=on_activity)
    raise ValueError(
        f"Unknown parser '{name}'. Known parsers: {', '.join(sorted(PARSER_NAMES))}"
    )
