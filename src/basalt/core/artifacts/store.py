"""Content-addressed local artifact storage with atomic writes and TTL cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class ArtifactError(RuntimeError):
    """Raised when artifact data is malformed, unavailable, or corrupted."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """Immutable descriptor for a JSON artifact stored on local disk."""

    digest: str
    size_bytes: int
    created_at: datetime
    media_type: str = "application/json"
    metadata: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        """Return the stable content-addressed artifact key."""
        return f"sha256:{self.digest}"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A named cache mapping to an artifact with optional expiry time."""

    key: str
    artifact_key: str
    created_at: datetime
    expires_at: datetime | None

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return whether this mapping should no longer be served."""
        return self.expires_at is not None and (now or datetime.now(UTC)) >= self.expires_at


class LocalArtifactStore:
    """Async-friendly, dependency-free local artifact and workflow-output cache.

    Artifact payloads are canonical JSON encoded before hashing.  Concurrent
    callers are serialized by an asyncio lock and individual payloads are
    written using ``os.replace`` so readers never observe a partial file.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._artifacts = self.root / "artifacts"
        self._index_path = self.root / "cache-index.json"
        self._lock = asyncio.Lock()

    async def put(self, value: Any, *, metadata: dict[str, Any] | None = None) -> Artifact:
        """Serialize a JSON value and return a descriptor for its immutable content."""
        payload = self._encode(value)
        digest = hashlib.sha256(payload).hexdigest()
        now = datetime.now(UTC)
        artifact = Artifact(digest, len(payload), now, metadata=metadata)
        async with self._lock:
            self._artifacts.mkdir(parents=True, exist_ok=True)
            target = self._path_for_digest(digest)
            if not target.exists():
                self._atomic_write(target, payload)
            self._write_descriptor_if_missing(artifact)
        return artifact

    async def get(self, key: str) -> Any:
        """Load and decode the JSON payload identified by an artifact key."""
        digest = self._digest_from_key(key)
        async with self._lock:
            path = self._path_for_digest(digest)
            try:
                raw = path.read_bytes()
            except FileNotFoundError as exc:
                raise ArtifactError(f"Artifact '{key}' does not exist") from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ArtifactError(f"Artifact '{key}' failed integrity verification")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"Artifact '{key}' is not valid JSON") from exc

    async def describe(self, key: str) -> Artifact:
        """Return the saved descriptor for an artifact key."""
        digest = self._digest_from_key(key)
        async with self._lock:
            try:
                data = json.loads(self._descriptor_path(digest).read_text("utf-8"))
            except FileNotFoundError as exc:
                raise ArtifactError(f"Artifact '{key}' does not exist") from exc
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        return Artifact(**data)

    async def cache_set(self, key: str, artifact: Artifact, ttl_seconds: float | None = None) -> CacheEntry:
        """Associate an application cache key with an existing artifact."""
        if not key or key != key.strip():
            raise ValueError("Cache key must be non-empty and trimmed")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive when supplied")
        now = datetime.now(UTC)
        entry = CacheEntry(
            key=key,
            artifact_key=artifact.key,
            created_at=now,
            expires_at=None if ttl_seconds is None else now + timedelta(seconds=ttl_seconds),
        )
        async with self._lock:
            index = self._read_index()
            index[key] = self._entry_to_json(entry)
            self._write_index(index)
        return entry

    async def cache_get(self, key: str) -> Artifact | None:
        """Resolve a live cache key to its artifact descriptor, if present."""
        async with self._lock:
            index = self._read_index()
            raw = index.get(key)
            if raw is None:
                return None
            entry = self._entry_from_json(raw)
            if entry.is_expired():
                del index[key]
                self._write_index(index)
                return None
        return await self.describe(entry.artifact_key)

    async def cache_delete(self, key: str) -> bool:
        """Remove a named mapping without deleting the underlying artifact."""
        async with self._lock:
            index = self._read_index()
            if key not in index:
                return False
            del index[key]
            self._write_index(index)
            return True

    def _encode(self, value: Any) -> bytes:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ArtifactError("Artifact values must be JSON serializable") from exc

    def _digest_from_key(self, key: str) -> str:
        prefix, separator, digest = key.partition(":")
        if prefix != "sha256" or not separator or len(digest) != 64:
            raise ArtifactError(f"Invalid artifact key '{key}'")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ArtifactError(f"Invalid artifact key '{key}'") from exc
        return digest

    def _path_for_digest(self, digest: str) -> Path:
        return self._artifacts / digest[:2] / f"{digest}.json"

    def _descriptor_path(self, digest: str) -> Path:
        return self._artifacts / digest[:2] / f"{digest}.meta.json"

    def _write_descriptor_if_missing(self, artifact: Artifact) -> None:
        path = self._descriptor_path(artifact.digest)
        if not path.exists():
            payload = asdict(artifact)
            payload["created_at"] = artifact.created_at.isoformat()
            self._atomic_write(path, json.dumps(payload, sort_keys=True).encode("utf-8"))

    def _read_index(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._index_path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise ArtifactError("Cache index is corrupted") from exc

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._index_path, json.dumps(index, sort_keys=True).encode("utf-8"))

    @staticmethod
    def _entry_to_json(entry: CacheEntry) -> dict[str, Any]:
        return {
            "key": entry.key,
            "artifact_key": entry.artifact_key,
            "created_at": entry.created_at.isoformat(),
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        }

    @staticmethod
    def _entry_from_json(raw: dict[str, Any]) -> CacheEntry:
        return CacheEntry(
            key=raw["key"], artifact_key=raw["artifact_key"],
            created_at=datetime.fromisoformat(raw["created_at"]),
            expires_at=datetime.fromisoformat(raw["expires_at"]) if raw["expires_at"] else None,
        )

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
