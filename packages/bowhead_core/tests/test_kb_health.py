from __future__ import annotations

from pathlib import Path
from time import sleep
from unittest.mock import AsyncMock, Mock

import anyio
import pytest
from botocore.exceptions import ClientError
from bowhead_core.artifact_store import LocalBackend, S3Backend
from bowhead_core.kb.document import ChunkMetadata, KbChunk
from bowhead_core.kb.health import SourceRecord, check_storage_health, inspect_file_health
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.kb.store import KbStore

pytestmark = pytest.mark.integration


@pytest.fixture
def file() -> KbFileDTO:
    return KbFileDTO(
        file_id="kbf_health",
        artifact_id="art_health",
        filename="private.txt",
        mime_type="text/plain",
        size_bytes=10,
        scope="personal",
        scope_id="u1",
        workspace_id="ws_health",
        team_id="team_health",
        uploaded_by="u1",
        uploaded_at="2026-09-28T00:00:00Z",
        parse_status="done",
        chunks_count=1,
    )


@pytest.mark.parametrize(
    ("present", "healthy", "expected"), [(True, True, "present"), (False, True, "confirmed_absent"), (False, False, "unchecked")]
)
async def test_local_presence_requires_independent_storage_health(
    tmp_path: Path, file: KbFileDTO, present: bool, healthy: bool, expected: str
) -> None:
    backend = LocalBackend(tmp_path / "blobs")
    path = tmp_path / "blobs" / "private.txt"
    if present:
        path.write_text("original")
    result = await inspect_file_health(file, source=SourceRecord(str(path), "ready"), backend=backend, storage_health=AsyncMock(return_value=healthy))
    assert result.source_health == expected
    assert result.binding_state == "unverified"
    assert result.index.state == "unknown"
    assert not result.eligibility_enforced
    assert str(path) not in result.model_dump_json()


@pytest.mark.parametrize(("status", "expected"), [(404, "confirmed_absent"), (403, "temporarily_unavailable"), (500, "temporarily_unavailable")])
async def test_s3_authoritative_absence_differs_from_denial_or_outage(file: KbFileDTO, monkeypatch, status: int, expected: str) -> None:
    client = Mock()
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": str(status), "Message": "private-bucket-secret"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject"
    )
    backend = S3Backend(endpoint_url="http://s3.internal", bucket="test", access_key="test", secret_key="test")
    monkeypatch.setattr(backend, "_get_client", lambda: client)
    result = await inspect_file_health(
        file, source=SourceRecord("private-key", "ready"), backend=backend, storage_health=AsyncMock(return_value=True)
    )
    assert result.source_health == expected
    assert "private" not in result.model_dump_json()


async def test_timeouts_and_empty_health_samples_do_not_confirm_loss(file: KbFileDTO) -> None:
    async def hanging_head(path: str) -> None:
        await anyio.sleep_forever()

    backend = Mock(head=hanging_head)
    assert not await check_storage_health(backend, [])
    result = await inspect_file_health(
        file, source=SourceRecord("key", "ready"), backend=backend, storage_health=AsyncMock(return_value=True), timeout=0.01
    )
    assert result.source_health == "temporarily_unavailable"


async def test_storage_probe_catches_missing_mount(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    paths = [str(tmp_path / str(i)) for i in range(5)]
    for path in paths[:4]:
        Path(path).write_bytes(b"x")
    assert await check_storage_health(backend, paths)
    for path in paths[:4]:
        Path(path).unlink()
    assert not await check_storage_health(backend, paths)


async def test_slow_local_stat_does_not_block_probe_deadline(tmp_path: Path, file: KbFileDTO, monkeypatch) -> None:
    backend = LocalBackend(tmp_path)
    path = tmp_path / "slow.txt"
    path.write_text("original")
    original = Path.stat

    def delayed_stat(target, *args, **kwargs):
        if target == path:
            sleep(0.1)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", delayed_stat)
    result = await inspect_file_health(
        file, source=SourceRecord(str(path), "ready"), backend=backend, storage_health=AsyncMock(return_value=True), timeout=0.01
    )
    assert result.source_health == "temporarily_unavailable"


async def test_real_chroma_observation_is_bounded_and_never_proves_legacy_binding(tmp_path: Path, file: KbFileDTO) -> None:
    store = KbStore(str(tmp_path / "chroma"))
    missing = await store.inspect_active_index(file)
    assert missing.reason == "collection_missing"
    assert store._client.list_collections() == []
    chunks = [
        KbChunk(
            chunk_id=f"ch_{i}",
            file_id=file.file_id,
            content=f"sample {i}",
            embedding=[0.1, 0.2],
            metadata=ChunkMetadata(
                file_id=file.file_id,
                file_name=file.filename,
                scope=file.scope,
                scope_owner=file.scope_id,
                workspace_id=file.workspace_id,
                team_id=file.team_id,
                chunk_index=i,
            ),
        )
        for i in range(2)
    ]
    await store.add_chunks(file, chunks)
    mismatch = await store.inspect_active_index(file)
    assert mismatch.state == "missing_or_corrupt"
    assert mismatch.observed_chunks == 2
    matching = file.model_copy(update={"chunks_count": 2})
    readable = await store.inspect_active_index(matching)
    assert readable.reason == "legacy_chunks_readable"
    assert readable.state == "unknown"
    bounded = await store.inspect_active_index(matching, limit=1)
    assert bounded.reason == "inspection_limit" and bounded.count_limited
    assert await store.count_chunks(file) == 2


async def test_processing_and_index_outages_are_not_corruption(tmp_path: Path, file: KbFileDTO) -> None:
    backend = LocalBackend(tmp_path)
    index = Mock(inspect_active_index=AsyncMock(side_effect=PermissionError("secret")))
    args = {"source": None, "backend": backend, "storage_health": AsyncMock(return_value=False), "index_reader": index}
    result = await inspect_file_health(file, **args)
    assert result.index.reason == "index_unavailable" and result.index.state == "unknown"
    index.inspect_active_index.reset_mock()
    result = await inspect_file_health(file.model_copy(update={"parse_status": "parsing"}), **args)
    assert result.index.state == "processing"
    index.inspect_active_index.assert_not_called()
