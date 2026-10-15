"""Regression tests for the public Basalt plugin SDK and registry lifecycle."""

from __future__ import annotations

import pytest

from basalt.plugins import (
    BasaltPlugin,
    PluginCapability,
    PluginContext,
    PluginDependency,
    PluginDependencyError,
    PluginLifecycleError,
    PluginManifest,
    PluginRegistry,
    PluginState,
)
from basalt.plugins.executors import ExecutorPlugin
from basalt.core.executors.base import BaseExecutor
from basalt.core.executors.pool import ExecutorFactory


class SamplePlugin(BasaltPlugin):
    """Small in-memory plugin used to assert lifecycle contracts."""

    def __init__(
        self,
        name: str,
        *,
        dependencies: tuple[PluginDependency, ...] = (),
        fail_start: bool = False,
        service_name: str | None = None,
    ) -> None:
        self.manifest = PluginManifest(
            name=name,
            version="1.2.0",
            capabilities=frozenset({PluginCapability.OBSERVER}),
            dependencies=dependencies,
        )
        self.fail_start = fail_start
        self.service_name = service_name or f"example.{name}"
        self.events: list[str] = []

    async def start(self, context: PluginContext) -> None:
        self.events.append("start")
        context.register_service(self.service_name, self.manifest.name)
        context.add_cleanup(lambda: self.events.append("cleanup"))
        if self.fail_start:
            raise RuntimeError("planned failure")

    async def stop(self, context: PluginContext) -> None:
        self.events.append("stop")

    async def health(self, context: PluginContext) -> tuple[bool, str | None]:
        return True, "ready"


class DemoExecutor(BaseExecutor):
    def __init__(self) -> None:
        super().__init__("demo")

    async def execute(self, step: object, context: object) -> dict[str, str]:
        return {"status": "done"}


class DemoExecutorPlugin(ExecutorPlugin):
    executor_name = "demo"
    manifest = PluginManifest(
        name="demo.executor",
        version="1.0.0",
        capabilities=frozenset({PluginCapability.EXECUTOR}),
    )

    def build_executor(self, context: PluginContext) -> BaseExecutor:
        return DemoExecutor()


@pytest.mark.asyncio
async def test_registry_starts_dependencies_before_dependents() -> None:
    dependency = SamplePlugin("dependency")
    app = SamplePlugin("app", dependencies=(PluginDependency("dependency", "1.2"),))
    registry = PluginRegistry()
    registry.register_many((app, dependency))

    await registry.start_all()

    assert dependency.events == ["start"]
    assert app.events == ["start"]
    assert registry.service("example.app") == "app"
    health = await registry.health()
    assert [item.name for item in health] == ["app", "dependency"]
    assert all(item.state is PluginState.RUNNING for item in health)


@pytest.mark.asyncio
async def test_registry_rolls_back_started_plugins_after_failure() -> None:
    good = SamplePlugin("good")
    bad = SamplePlugin("bad", dependencies=(PluginDependency("good"),), fail_start=True)
    registry = PluginRegistry()
    registry.register_many((good, bad))

    with pytest.raises(PluginLifecycleError, match="failed to start"):
        await registry.start_all()

    assert good.events == ["start", "stop", "cleanup"]
    assert registry.services == {}
    assert (await registry.health())[1].state is PluginState.FAILED


@pytest.mark.asyncio
async def test_running_dependents_prevent_dependency_stop() -> None:
    base = SamplePlugin("base")
    child = SamplePlugin("child", dependencies=(PluginDependency("base"),))
    registry = PluginRegistry()
    registry.register_many((base, child))
    await registry.start_all()

    with pytest.raises(PluginDependencyError, match="running dependents"):
        await registry.disable("base")

    await registry.disable("child")
    await registry.disable("base")
    assert [item.state for item in await registry.health()] == [PluginState.DISABLED, PluginState.DISABLED]


@pytest.mark.asyncio
async def test_plugin_context_rejects_service_takeover_and_runs_lifo_cleanup() -> None:
    first = SamplePlugin("first")
    second = SamplePlugin("second", service_name="example.first")
    registry = PluginRegistry()
    registry.register_many((first, second))
    await registry.start("first")

    with pytest.raises(PluginLifecycleError):
        await registry.start("second", {"unused": True})

    await registry.stop("first")
    assert first.events == ["start", "stop", "cleanup"]


@pytest.mark.asyncio
async def test_missing_dependency_has_clear_error() -> None:
    plugin = SamplePlugin("consumer", dependencies=(PluginDependency("absent"),))
    registry = PluginRegistry()
    registry.register(plugin)

    with pytest.raises(PluginDependencyError, match="missing plugin 'absent'"):
        await registry.start_all()


@pytest.mark.asyncio
async def test_executor_plugin_registers_and_cleans_up_its_executor() -> None:
    factory = ExecutorFactory()
    registry = PluginRegistry()
    registry.register_host_service("basalt.executor_factory", factory)
    registry.register(DemoExecutorPlugin())

    await registry.start_all()
    assert isinstance(factory.get_executor("demo"), DemoExecutor)

    await registry.stop_all()
    assert not factory.has_executor("demo")
