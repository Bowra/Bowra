"""kb-security-quickfix S2 (P1): destructive doc_type ops (PUT/DELETE) require
ws_admin OR the doc_type's creator — not any workspace member.

Before this fix, ``_require_workspace_member`` only checked membership, so any
member (including a viewer) could ``DELETE`` a doc_type — which cascades into
bound files' extraction — or ``PUT`` a new schema. This mirrors
``KbPermission.can_delete`` for files: owner (creator) or admin only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_doc_types
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_x"
WS = "ws_x"
CREATOR = "u_creator"
MEMBER = "u_member"
ADMIN = "u_admin"


class _MembershipStore:
    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, ws_id: str) -> str:
        return TEAM

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        if ws_id != WS:
            return None
        return {"u_admin": "ws_admin", "u_creator": "member", "u_member": "member"}.get(user_id)

    async def get_workspace(self, ws_id: str) -> dict | None:
        return {"id": WS, "status": "active"} if ws_id == WS else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return None

    async def is_team_admin(self, user_id: str) -> bool:
        return False


def _client_for(store: KbDocTypeStore, uid: str) -> TestClient:
    app = FastAPI()
    app.include_router(kb_doc_types.router)
    app.dependency_overrides[_current_user] = lambda: User(id=uid, email=f"{uid}@test.com", team_id=TEAM, tier="ultra", email_verified=True)
    app.state.kb_doc_type_store = store
    app.state.user_store = _MembershipStore()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
async def store(tmp_path: Path):
    s = KbDocTypeStore(tmp_path / "doc_types.db")
    await s.initialize()
    return s


async def _seed(store: KbDocTypeStore) -> str:
    return await store.create(workspace_id=WS, name="合同", created_by=CREATOR)


async def test_plain_member_cannot_delete(store) -> None:
    dt = await _seed(store)
    r = _client_for(store, MEMBER).delete(f"/api/v1/kb/doc_types/{dt}", params={"workspace_id": WS})
    assert r.status_code == 403, r.text
    assert await store.get(dt) is not None  # not deleted


async def test_plain_member_cannot_update(store) -> None:
    dt = await _seed(store)
    r = _client_for(store, MEMBER).put(
        f"/api/v1/kb/doc_types/{dt}",
        params={"workspace_id": WS},
        json={"name": "改名", "description": "", "fields": None},
    )
    assert r.status_code == 403, r.text


async def test_creator_can_delete(store) -> None:
    dt = await _seed(store)
    r = _client_for(store, CREATOR).delete(f"/api/v1/kb/doc_types/{dt}", params={"workspace_id": WS})
    assert r.status_code == 200, r.text
    assert await store.get(dt) is None


async def test_admin_can_delete(store) -> None:
    dt = await _seed(store)
    r = _client_for(store, ADMIN).delete(f"/api/v1/kb/doc_types/{dt}", params={"workspace_id": WS})
    assert r.status_code == 200, r.text


async def test_member_can_still_create(store) -> None:
    """CREATE stays member-allowed (additive, not destructive)."""
    r = _client_for(store, MEMBER).post(
        "/api/v1/kb/doc_types",
        json={"workspace_id": WS, "name": "新表", "description": "", "fields": []},
    )
    assert r.status_code == 200, r.text
