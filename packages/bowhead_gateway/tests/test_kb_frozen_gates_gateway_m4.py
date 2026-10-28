"""kb-matrix M4 决策 12: 冻结生态 gate 的 gateway 半边。

- gate①：bind graduated/template/seed → 422 doc_type_not_bindable（bind_scope fixture 范式）。
- gate③：admin 重抽对 auto_extract=off → 422 doc_type_extract_disabled（service 单元）。
- gate⑤：PUT graduated fields → 422 graduated_fields_frozen（仅名称/描述可改）；
  rename 撞名 → 409（顺修 pre-existing 500）。
- DELETE graduated → rows 级联软删 + cascaded_rows 计数。
- gate④：删文件级联豁免 graduated 行（kb.py 路由层组合）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb
from bowhead_gateway.routes import kb_doc_types as doc_type_routes
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.kb_extract_service import trigger_row_extraction
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_acme"
WS = "ws_main"
UID = "u_admin"


def _user() -> User:
    return User(id=UID, email="admin@acme.test", team_id=TEAM, tier="ultra", email_verified=True)


@pytest.fixture
async def ctx(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "acme.test")
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    dts = KbDocTypeStore(tmp_path / "doc_types.db")
    await dts.initialize()
    rows = KbExtractedRowsStore(db_path=tmp_path / "rows.db")
    await rows.initialize()
    app = FastAPI()
    app.include_router(kb.router)
    app.include_router(doc_type_routes.router)
    app.dependency_overrides[_current_user] = _user
    app.state.kb_files_store = files
    app.state.kb_doc_type_store = dts
    app.state.kb_extracted_rows_store = rows

    # File deletion first soft-deletes chroma chunks; stub it (test concerns only rows cascade exemption).
    class _StubKbStore:
        async def soft_delete_file(self, file) -> None:  # noqa: ANN001
            return None

    app.state.kb_store = _StubKbStore()
    return TestClient(app, raise_server_exceptions=False), files, dts, rows


async def _seed_ws_file(files: KbFilesStore, art: str = "art_1") -> str:
    return await files.create(
        artifact_id=art,
        filename=f"{art}.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        scope="workspace",
        scope_id=WS,
        workspace_id=WS,
        team_id=TEAM,
        uploaded_by=UID,
    )


async def _seed_graduated(dts: KbDocTypeStore, name: str = "转正快照") -> str:
    return await dts.create(
        workspace_id=WS,
        name=name,
        created_by=UID,
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx_1",
        graduated_from_seq=1,
        graduated_mode="snapshot",
    )


# ─── gate①: bind rejects column-group templates ─────────────


async def test_bind_graduated_422(ctx) -> None:
    client, files, dts, _rows = ctx
    fid = await _seed_ws_file(files)
    dt_g = await _seed_graduated(dts)
    r = client.patch(
        f"/api/v1/kb/files/{fid}/doc_type",
        json={"doc_type_id": dt_g},
        headers={"X-Space-Id": WS},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"]["code"] == "doc_type_not_bindable"
    # Ordinary doc_type still bindable (precise gate, no collateral rejection).
    dt_ok = await dts.create(workspace_id=WS, name="普通类型", created_by=UID)
    r2 = client.patch(
        f"/api/v1/kb/files/{fid}/doc_type",
        json={"doc_type_id": dt_ok},
        headers={"X-Space-Id": WS},
    )
    assert r2.status_code == 200, r2.text


# ─── gate③: re-extraction entry rejects (service unit test) ──


@pytest.mark.anyio
async def test_trigger_row_extraction_refuses_disabled(tmp_path) -> None:
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    dt_g = await _seed_graduated(dts)

    class _File:
        file_id = "f1"
        doc_type_id = dt_g
        auto_classified_doc_type_id = None

    res = await trigger_row_extraction(
        file=_File(),
        ws_id=WS,
        force=True,  # force invokes soft_delete_for_attempt that removes snapshots.
        rows_store=None,
        jobs_store=None,
        user_store=None,
        worker=None,
        active_tasks=None,
        actor_user_id=UID,
        dt_store=dts,
    )
    assert res["status"] == "extract_disabled"


# ─── gate⑤: graduated fields read-only + rename 409 ─────────


async def test_put_graduated_fields_frozen_422_but_rename_ok(ctx) -> None:
    client, _files, dts, _rows = ctx
    dt_g = await _seed_graduated(dts)
    # Edit fields → 422.
    r = client.put(
        f"/api/v1/kb/doc_types/{dt_g}",
        json={"fields": [{"name": "新列", "type": "string"}]},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"]["code"] == "graduated_fields_frozen"
    # Rename/description → 200 (only fields frozen).
    r2 = client.put(f"/api/v1/kb/doc_types/{dt_g}", json={"name": "改个名", "description": "新描述"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "改个名"


async def test_put_rename_conflict_409(ctx) -> None:
    client, _files, dts, _rows = ctx
    await dts.create(workspace_id=WS, name="占位", created_by=UID)
    dt2 = await dts.create(workspace_id=WS, name="待改名", created_by=UID)
    r = client.put(f"/api/v1/kb/doc_types/{dt2}", json={"name": "占位"})
    assert r.status_code == 409, r.text  # Previously uncaught IntegrityError = 500.
    assert r.json()["detail"]["error"]["code"] == "doc_type_name_taken"


# ─── DELETE graduated: rows cascade ─────────────────────────


async def test_delete_graduated_cascades_rows(ctx) -> None:
    client, _files, dts, rows = ctx
    dt_g = await _seed_graduated(dts)
    await rows.create_many_prewrapped(
        [
            {
                "file_id": "f1",
                "fields_v2": {"金额": [{"is_primary": True, "normalized": {"amount": "1", "currency": None}}]},
                "edited_fields": [],
                "page": None,
            }
        ],
        doc_type_id=dt_g,
        workspace_id=WS,
        created_by=UID,
    )
    r = client.delete(f"/api/v1/kb/doc_types/{dt_g}")
    assert r.status_code == 200, r.text
    assert r.json()["cascaded_rows"] == 1
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id=dt_g) == 0


# ─── gate④: file-deletion cascade exemption (route composition) ──


async def test_delete_file_exempts_graduated_rows(ctx) -> None:
    client, files, dts, rows = ctx
    fid = await _seed_ws_file(files, art="art_del")
    dt_g = await _seed_graduated(dts, name="转正快照2")
    dt_live = await dts.create(workspace_id=WS, name="普通行类型", created_by=UID, cardinality="many")
    entry = {"fields_v2": {"金额": [{"is_primary": True, "normalized": {"amount": "1", "currency": None}}]}, "edited_fields": [], "page": None}
    await rows.create_many_prewrapped([{**entry, "file_id": fid}], doc_type_id=dt_g, workspace_id=WS, created_by=UID)
    await rows.create_many_prewrapped([{**entry, "file_id": fid}], doc_type_id=dt_live, workspace_id=WS, created_by=UID)

    r = client.delete(f"/api/v1/kb/files/{fid}", headers={"X-Space-Id": WS})
    assert r.status_code in (200, 204), r.text
    # Retain graduated rows (snapshots unaffected by file deletion); delete ordinary rows as before (privacy cascade preserved).
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id=dt_g) == 1
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id=dt_live) == 0
