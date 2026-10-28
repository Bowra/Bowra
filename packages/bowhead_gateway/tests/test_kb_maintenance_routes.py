"""HTTP manifests and background execution keep the initiating identity and space."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from bowhead_core.kb.maintenance import KbMaintenanceStore
from bowhead_gateway.kb_maintenance import reconcile_page, run_rebuild_job
from test_kb_files_management_routes import UID, WS
from test_kb_files_management_routes import production_ctx as production_ctx
from test_kb_source_recovery import recovered as recovered

pytestmark = pytest.mark.integration


@pytest.fixture
async def maintenance(recovered, monkeypatch):
    client, files, fid, raw, embedder = recovered
    state = client.app.state
    state.user_store.get_team_llm_config_with_version = AsyncMock(return_value=({}, 1))
    state.team_kb_runtime_registry = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(kb_store=state.kb_store, worker=state.kb_worker)))
    monkeypatch.setattr("bowhead_gateway.kb_maintenance.start_rebuild_job", lambda *_: None)
    return client, files, fid, raw, embedder


def create(client, fid, **kwargs):
    return client.post("/api/v1/kb/maintenance/rebuilds", headers={"X-Space-Id": WS}, json={"file_ids": [fid], **kwargs})


async def test_explicit_preview_cost_budget_and_durable_completion(maintenance):
    client, files, fid, _, embedder = maintenance
    before = await files.get(fid)
    preview = create(client, fid)
    assert preview.status_code == 200, preview.text
    assert preview.json()["files"] == 1 and "job_id" not in preview.json()
    assert create(client, fid, apply=True).status_code == 409
    assert create(client, fid, max_bytes=1).status_code == 413
    response = create(client, fid, apply=True, accept_model_cost=True)
    assert response.status_code == 200, response.text
    jid = response.json()["job_id"]
    assert create(client, fid, apply=True, accept_model_cost=True).status_code == 409
    await run_rebuild_job(client.app, jid)
    result = client.get(f"/api/v1/kb/maintenance/rebuilds/{jid}", headers={"X-Space-Id": WS})
    assert result.json() == {"job_id": jid, "state": "completed", "total": 1, "processed": 1, "failed": 0, "changed": 0}
    assert (await files.get(fid)).active_generation_id != before.active_generation_id
    assert embedder.embed_batch.await_count == 2


@pytest.mark.parametrize("column,value", [("user_id", "other"), ("team_id", "other"), ("workspace_id", "other")])
async def test_job_control_does_not_cross_owner_team_or_space(maintenance, column, value):
    client, files, fid, _, _ = maintenance
    jid = create(client, fid, apply=True, accept_model_cost=True).json()["job_id"]
    async with aiosqlite.connect(files._db_path) as db:
        await db.execute(f"UPDATE kb_rebuild_jobs SET {column}=? WHERE job_id=?", (value, jid))
        await db.commit()
    for method, suffix in [("get", ""), ("post", "/cancel"), ("post", "/resume")]:
        response = getattr(client, method)(f"/api/v1/kb/maintenance/rebuilds/{jid}{suffix}", headers={"X-Space-Id": WS})
        assert response.status_code == 404
        assert fid not in response.text


async def test_revocation_before_execution_pauses_without_model_usage(maintenance):
    client, files, fid, _, embedder = maintenance
    jid = create(client, fid, apply=True, accept_model_cost=True).json()["job_id"]
    client.app.state.user_store.get_member_role = AsyncMock(return_value=None)
    await run_rebuild_job(client.app, jid)
    job = await KbMaintenanceStore(files._db_path).get_job(jid)
    assert job["state"] == "paused" and job["cursor"] == 0
    assert embedder.embed_batch.await_count == 1
    assert client.post(f"/api/v1/kb/maintenance/rebuilds/{jid}/resume", headers={"X-Space-Id": WS}).status_code == 403


async def test_revocation_during_model_call_prevents_publication(maintenance):
    client, files, fid, _, embedder = maintenance
    before = await files.get(fid)
    jid = create(client, fid, apply=True, accept_model_cost=True).json()["job_id"]

    async def revoked(texts):
        client.app.state.user_store.get_member_role = AsyncMock(return_value=None)
        return [[0.1, 0.2, 0.3] for _ in texts]

    embedder.embed_batch.side_effect = revoked
    await run_rebuild_job(client.app, jid)
    current = await files.get(fid)
    assert current.active_generation_id == before.active_generation_id
    assert current.parse_status == "failed"


async def test_crash_after_publication_resumes_without_duplicate_model_usage(maintenance):
    client, files, fid, _, embedder = maintenance
    jid = create(client, fid, apply=True, accept_model_cost=True).json()["job_id"]
    jobs = KbMaintenanceStore(files._db_path)
    assert await jobs.claim_file(jid, 0, await files.get(fid))
    await client.app.state.kb_worker.parse_and_index(fid, authorize=AsyncMock(return_value=True))
    await jobs.pause_interrupted()
    assert await jobs.resume(jid)
    await run_rebuild_job(client.app, jid)
    assert (await jobs.get_job(jid))["cursor"] == 1
    assert embedder.embed_batch.await_count == 2


async def test_scanner_uses_team_namespace_and_restarts_from_checkpoint(maintenance):
    client, files, fid, _, _ = maintenance
    assert await reconcile_page(client.app) == 1
    file = await files.get(fid)
    assert file.source_observation_json
    client.app.state.team_kb_runtime_registry.get.assert_awaited_once_with(WS, {}, 1)
    assert await reconcile_page(client.app) == 0  # End of pass resets cursor.
    assert await reconcile_page(client.app) == 1
    response = client.get("/api/v1/kb/files", params={"scope": "space"}, headers={"X-Space-Id": WS})
    assert response.status_code == 200
    assert response.json()["files"][0]["source_health"]["state"] == "eligible"


async def test_chunks_recheck_access_after_index_io(maintenance, monkeypatch):
    client, _, fid, _, _ = maintenance
    index = client.app.state.kb_store
    real = index.list_active_chunks

    async def revoke(*args, **kwargs):
        result = await real(*args, **kwargs)
        client.app.state.user_store.get_member_role = AsyncMock(return_value=None)
        return result

    monkeypatch.setattr(index, "list_active_chunks", revoke)
    response = client.get(f"/api/v1/kb/files/{fid}/chunks", headers={"X-Space-Id": WS})
    assert response.status_code == 403
    assert "Revenue" not in response.text


async def test_resource_write_denial_blocks_batch_without_disclosing_manifest(maintenance, monkeypatch):
    client, files, fid, _, embedder = maintenance
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    await client.app.state.access_control_store.create_or_update_resource_grant(
        team_id=WS, resource_kind="kb_file", resource_id=fid, principal_type="user", principal_id=UID, permission="write", effect="deny"
    )
    response = create(client, fid, apply=True, accept_model_cost=True)
    assert response.status_code == 404
    assert "report.md" not in response.text
    assert embedder.embed_batch.await_count == 1


async def test_local_background_rebuild_requires_live_license(maintenance):
    client, files, fid, _, embedder = maintenance
    jid = create(client, fid, apply=True, accept_model_cost=True).json()["job_id"]
    client.app.state.deployment_mode = "local"
    client.app.state.entitlements = None
    await run_rebuild_job(client.app, jid)
    assert (await KbMaintenanceStore(files._db_path).get_job(jid))["state"] == "paused"
    assert embedder.embed_batch.await_count == 1


@pytest.mark.parametrize("endpoint", ["preview", "download"])
async def test_source_bytes_recheck_space_access_after_storage_read(maintenance, monkeypatch, endpoint):
    client, _, fid, _, _ = maintenance
    backend = client.app.state.artifact_store.backend
    read = backend.read

    async def revoke(path):
        content = await read(path)
        client.app.state.user_store.get_member_role = AsyncMock(return_value=None)
        return content

    monkeypatch.setattr(backend, "read", revoke)
    response = client.get(f"/api/v1/kb/files/{fid}/{endpoint}", headers={"X-Space-Id": WS})
    assert response.status_code == 403
    assert "Revenue" not in response.text


@pytest.mark.parametrize("failure,status", [(FileNotFoundError, 404), (TimeoutError, 503), (PermissionError, 503)])
async def test_preview_storage_errors_remain_neutral(maintenance, monkeypatch, failure, status):
    client, _, fid, _, _ = maintenance
    monkeypatch.setattr(client.app.state.artifact_store.backend, "read", AsyncMock(side_effect=failure("private storage path")))
    response = client.get(f"/api/v1/kb/files/{fid}/preview", headers={"X-Space-Id": WS})
    assert response.status_code == status
    assert "private storage path" not in response.text


async def test_single_rebuild_uses_durable_job_and_can_be_rediscovered(maintenance):
    client, files, fid, _, _ = maintenance
    response = client.post(f"/api/v1/kb/files/{fid}/rebuild", headers={"X-Space-Id": WS})
    assert response.status_code == 200, response.text
    result = client.get("/api/v1/kb/maintenance/rebuilds", headers={"X-Space-Id": WS})
    assert result.status_code == 200
    jid = result.json()["job_id"]
    assert jid and (await KbMaintenanceStore(files._db_path).get_job(jid))["items"][0]["file_id"] == fid
    async with aiosqlite.connect(files._db_path) as db:
        await db.execute("UPDATE kb_rebuild_jobs SET workspace_id='ws_other' WHERE job_id=?", (jid,))
        await db.commit()
    assert client.get("/api/v1/kb/maintenance/rebuilds", headers={"X-Space-Id": WS}).json()["job_id"] is None
