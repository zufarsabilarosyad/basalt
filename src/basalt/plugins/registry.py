"""Dependency-aware asynchronous registry for Basalt plugins."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from basalt.plugins.base import BasaltPlugin, PluginContext, PluginError, PluginLifecycleError
from basalt.plugins.models import PluginFailure, PluginHealth, PluginRecord, PluginState


class PluginAlreadyRegisteredError(PluginError):
    """Raised when an attempt would replace a registered plugin implicitly."""


class PluginNotFoundError(PluginError):
    """Raised when a named plugin is unavailable in this registry."""


class PluginDependencyError(PluginError):
    """Raised for missing, incompatible, disabled, or cyclic dependencies."""


class PluginRegistry:
    """Own plugin identity, configuration, dependency ordering, and lifecycle.

    All lifecycle operations are serialized.  This avoids a race where a
    dependency is stopping while another plugin is starting against it.  The
    registry intentionally does not install packages: callers control package
    provenance, while discovery only loads already-installed entry points.
    """

    def __init__(self, api_version: str = "1") -> None:
        self.api_version = api_version
        self._records: dict[str, PluginRecord] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._services: dict[str, Any] = {}
        self._service_owners: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    def register(self, plugin: BasaltPlugin, *, enabled: bool = True) -> None:
        """Register a plugin instance after validating static metadata."""
        manifest = self._manifest_for(plugin)
        if manifest.name in self._records:
            raise PluginAlreadyRegisteredError(f"Plugin '{manifest.name}' is already registered")
        self._counter += 1
        self._records[manifest.name] = PluginRecord(
            plugin=plugin,
            state=PluginState.REGISTERED if enabled else PluginState.DISABLED,
            enabled=enabled,
            registration_order=self._counter,
        )

    def register_many(self, plugins: Iterable[BasaltPlugin]) -> None:
        """Register a group atomically with rollback on invalid metadata."""
        registered: list[str] = []
        try:
            for plugin in plugins:
                self.register(plugin)
                registered.append(plugin.manifest.name)
        except Exception:
            for name in registered:
                self._records.pop(name, None)
            raise

    def register_host_service(self, name: str, service: Any) -> None:
        """Publish a stable engine-owned service before plugins are started.

        Host services use a reserved owner and are never removed as part of a
        plugin cleanup.  This is the supported bridge for integrations such as
        an executor plugin obtaining the engine's executor factory.
        """
        normalized = name.strip()
        if not normalized:
            raise ValueError("Host service name cannot be empty")
        if normalized in self._services:
            raise PluginAlreadyRegisteredError(f"Service '{normalized}' is already registered")
        self._services[normalized] = service
        self._service_owners[normalized] = "__basalt_host__"

    def names(self) -> tuple[str, ...]:
        """List registered plugins in deterministic registration order."""
        return tuple(
            name
            for name, _ in sorted(
                self._records.items(), key=lambda item: item[1].registration_order
            )
        )

    def get(self, name: str) -> BasaltPlugin:
        """Return a registered plugin instance by name."""
        return self._record(name).plugin

    async def start_all(self, configurations: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        """Start every enabled plugin in dependency order.

        Plugins started in this invocation are rolled back in reverse order if
        any later plugin fails, leaving the registry in a predictable state.
        """
        async with self._lock:
            config = configurations or {}
            started: list[str] = []
            try:
                for name in self._topological_order():
                    record = self._records[name]
                    if record.enabled and record.state is not PluginState.RUNNING:
                        await self._start_unlocked(name, config.get(name, {}))
                        started.append(name)
            except Exception:
                for name in reversed(started):
                    await self._stop_unlocked(name, disable=False)
                raise

    async def start(self, name: str, configuration: Mapping[str, Any] | None = None) -> None:
        """Start one plugin after starting its enabled dependencies."""
        async with self._lock:
            for dependency_name in self._dependency_order_for(name):
                dependency = self._records[dependency_name]
                if dependency.state is not PluginState.RUNNING:
                    await self._start_unlocked(dependency_name, {})
            await self._start_unlocked(name, configuration or {})

    async def stop_all(self) -> None:
        """Stop running plugins in reverse dependency order."""
        async with self._lock:
            for name in reversed(self._topological_order()):
                await self._stop_unlocked(name, disable=False)

    async def stop(self, name: str, *, disable: bool = False) -> None:
        """Stop a plugin unless another running plugin requires it."""
        async with self._lock:
            dependents = self._running_dependents(name)
            if dependents:
                raise PluginDependencyError(
                    f"Cannot stop '{name}'; running dependents: {', '.join(sorted(dependents))}."
                )
            await self._stop_unlocked(name, disable=disable)

    async def enable(self, name: str, configuration: Mapping[str, Any] | None = None) -> None:
        """Enable and start a previously disabled plugin."""
        async with self._lock:
            record = self._record(name)
            record.enabled = True
            record.state = PluginState.REGISTERED
            await self._start_unlocked(name, configuration or {})

    async def disable(self, name: str) -> None:
        """Stop and mark a plugin disabled; running dependents are rejected."""
        await self.stop(name, disable=True)

    async def health(self) -> tuple[PluginHealth, ...]:
        """Collect best-effort health records without failing the whole report."""
        async with self._lock:
            reports: list[PluginHealth] = []
            for name in self.names():
                record = self._records[name]
                manifest = record.plugin.manifest
                healthy = record.state is PluginState.RUNNING
                detail = record.failure.message if record.failure else None
                if record.state is PluginState.RUNNING:
                    context = self._contexts[name]
                    try:
                        healthy, detail = await record.plugin.health(context)
                    except Exception as exc:
                        healthy, detail = False, f"health check failed: {exc}"
                reports.append(
                    PluginHealth(
                        name=name,
                        version=manifest.version,
                        state=record.state,
                        healthy=healthy,
                        capabilities=tuple(sorted(item.value for item in manifest.capabilities)),
                        detail=detail,
                    )
                )
            return tuple(reports)

    def service(self, name: str, expected_type: type[Any] | None = None) -> Any:
        """Resolve a plugin-published service for an engine integration."""
        try:
            service = self._services[name]
        except KeyError as exc:
            raise PluginNotFoundError(f"Plugin service '{name}' is not registered") from exc
        if expected_type is not None and not isinstance(service, expected_type):
            raise PluginError(f"Service '{name}' is not {expected_type.__name__}")
        return service

    @property
    def services(self) -> Mapping[str, Any]:
        """Expose a read-only snapshot of currently available services."""
        return MappingProxyType(self._services.copy())

    async def _start_unlocked(self, name: str, configuration: Mapping[str, Any]) -> None:
        record = self._record(name)
        if not record.enabled:
            raise PluginLifecycleError(f"Plugin '{name}' is disabled")
        if record.state is PluginState.RUNNING:
            return
        self._validate_dependencies(name)
        record.state = PluginState.STARTING
        context = PluginContext(
            plugin_name=name,
            config=MappingProxyType(dict(configuration)),
            _services=self._services,
            _owner_by_service=self._service_owners,
        )
        try:
            await record.plugin.start(context)
        except Exception as exc:
            record.state = PluginState.FAILED
            record.failure = PluginFailure(name, "start", type(exc).__name__, str(exc))
            try:
                await context.close()
            except Exception:
                pass
            raise PluginLifecycleError(f"Plugin '{name}' failed to start: {exc}") from exc
        self._contexts[name] = context
        record.owned_services = {key for key, owner in self._service_owners.items() if owner == name}
        record.failure = None
        record.state = PluginState.RUNNING

    async def _stop_unlocked(self, name: str, *, disable: bool) -> None:
        record = self._record(name)
        if record.state is not PluginState.RUNNING:
            if disable:
                record.enabled = False
                record.state = PluginState.DISABLED
            return
        record.state = PluginState.STOPPING
        context = self._contexts.pop(name)
        try:
            await record.plugin.stop(context)
            await context.close()
        except Exception as exc:
            record.failure = PluginFailure(name, "stop", type(exc).__name__, str(exc))
            record.state = PluginState.FAILED
            raise PluginLifecycleError(f"Plugin '{name}' failed to stop: {exc}") from exc
        record.owned_services.clear()
        record.enabled = not disable
        record.state = PluginState.DISABLED if disable else PluginState.STOPPED

    def _manifest_for(self, plugin: BasaltPlugin):
        if not isinstance(plugin, BasaltPlugin):
            raise TypeError("Plugins must inherit from BasaltPlugin")
        manifest = plugin.manifest
        if manifest.api_version != self.api_version:
            raise PluginDependencyError(
                f"Plugin '{manifest.name}' requires API '{manifest.api_version}', registry provides '{self.api_version}'."
            )
        return manifest

    def _record(self, name: str) -> PluginRecord:
        try:
            return self._records[name]
        except KeyError as exc:
            raise PluginNotFoundError(f"Plugin '{name}' is not registered") from exc

    def _validate_dependencies(self, name: str) -> None:
        for dependency in self._record(name).plugin.manifest.dependencies:
            record = self._records.get(dependency.name)
            if record is None:
                if dependency.optional:
                    continue
                raise PluginDependencyError(
                    f"Plugin '{name}' requires missing plugin '{dependency.name}'."
                )
            if dependency.version_prefix and not record.plugin.manifest.version.startswith(
                dependency.version_prefix
            ):
                raise PluginDependencyError(
                    f"Plugin '{name}' requires '{dependency.name}' version prefix "
                    f"'{dependency.version_prefix}', found '{record.plugin.manifest.version}'."
                )
            if not dependency.optional and record.state is not PluginState.RUNNING:
                raise PluginDependencyError(
                    f"Plugin '{name}' requires running plugin '{dependency.name}'."
                )

    def _topological_order(self) -> tuple[str, ...]:
        pending = set(self._records)
        ordered: list[str] = []
        while pending:
            ready = sorted(
                (
                    name
                    for name in pending
                    if all(
                        dep.optional or dep.name not in pending
                        for dep in self._records[name].plugin.manifest.dependencies
                    )
                ),
                key=lambda name: self._records[name].registration_order,
            )
            if not ready:
                raise PluginDependencyError("Plugin dependency graph contains a cycle")
            for name in ready:
                pending.remove(name)
                ordered.append(name)
        return tuple(ordered)

    def _dependency_order_for(self, name: str) -> tuple[str, ...]:
        order = self._topological_order()
        required = {name}
        changed = True
        while changed:
            changed = False
            for candidate in tuple(required):
                for dep in self._record(candidate).plugin.manifest.dependencies:
                    if not dep.optional and dep.name not in required:
                        required.add(dep.name)
                        changed = True
        return tuple(candidate for candidate in order if candidate in required and candidate != name)

    def _running_dependents(self, name: str) -> set[str]:
        return {
            candidate
            for candidate, record in self._records.items()
            if record.state is PluginState.RUNNING
            and any(
                dependency.name == name and not dependency.optional
                for dependency in record.plugin.manifest.dependencies
            )
        }
