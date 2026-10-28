"""kb-matrix M4 wave-2: doc_types 的显式 seed write.

boot seed 只扫「有文件的 ws」（list_distinct_workspaces）——新建/无文件空间到
下次重启才有内置模板卡。前端在 list 前 best-effort 调显式 seed（marker 幂等）：
- 首次 seed 后见 5 张 kind=seed；再 seed 不重复。
- marker store 缺失：静默跳过（不 500）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore
from bowhead_core.kb.template_seed import TEMPLATE_SEEDS
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_doc_types as doc_type_routes
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_acme"
WS = "ws_fresh"  # Fileless space (missed by boot seed).
UID = "u_admin"


def _user() -> User:
    return User(id=UID, email="admin@acme.test", team_id=TEAM, tier="ultra", email_verified=True)


class _MembershipStore:
    """RBAC hooks 全须齐——seed 是写操作，has_workspace_access_source 缺失即不种
    （安全 review P3：写路不继承 legacy fail-open）。"""

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, ws_id: str) -> str:
        return TEAM

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        return "member" if (ws_id, user_id) == (WS, UID) else None

    async def get_workspace(self, ws_id: str) -> dict | None:
        return {"id": WS, "status": "active"} if ws_id == WS else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return None

    async def is_team_admin(self, user_id: str, team_id: str | None = None) -> bool:
        return False


@pytest.fixture
async def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "acme.test")
    dts = KbDocTypeStore(tmp_path / "doc_types.db")
    await dts.initialize()
    markers = KbLifespanMarkerStore(db_path=tmp_path / "doc_types.db")
    await markers.initialize()
    app = FastAPI()
    app.include_router(doc_type_routes.router)
    app.dependency_overrides[_current_user] = _user
    app.state.kb_doc_type_store = dts
    app.state.kb_marker_store = markers
    app.state.user_store = _MembershipStore()
    return TestClient(app, raise_server_exceptions=False), dts, markers, app


def _list(client: TestClient) -> list[dict]:
    r = client.get("/api/v1/kb/doc_types", params={"workspace_id": WS})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _seed(client: TestClient) -> dict:
    r = client.post("/api/v1/kb/doc_types/seed", json={"workspace_id": WS})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.anyio
async def test_explicit_seed_before_list(ctx) -> None:
    client, _dts, _markers, _app = ctx
    assert [d for d in _list(client) if d["kind"] == "seed"] == []
    assert _seed(client)["seeded"] is True
    items = _list(client)
    seeds = [d for d in items if d["kind"] == "seed"]
    assert len(seeds) == len(TEMPLATE_SEEDS) == 5
    # Frozen ecosystem invariant: seed always auto=off + many (no change to any space upload/classification behavior).
    assert all(d["auto_extract_enabled"] is False and d["cardinality"] == "many" for d in seeds)
    # Idempotent: reseeding creates no duplicates.
    _seed(client)
    again = [d for d in _list(client) if d["kind"] == "seed"]
    assert len(again) == 5


@pytest.mark.anyio
async def test_missing_marker_store_is_silent(ctx) -> None:
    client, _dts, _markers, app = ctx
    del app.state.kb_marker_store
    _seed(client)
    assert [d for d in _list(client) if d["kind"] == "seed"] == []  # Neither 500 nor seed.


@pytest.mark.anyio
async def test_no_rbac_hooks_no_seed(ctx) -> None:
    """写路不继承 legacy fail-open：user_store hooks 缺失 → 读照常、seed 跳过。"""
    client, _dts, _markers, app = ctx
    del app.state.user_store
    _seed(client)
    assert [d for d in _list(client) if d["kind"] == "seed"] == []


@pytest.mark.anyio
async def test_list_reports_seed_state_without_writing(ctx) -> None:
    """列表只报告是否已播种（前端据此只在首次调一次 /seed）；列表本身不写。"""
    client, _dts, _markers, _app = ctx

    def _get(**params: object) -> dict:
        r = client.get("/api/v1/kb/doc_types", params={"workspace_id": WS, **params})
        assert r.status_code == 200, r.text
        return r.json()

    before = _get()
    assert before["templates_seeded"] is False
    assert _get(include_stats=1)["templates_seeded"] is False
    assert [d for d in _get()["items"] if d["kind"] == "seed"] == []  # GET does not write.

    _seed(client)
    assert _get()["templates_seeded"] is True
    assert _get(include_stats=1)["templates_seeded"] is True
