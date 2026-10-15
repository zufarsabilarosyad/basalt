"""Convenience base class for plugins that contribute a step executor."""

from __future__ import annotations

import abc

from basalt.core.executors.base import BaseExecutor
from basalt.core.executors.pool import ExecutorFactory
from basalt.plugins.base import BasaltPlugin, PluginConfigurationError, PluginContext
from basalt.plugins.models import PluginCapability


class ExecutorPlugin(BasaltPlugin):
    """Plugin base that registers one executor for the duration of its lifecycle.

    Subclasses set ``executor_name`` and implement :meth:`build_executor`.
    Their manifest must declare the ``executor`` capability; the defensive
    check prevents a package from gaining an execution surface accidentally.
    """

    executor_name: str

    @abc.abstractmethod
    def build_executor(self, context: PluginContext) -> BaseExecutor:
        """Create the executor instance to add to the engine factory."""

    async def start(self, context: PluginContext) -> None:
        if PluginCapability.EXECUTOR not in self.manifest.capabilities:
            raise PluginConfigurationError(
                f"Executor plugin '{self.manifest.name}' must declare the executor capability."
            )
        name = getattr(self, "executor_name", "").strip().lower()
        if not name:
            raise PluginConfigurationError("Executor plugin must set a non-empty executor_name")
        factory = context.get_service("basalt.executor_factory", ExecutorFactory)
        if factory.has_executor(name):
            raise PluginConfigurationError(f"Executor '{name}' is already registered")
        factory.register(name, self.build_executor(context))
        context.add_cleanup(lambda: factory.unregister(name))
