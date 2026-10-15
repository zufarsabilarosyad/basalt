"""Contract tests for content-addressed artifact and cache storage."""

import asyncio

import pytest

from basalt.core.artifacts import ArtifactError, LocalArtifactStore


@pytest.mark.asyncio
async def test_artifact_round_trip_is_content_addressed(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    first = await store.put({"b": 2, "a": [1]})
    second = await store.put({"a": [1], "b": 2})
    assert first.key == second.key
    assert await store.get(first.key) == {"a": [1], "b": 2}
    assert (await store.describe(first.key)).size_bytes == first.size_bytes


@pytest.mark.asyncio
async def test_cache_expires_and_does_not_remove_artifact(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    artifact = await store.put({"value": 1})
    await store.cache_set("result", artifact, ttl_seconds=0.001)
    await asyncio.sleep(0.01)
    assert await store.cache_get("result") is None
    assert await store.get(artifact.key) == {"value": 1}


@pytest.mark.asyncio
async def test_invalid_or_missing_artifact_is_actionable(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(ArtifactError, match="Invalid"):
        await store.get("not-an-artifact")
    with pytest.raises(ArtifactError, match="does not exist"):
        await store.get("sha256:" + "0" * 64)
