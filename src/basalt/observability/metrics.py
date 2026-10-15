"""A dependency-free metrics registry with counters, gauges, and timers."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import RLock
from typing import Iterable, Mapping


LabelValues = tuple[tuple[str, str], ...]


def normalize_labels(labels: Mapping[str, object] | None = None) -> LabelValues:
    """Normalize labels into an immutable key suitable for dictionary lookups."""
    return tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One materialized numeric metric and its dimensions."""

    name: str
    value: float
    kind: str
    labels: LabelValues = ()
    count: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    total: float | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a simple form suitable for JSON response bodies."""
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind,
            "labels": dict(self.labels),
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "total": self.total,
        }


@dataclass(slots=True)
class _Summary:
    count: int = 0
    total: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


@dataclass(slots=True)
class Timer:
    """A context manager that records elapsed seconds into one histogram."""

    registry: "MetricsRegistry"
    name: str
    labels: Mapping[str, object] | None = None
    _started: float = field(init=False, default=0.0)

    def __enter__(self) -> "Timer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.registry.observe(self.name, time.perf_counter() - self._started, self.labels)


class MetricsRegistry:
    """Thread-safe local aggregation for operational measurements.

    Metric names are caller owned.  The registry retains values in memory to stay
    embeddable, and an application may periodically call :meth:`snapshot` to
    export them.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, LabelValues], float] = defaultdict(float)
        self._gauges: dict[tuple[str, LabelValues], float] = {}
        self._summaries: dict[tuple[str, LabelValues], _Summary] = defaultdict(_Summary)
        self._lock = RLock()

    def increment(self, name: str, value: float = 1.0, labels: Mapping[str, object] | None = None) -> None:
        """Increase a monotonically accumulated counter."""
        if value < 0:
            raise ValueError("counter increments must be non-negative")
        with self._lock:
            self._counters[(name, normalize_labels(labels))] += value

    def set_gauge(self, name: str, value: float, labels: Mapping[str, object] | None = None) -> None:
        """Set a measurement whose value may move in either direction."""
        with self._lock:
            self._gauges[(name, normalize_labels(labels))] = float(value)

    def adjust_gauge(self, name: str, delta: float, labels: Mapping[str, object] | None = None) -> float:
        """Adjust and return a gauge atomically."""
        key = (name, normalize_labels(labels))
        with self._lock:
            value = self._gauges.get(key, 0.0) + float(delta)
            self._gauges[key] = value
            return value

    def observe(self, name: str, value: float, labels: Mapping[str, object] | None = None) -> None:
        """Add a sample to a summary distribution."""
        if not math.isfinite(value):
            raise ValueError("metric observations must be finite")
        with self._lock:
            self._summaries[(name, normalize_labels(labels))].observe(float(value))

    def timer(self, name: str, labels: Mapping[str, object] | None = None) -> Timer:
        """Create a context manager that observes elapsed time in seconds."""
        return Timer(self, name, labels)

    def get_counter(self, name: str, labels: Mapping[str, object] | None = None) -> float:
        """Read a counter without materializing a missing measurement."""
        with self._lock:
            return self._counters.get((name, normalize_labels(labels)), 0.0)

    def get_gauge(self, name: str, labels: Mapping[str, object] | None = None) -> float | None:
        """Read a gauge or return ``None`` when it has never been set."""
        with self._lock:
            return self._gauges.get((name, normalize_labels(labels)))

    def snapshot(self) -> list[MetricPoint]:
        """Return a consistent materialized view sorted for stable exports."""
        with self._lock:
            points: list[MetricPoint] = []
            points.extend(MetricPoint(name, value, "counter", labels) for (name, labels), value in self._counters.items())
            points.extend(MetricPoint(name, value, "gauge", labels) for (name, labels), value in self._gauges.items())
            points.extend(MetricPoint(name, summary.average, "summary", labels, summary.count, summary.minimum, summary.maximum, summary.total) for (name, labels), summary in self._summaries.items())
        return sorted(points, key=lambda point: (point.name, point.kind, point.labels))

    def clear(self) -> None:
        """Remove all currently accumulated values."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._summaries.clear()

    def merge(self, points: Iterable[MetricPoint]) -> None:
        """Merge a snapshot from another registry.

        Summaries retain aggregate accuracy where the input includes total and
        count; point estimates are used for externally generated summaries.
        """
        for point in points:
            labels = dict(point.labels)
            if point.kind == "counter":
                self.increment(point.name, point.value, labels)
            elif point.kind == "gauge":
                self.set_gauge(point.name, point.value, labels)
            elif point.kind == "summary":
                count = point.count or 1
                total = point.total if point.total is not None else point.value * count
                with self._lock:
                    target = self._summaries[(point.name, normalize_labels(labels))]
                    target.count += count
                    target.total += total
                    target.minimum = min(target.minimum, point.minimum if point.minimum is not None else point.value)
                    target.maximum = max(target.maximum, point.maximum if point.maximum is not None else point.value)
