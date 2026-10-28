"""kb-security-quickfix S5 (P2): team-scope files must be bindable to a doc_type
in the caller's current workspace.

Bug: ``_bind_doc_type_core`` validated ``dt.workspace_id != file.workspace_id``.
team-scope files store ``workspace_id=""`` so this never equalled a real doc_type
ws → team-scope files could be bound at upload (which checks ``ctx.workspace_id``)
but got a permanent 403 on any later PATCH/bulk rebind. Fix aligns the bind check
with the upload path (``ctx.workspace_id``). Cross-ws doc_types stay blocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_acme"
WS = "ws_home"
OTHER_WS = "ws_other"
UID = "u_admin"


def _user() -> User:
    # team_admin (via BOWHEAD_AUTO_ADMIN_DOMAINS) so can_delete passes for team-scope files.
    return User(id=UID, email="admin@acme.test", team_id=TEAM, tier="ultra", email_verified=True)


@pytest.fixture
async def ctx(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "acme.test")
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    dts = KbDocTypeStore(tmp_path / "doc_types.db")
    await dts.initialize()
    app = FastAPI()
    app.include_router(kb.router)
    app.dependency_overrides[_current_user] = _user
    app.state.kb_files_store = files
    app.state.kb_doc_type_store = dts
    return TestClient(app, raise_server_exceptions=False), files, dts


async def _seed_team_file(files: KbFilesStore) -> str:
    # team-scope file: scope_id=team, workspace_id="" (how team files are stored).
    return await files.create(
        artifact_id="art_o",
        filename="contract.pdf",
        mime_type="application/pdf",
        size_bytes=1234,
        scope="team",
        scope_id=TEAM,
        workspace_id="",
        team_id=TEAM,
        uploaded_by=UID,
    )


async def test_team_file_can_bind_doc_type_in_current_ws(ctx) -> None:
    client, files, dts = ctx
    fid = await _seed_team_file(files)
    dt = await dts.create(workspace_id=WS, name="合同", created_by=UID)
    r = client.patch(
        f"/api/v1/kb/files/{fid}/doc_type",
        params={},
        json={"doc_type_id": dt},
        headers={"X-Space-Id": WS},
    )
    assert r.status_code == 200, r.text  # was 403 over-block before the fix
    assert r.json()["doc_type_id"] == dt


async def test_team_file_cannot_bind_cross_ws_doc_type(ctx) -> None:
    client, files, dts = ctx
    fid = await _seed_team_file(files)
    # doc_type lives in OTHER_WS, but caller's current ws is WS → still blocked.
    dt_other = await dts.create(workspace_id=OTHER_WS, name="回单", created_by=UID)
    r = client.patch(
        f"/api/v1/kb/files/{fid}/doc_type",
        params={},
        json={"doc_type_id": dt_other},
        headers={"X-Space-Id": WS},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"]["code"] == "doc_type_not_found"
