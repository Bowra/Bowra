"""Source restore goes through HTTP authorization, immutable upload and SQLite CAS."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.store import KbStore
from bowhead_core.kb.worker import KbWorker
from test_kb_files_management_routes import UID, WS
from test_kb_files_management_routes import production_ctx as production_ctx

pytestmark = pytest.mark.integration


@pytest.fixture
async def recovered(production_ctx, tmp_path, initialize_migrated_core_database, monkeypatch):
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    monkeypatch.setenv("FEATURE_KB_USE_GUARD", "1")
    client, files = production_ctx
    client.app.state.user_store.get_user = AsyncMock(return_value=SimpleNamespace(status="active"))
    initialize_migrated_core_database(tmp_path / "core.db")
    artifacts = ArtifactStore(str(tmp_path / "core.db"), backend=LocalBackend(tmp_path / "blobs"))
    await artifacts.initialize()
    raw = b"# Annual report\n\nRevenue reached 123 units."
    artifact = await artifacts.put_bytes(
        created_by=None, content=raw, filename="report.md", mime="text/markdown", source="kb_upload", workspace_id=WS
    )
    fid = await files.create(
        artifact_id=artifact.id,
        filename="report.md",
        mime_type="text/markdown",
        size_bytes=len(raw),
        scope="space",
        scope_id=WS,
        workspace_id=WS,
        team_id=WS,
        uploaded_by=UID,
    )
    index = KbStore(str(tmp_path / "chroma"))
    embedder = SimpleNamespace(embed_batch=AsyncMock(side_effect=lambda texts: [[0.1, 0.2, 0.3] for _ in texts]))
    worker = KbWorker(files_store=files, store=index, embedder=embedder, artifact_fetcher=artifacts)
    await worker.parse_and_index(fid)
    client.app.state.artifact_store = artifacts
    client.app.state.kb_store = index
    client.app.state.kb_retriever = SimpleNamespace(_store=index)
    client.app.state.kb_worker = worker
    return client, files, fid, raw, embedder


async def test_restore_same_bytes_preserves_generation_and_different_bytes_do_not_replace(recovered):
    client, files, fid, raw, embedder = recovered
    before = await files.get(fid)
    headers = {"X-Workspace-Id": WS}
    result = client.post(f"/api/v1/kb/files/{fid}/restore-source", headers=headers, files={"upload": ("report.md", b"changed", "text/markdown")})
    assert result.status_code == 200, result.text
    assert result.json()["outcome"] == "different_content_requires_new_file"
    assert (await files.get(fid)).artifact_id == before.artifact_id
    result = client.post(f"/api/v1/kb/files/{fid}/restore-source", headers=headers, files={"upload": ("report.md", raw, "text/markdown")})
    assert result.status_code == 200, result.text
    assert result.json()["outcome"] == "same_content_restored"
    current = await files.get(fid)
    assert current.active_generation_id == before.active_generation_id and current.artifact_id != before.artifact_id
    verified = client.post(f"/api/v1/kb/files/{fid}/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["use_state"] == "eligible", verified.json().get("use_reason")
    assert verified.json()["binding_state"] == "verified"
    assert embedder.embed_batch.await_count == 1


async def test_recovery_requires_current_space_membership(recovered):
    client, files, fid, raw, _ = recovered
    before = await files.get(fid)
    client.app.state.user_store.get_member_role = AsyncMock(return_value=None)
    response = client.post(
        f"/api/v1/kb/files/{fid}/restore-source", headers={"X-Workspace-Id": WS}, files={"upload": ("report.md", raw, "text/markdown")}
    )
    assert response.status_code == 403
    assert (await files.get(fid)).artifact_id == before.artifact_id


async def test_verify_does_not_return_health_after_membership_is_revoked(recovered, monkeypatch):
    client, _, fid, _, _ = recovered
    backend = client.app.state.artifact_store.backend
    head = backend.head
    calls = 0

    async def revoke_during_verify(path):
        nonlocal calls
        result = await head(path)
        calls += 1
        if calls == 2:
            client.app.state.user_store.get_member_role = AsyncMock(return_value=None)
        return result

    monkeypatch.setattr(backend, "head", revoke_during_verify)
    response = client.post(f"/api/v1/kb/files/{fid}/verify", headers={"X-Workspace-Id": WS})
    assert calls >= 2
    assert response.status_code == 403
    assert "source_health" not in response.json()
