"""Structured, serializable events emitted by workflow operations.

Events are deliberately immutable at their boundary.  This makes them safe to
retain, forward to queues, or expose from an API without a listener mutating data
seen by another listener.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Mapping


class EventLevel(str, Enum):
    """Severity assigned to an operational event."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def utc_now() -> datetime:
    """Return an aware UTC timestamp from one well-known helper."""
    return datetime.now(UTC)


def _safe_value(value: Any) -> Any:
    """Convert an arbitrary value to data that can be encoded as JSON.

    Operational telemetry must never cause a workflow to fail just because an
    executor placed a custom Python object in its output.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


@dataclass(frozen=True, slots=True)
class RunEvent:
    """A lifecycle event associated with exactly one workflow run."""

    name: str
    run_id: str
    dag_id: str | None = None
    level: EventLevel = EventLevel.INFO
    timestamp: datetime = field(default_factory=utc_now)
    message: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Produce a transport-safe dictionary representation."""
        return {
            "name": self.name,
            "run_id": self.run_id,
            "dag_id": self.dag_id,
            "level": self.level.value,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "attributes": {key: _safe_value(value) for key, value in self.attributes.items()},
            "trace_id": self.trace_id,
            "span_id": self.span_id,
        }

    def to_json(self) -> str:
        """Serialize the event using compact, stable JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunEvent":
        """Build an event received from a JSON-compatible payload."""
        raw_timestamp = value.get("timestamp")
        timestamp = (
            datetime.fromisoformat(raw_timestamp)
            if isinstance(raw_timestamp, str)
            else utc_now()
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return cls(
            name=str(value["name"]),
            run_id=str(value["run_id"]),
            dag_id=value.get("dag_id"),
            level=EventLevel(value.get("level", EventLevel.INFO.value)),
            timestamp=timestamp,
            message=value.get("message"),
            attributes=dict(value.get("attributes") or {}),
            trace_id=value.get("trace_id"),
            span_id=value.get("span_id"),
        )


@dataclass(frozen=True, slots=True)
class StepEvent(RunEvent):
    """A run event augmented with step execution identity and attempt number."""

    step_id: str = ""
    attempt: int = 1
    state: str | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Produce a JSON-safe step event payload."""
        value = super().to_dict()
        value.update(
            {
                "step_id": self.step_id,
                "attempt": self.attempt,
                "state": self.state,
                "duration_ms": self.duration_ms,
            }
        )
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StepEvent":
        """Restore a step event from an external representation."""
        base = RunEvent.from_dict(value)
        return cls(
            name=base.name,
            run_id=base.run_id,
            dag_id=base.dag_id,
            level=base.level,
            timestamp=base.timestamp,
            message=base.message,
            attributes=base.attributes,
            trace_id=base.trace_id,
            span_id=base.span_id,
            step_id=str(value.get("step_id", "")),
            attempt=int(value.get("attempt", 1)),
            state=value.get("state"),
            duration_ms=value.get("duration_ms"),
        )
