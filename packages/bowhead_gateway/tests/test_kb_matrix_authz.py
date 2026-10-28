"""kb-matrix M2 验收 5 安全断言（test_kb_records_authz 范式 + owner-or-admin 用例）。

覆盖：非成员 4xx / 跨空间 matrix_id 404 / 跨空间 doc_type_id 404（from_template，
review P0）/ 跨空间 file_id 404 / personal 文件 422 / viewer 全 mutation 403 +
snapshot 200 / **member 非创建者删矩阵·删列·取消他人 run → 403**（owner-or-admin，
不能只测非成员）/ team_admin 非成员 4xx / flag off 404 / strict 无 hooks 503。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_engine import KbMatrixEngine
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.kb.structured_extractor import StructuredExtractor
from bowhead_core.kb.workspace_quotas_store import KbWorkspaceQuotasStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import kb_matrix
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_shared"
WS_A = "ws_alpha"
WS_B = "ws_beta"

U_MEMBER = "u_member"  # WS_A member (matrix creator).
U_MEMBER2 = "u_member2"  # WS_A member, not creator.
U_VIEWER = "u_viewer"  # WS_A viewer
U_OUTSIDER = "u_outsider"  # WS_B member only.
U_TEAM_ADMIN = "u_team_admin"  # Team admin, member of no space.


def _mk_user(uid: str) -> User:
    return User(id=uid, email=f"{uid}@test.com", team_id=TEAM, tier="ultra", email_verified=True)


class _MembershipStore:
    ROLES = {
        (WS_A, U_MEMBER): "member",
        (WS_A, U_MEMBER2): "member",
        (WS_A, U_VIEWER): "viewer",
        (WS_B, U_OUTSIDER): "member",
    }

    def __init__(self) -> None:
        self.workspace_statuses = {
            WS_A: "active",
            WS_B: "active",
        }

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, ws_id: str) -> str:
        return TEAM

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        return self.ROLES.get((ws_id, user_id))

    async def get_workspace(self, ws_id: str) -> dict[str, str] | None:
        status = self.workspace_statuses.get(ws_id)
        return {"id": ws_id, "status": status} if status is not None else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "team_admin" if user_id == U_TEAM_ADMIN else None

    async def is_team_admin(self, user_id: str) -> bool:
        return user_id == U_TEAM_ADMIN


class _FakeKbStore:
    async def get_ordered_chunks_for_file(self, file: Any) -> list[tuple[str, str, int]]:
        return [("chk_1", "文本", 1)]


class _FakeRouter:
    def resolve(self, agent_type: str, profile: Any = None, user_pref: Any = None) -> Any:
        class _B:
            primary = ("prov_a", "model_a")

        return _B()

    async def invoke(self, agent_type: str, req: Any, profile: Any = None, user_pref: Any = None) -> Any:
        class _R:
            content = "{}"

        return _R()


@pytest.fixture(autouse=True)
def _single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


@pytest.fixture
async def ctx(initialize_migrated_kb_files_database, tmp_path: Path):
    matrix_store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await matrix_store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    engine_user_store = UserStore(str(tmp_path / "engine_users.db"))
    await engine_user_store.initialize()
    quotas = KbWorkspaceQuotasStore(tmp_path / "quotas.db")
    await quotas.initialize()
    fake_router = _FakeRouter()
    engine = KbMatrixEngine(
        matrix_store=matrix_store,
        files_store=files_store,
        kb_store=_FakeKbStore(),  # type: ignore[arg-type]
        extractor=StructuredExtractor(router=fake_router),
        router=fake_router,
        user_store=engine_user_store,
        quotas_store=quotas,
    )

    app = FastAPI()
    app.include_router(kb_matrix.router)
    app.state.kb_matrix_store = matrix_store
    app.state.kb_matrix_engine = engine
    app.state.kb_files_store = files_store
    membership_store = _MembershipStore()
    app.state.user_store = membership_store
    client = TestClient(app, raise_server_exceptions=False)

    # Seed: one WS_A matrix (creator U_MEMBER) + WS_A/WS_B/personal files.
    file_a = await files_store.create(
        artifact_id="a",
        filename="a.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="workspace",
        scope_id=WS_A,
        workspace_id=WS_A,
        team_id=TEAM,
        uploaded_by=U_MEMBER,
    )
    file_b = await files_store.create(
        artifact_id="b",
        filename="b.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="workspace",
        scope_id=WS_B,
        workspace_id=WS_B,
        team_id=TEAM,
        uploaded_by=U_OUTSIDER,
    )
    file_personal = await files_store.create(
        artifact_id="p",
        filename="p.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="personal",
        scope_id=U_MEMBER,
        workspace_id=WS_A,
        team_id=TEAM,
        uploaded_by=U_MEMBER,
    )
    matrix = await matrix_store.create_matrix(
        name="测试矩阵",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": file_a, "char_count": 100}],
        columns=[{"name": "列A", "type": "question", "question": "a?"}],
    )
    return {
        "client": client,
        "app": app,
        "store": matrix_store,
        "files_store": files_store,
        "matrix": matrix,
        "file_a": file_a,
        "file_b": file_b,
        "file_personal": file_personal,
        "membership_store": membership_store,
    }


def _as(app: FastAPI, uid: str) -> None:
    app.dependency_overrides[_current_user] = lambda: _mk_user(uid)


def _h(ws: str) -> dict[str, str]:
    return {"X-Space-Id": ws}


def _assert_code_only(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == {"code": code}


# ─── Strict baseline ───────────────────────────────────────


async def test_strict_no_hooks_503(ctx) -> None:
    """§7.4：RBAC hooks 缺失直接 503——无 legacy fallback（kb_records M2 警示落地）。"""
    client, app = ctx["client"], ctx["app"]
    _as(app, U_MEMBER)
    app.state.user_store = object()  # No hooks.
    r = client.get("/api/v1/kb/matrices", params={"workspace_id": WS_A}, headers=_h(WS_A))
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == {"code": "access_control_unavailable"}


# ─── Nonmembers / cross-space ───────────────────────────────


async def test_non_member_rejected(ctx) -> None:
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_OUTSIDER)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"]["code"] == "workspace_access_denied"
    assert "message" not in r.json()["detail"]["error"]
    r = client.post("/api/v1/kb/matrices", json={"workspace_id": WS_A, "name": "x"}, headers=_h(WS_A))
    assert r.status_code == 403, r.text


async def test_team_admin_non_member_rejected(ctx) -> None:
    """team_admin 但非 ws 成员 → not_a_member 403（authorize_ws_access fail-closed）。"""
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_TEAM_ADMIN)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.status_code == 403, r.text


async def test_cross_ws_matrix_id_404(ctx) -> None:
    """WS_B 合法成员拿 WS_A 的 matrix_id 探测 → 404 不 403（枚举防护）。"""
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_OUTSIDER)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_B))
    assert r.status_code == 404, r.text
    r = client.delete(f"/api/v1/kb/matrices/{m.matrix_id}", headers=_h(WS_B))
    assert r.status_code == 404, r.text


async def test_cross_ws_file_id_404_and_personal_422(ctx) -> None:
    client, app = ctx["client"], ctx["app"]
    _as(app, U_MEMBER)
    r = client.post(
        "/api/v1/kb/matrices",
        json={"workspace_id": WS_A, "name": "x", "file_ids": [ctx["file_b"]]},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 404, "matrix_source_file_unavailable")
    r = client.post(
        "/api/v1/kb/matrices",
        json={"workspace_id": WS_A, "name": "x", "file_ids": [ctx["file_personal"]]},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "personal_files_not_allowed")


async def test_archived_workspace_keeps_matrix_history_readable_and_blocks_actions(ctx) -> None:
    client, app, matrix = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    ctx["membership_store"].workspace_statuses[WS_A] = "archived"

    listed = client.get(
        "/api/v1/kb/matrices",
        params={"workspace_id": WS_A},
        headers=_h(WS_A),
    )
    snapshot = client.get(
        f"/api/v1/kb/matrices/{matrix.matrix_id}/snapshot",
        headers=_h(WS_A),
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["matrix_id"] == matrix.matrix_id
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["matrix"]["matrix_id"] == matrix.matrix_id

    blocked = [
        client.post(
            "/api/v1/kb/matrices",
            json={"workspace_id": WS_A, "name": "归档后新建"},
            headers=_h(WS_A),
        ),
        client.patch(
            f"/api/v1/kb/matrices/{matrix.matrix_id}",
            json={"name": "归档后改名"},
            headers=_h(WS_A),
        ),
        client.post(
            f"/api/v1/kb/matrices/{matrix.matrix_id}/columns/draft",
            json={"question": "归档后起草"},
            headers=_h(WS_A),
        ),
        client.post(
            f"/api/v1/kb/matrices/{matrix.matrix_id}/runs/estimate",
            json={"kind": "all_pending"},
            headers=_h(WS_A),
        ),
        client.post(
            f"/api/v1/kb/matrices/{matrix.matrix_id}/runs/run_archived/cancel",
            headers=_h(WS_A),
        ),
    ]
    for response in blocked:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "workspace_archived"

    persisted = await ctx["store"].get_matrix(matrix.matrix_id)
    assert persisted is not None
    assert persisted.name == "测试矩阵"


async def test_create_validation_failures_use_finite_code_only_contract(ctx) -> None:
    client, app = ctx["client"], ctx["app"]
    _as(app, U_MEMBER)
    app.state.kb_store = _FakeKbStore()

    r = client.post(
        "/api/v1/kb/matrices",
        json={
            "workspace_id": WS_A,
            "name": "invalid type",
            "columns": [{"name": "x", "type": "future_type"}],
        },
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "invalid_column_type")

    r = client.post(
        "/api/v1/kb/matrices",
        json={
            "workspace_id": WS_A,
            "name": "missing options",
            "columns": [{"name": "x", "type": "select"}],
        },
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "options_required")


async def test_from_template_missing_stores_uses_stable_service_code(ctx) -> None:
    client, app = ctx["client"], ctx["app"]
    _as(app, U_MEMBER)

    r = client.post(
        "/api/v1/kb/matrices/from_template",
        json={"workspace_id": WS_A, "doc_type_id": "dt_missing"},
        headers=_h(WS_A),
    )

    _assert_code_only(r, 503, "matrix_data_service_unavailable")


async def test_from_template_cross_ws_doc_type_404(ctx, tmp_path: Path) -> None:
    """review P0：他空间 doc_type_id → 404，否则合法成员可把对方全量抽取记录物化进自己矩阵。"""
    from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
    from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore

    client, app = ctx["client"], ctx["app"]
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()
    app.state.kb_doc_type_store = dt_store
    app.state.kb_extracted_record_store = rec_store
    dt_foreign = await dt_store.create(
        workspace_id=WS_B,
        name="他空间文档表",
        created_by=U_OUTSIDER,
        fields=[ExtractField(name="金额", type="currency")],
    )
    _as(app, U_MEMBER)
    r = client.post(
        "/api/v1/kb/matrices/from_template",
        json={"workspace_id": WS_A, "doc_type_id": dt_foreign},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 404, "matrix_template_not_found")


# ─── viewer: all mutations 403 + snapshot 200 ────────────────


async def test_viewer_snapshot_200_mutations_403(ctx) -> None:
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    col = m.columns[0]["column_id"]
    _as(app, U_VIEWER)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert r.json()["matrix"]["can_edit"] is False  # e2e-plan P2-1: canEdit provided server-side.

    mutations = [
        ("POST", "/api/v1/kb/matrices", {"workspace_id": WS_A, "name": "x"}),
        ("PATCH", f"/api/v1/kb/matrices/{m.matrix_id}", {"name": "y"}),
        ("DELETE", f"/api/v1/kb/matrices/{m.matrix_id}", None),
        ("PUT", f"/api/v1/kb/matrices/{m.matrix_id}/files", {"file_ids": []}),
        ("POST", f"/api/v1/kb/matrices/{m.matrix_id}/columns", {"name": "n", "type": "question", "question": "q?"}),
        ("POST", f"/api/v1/kb/matrices/{m.matrix_id}/columns/draft", {"question": "q?"}),
        ("PATCH", f"/api/v1/kb/matrices/{m.matrix_id}/columns/{col}", {"name": "n2"}),
        ("DELETE", f"/api/v1/kb/matrices/{m.matrix_id}/columns/{col}", None),
        ("POST", f"/api/v1/kb/matrices/{m.matrix_id}/runs/estimate", {"kind": "all_pending"}),
        ("POST", f"/api/v1/kb/matrices/{m.matrix_id}/runs", {"kind": "all_pending"}),
        ("POST", f"/api/v1/kb/matrices/{m.matrix_id}/cells/{ctx['file_a']}/{col}/rerun", None),
        ("PATCH", f"/api/v1/kb/matrices/{m.matrix_id}/cells/{ctx['file_a']}/{col}", {"op": "edit_value", "value": "x"}),
    ]
    for method, path, body in mutations:
        r = client.request(method, path, json=body, headers=_h(WS_A))
        assert r.status_code == 403, f"{method} {path} → {r.status_code}: {r.text}"
        assert "workspace_member_role_required" in r.text, f"{method} {path}"


# ─── owner-or-admin (review P1: must include noncreator member case) ──


async def test_member_non_creator_cannot_delete_matrix_or_column(ctx) -> None:
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    col = m.columns[0]["column_id"]
    _as(app, U_MEMBER2)
    r = client.delete(f"/api/v1/kb/matrices/{m.matrix_id}", headers=_h(WS_A))
    assert r.status_code == 403, r.text
    assert "owner_or_admin_required" in r.text
    r = client.delete(f"/api/v1/kb/matrices/{m.matrix_id}/columns/{col}", headers=_h(WS_A))
    assert r.status_code == 403, r.text
    # Creator can delete columns.
    _as(app, U_MEMBER)
    r = client.delete(f"/api/v1/kb/matrices/{m.matrix_id}/columns/{col}", headers=_h(WS_A))
    assert r.status_code == 200, r.text


async def test_member_non_initiator_cannot_cancel_run(ctx) -> None:
    client, app, m, store = ctx["client"], ctx["app"], ctx["matrix"], ctx["store"]
    col = m.columns[0]["column_id"]
    run = await store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=U_MEMBER,
        targets=[(ctx["file_a"], col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    _as(app, U_MEMBER2)
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/{run.run_id}/cancel", headers=_h(WS_A))
    assert r.status_code == 403, r.text
    assert "owner_or_admin_required" in r.text
    # Initiator can cancel (cancel bypasses readonly gate: ALLOW_ROUTES, stop losses for frozen teams).
    _as(app, U_MEMBER)
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/{run.run_id}/cancel", headers=_h(WS_A))
    assert r.status_code == 200, r.text
    got = await store.get_run(run.run_id)
    assert got is not None
    assert got.status in ("cancelling", "cancelled")


async def test_finished_run_returns_code_only_not_cancellable(ctx) -> None:
    client, app, m, store = ctx["client"], ctx["app"], ctx["matrix"], ctx["store"]
    col = m.columns[0]["column_id"]
    run = await store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=U_MEMBER,
        targets=[(ctx["file_a"], col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    assert await store.set_run_status(run.run_id, "succeeded")
    _as(app, U_MEMBER)

    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/{run.run_id}/cancel", headers=_h(WS_A))

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == {"code": "run_not_cancellable"}


# ─── Normal member flow (estimate uses same authorization) ──


async def test_member_estimate_and_snapshot_ok(ctx) -> None:
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    r = client.post(
        f"/api/v1/kb/matrices/{m.matrix_id}/runs/estimate",
        json={"kind": "all_pending"},
        headers=_h(WS_A),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cells_pending"] == 1
    assert "credits_estimated" in body
    assert "blocked" in body
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.status_code == 200
    assert r.json()["matrix"]["can_edit"] is True


# ─── Additional CTO security review coverage (S-P2-6) ────────


async def test_cancel_foreign_run_id_404(ctx) -> None:
    """cancel 是唯一以裸全局 run_id 入库的查询——他矩阵 run_id 必须 404。"""
    client, app, m, store = ctx["client"], ctx["app"], ctx["matrix"], ctx["store"]
    other = await store.create_matrix(
        name="另一矩阵",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[],
        columns=[{"name": "B", "type": "question", "question": "b?"}],
    )
    other_col = other.columns[0]["column_id"]
    foreign_run = await store.create_run_and_enqueue(
        matrix_id=other.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=U_MEMBER,
        targets=[("kbf_foreign", other_col)],  # M3: empty targets roll back, return None (decision 4); real targets required to create runs.
        skipped_edited=0,
        credits_estimated=0,
        column_versions={other_col: 1},
        file_versions={},
    )
    assert foreign_run is not None
    _as(app, U_MEMBER)
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/{foreign_run.run_id}/cancel", headers=_h(WS_A))
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == {"code": "matrix_run_not_found"}


async def test_cross_team_team_scope_file_404(ctx) -> None:
    """team scope 文件跨 team → 404（_validate_file_refs can_read 分支）。"""
    client, app, files_store = ctx["client"], ctx["app"], ctx["files_store"]
    foreign_team_file = await files_store.create(
        artifact_id="x",
        filename="x.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="team",
        scope_id="team_other",
        workspace_id="",
        team_id="team_other",
        uploaded_by="u_x",
    )
    _as(app, U_MEMBER)
    r = client.post(
        "/api/v1/kb/matrices",
        json={"workspace_id": WS_A, "name": "x", "file_ids": [foreign_team_file]},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 404, "matrix_source_file_unavailable")


async def test_402_body_masks_team_pool(ctx, tmp_path: Path) -> None:
    """S-P1-1 回归：team 池更紧时 402 available 回 user 层数字, 不泄 team 原值。"""
    import aiosqlite
    from bowhead_core.user_store import _now

    engine = ctx["app"].state.kb_matrix_engine
    async with aiosqlite.connect(engine._user_store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'O', 'o.com', 10, 'ultra', 1, 1, '{}', 'active', ?)",
            (TEAM, _now()),
        )
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, 'm@o.com', 1, '', '[]', 'ultra', 1000, 1000, ?, 'zh-CN', 'UTC', '{}', 'active', ?)",
            (U_MEMBER, TEAM, _now(), _now()),
        )
        await db.commit()
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs", json={"kind": "all_pending"}, headers=_h(WS_A))
    assert r.status_code == 402, r.text
    err = r.json()["detail"]["error"]
    assert err["code"] == "insufficient_credits"
    assert "message" not in err
    assert err["available"] == 1000  # User level, never the team pool's 1.
    assert err["team_pool_tighter"] is True


async def test_run_service_unavailable_uses_stable_code(ctx) -> None:
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    app.state.kb_matrix_engine = None

    r = client.post(
        f"/api/v1/kb/matrices/{m.matrix_id}/runs/estimate",
        json={"kind": "all_pending"},
        headers=_h(WS_A),
    )

    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == {"code": "matrix_service_unavailable"}


async def test_rerun_unknown_column_404(ctx) -> None:
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    r = client.post(
        f"/api/v1/kb/matrices/{m.matrix_id}/cells/{ctx['file_a']}/mxcol_nonexistent/rerun",
        headers=_h(WS_A),
    )
    assert r.status_code == 404, r.text


# ─── M3 increments (tech-design-m3: dual undo ops / stale scope / runs[] / options validation) ──


async def test_m3_revert_and_unverify_ops(ctx) -> None:
    """FR-9/FR-12 r5：撤销编辑（422 not_edited 帮 FE 揪 stale UI）+ 撤销已核
    （false 放开、no-op 200）；viewer 两 op 403。"""
    client, app, m, fa = ctx["client"], ctx["app"], ctx["matrix"], ctx["file_a"]
    col = m.columns[0]["column_id"]
    _as(app, U_MEMBER)
    url = f"/api/v1/kb/matrices/{m.matrix_id}/cells/{fa}/{col}"
    r = client.patch(url, json={"op": "edit_value", "value": "人工值"}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert r.json()["cell"]["edited"] is True
    r = client.patch(url, json={"op": "set_verified", "verified": True}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    r = client.patch(url, json={"op": "set_verified", "verified": False}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert r.json()["cell"]["verified"] is False
    # Repeat no-op false → idempotent 200.
    r = client.patch(url, json={"op": "set_verified", "verified": False}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    r = client.patch(url, json={"op": "revert_edited"}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert r.json()["cell"]["edited"] is False
    r = client.patch(url, json={"op": "revert_edited"}, headers=_h(WS_A))
    _assert_code_only(r, 422, "not_edited")
    _as(app, U_VIEWER)
    for op in ({"op": "revert_edited"}, {"op": "set_verified", "verified": False}):
        r = client.patch(url, json=op, headers=_h(WS_A))
        assert r.status_code == 403, r.text


async def test_m3_stale_scope_plumbing(ctx) -> None:
    """stale/stale_column 过 Literal + file_versions 取数链；stale_column 缺 column_id → 422。"""
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    col = m.columns[0]["column_id"]
    _as(app, U_MEMBER)
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/estimate", json={"kind": "stale"}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert r.json()["cells_pending"] == 0  # No stale cells.
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/estimate", json={"kind": "stale_column", "column_id": col}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs/estimate", json={"kind": "stale_column"}, headers=_h(WS_A))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == {"code": "invalid_matrix_run_scope"}
    # stale with nothing pending → run rejected with no_pending_cells.
    r = client.post(f"/api/v1/kb/matrices/{m.matrix_id}/runs", json={"kind": "stale"}, headers=_h(WS_A))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == {"code": "no_pending_cells", "skipped_edited": 0}


async def test_m3_snapshot_runs_and_can_cancel(ctx) -> None:
    """决策 5/7：runs[] 含活跃批量 run、排除 single_cell、run 兼容字段=runs[0]；
    can_cancel 服务端按请求者算（member 只对自己的 run true）。"""
    client, app, m, store, fa = ctx["client"], ctx["app"], ctx["matrix"], ctx["store"], ctx["file_a"]
    col = m.columns[0]["column_id"]
    batch = await store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "column", "column_id": col},
        initiated_by=U_MEMBER,
        targets=[(fa, col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    assert batch is not None
    await store.create_run(
        matrix_id=m.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "single_cell", "file_id": fa, "column_id": col},
        initiated_by=U_MEMBER,
        cells_total=1,
        skipped_edited=0,
        credits_estimated=1,
    )
    _as(app, U_MEMBER)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["runs"]) == 1  # single_cell excluded from banner stack.
    assert body["runs"][0]["run_id"] == batch.run_id
    assert body["runs"][0]["scope"]["kind"] == "column"
    assert body["runs"][0]["can_cancel"] is True  # Initiator themself.
    assert body["run"]["run_id"] == batch.run_id  # Deprecated compatibility field = runs[0].
    _as(app, U_MEMBER2)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.json()["runs"][0]["can_cancel"] is False  # Member, not initiator.
    _as(app, U_TEAM_ADMIN)
    r = client.get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A))
    assert r.status_code in (403, 404)  # Nonmember team_admin still fully denied (runs[] does not change S1 semantics).


async def test_m3_options_and_cv_validation_codes(ctx) -> None:
    """FR-2 r5 校验码经路由直传：options_required / options_not_applicable /
    options_too_many（PATCH 同源，S-P2-7）/ cross_verify_not_applicable。"""
    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    r = client.post(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns",
        json={"name": "情感", "type": "select", "question": "q?"},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "options_required")
    r = client.post(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns",
        json={"name": "是非", "type": "boolean", "question": "q?", "options": ["是", "否"]},
        headers=_h(WS_A),
    )
    assert r.status_code == 422, r.text
    r = client.post(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns",
        json={"name": "标签", "type": "select", "question": "q?", "options": ["a", "b"]},
        headers=_h(WS_A),
    )
    assert r.status_code == 200, r.text
    new_col = r.json()["column_id"]
    r = client.patch(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns/{new_col}",
        json={"options": [f"o{i}" for i in range(101)]},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "options_too_many")
    r = client.patch(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns/{m.columns[0]['column_id']}",
        json={"cross_verify": True},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "cross_verify_not_applicable")
    # promote plus cross_verify → sole-change rejects (no silent discard, impl-diff review P3-2).
    r = client.patch(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns/{m.columns[0]['column_id']}",
        json={"type": "number", "cross_verify": True},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "promote_must_be_sole_change")


async def test_column_promotion_and_cell_failures_are_code_only(ctx) -> None:
    client, app, m, store = ctx["client"], ctx["app"], ctx["matrix"], ctx["store"]
    col = m.columns[0]["column_id"]
    _as(app, U_MEMBER)

    r = client.patch(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns/{col}",
        json={"type": "string"},
        headers=_h(WS_A),
    )
    assert r.status_code == 200, r.text
    r = client.patch(
        f"/api/v1/kb/matrices/{m.matrix_id}/columns/{col}",
        json={"type": "number"},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "invalid_promotion")

    missing_cell_url = f"/api/v1/kb/matrices/{m.matrix_id}/cells/{ctx['file_a']}/{col}"
    r = client.patch(
        missing_cell_url,
        json={"op": "keep_as_text"},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 404, "matrix_cell_not_found")

    await store.insert_cells_bulk(
        m.matrix_id,
        [
            {
                "file_id": ctx["file_a"],
                "column_id": col,
                "workspace_id": WS_A,
                "team_id": TEAM,
                "status": "failed",
                "variants": [],
                "edited_by_user": False,
                "verified": False,
                "prompt_version": 1,
                "file_version": 1,
            },
        ],
    )
    r = client.patch(
        missing_cell_url,
        json={"op": "set_verified", "verified": True},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "verify_requires_done")


@pytest.mark.parametrize("change", ["space", "team", "personal"])
@pytest.mark.parametrize("incremental", [False, True])
async def test_snapshot_reprojects_source_permissions_even_when_matrix_seq_is_unchanged(ctx, change, incremental):
    import aiosqlite

    client, app, matrix = ctx["client"], ctx["app"], ctx["matrix"]
    _as(app, U_MEMBER)
    column = matrix.columns[0]
    await ctx["store"].edit_cell_value(
        matrix.matrix_id, ctx["file_a"], column["column_id"], value="confidential value", column=column, workspace_id=WS_A, team_id=TEAM
    )
    route = f"/api/v1/kb/matrices/{matrix.matrix_id}/snapshot"
    before = client.get(route, headers=_h(WS_A))
    assert before.status_code == 200
    assert "confidential value" in before.text and "a.md" in before.text
    assignments = {
        "space": "workspace_id='ws_beta',scope_id='ws_beta'",
        "team": "team_id='team_foreign'",
        "personal": "scope='personal',scope_id='u_other',uploaded_by='u_other'",
    }
    async with aiosqlite.connect(ctx["files_store"]._db_path) as db:
        await db.execute(f"UPDATE kb_files SET {assignments[change]} WHERE file_id=?", (ctx["file_a"],))
        await db.commit()
    result = client.get(route, headers=_h(WS_A), params={"since_seq": before.json()["matrix"]["seq"]} if incremental else {})
    assert result.status_code == 200, result.text
    assert result.json()["rows"][0]["missing"]
    assert result.json()["rows"][0]["file_name"] == ""
    cell = result.json()["cells"][f"{ctx['file_a']}:{column['column_id']}"]
    assert cell["variants"] == []
    edited = client.patch(
        f"/api/v1/kb/matrices/{matrix.matrix_id}/cells/{ctx['file_a']}/{column['column_id']}",
        headers=_h(WS_A),
        json={"op": "set_verified", "verified": True},
    )
    assert edited.status_code in {403, 404, 422}


async def test_snapshot_hides_revoked_resource_grant(ctx, monkeypatch, tmp_path):
    from bowhead_core.access_control import AccessControlStore

    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    ctx["app"].state.access_control_store = access
    _as(ctx["app"], U_MEMBER)
    route = f"/api/v1/kb/matrices/{ctx['matrix'].matrix_id}/snapshot"
    before = ctx["client"].get(route, headers=_h(WS_A))
    assert before.status_code == 200 and before.json()["rows"][0]["file_name"] == "a.md"
    await access.create_or_update_resource_grant(
        team_id=TEAM,
        resource_kind="kb_file",
        resource_id=ctx["file_a"],
        principal_type="user",
        principal_id=U_MEMBER,
        permission="read",
        effect="deny",
    )
    after = ctx["client"].get(route, headers=_h(WS_A), params={"since_seq": before.json()["matrix"]["seq"]})
    assert after.status_code == 200, after.text
    assert after.json()["rows"][0]["missing"]
    assert after.json()["rows"][0]["file_name"] == ""


async def test_read_grant_denied_source_file_answers_like_a_missing_one(ctx, monkeypatch) -> None:
    """A source file the caller cannot read (grant denial) is refused exactly like a missing one."""
    from bowhead_gateway.routes import kb

    client, app, m = ctx["client"], ctx["app"], ctx["matrix"]
    col = m.columns[0]["column_id"]
    _as(app, U_MEMBER)
    gone = await ctx["files_store"].create(
        artifact_id="g",
        filename="g.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="workspace",
        scope_id=WS_A,
        workspace_id=WS_A,
        team_id=TEAM,
        uploaded_by=U_MEMBER,
    )
    await ctx["store"].replace_files(m.matrix_id, [*m.files, {"file_id": gone, "char_count": 1}], workspace_id=WS_A)
    await ctx["files_store"].soft_delete(gone)

    async def deny(request, scope_ctx, file, *, permission, strict=True):  # noqa: ANN001, ANN202
        raise kb._kb_error(403, "resource_grant_denied", resource_kind="kb_file", resource_id=file.file_id, permission=permission)

    def calls(file_id: str, cell_file_id: str) -> list[Any]:
        return [
            client.post("/api/v1/kb/matrices", json={"workspace_id": WS_A, "name": "x", "file_ids": [file_id]}, headers=_h(WS_A)),
            client.put(f"/api/v1/kb/matrices/{m.matrix_id}/files", json={"file_ids": [file_id]}, headers=_h(WS_A)),
            client.patch(f"/api/v1/kb/matrices/{m.matrix_id}/cells/{cell_file_id}/{col}", json={"op": "revert_edited"}, headers=_h(WS_A)),
        ]

    # A cell can only address files already in the matrix, so its "missing" twin is a deleted member file.
    missing = calls("kbf_missing", gone)
    monkeypatch.setattr(kb, "_require_kb_file_grant", deny)
    denied = calls(ctx["file_a"], ctx["file_a"])

    for got, expected in zip(denied, missing, strict=True):
        _assert_code_only(got, 404, "matrix_source_file_unavailable")
        assert (got.status_code, got.content) == (expected.status_code, expected.content)
