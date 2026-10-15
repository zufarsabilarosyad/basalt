"""Public data models for Basalt's extension framework.

The models are deliberately standard-library dataclasses.  Plugin authors do
not need to couple their package to Pydantic's runtime behaviour merely to
describe metadata, and the objects remain straightforward to serialize for an
API or CLI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PluginCapability(str, Enum):
    """Supported contribution points exposed by the Basalt runtime."""

    EXECUTOR = "executor"
    TRIGGER = "trigger"
    HOOK = "hook"
    STORAGE = "storage"
    SERIALIZER = "serializer"
    OBSERVER = "observer"
    API_ROUTER = "api_router"


class PluginState(str, Enum):
    """Lifecycle state assigned by :class:`PluginRegistry`."""

    DISCOVERED = "discovered"
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class PluginDependency:
    """A dependency on another Basalt plugin.

    Version constraints intentionally use a conservative ``major.minor``
    prefix format instead of pretending to implement the full Python packaging
    specifier grammar.  It supports stable compatibility checks while keeping
    the SDK dependency-free.
    """

    name: str
    version_prefix: str | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError("Plugin dependency name must be non-empty and trimmed")
        if self.version_prefix is not None and not self.version_prefix.strip():
            raise ValueError("version_prefix must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Static identity and compatibility declaration for a plugin."""

    name: str
    version: str
    description: str = ""
    api_version: str = "1"
    capabilities: frozenset[PluginCapability] = frozenset()
    dependencies: tuple[PluginDependency, ...] = ()
    author: str | None = None
    homepage: str | None = None

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError("Plugin name must be non-empty and trimmed")
        if not self.version or self.version != self.version.strip():
            raise ValueError("Plugin version must be non-empty and trimmed")
        if not self.api_version or self.api_version != self.api_version.strip():
            raise ValueError("Plugin API version must be non-empty and trimmed")
        names = [dependency.name for dependency in self.dependencies]
        if len(names) != len(set(names)):
            raise ValueError(f"Plugin '{self.name}' declares duplicate dependencies")
        if self.name in names:
            raise ValueError(f"Plugin '{self.name}' cannot depend on itself")


@dataclass(frozen=True, slots=True)
class PluginHealth:
    """One plugin's externally reportable health state."""

    name: str
    version: str
    state: PluginState
    healthy: bool
    capabilities: tuple[str, ...]
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Produce JSON-friendly output without exposing implementation objects."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PluginFailure:
    """Captured lifecycle failure retained for diagnostics and health reporting."""

    plugin_name: str
    phase: str
    exception_type: str
    message: str


@dataclass(slots=True)
class PluginRecord:
    """Registry-owned mutable lifecycle bookkeeping for one plugin."""

    plugin: Any
    state: PluginState = PluginState.DISCOVERED
    enabled: bool = True
    failure: PluginFailure | None = None
    registration_order: int = 0
    owned_services: set[str] = field(default_factory=set)
