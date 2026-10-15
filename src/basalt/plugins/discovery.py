"""Safe discovery of installed Basalt plugin entry points."""

from __future__ import annotations

from importlib import metadata
from typing import Iterable

from basalt.plugins.base import BasaltPlugin, PluginError


DEFAULT_ENTRY_POINT_GROUP = "basalt.plugins"


class PluginDiscoveryError(PluginError):
    """Raised when an installed entry point is invalid or cannot be loaded."""


def discover_plugins(group: str = DEFAULT_ENTRY_POINT_GROUP) -> tuple[BasaltPlugin, ...]:
    """Load already-installed plugins declared under an entry-point group.

    Entry points may expose either a plugin instance or a zero-argument factory.
    Loading is explicit and never performs package installation or imports from
    arbitrary user-provided paths.
    """
    entry_points = metadata.entry_points()
    selected: Iterable[metadata.EntryPoint]
    if hasattr(entry_points, "select"):
        selected = entry_points.select(group=group)
    else:  # pragma: no cover - compatibility with older importlib.metadata
        selected = entry_points.get(group, ())
    plugins: list[BasaltPlugin] = []
    names: set[str] = set()
    for entry_point in sorted(selected, key=lambda item: item.name):
        try:
            loaded = entry_point.load()
            plugin = loaded() if isinstance(loaded, type) or callable(loaded) else loaded
        except Exception as exc:
            raise PluginDiscoveryError(
                f"Could not load plugin entry point '{entry_point.name}': {exc}"
            ) from exc
        if not isinstance(plugin, BasaltPlugin):
            raise PluginDiscoveryError(
                f"Entry point '{entry_point.name}' did not produce a BasaltPlugin instance."
            )
        if plugin.manifest.name in names:
            raise PluginDiscoveryError(f"Duplicate discovered plugin '{plugin.manifest.name}'.")
        names.add(plugin.manifest.name)
        plugins.append(plugin)
    return tuple(plugins)
