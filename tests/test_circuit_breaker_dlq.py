"""Contracts for circuit breaking and in-process dead-letter retention."""

import asyncio

import pytest

from basalt.core.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
)
from basalt.core.resilience.dlq import DeadLetterQueue


@pytest.mark.asyncio
async def test_circuit_opens_after_configured_failures() -> None:
    breaker = CircuitBreaker("partner", CircuitBreakerConfig(failure_threshold=2))
    await breaker.record_failure(RuntimeError("one"))
    assert (await breaker.snapshot()).state is CircuitState.CLOSED
    await breaker.record_failure(RuntimeError("two"))
    assert (await breaker.snapshot()).state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.allow_call()


@pytest.mark.asyncio
async def test_circuit_call_records_success_and_failure() -> None:
    breaker = CircuitBreaker("dependency", CircuitBreakerConfig(failure_threshold=1))
    assert await breaker.call(lambda: "ok") == "ok"
    with pytest.raises(ValueError, match="bad"):
        await breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert (await breaker.snapshot()).state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_excluded_exception_does_not_open() -> None:
    breaker = CircuitBreaker(
        "validation", CircuitBreakerConfig(failure_threshold=1, excluded_exceptions=(ValueError,))
    )
    await breaker.record_failure(ValueError("input"))
    assert (await breaker.snapshot()).state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_recovers_through_half_open_probe() -> None:
    breaker = CircuitBreaker(
        "fast", CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=0)
    )
    await breaker.record_failure(RuntimeError())
    await breaker.allow_call()
    assert (await breaker.snapshot()).state is CircuitState.HALF_OPEN
    await breaker.record_success()
    assert (await breaker.snapshot()).state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_dlq_captures_and_filters_failures() -> None:
    queue = DeadLetterQueue()
    first = await queue.capture(run_id="r1", dag_id="first", error=ValueError("bad"), payload={"x": 1})
    await queue.capture(run_id="r2", dag_id="second", error=RuntimeError("worse"))
    page = await queue.list(dag_id="first")
    assert page.total == 1
    assert page.entries == (first,)
    assert first.payload == {"x": 1}


@pytest.mark.asyncio
async def test_dlq_evicts_oldest_entry_at_capacity() -> None:
    queue = DeadLetterQueue(max_entries=1)
    first = await queue.capture(run_id="one", dag_id="dag", error=RuntimeError())
    second = await queue.capture(run_id="two", dag_id="dag", error=RuntimeError())
    assert await queue.get(first.id) is None
    assert (await queue.list()).entries == (second,)
    assert await queue.stats() == {"retained": 1, "dropped": 1, "capacity": 1}


@pytest.mark.asyncio
async def test_dlq_retry_acknowledges_only_a_successful_callback() -> None:
    queue = DeadLetterQueue()
    entry = await queue.capture(run_id="run", dag_id="dag", error=RuntimeError())
    called: list[str] = []

    async def retry(letter: object) -> None:
        await asyncio.sleep(0)
        called.append(getattr(letter, "id"))

    assert await queue.retry(entry.id, retry)
    assert called == [entry.id]
    assert await queue.get(entry.id) is None


@pytest.mark.asyncio
async def test_dlq_failed_retry_leaves_entry_available() -> None:
    queue = DeadLetterQueue()
    entry = await queue.capture(run_id="run", dag_id="dag", error=RuntimeError())

    def retry(_: object) -> None:
        raise RuntimeError("still unavailable")

    with pytest.raises(RuntimeError, match="still unavailable"):
        await queue.retry(entry.id, retry)
    assert await queue.get(entry.id) == entry
