from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore
from bowhead_core.kb.per_row_jobs_store import KbPerRowJobsStore
from bowhead_core.kb.schemas import KbSearchResult
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import kb, kb_admin, kb_doc_types, kb_presign, kb_records
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Retriever:
    async def invoke(self, **_kwargs) -> KbSearchResult:
        return KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=1,
        )


@pytest.fixture
async def lifecycle_ctx(initialize_migrated_kb_files_database, tmp_path):
    users = UserStore(tmp_path / "bowhead.db")
    await users.initialize()
    team = await users.create_team(name="KB Lifecycle", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="kb-lifecycle@example.com",
        password="Passw0rd!",
    )
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Archived KB",
        creator_user_id=user.id,
    )

    doc_types = KbDocTypeStore(tmp_path / "kb_doc_types.db")
    await doc_types.initialize()
    records = KbExtractedRecordStore(tmp_path / "kb_records.db")
    await records.initialize()
    markers = KbLifespanMarkerStore(db_path=tmp_path / "kb_doc_types.db")
    await markers.initialize()
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    per_row_jobs = KbPerRowJobsStore(tmp_path / "kb_per_row_jobs.db")
    await per_row_jobs.initialize()
    doc_type_id = await doc_types.create(
        workspace_id=workspace_id,
        name="Contracts",
        created_by=user.id,
    )
    record_id = await records.create(
        workspace_id=workspace_id,
        doc_type_id=doc_type_id,
        file_id="kf_contract",
        fields={},
        verify_status="ready",
        created_by=user.id,
    )
    file_id = await files.create(
        artifact_id="art_contract",
        filename="contract.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        scope="space",
        scope_id=workspace_id,
        workspace_id=workspace_id,
        team_id=team.id,
        uploaded_by=user.id,
    )
    async with aiosqlite.connect(users.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    app = FastAPI()
    app.include_router(kb.router)
    app.include_router(kb_admin.router)
    app.include_router(kb_doc_types.router)
    app.include_router(kb_presign.router)
    app.include_router(kb_records.router)
    app.dependency_overrides[_current_user] = lambda: user
    app.state.user_store = users
    app.state.kb_doc_type_store = doc_types
    app.state.kb_extracted_record_store = records
    app.state.kb_marker_store = markers
    app.state.kb_files_store = files
    app.state.kb_per_row_jobs_store = per_row_jobs
    app.state.kb_retriever = _Retriever()
    app.state.kb_backfill_active_ws = {}
    app.state.kb_backfill_tasks_by_id = {}
    app.state.kb_backfill_status = {
        "bf_archived": kb_admin.BackfillStatus(
            task_id="bf_archived",
            workspace_id=workspace_id,
            state="completed",
            requested_by=user.id,
            requested_at="2026-07-29T00:00:00+00:00",
            completed_at="2026-07-29T00:01:00+00:00",
        ),
    }
    client = TestClient(app, raise_server_exceptions=False)
    return (
        client,
        doc_types,
        records,
        files,
        workspace_id,
        doc_type_id,
        record_id,
        file_id,
    )


async def test_archived_kb_history_reads_are_side_effect_free(
    lifecycle_ctx,
) -> None:
    (
        client,
        doc_types,
        records,
        files,
        workspace_id,
        doc_type_id,
        record_id,
        file_id,
    ) = lifecycle_ctx
    headers = {"X-Space-Id": workspace_id}

    listed = client.get(
        "/api/v1/kb/doc_types",
        params={"workspace_id": workspace_id},
    )
    record_list = client.get(
        f"/api/v1/kb/doc_types/{doc_type_id}/records",
        params={"workspace_id": workspace_id},
    )
    record = client.get(
        f"/api/v1/kb/records/{record_id}",
        params={"workspace_id": workspace_id},
    )
    file_list = client.get(
        "/api/v1/kb/files",
        params={"scope": "space"},
        headers=headers,
    )
    file_detail = client.get(
        f"/api/v1/kb/files/{file_id}",
        headers=headers,
    )
    search = client.post(
        "/api/v1/kb/search",
        json={"query": "contract"},
        headers=headers,
    )
    backfill_status = client.get(
        "/api/v1/admin/kb/files/backfill/bf_archived",
        headers=headers,
    )
    extract_status = client.get(
        f"/api/v1/kb/files/{file_id}/extract_rows_status",
        headers=headers,
    )
    extract_status_batch = client.get(
        "/api/v1/kb/files/_batch/extract_rows_status",
        params={"file_ids": file_id},
        headers=headers,
    )
    llm_cost_summary = client.get(
        "/api/v1/kb/admin/llm_cost_summary",
        headers=headers,
    )

    assert listed.status_code == 200
    assert [item["doc_type_id"] for item in listed.json()["items"]] == [doc_type_id]
    assert record_list.status_code == 200
    assert record.status_code == 200
    assert file_list.status_code == 200
    assert file_list.json()["total"] == 1
    assert file_detail.status_code == 200
    assert search.status_code == 200
    assert backfill_status.status_code == 200
    assert backfill_status.json()["state"] == "completed"
    assert extract_status.status_code == 200
    assert extract_status.json()["status"] == "na"
    assert extract_status_batch.status_code == 200
    assert extract_status_batch.json() == {"jobs": {}}
    assert llm_cost_summary.status_code == 200
    assert llm_cost_summary.json()["workspace_id"] == workspace_id
    assert len(await doc_types.list_for_ws(workspace_id)) == 1
    assert (await records.get(record_id)).verify_status == "ready"
    assert (await files.get(file_id)).soft_deleted_at is None


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "post",
            "/api/v1/kb/doc_types/seed",
            {"workspace_id": "{workspace_id}"},
        ),
        (
            "post",
            "/api/v1/kb/doc_types",
            {"workspace_id": "{workspace_id}", "name": "New Type"},
        ),
        (
            "post",
            "/api/v1/kb/doc_types/draft",
            {"workspace_id": "{workspace_id}", "user_query": "extract amount"},
        ),
        (
            "put",
            "/api/v1/kb/doc_types/{doc_type_id}",
            {"name": "Renamed"},
        ),
        (
            "delete",
            "/api/v1/kb/doc_types/{doc_type_id}",
            None,
        ),
        (
            "patch",
            "/api/v1/kb/records/{record_id}/field?workspace_id={workspace_id}",
            {"field_name": "amount", "new_value": "100"},
        ),
        (
            "patch",
            "/api/v1/kb/records/{record_id}/verify_status?workspace_id={workspace_id}",
            {"status": "approved"},
        ),
        (
            "delete",
            "/api/v1/kb/files/{file_id}",
            None,
        ),
        (
            "post",
            "/api/v1/kb/files/{file_id}/reparse",
            None,
        ),
        (
            "post",
            "/api/v1/admin/kb/files/backfill",
            {"workspace_id": "{workspace_id}"},
        ),
        (
            "post",
            "/api/v1/admin/kb/files/backfill/bf_archived/cancel",
            None,
        ),
        (
            "post",
            "/api/v1/kb/files/{file_id}/extract_rows",
            {"force": False},
        ),
        (
            "post",
            "/api/v1/kb/presign",
            {
                "filename": "archived.pdf",
                "mime": "application/pdf",
                "size_bytes": 100,
                "scope": "space",
                "scope_id": "{workspace_id}",
            },
        ),
        (
            "post",
            "/api/v1/kb/presign/{file_id}/confirm",
            {},
        ),
        (
            "post",
            "/api/v1/kb/presign/bundle",
            {
                "filename": "archived.zip",
                "mime": "application/zip",
                "size_bytes": 100,
                "scope": "space",
                "scope_id": "{workspace_id}",
            },
        ),
        (
            "post",
            "/api/v1/kb/presign/bundle/{file_id}/confirm",
            {},
        ),
    ],
)
async def test_archived_kb_mutations_and_execution_are_rejected_before_write(
    lifecycle_ctx,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    (
        client,
        doc_types,
        records,
        files,
        workspace_id,
        doc_type_id,
        record_id,
        file_id,
    ) = lifecycle_ctx
    values = {
        "workspace_id": workspace_id,
        "doc_type_id": doc_type_id,
        "record_id": record_id,
        "file_id": file_id,
    }
    target = path.format(**values)
    body = {key: value.format(**values) if isinstance(value, str) else value for key, value in payload.items()} if payload is not None else None

    response = client.request(
        method.upper(),
        target,
        json=body,
        headers={"X-Space-Id": workspace_id},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"
    persisted = await doc_types.get(doc_type_id)
    assert persisted is not None
    assert persisted.name == "Contracts"
    record = await records.get(record_id)
    assert record is not None
    assert record.verify_status == "ready"
    assert "amount" not in record.edited_by_user
    file = await files.get(file_id)
    assert file is not None
    assert file.soft_deleted_at is None
    assert file.parse_status == "pending"
