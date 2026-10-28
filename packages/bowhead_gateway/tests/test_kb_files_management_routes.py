"""kb-management sprint: route-level coverage for GET /kb/files pagination +
filter contract, and a registration smoke for the new mutation routes.

The bulk/reparse handlers are thin delegations to store methods + core helpers
that are unit-tested in bowhead_core/tests/test_kb_files_store_management.py;
here we lock the route contract that's novel: total = post-filter full count
(not page size), has_more, offset slicing, and server-side filters.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.health import IndexObservation
from bowhead_core.team_kb_runtime import TeamKbRuntimeRegistry
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

WS = "team_test"
UID = "u_alice"


def _user() -> User:
    return User(id=UID, email="alice@test.com", team_id=WS, tier="ultra", email_verified=True)


class _ProductionUserStore:
    async def get_workspace(self, workspace_id: str) -> dict[str, str] | None:
        return {"id": WS, "status": "active"} if workspace_id == WS else None

    async def get_user_team_id(self, user_id: str) -> str | None:
        return WS if user_id == UID else None

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        return WS if workspace_id == WS else None

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        return "member" if workspace_id == WS and user_id == UID else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "member" if user_id == UID and team_id == WS else None


@pytest.fixture
async def ctx(initialize_migrated_kb_files_database, tmp_path: Path):
    a = FastAPI()
    a.include_router(kb.router)
    a.dependency_overrides[_current_user] = _user
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    a.state.kb_files_store = files_store
    return TestClient(a), files_store


@pytest.fixture
async def production_ctx(initialize_migrated_kb_files_database, tmp_path: Path):
    a = FastAPI()
    a.include_router(kb.router)
    a.dependency_overrides[_current_user] = _user
    a.state.user_store = _ProductionUserStore()
    access_store = AccessControlStore(tmp_path / "access.db")
    await access_store.initialize()
    a.state.access_control_store = access_store
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    a.state.kb_files_store = files_store
    return TestClient(a), files_store


async def _seed(store: KbFilesStore, n: int, **overrides) -> list[str]:
    ids = []
    for i in range(n):
        defaults = {
            "artifact_id": f"art_{i}",
            "filename": f"file_{i:03d}.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1000 + i,
            "scope": "personal",
            "scope_id": UID,
            "workspace_id": WS,
            "team_id": WS,
            "uploaded_by": UID,
        }
        defaults.update(overrides)
        ids.append(await store.create(**defaults))
    return ids


# ─── pagination contract ──────────────────────────────


async def test_personal_list_without_space_context_uses_team_access_context(production_ctx) -> None:
    client, store = production_ctx
    await _seed(store, 1)

    r = client.get("/api/v1/kb/files", params={"scope": "personal"})

    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


async def test_team_list_without_space_context_uses_team_access_context(production_ctx) -> None:
    client, store = production_ctx
    await _seed(store, 1, scope="team", scope_id=WS, workspace_id="")

    r = client.get("/api/v1/kb/files", params={"scope": "team"})

    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


async def test_space_list_without_space_context_stays_fail_closed(production_ctx) -> None:
    client, _store = production_ctx

    r = client.get("/api/v1/kb/files", params={"scope": "space"})

    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "workspace_required"


async def test_total_is_full_count_not_page_size(ctx) -> None:
    client, store = ctx
    await _seed(store, 5)
    r = client.get("/api/v1/kb/files", params={"scope": "personal", "limit": 2, "offset": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5  # full count, NOT page size (was the latent bug)
    assert len(body["files"]) == 2  # page size honored
    assert body["has_more"] is True


async def test_offset_paging_partitions_set(ctx) -> None:
    client, store = ctx
    await _seed(store, 5)
    seen: list[str] = []
    for off in (0, 2, 4):
        r = client.get("/api/v1/kb/files", params={"scope": "personal", "limit": 2, "offset": off})
        assert r.status_code == 200, r.text
        seen.extend(f["file_id"] for f in r.json()["files"])
    assert len(seen) == 5
    assert len(set(seen)) == 5  # no overlap/gap across pages


async def test_last_page_has_more_false(ctx) -> None:
    client, store = ctx
    await _seed(store, 3)
    r = client.get("/api/v1/kb/files", params={"scope": "personal", "limit": 2, "offset": 2})
    body = r.json()
    assert body["total"] == 3
    assert len(body["files"]) == 1
    assert body["has_more"] is False


async def test_offset_beyond_end_empty_page(ctx) -> None:
    client, store = ctx
    await _seed(store, 3)
    r = client.get("/api/v1/kb/files", params={"scope": "personal", "limit": 10, "offset": 99})
    body = r.json()
    assert body["total"] == 3
    assert body["files"] == []
    assert body["has_more"] is False


# ─── scope isolation (dingsiyan 2026-07-07) ───────────


async def test_workspace_tab_excludes_personal_in_ws(ctx) -> None:
    """A personal upload carries the current workspace_id (needed for retrieval),
    but the workspace tab must show workspace-scope files ONLY — personal docs
    must not leak across the tab boundary. Regression for dingsiyan 2026-07-07."""
    client, store = ctx
    # 2 personal docs routed to this ws (default _seed: scope=personal, ws=WS).
    await _seed(store, 2)
    # 1 genuine workspace-scope doc in the same ws.
    ws_ids = await _seed(store, 1, scope="workspace", scope_id=WS, filename="ws_shared.pdf")
    r = client.get("/api/v1/kb/files", params={"scope": "workspace"}, headers={"X-Workspace-Id": WS})
    assert r.status_code == 200, r.text
    body = r.json()
    # only the workspace-scope doc; the 2 personal-in-ws docs are excluded.
    assert body["total"] == 1
    assert [f["file_id"] for f in body["files"]] == ws_ids
    assert all(f["scope"] == "space" for f in body["files"])  # Batch 3/c1 canonical normalization on reads.


async def test_workspace_badge_count_excludes_personal(ctx) -> None:
    """The '2 但空' bug: the workspace-tab badge (unfiltered `total`) counted the
    leaked personal docs (→ 2) while the filtered list rendered empty. With the
    leak fixed, an all-personal upload yields total=0 for the workspace tab."""
    client, store = ctx
    await _seed(store, 2)  # personal only, routed to WS
    r = client.get("/api/v1/kb/files", params={"scope": "workspace", "limit": 1}, headers={"X-Workspace-Id": WS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["files"] == []


# ─── server-side filters ──────────────────────────────


async def test_status_filter(ctx) -> None:
    client, store = ctx
    await _seed(store, 2, initial_parse_status="failed")
    await _seed(store, 3, initial_parse_status="done")
    r = client.get("/api/v1/kb/files", params={"scope": "personal", "status": "failed"})
    body = r.json()
    assert body["total"] == 2
    assert all(f["parse_status"] == "failed" for f in body["files"])


async def test_q_filename_filter(ctx) -> None:
    client, store = ctx
    await _seed(store, 1, filename="budget-2026.xlsx")
    await _seed(store, 2, filename="random.pdf")
    r = client.get("/api/v1/kb/files", params={"scope": "personal", "q": "budget"})
    body = r.json()
    assert body["total"] == 1
    assert body["files"][0]["filename"] == "budget-2026.xlsx"


# ─── route registration smoke ─────────────────────────


def test_new_routes_registered() -> None:
    """The new mutation routes must be mounted (catches import/wiring breakage)."""
    paths = {getattr(r, "path", None) for r in kb.router.routes}
    for expected in (
        "/api/v1/kb/files/bulk_delete",
        "/api/v1/kb/files/bulk_scope",
        "/api/v1/kb/files/bulk_doc_type",
        "/api/v1/kb/files/{file_id}/doc_type",
        "/api/v1/kb/files/{file_id}/reparse",
    ):
        assert expected in paths, f"missing route {expected}"


@pytest.mark.anyio
async def test_bulk_partial_result_returns_only_stable_error_code() -> None:
    async def _deny(_file_id: str) -> None:
        raise kb._kb_error(403, "resource_grant_denied", reason="RAW_POLICY")

    result = await kb._run_bulk(["kbf_denied"], _deny)

    assert result.succeeded == 0
    assert result.failed == 1
    item = result.results[0]
    assert item.error_code == "resource_grant_denied"
    assert item.error is None
    assert "RAW_POLICY" not in item.model_dump_json()


def test_missing_kb_store_uses_code_only_service_error() -> None:
    app = FastAPI()
    app.include_router(kb.router)
    app.dependency_overrides[_current_user] = _user
    client = TestClient(app)

    response = client.get("/api/v1/kb/files", params={"scope": "personal"})

    assert response.status_code == 503
    assert response.json()["detail"] == {"error": {"code": "kb_service_unavailable"}}


# ─── Uploader names (KB experience cleanup PRD FR2.3) ────────


class _NamingUserStore(_ProductionUserStore):
    def __init__(self, labels: dict[str, str] | None, *, fail: bool = False) -> None:
        self.labels = labels or {}
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    async def get_team_member_labels(self, team_id: str, user_ids: list[str]) -> dict[str, str]:
        self.calls.append((team_id, user_ids))
        if self.fail:
            raise RuntimeError("users db locked")
        return {uid: name for uid, name in self.labels.items() if uid in user_ids}


async def test_list_fills_uploader_name_from_team_labels(production_ctx) -> None:
    client, store = production_ctx
    naming = _NamingUserStore({UID: "Alice Zhang"})
    client.app.state.user_store = naming
    await _seed(store, 1)

    listed = client.get("/api/v1/kb/files", params={"scope": "personal"})

    assert listed.status_code == 200, listed.text
    assert listed.json()["files"][0]["uploaded_by_name"] == "Alice Zhang"
    assert all(team_id == WS for team_id, _ in naming.calls)


async def test_list_survives_uploader_name_lookup_failure(production_ctx) -> None:
    client, store = production_ctx
    client.app.state.user_store = _NamingUserStore(None, fail=True)
    await _seed(store, 1)

    r = client.get("/api/v1/kb/files", params={"scope": "personal"})

    assert r.status_code == 200, r.text
    assert r.json()["files"][0]["uploaded_by_name"] is None


async def test_health_checks_permissions_before_probing_and_default_off(production_ctx, monkeypatch) -> None:
    client, files = production_ctx
    ids = await _seed(files, 1)
    denied = await _seed(files, 1, team_id="other_team")
    artifacts = Mock(get=AsyncMock(side_effect=AssertionError("must authorize first")))
    client.app.state.artifact_store = artifacts
    monkeypatch.delenv("FEATURE_KB_TRUST_V2", raising=False)
    assert client.get(f"/api/v1/kb/files/{ids[0]}/health").status_code == 404
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    response = client.get(f"/api/v1/kb/files/{denied[0]}/health")
    assert response.status_code == 404, response.text
    assert "source_health" not in response.text and "file_000" not in response.text
    await files.soft_delete(ids[0])
    deleted = client.get(f"/api/v1/kb/files/{ids[0]}/health")
    assert (deleted.status_code, deleted.content) == (response.status_code, response.content)
    artifacts.get.assert_not_called()


async def test_health_reads_real_source_without_mutating_parse_state(production_ctx, tmp_path, monkeypatch) -> None:
    client, files = production_ctx
    artifacts = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await artifacts.initialize()
    art = await artifacts.put_bytes(content=b"source", filename="private.txt", mime="text/plain", created_by=UID)
    (file_id,) = await _seed(files, 1, artifact_id=art.id)
    await files.update(file_id, parse_status="done", chunks_count=1)
    before = await files.get(file_id)
    client.app.state.artifact_store = artifacts
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    response = client.get(f"/api/v1/kb/files/{file_id}/health")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source_health"] == "present"
    assert result["binding_state"] == "unverified" and not result["eligibility_enforced"]
    assert art.path not in response.text and "private.txt" not in response.text
    assert await files.get(file_id) == before


@pytest.mark.parametrize(("source_present", "expected_status"), [(False, 409), (True, 200)])
async def test_reparse_source_missing_checks_restored_original(production_ctx, tmp_path, monkeypatch, source_present, expected_status) -> None:
    client, files = production_ctx
    artifacts = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await artifacts.initialize()
    art = await artifacts.put_bytes(content=b"source", filename="private.txt", mime="text/plain", created_by=UID)
    (file_id,) = await _seed(files, 1, artifact_id=art.id)
    await files.update(file_id, parse_status="failed", parse_error="source_missing: original unavailable")
    client.app.state.artifact_store = artifacts
    worker = SimpleNamespace(parse_and_index=AsyncMock())
    monkeypatch.setattr(kb, "_get_effective_worker", AsyncMock(return_value=worker))
    if not source_present:
        monkeypatch.setattr(artifacts.backend, "head", AsyncMock(return_value=None))

    response = client.post(f"/api/v1/kb/files/{file_id}/reparse", headers={"X-Space-Id": WS})

    assert response.status_code == expected_status, response.text
    if source_present:
        assert response.json()["parse_status"] == "queued"
        worker.parse_and_index.assert_awaited_once_with(file_id)
    else:
        assert response.json()["detail"] == {"error": {"code": "kb_source_missing"}}
        assert (await files.get(file_id)).parse_status == "failed"
        worker.parse_and_index.assert_not_awaited()


async def test_reparse_source_missing_rechecks_permission_after_probe(production_ctx, tmp_path, monkeypatch) -> None:
    client, files = production_ctx
    artifacts = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await artifacts.initialize()
    art = await artifacts.put_bytes(content=b"source", filename="private.txt", mime="text/plain", created_by=UID)
    (file_id,) = await _seed(files, 1, artifact_id=art.id, scope="space", scope_id=WS)
    await files.update(file_id, parse_status="failed", parse_error="source_missing: original unavailable")
    client.app.state.artifact_store = artifacts
    original_head = artifacts.backend.head

    async def revoke_during_probe(path):
        monkeypatch.setattr(client.app.state.user_store, "get_member_role", AsyncMock(return_value=None))
        return await original_head(path)

    monkeypatch.setattr(artifacts.backend, "head", revoke_during_probe)
    monkeypatch.setattr(kb, "_get_effective_worker", AsyncMock(side_effect=AssertionError("worker must not start")))
    response = client.post(f"/api/v1/kb/files/{file_id}/reparse", headers={"X-Space-Id": WS})
    assert response.status_code == 403, response.text
    assert (await files.get(file_id)).parse_status == "failed"


async def test_reparse_restored_source_clears_artifact_missing_status(production_ctx, tmp_path, monkeypatch) -> None:
    client, files = production_ctx
    artifacts = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await artifacts.initialize()
    art = await artifacts.put_bytes(content=b"source", filename="private.txt", mime="text/plain", created_by=UID)
    assert await artifacts.set_blob_missing([art.id], missing=True) == 1
    (file_id,) = await _seed(files, 1, artifact_id=art.id)
    await files.update(file_id, parse_status="failed", parse_error="source_missing: original unavailable")
    client.app.state.artifact_store = artifacts
    worker = SimpleNamespace(parse_and_index=AsyncMock())
    monkeypatch.setattr(kb, "_get_effective_worker", AsyncMock(return_value=worker))

    response = client.post(f"/api/v1/kb/files/{file_id}/reparse", headers={"X-Space-Id": WS})

    assert response.status_code == 200, response.text
    assert (await artifacts.get(art.id)).status == "ready"
    worker.parse_and_index.assert_awaited_once_with(file_id)


async def test_reparse_requires_kb_license(production_ctx, monkeypatch) -> None:
    client, files = production_ctx
    (file_id,) = await _seed(files, 1)
    await files.update(file_id, parse_status="failed", parse_error="parse_failed: invalid content")
    client.app.state.entitlements = SimpleNamespace(
        assert_can_run_turn=AsyncMock(return_value=None),
        is_feature_licensed=AsyncMock(return_value=False),
    )
    monkeypatch.setattr("bowhead_gateway._entitlement_gate._resolve_team_id", AsyncMock(return_value=WS))

    response = client.post(f"/api/v1/kb/files/{file_id}/reparse", headers={"X-Space-Id": WS})

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"]["code"] == "feature_unlicensed"
    assert (await files.get(file_id)).parse_status == "failed"


@pytest.mark.parametrize(("change", "status"), [("delete", 404), ("revoke", 403), ("version", 409)])
async def test_health_rechecks_changes_after_probe(production_ctx, tmp_path, monkeypatch, change, status) -> None:
    client, files = production_ctx
    artifacts = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await artifacts.initialize()
    art = await artifacts.put_bytes(content=b"source", filename="private.txt", mime="text/plain", created_by=UID)
    (file_id,) = await _seed(files, 1, artifact_id=art.id, scope="space", scope_id=WS)
    client.app.state.artifact_store = artifacts
    original_head = artifacts.backend.head

    async def delete_during_read(path):
        if change == "delete":
            await files.soft_delete(file_id)
        elif change == "version":
            await files.update(file_id, version=2)
        else:
            monkeypatch.setattr(client.app.state.user_store, "get_member_role", AsyncMock(return_value=None))
        return await original_head(path)

    monkeypatch.setattr(artifacts.backend, "head", delete_during_read)
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    response = client.get(f"/api/v1/kb/files/{file_id}/health", headers={"X-Space-Id": WS})
    assert response.status_code == status, response.text
    assert "source_health" not in response.text


@pytest.mark.parametrize("cached_version", [None, 1, 2])
async def test_health_reads_only_current_team_runtime(production_ctx, tmp_path, monkeypatch, cached_version) -> None:
    client, files = production_ctx
    (file_id,) = await _seed(files, 1, initial_parse_status="done")
    client.app.state.artifact_store = Mock(get=AsyncMock(return_value=None))
    default_index = Mock(inspect_active_index=AsyncMock(side_effect=AssertionError("must not fall back")))
    client.app.state.kb_store = default_index
    registry = TeamKbRuntimeRegistry(
        persist_dir=str(tmp_path / "unused"),
        files_store=files,
        artifact_store=None,
        router=None,
        team_router_registry=None,
        doc_type_store=None,
        extracted_record_store=None,
        extracted_rows_store=None,
        per_row_jobs_store=None,
        row_extractor=None,
        active_per_row_tasks_ref=None,
    )
    team_index = Mock(inspect_active_index=AsyncMock(return_value=IndexObservation(reason="legacy_chunks_readable")))
    if cached_version is not None:
        registry._cache[WS] = SimpleNamespace(version=cached_version, kb_store=team_index)
    client.app.state.team_kb_runtime_registry = registry
    monkeypatch.setattr(client.app.state.user_store, "get_team_llm_config_with_version", AsyncMock(return_value=({}, 2)), raising=False)
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    response = client.get(f"/api/v1/kb/files/{file_id}/health")
    assert response.status_code == 200, response.text
    expected = "legacy_chunks_readable" if cached_version == 2 else "not_checked"
    assert response.json()["index"]["reason"] == expected
    assert team_index.inspect_active_index.call_count == (1 if cached_version == 2 else 0)
    default_index.inspect_active_index.assert_not_called()


async def test_exact_file_filter_precedes_pagination_and_preserves_authorization(ctx, monkeypatch) -> None:
    client, store = ctx
    wanted = (await _seed(store, 1, filename="wanted.pdf"))[0]
    hidden = (await _seed(store, 1, filename="hidden.pdf"))[0]
    foreign = (await _seed(store, 1, scope_id="u_other", uploaded_by="u_other"))[0]
    deleted = (await _seed(store, 1, filename="deleted.pdf"))[0]
    await store.soft_delete(deleted)
    await _seed(store, 3, filename="newer.pdf")
    original_filter = kb._filter_kb_files_by_file_grants

    async def filter_grants(request, context, rows, **kwargs):
        allowed = await original_filter(request, context, rows, **kwargs)
        return [row for row in allowed if row.file_id != hidden]

    monkeypatch.setattr(kb, "_filter_kb_files_by_file_grants", filter_grants)
    response = client.get(
        "/api/v1/kb/files",
        params=[("scope", "personal"), ("limit", "1"), *[("file_ids", fid) for fid in (wanted, hidden, foreign, deleted)]],
    )
    assert response.status_code == 200, response.text
    assert [row["file_id"] for row in response.json()["files"]] == [wanted]
    assert response.json()["total"] == 1
    assert response.json()["has_more"] is False


async def test_exact_file_filter_has_a_bounded_request(ctx) -> None:
    client, _store = ctx
    response = client.get("/api/v1/kb/files", params=[("file_ids", f"kbf_{i}") for i in range(101)])
    assert response.status_code == 422
