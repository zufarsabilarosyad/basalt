"""In-memory distributed-tracing compatible identifiers and span recorder."""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Iterator, Mapping


def _identifier(size: int) -> str:
    return secrets.token_hex(size)


@dataclass(slots=True)
class Span:
    """A timed operation within a trace."""

    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: _identifier(8))
    parent_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, Any] = field(default_factory=dict)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    error: str | None = None
    _start_tick: float = field(default_factory=time.perf_counter, repr=False)

    @property
    def finished(self) -> bool:
        return self.ended_at is not None

    def finish(self, error: BaseException | str | None = None) -> None:
        """Finish the span once; subsequent calls preserve its original result."""
        if self.finished:
            return
        self.ended_at = datetime.now(UTC)
        self.duration_ms = (time.perf_counter() - self._start_tick) * 1000
        if error is not None:
            self.error = str(error)

    def set_attribute(self, key: str, value: Any) -> None:
        """Attach queryable operation context to the span."""
        if self.finished:
            raise RuntimeError("cannot modify a completed span")
        self.attributes[key] = value

    def to_dict(self) -> dict[str, Any]:
        """Produce a portable trace event representation."""
        return {"name": self.name, "trace_id": self.trace_id, "span_id": self.span_id, "parent_id": self.parent_id, "started_at": self.started_at.isoformat(), "ended_at": self.ended_at.isoformat() if self.ended_at else None, "duration_ms": self.duration_ms, "attributes": self.attributes.copy(), "error": self.error}


@dataclass(slots=True)
class Trace:
    """A collection of spans recorded for one causally related operation."""

    trace_id: str = field(default_factory=lambda: _identifier(16))
    spans: list[Span] = field(default_factory=list)

    def root_spans(self) -> list[Span]:
        """Return spans without a local parent."""
        return [span for span in self.spans if span.parent_id is None]

    def to_dict(self) -> dict[str, Any]:
        """Export all spans in their creation order."""
        return {"trace_id": self.trace_id, "spans": [span.to_dict() for span in self.spans]}


class TraceRecorder:
    """Thread-safe bounded trace recorder for embedded Basalt deployments."""

    def __init__(self, max_traces: int = 1_000) -> None:
        if max_traces < 1:
            raise ValueError("max_traces must be positive")
        self.max_traces = max_traces
        self._traces: dict[str, Trace] = {}
        self._order: list[str] = []
        self._lock = RLock()

    def create_trace(self, trace_id: str | None = None) -> Trace:
        """Create or return a trace with the requested correlation identifier."""
        active_id = trace_id or _identifier(16)
        with self._lock:
            existing = self._traces.get(active_id)
            if existing:
                return existing
            trace = Trace(active_id)
            self._traces[active_id] = trace
            self._order.append(active_id)
            while len(self._order) > self.max_traces:
                expired = self._order.pop(0)
                self._traces.pop(expired, None)
            return trace

    def start_span(self, name: str, trace_id: str | None = None, parent_id: str | None = None, attributes: Mapping[str, Any] | None = None) -> Span:
        """Start and retain a span under a trace."""
        trace = self.create_trace(trace_id)
        span = Span(name=name, trace_id=trace.trace_id, parent_id=parent_id, attributes=dict(attributes or {}))
        with self._lock:
            trace.spans.append(span)
        return span

    @contextmanager
    def span(self, name: str, trace_id: str | None = None, parent_id: str | None = None, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
        """Record successful and failed blocks with a single context manager."""
        span = self.start_span(name, trace_id, parent_id, attributes)
        try:
            yield span
        except BaseException as error:
            span.finish(error)
            raise
        else:
            span.finish()

    def get_trace(self, trace_id: str) -> Trace | None:
        """Return a trace by identity."""
        with self._lock:
            return self._traces.get(trace_id)

    def recent(self, limit: int = 100) -> list[Trace]:
        """Return newest traces first."""
        if limit < 0:
            raise ValueError("limit cannot be negative")
        with self._lock:
            return [self._traces[trace_id] for trace_id in reversed(self._order[-limit:]) if trace_id in self._traces]

    def clear(self) -> None:
        """Discard local trace history."""
        with self._lock:
            self._traces.clear()
            self._order.clear()
