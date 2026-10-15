"""Async-safe circuit breaker primitives for workflow dependencies.

The breaker is deliberately independent from an executor.  A single instance
can protect an HTTP endpoint, an inline integration, or a custom executor and
is safe to share between concurrent workflow runs.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar


T = TypeVar("T")


class CircuitState(str, Enum):
    """States defined by the circuit-breaker state machine."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected while a circuit is open."""

    def __init__(self, name: str, retry_after_seconds: float) -> None:
        self.name = name
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            f"Circuit '{name}' is open; retry in {self.retry_after_seconds:.3f} seconds."
        )


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Immutable tuning values for a :class:`CircuitBreaker`."""

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_calls: int = 1
    success_threshold: int = 1
    excluded_exceptions: tuple[type[BaseException], ...] = ()

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_timeout_seconds < 0:
            raise ValueError("recovery_timeout_seconds cannot be negative")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be at least 1")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be at least 1")


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    """Read-only representation used by health endpoints and metrics."""

    name: str
    state: CircuitState
    failure_count: int
    success_count: int
    half_open_calls: int
    opened_at: float | None
    retry_after_seconds: float


@dataclass(slots=True)
class CircuitBreaker:
    """Protect calls using closed, open, and half-open transitions.

    A failed protected call counts towards ``failure_threshold`` unless its
    exception type is explicitly excluded.  Once opened, calls are rejected
    until ``recovery_timeout_seconds`` elapses.  The next permitted calls are
    probes; enough successful probes closes the circuit, while one failed
    probe reopens it.
    """

    name: str
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    _state: CircuitState = field(init=False, default=CircuitState.CLOSED)
    _failure_count: int = field(init=False, default=0)
    _success_count: int = field(init=False, default=0)
    _half_open_calls: int = field(init=False, default=0)
    _opened_at: float | None = field(init=False, default=None)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    @property
    def state(self) -> CircuitState:
        """Return the last known state without causing a transition."""
        return self._state

    async def allow_call(self) -> None:
        """Acquire permission for one protected call or raise ``CircuitOpenError``."""
        async with self._lock:
            now = time.monotonic()
            self._advance_clock(now)
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError(self.name, self._retry_after(now))
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitOpenError(self.name, self._retry_after(now))
                self._half_open_calls += 1

    async def record_success(self) -> None:
        """Record a successful protected call and close a healthy circuit."""
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._close()
                return
            if self._state is CircuitState.CLOSED:
                self._failure_count = 0

    async def record_failure(self, error: BaseException | None = None) -> None:
        """Record a failure, respecting excluded exception classes."""
        if error is not None and isinstance(error, self.config.excluded_exceptions):
            await self.record_success()
            return
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._open(time.monotonic())
                return
            if self._state is CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._open(time.monotonic())

    async def call(self, operation: Callable[..., T | Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Run ``operation`` under circuit protection.

        Both normal callables and awaitable-returning callables are accepted.
        Exceptions are re-raised unchanged after the circuit state is updated.
        """
        await self.allow_call()
        try:
            result = operation(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            await self.record_failure(exc)
            raise
        await self.record_success()
        return result  # type: ignore[return-value]

    async def snapshot(self) -> CircuitSnapshot:
        """Return a coherent state snapshot, advancing timed transitions first."""
        async with self._lock:
            now = time.monotonic()
            self._advance_clock(now)
            return CircuitSnapshot(
                name=self.name,
                state=self._state,
                failure_count=self._failure_count,
                success_count=self._success_count,
                half_open_calls=self._half_open_calls,
                opened_at=self._opened_at,
                retry_after_seconds=self._retry_after(now),
            )

    async def reset(self) -> None:
        """Manually close the circuit and discard its failure history."""
        async with self._lock:
            self._close()

    def _advance_clock(self, now: float) -> None:
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return
        if now - self._opened_at >= self.config.recovery_timeout_seconds:
            self._state = CircuitState.HALF_OPEN
            self._success_count = 0
            self._half_open_calls = 0

    def _retry_after(self, now: float) -> float:
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return 0.0
        return max(0.0, self.config.recovery_timeout_seconds - (now - self._opened_at))

    def _open(self, now: float) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = now
        self._success_count = 0
        self._half_open_calls = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._opened_at = None
