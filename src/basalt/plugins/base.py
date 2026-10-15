"""Plugin protocol and controlled runtime context.

Plugins receive a deliberately narrow context rather than the engine object.
That makes the extension boundary explicit, prevents accidental access to
private internals, and gives the registry enough information to clean up
resources on disable or failed startup.
"""

from __future__ import annotations

import abc
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias, TypeVar

from basalt.plugins.models import PluginManifest


T = TypeVar("T")
LifecycleResult: TypeAlias = None | Awaitable[None]
CleanupCallback: TypeAlias = Callable[[], LifecycleResult]


class PluginError(RuntimeError):
    """Base error for plugin discovery, validation, and lifecycle failures."""


class PluginConfigurationError(PluginError):
    """Raised when a plugin receives unsupported or invalid configuration."""


class PluginLifecycleError(PluginError):
    """Raised when a plugin cannot complete a requested lifecycle transition."""


@dataclass(slots=True)
class PluginContext:
    """Runtime services and configuration visible to one plugin.

    Service names are global and cannot be overwritten.  A plugin should use a
    fully qualified name such as ``acme.slack.client``.  Registered cleanup
    callbacks run in reverse registration order when the plugin stops.
    """

    plugin_name: str
    config: Mapping[str, Any]
    _services: dict[str, Any]
    _owner_by_service: dict[str, str]
    _cleanups: list[CleanupCallback] = field(default_factory=list)

    def get_config(self, key: str, default: T | None = None) -> Any | T | None:
        """Return a configured value without leaking a mutable config mapping."""
        return self.config.get(key, default)

    def require_config(self, key: str) -> Any:
        """Return a required setting or raise an actionable configuration error."""
        if key not in self.config:
            raise PluginConfigurationError(
                f"Plugin '{self.plugin_name}' requires configuration key '{key}'."
            )
        return self.config[key]

    def register_service(self, name: str, service: Any) -> None:
        """Publish a service for other plugins and engine adapters.

        Names are write-once to avoid load-order-dependent replacement of
        connections or security-sensitive dependencies.
        """
        normalized = name.strip()
        if not normalized:
            raise PluginConfigurationError("Service name cannot be empty")
        if normalized in self._services:
            owner = self._owner_by_service[normalized]
            raise PluginConfigurationError(
                f"Service '{normalized}' is already registered by plugin '{owner}'."
            )
        self._services[normalized] = service
        self._owner_by_service[normalized] = self.plugin_name

    def get_service(self, name: str, expected_type: type[T] | None = None) -> T | Any:
        """Resolve a published service, optionally checking its type."""
        try:
            service = self._services[name]
        except KeyError as exc:
            raise PluginConfigurationError(f"Service '{name}' is not available") from exc
        if expected_type is not None and not isinstance(service, expected_type):
            raise PluginConfigurationError(
                f"Service '{name}' is {type(service).__name__}, expected {expected_type.__name__}."
            )
        return service

    def add_cleanup(self, callback: CleanupCallback) -> None:
        """Register a synchronous or asynchronous callback for plugin shutdown."""
        self._cleanups.append(callback)

    async def close(self) -> None:
        """Run cleanup handlers in LIFO order and remove owned services."""
        errors: list[BaseException] = []
        while self._cleanups:
            callback = self._cleanups.pop()
            try:
                outcome = callback()
                if inspect.isawaitable(outcome):
                    await outcome
            except BaseException as exc:  # cleanup must continue for sibling resources
                errors.append(exc)
        for name, owner in tuple(self._owner_by_service.items()):
            if owner == self.plugin_name:
                self._owner_by_service.pop(name, None)
                self._services.pop(name, None)
        if errors:
            raise PluginLifecycleError(
                f"Plugin '{self.plugin_name}' cleanup failed: {errors[0]}"
            ) from errors[0]


class BasaltPlugin(abc.ABC):
    """Base class implemented by supported Basalt plugins."""

    manifest: PluginManifest

    @abc.abstractmethod
    async def start(self, context: PluginContext) -> None:
        """Create and register plugin resources.  Must be safe to await once."""

    async def stop(self, context: PluginContext) -> None:
        """Release plugin-owned resources before its context is closed."""

    async def health(self, context: PluginContext) -> tuple[bool, str | None]:
        """Return plugin-specific health information for a status endpoint."""
        return True, None
