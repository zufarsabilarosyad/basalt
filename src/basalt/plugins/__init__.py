"""Stable public SDK for extending a Basalt runtime."""

from basalt.plugins.base import (
    BasaltPlugin,
    PluginConfigurationError,
    PluginContext,
    PluginError,
    PluginLifecycleError,
)
from basalt.plugins.discovery import DEFAULT_ENTRY_POINT_GROUP, PluginDiscoveryError, discover_plugins
from basalt.plugins.executors import ExecutorPlugin
from basalt.plugins.models import (
    PluginCapability,
    PluginDependency,
    PluginFailure,
    PluginHealth,
    PluginManifest,
    PluginState,
)
from basalt.plugins.registry import (
    PluginAlreadyRegisteredError,
    PluginDependencyError,
    PluginNotFoundError,
    PluginRegistry,
)

__all__ = [
    "DEFAULT_ENTRY_POINT_GROUP",
    "BasaltPlugin",
    "PluginAlreadyRegisteredError",
    "PluginCapability",
    "PluginConfigurationError",
    "PluginContext",
    "PluginDependency",
    "PluginDependencyError",
    "PluginDiscoveryError",
    "PluginError",
    "ExecutorPlugin",
    "PluginFailure",
    "PluginHealth",
    "PluginLifecycleError",
    "PluginManifest",
    "PluginNotFoundError",
    "PluginRegistry",
    "PluginState",
    "discover_plugins",
]
