"""Bounded in-memory dead-letter queue for workflow failures.

The queue is an intentionally small local primitive.  Repository adapters can
periodically persist its entries, while applications can inspect, retry, or
acknowledge failures without coupling the workflow runner to a broker.
"""

from __future__ import annotations

import asyncio
import copy
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """An immutable record for a workflow item that could not be processed."""

    id: str
    run_id: str
    dag_id: str
    step_id: str | None
    payload: Any
    error_type: str
    error_message: str
    failed_at: datetime
    attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_exception(
        cls,
        *,
        run_id: str,
        dag_id: str,
        error: BaseException,
        step_id: str | None = None,
        payload: Any = None,
        attempts: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> "DeadLetter":
        """Create an entry while retaining enough context to retry it later."""
        return cls(
            id=uuid4().hex,
            run_id=run_id,
            dag_id=dag_id,
            step_id=step_id,
            payload=copy.deepcopy(payload),
            error_type=type(error).__name__,
            error_message=str(error),
            failed_at=_utcnow(),
            attempts=attempts,
            metadata=copy.deepcopy(metadata or {}),
        )


@dataclass(frozen=True, slots=True)
class DeadLetterPage:
    """A stable page returned from :meth:`DeadLetterQueue.list`."""

    entries: tuple[DeadLetter, ...]
    total: int
    offset: int
    limit: int


RetryCallback = Callable[[DeadLetter], Awaitable[None] | None]


class DeadLetterQueue:
    """Concurrent bounded FIFO queue with acknowledgement and retry support."""

    def __init__(self, max_entries: int = 1000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._entries: deque[DeadLetter] = deque()
        self._lock = asyncio.Lock()
        self._dropped = 0

    @property
    def max_entries(self) -> int:
        """Maximum retained entries before oldest entries are evicted."""
        return self._max_entries

    async def put(self, entry: DeadLetter) -> None:
        """Append a dead letter, evicting the oldest entry when at capacity."""
        async with self._lock:
            if len(self._entries) >= self._max_entries:
                self._entries.popleft()
                self._dropped += 1
            self._entries.append(entry)

    async def capture(
        self,
        *,
        run_id: str,
        dag_id: str,
        error: BaseException,
        step_id: str | None = None,
        payload: Any = None,
        attempts: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> DeadLetter:
        """Build and enqueue a :class:`DeadLetter` from an exception."""
        entry = DeadLetter.from_exception(
            run_id=run_id,
            dag_id=dag_id,
            error=error,
            step_id=step_id,
            payload=payload,
            attempts=attempts,
            metadata=metadata,
        )
        await self.put(entry)
        return entry

    async def get(self, entry_id: str) -> DeadLetter | None:
        """Find an entry by ID without removing it."""
        async with self._lock:
            return next((entry for entry in self._entries if entry.id == entry_id), None)

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        dag_id: str | None = None,
        run_id: str | None = None,
    ) -> DeadLetterPage:
        """List entries newest first with optional exact DAG/run filtering."""
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        async with self._lock:
            entries = reversed(self._entries)
            filtered = tuple(
                entry
                for entry in entries
                if (dag_id is None or entry.dag_id == dag_id)
                and (run_id is None or entry.run_id == run_id)
            )
        return DeadLetterPage(
            entries=filtered[offset : offset + limit],
            total=len(filtered),
            offset=offset,
            limit=limit,
        )

    async def acknowledge(self, entry_id: str) -> bool:
        """Delete one entry.  Returns ``False`` when it was already absent."""
        async with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.id == entry_id:
                    del self._entries[index]
                    return True
        return False

    async def retry(self, entry_id: str, callback: RetryCallback) -> bool:
        """Retry one entry and acknowledge it only if the callback succeeds."""
        entry = await self.get(entry_id)
        if entry is None:
            return False
        result = callback(entry)
        if hasattr(result, "__await__"):
            await result
        return await self.acknowledge(entry_id)

    async def clear(self) -> int:
        """Remove all retained entries and return how many were removed."""
        async with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    async def stats(self) -> dict[str, int]:
        """Return cheap operational counters suitable for a health endpoint."""
        async with self._lock:
            return {"retained": len(self._entries), "dropped": self._dropped, "capacity": self._max_entries}
