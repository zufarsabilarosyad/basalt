"""Worker Pool & Executor Factory Subsystem Module for Strata Engine.

Provides an ExecutorFactory for dynamic executor plugin lookup, a WorkerPool
managing concurrent step execution using asyncio.Semaphore and asyncio.gather,
and pool execution metrics tracking.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from strata.core.dag.ast import ExecutorType, StepSpec
from strata.core.engine.context import ExecutionContext
from strata.core.engine.state_machine import StepState
from strata.core.executors.base import BaseExecutor, ExecutorError, ExecutorTimeoutError
from strata.core.executors.http import HTTPExecutor
from strata.core.executors.inline import PythonInlineExecutor
from strata.core.executors.subprocess import SubprocessExecutor
from strata.utils.logger import get_logger

logger = get_logger(__name__)


class WorkerPoolMetrics(BaseModel):
    """Real-time metrics snapshot for WorkerPool execution state."""

    max_concurrency: int
    active_workers: int = 0
    available_slots: int = 0
    total_steps_executed: int = 0
    total_steps_succeeded: int = 0
    total_steps_failed: int = 0
    total_steps_timed_out: int = 0


class ExecutorFactory:
    """Factory registry mapping executor types to BaseExecutor instances."""

    def __init__(self) -> None:
        self._executors: dict[str, BaseExecutor] = {}
        # Register standard default executors
        self.register("subprocess", SubprocessExecutor())
        self.register("http", HTTPExecutor())
        self.register("python_inline", PythonInlineExecutor())

    def register(self, executor_type: str, executor: BaseExecutor) -> None:
        """Register an executor plugin instance.

        Args:
            executor_type: String identifier key (e.g. 'subprocess', 'http').
            executor: Instance of BaseExecutor subclass.
        """
        key = str(executor_type).lower().strip()
        self._executors[key] = executor
        logger.debug(f"Registered executor plugin '{key}': {executor.__class__.__name__}")

    def unregister(self, executor_type: str) -> None:
        """Unregister an executor plugin by type string."""
        key = str(executor_type).lower().strip()
        if key in self._executors:
            del self._executors[key]
            logger.debug(f"Unregistered executor plugin '{key}'")

    def has_executor(self, executor_type: str | ExecutorType) -> bool:
        """Check if executor type is registered in factory."""
        key = (
            str(executor_type.value if isinstance(executor_type, ExecutorType) else executor_type)
            .lower()
            .strip()
        )
        return key in self._executors

    def get_executor(self, executor_type: str | ExecutorType) -> BaseExecutor:
        """Retrieve executor instance by type.

        Args:
            executor_type: Enum value or string name.

        Returns:
            Registered BaseExecutor instance.

        Raises:
            ExecutorError: If executor type is not registered.
        """
        key = (
            str(executor_type.value if isinstance(executor_type, ExecutorType) else executor_type)
            .lower()
            .strip()
        )
        if key not in self._executors:
            raise ExecutorError(
                step_id="<factory>",
                executor_type=key,
                message=f"Unknown executor type '{key}'. Available types: {sorted(list(self._executors.keys()))}.",
            )
        return self._executors[key]


class WorkerPool:
    """Async worker pool managing concurrent step execution limits and metrics."""

    def __init__(
        self,
        max_concurrency: int = 10,
        factory: ExecutorFactory | None = None,
    ) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self.factory = factory or ExecutorFactory()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._thread_pool = ThreadPoolExecutor(
            max_workers=min(32, self.max_concurrency * 2),
            thread_name_prefix="strata_worker",
        )

        # Counter metrics
        self._total_executed = 0
        self._total_succeeded = 0
        self._total_failed = 0
        self._total_timed_out = 0

    def get_metrics(self) -> WorkerPoolMetrics:
        """Generate current WorkerPoolMetrics snapshot."""
        available = self._semaphore._value  # Available semaphore permits
        active = self.max_concurrency - available
        return WorkerPoolMetrics(
            max_concurrency=self.max_concurrency,
            active_workers=max(0, active),
            available_slots=max(0, available),
            total_steps_executed=self._total_executed,
            total_steps_succeeded=self._total_succeeded,
            total_steps_failed=self._total_failed,
            total_steps_timed_out=self._total_timed_out,
        )

    async def execute_step(
        self,
        step: StepSpec,
        context: ExecutionContext,
    ) -> tuple[StepState, dict[str, Any], str | None]:
        """Execute a single step bounded by worker pool concurrency limits.

        Args:
            step: StepSpec AST model.
            context: Active ExecutionContext.

        Returns:
            Tuple of (StepState, output_payload_dict, error_message_or_none).
        """
        async with self._semaphore:
            self._total_executed += 1
            logger.debug(
                f"WorkerPool executing step '{step.id}' (executor: {step.executor_type.value})"
            )
            try:
                executor = self.factory.get_executor(step.executor_type)
                context.set_step_state(step.id, StepState.RUNNING)

                output = await executor.execute(step, context)
                context.set_step_state(step.id, StepState.COMPLETED)
                context.set_step_output(step.id, output)

                self._total_succeeded += 1
                return StepState.COMPLETED, output, None

            except ExecutorTimeoutError as exc:
                logger.error(f"Step '{step.id}' execution timed out: {exc}")
                context.set_step_state(step.id, StepState.TIMEOUT)
                self._total_timed_out += 1
                return StepState.TIMEOUT, {}, str(exc)

            except ExecutorError as exc:
                logger.error(f"Step '{step.id}' execution failed: {exc}")
                context.set_step_state(step.id, StepState.FAILED)
                self._total_failed += 1
                return StepState.FAILED, {}, str(exc)

            except Exception as exc:
                logger.error(f"Unexpected error executing step '{step.id}': {exc}", exc_info=True)
                context.set_step_state(step.id, StepState.FAILED)
                self._total_failed += 1
                return StepState.FAILED, {}, f"Unexpected execution failure: {exc}"

    async def execute_steps_parallel(
        self,
        steps: list[StepSpec],
        context: ExecutionContext,
    ) -> dict[str, tuple[StepState, dict[str, Any], str | None]]:
        """Execute multiple independent steps concurrently in parallel.

        Args:
            steps: List of StepSpec AST models to execute.
            context: Active ExecutionContext.

        Returns:
            Dictionary mapping step_id to (StepState, output_dict, error_msg).
        """
        if not steps:
            return {}

        logger.debug(f"WorkerPool launching {len(steps)} parallel step tasks")
        tasks = [self.execute_step(step, context) for step in steps]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        output_map: dict[str, tuple[StepState, dict[str, Any], str | None]] = {}
        for step, res in zip(steps, results):
            output_map[step.id] = res

        return output_map

    def shutdown(self) -> None:
        """Shutdown thread pool workers gracefully."""
        self._thread_pool.shutdown(wait=False)


@lru_cache(maxsize=1)
def get_global_executor_factory() -> ExecutorFactory:
    """Retrieve global LRU-cached ExecutorFactory instance."""
    return ExecutorFactory()
