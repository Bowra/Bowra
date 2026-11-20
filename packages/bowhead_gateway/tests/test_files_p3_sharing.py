"""file-management-v2 P3 —— 文件分享（空间/团队作用域）。

设计见 `docs/file-management-p3-space-scope-design-2026-07-25.md`。断言集中在
三条产品硬规则：

  1. **只读**：被分享方能看能下载，不能删/改分类/加知识库（design §2.3-1）
  2. **运行数据与知识库文件不参与**（§2.3-2/3）
  3. **撤销立即生效**——不缓存 grant（§2.3-4）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.session_store import SessionStore
from bowhead_core.share_service import reset_content_sharing_cache
from bowhead_core.user_store import UserStore
from bowhead_gateway.files_sharing import (
    ARTIFACT_RESOURCE_KIND,
    ShareRejected,
    assert_shareable,
    shared_artifact_ids,
)
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "usr_owner"
_PEER = "usr_peer"
_TEAM = "team_1"
_WS = "ws_1"
_H_OWNER = {"X-Bowhead-User-Id": _OWNER, "X-Space-Id": _WS}
_H_PEER = {"X-Bowhead-User-Id": _PEER, "X-Space-Id": _WS}


class _FakeUserStore:
    async def get_user(self, user_id: str):  # noqa: ANN201
        return SimpleNamespace(id=user_id, team_id=_TEAM)

    async def get_workspace_team_id(self, workspace_id: str) -> str:
        return _TEAM


@pytest.fixture
async def app_client(tmp_path: Path):
    db_path = tmp_path / "a.db"
    ss = SessionStore(db_path)
    await ss.initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store.initialize()
    access = AccessControlStore(db_path)  # One database, as in production (pruning reads share grants there).
    await access.initialize()

    app = FastAPI()
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.access_control_store = access
    app.state.user_store = _FakeUserStore()
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    return TestClient(app), store, access


async def _asset(store: ArtifactStore, *, filename="Q1.pptx", kind="user_upload", owner=_OWNER):
    return await store.put_bytes(
        content=b"payload",
        filename=filename,
        mime="application/pdf",
        source="upload",
        created_by=owner,
        kind=kind,
    )


# ── Rules: what can be shared ───────────────────────────────


def _art(kind="user_upload", deleted=None):
    return SimpleNamespace(id="a", kind=kind, deleted_at=deleted, mime="application/pdf")


def test_run_data_is_not_shareable() -> None:
    for kind in ("tool_data", "sandbox_output", "web_clip"):
        with pytest.raises(ShareRejected) as ei:
            assert_shareable(_art(kind=kind), in_kb=False)
        assert "kind_not_shareable" in ei.value.reason


def test_kb_file_is_not_shareable_here() -> None:
    """知识库文件的共享归 kb_files.scope 管——两套共享语义不能并存。"""
    with pytest.raises(ShareRejected) as ei:
        assert_shareable(_art(), in_kb=True)
    assert ei.value.reason == "in_kb_use_knowledge_base_scope"


def test_deleted_is_not_shareable() -> None:
    with pytest.raises(ShareRejected):
        assert_shareable(_art(deleted="2026-07-25T00:00:00Z"), in_kb=False)


@pytest.mark.parametrize("kind", ["user_upload", "deliverable", "canvas_export"])
def test_assets_are_shareable(kind) -> None:
    # Contract is 'does not raise when allowed'; calling directly is the assertion (`... is None` is a useless comparison,
    # caught by ruff B015: it would not fail even if the function changed to return a value).
    assert_shareable(_art(kind=kind), in_kb=False)


# ── End to end: share → visible → revoke ────────────────────


async def test_share_makes_file_visible_and_downloadable_to_peer(app_client) -> None:
    client, store, _ = app_client
    art = await _asset(store)

    # Before sharing: peer cannot see or download it.
    before = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()
    assert before["items"] == []
    assert client.get(f"/api/v1/files/{art.id}", headers=_H_PEER).status_code == 404

    r = client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    assert r.status_code == 201

    # After sharing: peer sees it in Shared with me and can download.
    after = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()
    assert [i["file_id"] for i in after["items"]] == [art.id]
    assert after["items"][0]["shared_readonly"] is True, "被分享方必须被标记只读"
    assert client.get(f"/api/v1/files/{art.id}", headers=_H_PEER).status_code == 200


@pytest.mark.parametrize("mode", ["preview", "raw", "details"])
async def test_shared_file_representation_rechecks_existing_grant(app_client, mode):
    client, store, _ = app_client
    art = await store.put_bytes(content=b"a,b\n1,2\n", filename="rows.csv", mime="text/csv", source="upload", kind="user_upload", created_by=_OWNER)
    path = f"/api/v1/files/{art.id}/representations/{mode}"
    assert client.get(path, headers=_H_PEER).status_code == 404
    assert client.post(f"/api/v1/files/{art.id}/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER).status_code == 201
    assert client.get(path, headers=_H_PEER).status_code == 200
    assert client.delete(f"/api/v1/files/{art.id}/share/workspace/{_WS}", headers=_H_OWNER).status_code == 204
    assert client.get(path, headers=_H_PEER).status_code == 404


async def test_revoke_takes_effect_immediately(app_client) -> None:
    """撤销不能有缓存窗口——grant 没了就是没了。"""
    client, store, _ = app_client
    art = await _asset(store)
    client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    assert client.get(f"/api/v1/files/{art.id}", headers=_H_PEER).status_code == 200

    r = client.delete(f"/api/v1/files/{art.id}/share/workspace/{_WS}", headers=_H_OWNER)
    assert r.status_code == 204

    assert client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()["items"] == []
    assert client.get(f"/api/v1/files/{art.id}", headers=_H_PEER).status_code == 404


async def test_peer_cannot_delete_or_reclassify_shared_file(app_client) -> None:
    """只读共享：能看能下载，但删/改都不行（design §2.3-1）。"""
    client, store, _ = app_client
    art = await _asset(store)
    client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )

    assert client.delete(f"/api/v1/files/{art.id}", headers=_H_PEER).status_code == 404
    r = client.post(f"/api/v1/files/{art.id}/category", json={"category": "dataset"}, headers=_H_PEER)
    assert r.status_code == 404
    # File really was not deleted.
    still = await store.get(art.id)
    assert still is not None and still.deleted_at is None


async def test_peer_cannot_reshare(app_client) -> None:
    """分享是所有权行为——被分享方不能再转分享出去。"""
    client, store, _ = app_client
    art = await _asset(store)
    client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    r = client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "team", "subject_id": _TEAM},
        headers=_H_PEER,
    )
    assert r.status_code == 404


async def test_sharing_run_data_rejected_at_api(app_client) -> None:
    """UI 不给入口，服务层也要挡——防线不能只在前端。"""
    client, store, _ = app_client
    art = await _asset(store, filename="rows.csv", kind="tool_data")
    r = client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "file_not_shareable"


async def test_list_shares_owner_only(app_client) -> None:
    client, store, _ = app_client
    art = await _asset(store)
    client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    owner_view = client.get(f"/api/v1/files/{art.id}/shares", headers=_H_OWNER).json()
    assert owner_view["shares"] == [{"subject_kind": "workspace", "subject_id": _WS}]
    # Non-owners must not even see the share recipients.
    assert client.get(f"/api/v1/files/{art.id}/shares", headers=_H_PEER).status_code == 404


# ── Permission engine: workspace principal previously not found ──


async def test_workspace_principal_now_resolvable(tmp_path: Path) -> None:
    """回归：`workspace` 一直是合法 principal_type，但查询链不含它——
    写下的空间级 grant 静默失效。P3 给 `granted_resource_ids` 加了
    `workspace_id` 参数后才真正生效。"""
    access = AccessControlStore(tmp_path / "ac2.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id=_TEAM,
        resource_kind=ARTIFACT_RESOURCE_KIND,
        resource_id="art_ws",
        principal_type="workspace",
        principal_id=_WS,
        permission="read",
    )
    # Without workspace_id → not found (pre-change behavior, also behavior of existing callers).
    without = await access.granted_resource_ids(_TEAM, user_id=_PEER, kind=ARTIFACT_RESOURCE_KIND)
    assert "art_ws" not in without
    # With it → found.
    with_ws = await shared_artifact_ids(
        access,
        team_id=_TEAM,
        user_id=_PEER,
        workspace_id=_WS,
    )
    assert "art_ws" in with_ws


# ── Space membership: cannot simply trust X-Space-Id ─────────


class _RbacUserStore:
    """带全套 RBAC hook 的 user_store —— `has_workspace_access_source` 认它，
    files 路由据此走 `build_access_context`（真校验成员资格）。

    成员表：owner 与 peer 都在 ws_1；**没人在 ws_secret**。
    """

    _MEMBERS = {("ws_1", _OWNER): "member", ("ws_1", _PEER): "member"}

    async def get_user(self, user_id: str):  # noqa: ANN201
        return SimpleNamespace(id=user_id, team_id=_TEAM)

    async def get_user_team_id(self, user_id: str) -> str:  # noqa: ARG002
        return _TEAM

    async def get_workspace_team_id(self, workspace_id: str) -> str:  # noqa: ARG002
        return _TEAM

    async def get_member_role(self, workspace_id: str, user_id: str) -> str:
        return self._MEMBERS.get((workspace_id, user_id), "")

    async def get_workspace(self, workspace_id: str) -> dict[str, str] | None:
        if workspace_id not in {_WS, "ws_secret"}:
            return None
        return {"id": workspace_id, "status": "active"}

    async def get_team_role(self, user_id: str, team_id: str) -> str:  # noqa: ARG002
        return "member"


@pytest.fixture
async def rbac_client(tmp_path: Path):
    db_path = tmp_path / "r.db"
    await SessionStore(db_path).initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "rf"), db_path=db_path)
    await store.initialize()
    access = AccessControlStore(tmp_path / "rac.db")
    await access.initialize()
    app = FastAPI()
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.access_control_store = access
    app.state.user_store = _RbacUserStore()
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    return TestClient(app), store


async def test_non_member_cannot_claim_a_space_to_read_its_shares(rbac_client) -> None:
    """**越权回归**：`X-Space-Id` 是客户端随手可填的字段，而空间级 grant 正是按它
    匹配的。只读 header 不验成员资格 → 同团队任意成员填上 `ws_secret` 就能读到
    分享进该空间的全部文件。修复后这类请求 403，读不到任何东西。"""
    client, store = rbac_client
    art = await _asset(store)
    # Owner shares the file into a space where neither user is a member.
    r = client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": "ws_secret"},
        headers=_H_OWNER,
    )
    assert r.status_code == 201

    faked = {"X-Bowhead-User-Id": _PEER, "X-Space-Id": "ws_secret"}
    list_denied = client.get("/api/v1/files/?shared_with_me=1", headers=faked)
    assert list_denied.status_code == 403
    assert list_denied.json()["detail"] == {"error": {"code": "workspace_access_denied"}}
    # Per-id routes answer a Space denial like a missing id (existence concealment).
    download_denied = client.get(f"/api/v1/files/{art.id}", headers=faked)
    assert download_denied.status_code == 404
    assert download_denied.json()["detail"] == {"error": {"code": "file_not_found"}}
    assert download_denied.content == client.get("/api/v1/files/art_missing", headers=faked).content
    # Back to a space with actual membership: valid request, but no share there → empty.
    ok = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER)
    assert ok.status_code == 200 and ok.json()["items"] == []


async def test_member_of_the_space_still_sees_the_share(rbac_client) -> None:
    """校验加严不能把正常路径也关上。"""
    client, store = rbac_client
    art = await _asset(store)
    client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    got = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()
    assert [i["file_id"] for i in got["items"]] == [art.id]


async def test_shared_file_is_previewable_not_just_downloadable(app_client) -> None:
    """Download works but preview is refused = half-wired feature: the drawer opens on an error."""
    client, store, _ = app_client
    art = await store.put_bytes(
        content=b"col_a,col_b\n1,2\n",
        filename="rows.csv",
        mime="text/csv",
        source="upload",
        created_by=_OWNER,
        kind="user_upload",
    )
    assert client.get(f"/api/v1/files/{art.id}/preview", headers=_H_PEER).status_code == 404
    client.post(
        f"/api/v1/files/{art.id}/share",
        json={"subject_kind": "workspace", "subject_id": _WS},
        headers=_H_OWNER,
    )
    assert client.get(f"/api/v1/files/{art.id}/preview", headers=_H_PEER).status_code == 200


async def test_shared_list_connection_count_does_not_grow_with_the_number_of_shares(app_client, monkeypatch) -> None:
    """「共享给我」：grant 到的文件整批取、整批判可见——开库次数与分享数无关。"""
    import aiosqlite

    client, store, _ = app_client

    async def share_one(i: int) -> None:
        art = await _asset(store, filename=f"s{i}.pptx")
        r = client.post(f"/api/v1/files/{art.id}/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER)
        assert r.status_code == 201

    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    async def shared_opens(expected: int) -> int:
        opened["n"] = 0
        monkeypatch.setattr(aiosqlite, "connect", counting)
        try:
            body = client.get("/api/v1/files/?shared_with_me=1&limit=200", headers=_H_PEER).json()
        finally:
            monkeypatch.setattr(aiosqlite, "connect", real_connect)
        assert body["total"] == expected
        return opened["n"]

    for i in range(2):
        await share_one(i)
    await shared_opens(2)  # Warm up: pools/caches built on the first process request do not count toward steady state.
    few = await shared_opens(2)
    for i in range(2, 8):
        await share_one(i)
    many = await shared_opens(8)
    assert many == few, f"2 个分享开库 {few} 次，8 个分享开库 {many} 次"


async def test_shared_list_hides_files_whose_own_space_the_peer_cannot_read(rbac_client, monkeypatch) -> None:
    """grant 只是「可以分享给你」；文件本身所在的空间 peer 读不了，「共享给我」里也不能出现。"""
    import aiosqlite

    client, store = rbac_client
    monkeypatch.setitem(_RbacUserStore._MEMBERS, ("ws_secret", _OWNER), "member")  # Owner is a member; peer is not.
    readable = await _asset(store, filename="ok.pptx")
    secret = await _asset(store, filename="secret.pptx")
    async with aiosqlite.connect(store.db_path) as db:
        for art, ws in ((readable, _WS), (secret, "ws_secret")):
            await db.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?,?,?,?,?)",
                (art.id, ws, _OWNER, None, "2026-09-01T00:00:00+00:00"),
            )
        await db.commit()
    for art in (readable, secret):
        r = client.post(f"/api/v1/files/{art.id}/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER)
        assert r.status_code == 201, r.text

    got = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()
    assert [i["file_id"] for i in got["items"]] == [readable.id]
    assert got["total"] == 1


async def test_disabled_file_sharing_suspends_reads_and_preserves_owner_revocation(app_client, monkeypatch) -> None:
    from bowhead_core import share_service

    client, store, _access = app_client
    art = await _asset(store)
    path = f"/api/v1/files/{art.id}"
    assert client.post(path + "/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER).status_code == 201
    assert client.get(path, headers=_H_PEER).content == b"payload"
    monkeypatch.setattr(share_service, "_content_sharing_cache", False)
    for suffix in ("", "/preview", "/representations/raw", "/representations/details"):
        assert client.get(path + suffix, headers=_H_PEER).status_code in {403, 404}
    assert client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()["items"] == []
    assert client.get(path, headers=_H_OWNER).content == b"payload"
    assert client.get(path + "/shares", headers=_H_OWNER).json()["shares"]
    response = client.post(path + "/share", json={"subject_kind": "team", "subject_id": _TEAM}, headers=_H_OWNER)
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "content_sharing_disabled"
    monkeypatch.setattr(share_service, "_content_sharing_cache", True)
    assert client.get(path, headers=_H_PEER).content == b"payload"
    monkeypatch.setattr(share_service, "_content_sharing_cache", False)
    assert client.delete(path + f"/share/workspace/{_WS}", headers=_H_OWNER).status_code == 204
    monkeypatch.setattr(share_service, "_content_sharing_cache", True)
    assert client.get(path, headers=_H_PEER).status_code == 404


async def _versioned_file(store: ArtifactStore, *, owner_id: str = _OWNER, workspace_id: str = _WS):
    """A deliverable with the 0228 version columns (the frozen legacy layout lacks them)."""
    from importlib import import_module

    import aiosqlite

    contract = import_module("bowhead_core.migrations.versions.0228_artifact_versions")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS artifact_purge_claims (artifact_id TEXT PRIMARY KEY)")  # Migration 0046 shape.
        for column, declaration in contract.COLUMNS:
            await db.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        for statement in (contract.LINEAGE_INDEX, contract.VERSION_OF_UNIQUE, contract.VERSION_IMMUTABLE):
            await db.execute(statement)
        await db.commit()
    store._version_cols_present = None
    sessions = SessionStore(store.db_path)
    await sessions.ensure_session(session_id="s_v", workspace_id=workspace_id, user_id=owner_id)
    for turn in ("t1", "t2", "t3", "t4", "t5", "t6", "t7"):
        await sessions.record_turn(turn_id=turn, session_id="s_v", user_text="revise the report")
        await sessions.mark_completed(turn_id=turn, intent=None, final_answer="done", tokens_input=0, tokens_output=0, cost_usd=0.0)
        await sessions.finalize_turn(session_id="s_v", current_turn_id=turn, next_turn_id=None)

    async def version(turn: str, target: str | None = None):
        return await store.put_bytes(
            content=f"<html>{turn}</html>".encode(), filename="report.html", mime="text/html", source="python_sandbox",
            kind="deliverable", created_by=owner_id, created_by_turn=turn, session_id="s_v", workspace_id=workspace_id, version_of_target=target,
        )  # fmt: skip

    return version


async def test_html_share_is_file_scoped_and_tracks_version_and_audience(app_client, monkeypatch) -> None:
    client, store, access = app_client
    users = UserStore(store.db_path)
    await users.initialize()
    team = await users.create_team(name="File Sharing Team", max_seats=6)
    foreign_team = await users.create_team(name="Foreign Team", max_seats=6)
    owner = await users.create_user(team_id=team.id, email="owner@example.test", password="Passw0rd!")
    recipient = await users.create_user(team_id=team.id, email="reader@example.test", password="Passw0rd!")
    stranger = await users.create_user(team_id=foreign_team.id, email="stranger@example.test", password="Passw0rd!")
    workspace = await users.create_local_workspace(team_id=team.id, name="Source", creator_user_id=owner.id)

    class EmptyKnowledgeBase:
        async def artifact_ids_in_kb(self, artifact_ids: list[str]) -> set[str]:
            return set()

    client.app.state.user_store = users
    client.app.state.session_store = SessionStore(store.db_path)
    client.app.state.kb_files_store = EmptyKnowledgeBase()
    notifications = NotificationStore(db_path=store.db_path)
    await notifications.initialize()
    client.app.state.notification_service = NotificationService(store=notifications, user_store=users)
    version = await _versioned_file(store, owner_id=owner.id, workspace_id=workspace)
    first = await version("t1")
    files = "/api/v1/files"
    manage = f"{files}/{first.id}/share"
    view = f"{files}/shared/{first.lineage_root}/view"
    content = f"{files}/shared/{first.lineage_root}/content"
    owner_headers = {"X-Bowhead-User-Id": owner.id, "X-Space-Id": workspace}
    recipient_headers = {"X-Bowhead-User-Id": recipient.id}
    stranger_headers = {"X-Bowhead-User-Id": stranger.id}
    member = {"subject_kind": "user", "subject_id": recipient.id}

    assert client.get(view, headers=recipient_headers).status_code == 404
    assert client.post(manage, json=member, headers=recipient_headers).status_code == 404
    assert client.post(manage, json={"subject_kind": "user"}, headers=owner_headers).status_code == 422
    assert client.post(manage, json={**member, "subject_id": stranger.id}, headers=owner_headers).status_code == 422
    assert (await notifications.list_for_user(user_id=recipient.id))[0] == []
    granted = client.post(manage, json=member, headers=owner_headers)
    assert granted.status_code == 201, granted.text
    recipient_items, _ = await notifications.list_for_user(user_id=recipient.id)
    [notice] = recipient_items
    assert notice.type == "content_shared"
    assert notice.action_url == f"/files/view/?file={first.lineage_root}"
    assert notice.metadata["resourceLabel"] == first.filename
    assert (await notifications.list_for_user(user_id=owner.id))[0] == []
    assert (await notifications.list_for_user(user_id=stranger.id))[0] == []
    await notifications.mark_read(notification_id=notice.id, user_id=recipient.id)
    assert client.get(view, headers=recipient_headers).json()["version_id"] == first.id
    assert client.get(view, headers=stranger_headers).status_code == 404
    assert client.get(f"{files}/{first.id}", headers=recipient_headers).status_code == 404

    second = await version("t2", first.id)
    assert client.post(f"{files}/{second.id}/share", json=member, headers=owner_headers).status_code == 201
    assert len(await access.list_resource_grants(team.id, resource_kind=ARTIFACT_RESOURCE_KIND)) == 1
    [same_notice], _ = await notifications.list_for_user(user_id=recipient.id)
    assert same_notice.id == notice.id and same_notice.read_at is not None
    manifest = client.get(view, headers=recipient_headers)
    assert manifest.headers["cache-control"] == "private, no-store"
    assert manifest.json()["version_id"] == second.id
    assert client.get(content, params={"version_id": first.id}, headers=recipient_headers).status_code == 409
    loaded = client.get(content, params={"version_id": second.id}, headers=recipient_headers)
    assert loaded.content == b"<html>t2</html>"
    assert loaded.headers["content-disposition"].startswith("attachment")
    assert loaded.headers["content-security-policy"].startswith("sandbox;")

    with monkeypatch.context() as disabled:
        disabled.setenv("BOWHEAD_CONTENT_SHARING", "0")
        reset_content_sharing_cache()
        try:
            assert client.get(view, headers=recipient_headers).status_code == 404
            assert client.get(view, headers=owner_headers).status_code == 200
            owner_state = client.get(f"{files}/{second.id}/shares?html=true", headers=owner_headers).json()
            assert owner_state["sharing_enabled"] is False
            assert owner_state["shares"][0]["subject_id"] == recipient.id
            assert client.delete(f"{files}/{second.id}/share/user/{recipient.id}", headers=owner_headers).status_code == 204
        finally:
            reset_content_sharing_cache()

    revoked = client.get(content, params={"version_id": second.id}, headers={**recipient_headers, "Range": "bytes=0-1"})
    assert revoked.status_code == 404
    assert revoked.headers["cache-control"] == "private, no-store"
    assert len((await notifications.list_for_user(user_id=recipient.id))[0]) == 1
    assert client.post(f"{files}/{second.id}/share", json=member, headers=owner_headers).status_code == 201
    notifications_after_reshare, _ = await notifications.list_for_user(user_id=recipient.id)
    assert len(notifications_after_reshare) == 2
    assert {item.action_url for item in notifications_after_reshare} == {notice.action_url}

    other = await users.create_user(team_id=team.id, email="another-reader@example.test", password="Passw0rd!")
    late = await users.create_user(team_id=team.id, email="late-reader@example.test", password="Passw0rd!")
    other_headers = {"X-Bowhead-User-Id": other.id}
    late_headers = {"X-Bowhead-User-Id": late.id}
    audience = f"{files}/{second.id}/shares/audience"
    roster = f"{files}/{first.id}/shares?html=true"
    group = await access.create_group(team_id=team.id, name="Analysis Readers", created_by=owner.id)
    foreign_group = await access.create_group(team_id=foreign_team.id, name="Foreign Readers", created_by=stranger.id)
    local_group = await access.create_group(team_id=team.id, workspace_id=workspace, name="Space Readers", created_by=owner.id)
    target = {"subject_kind": "group", "subject_id": group.id}
    await access.add_group_member(team_id=team.id, group_id=group.id, user_id=other.id)
    await access.add_group_member(team_id=team.id, group_id=group.id, user_id=owner.id)

    assert client.put(audience, json={"audience": "team_wide"}, headers=recipient_headers).status_code == 404
    assert client.get(view, headers=other_headers).status_code == 404
    for ineligible_group in (foreign_group, local_group):
        assert client.post(manage, json={**target, "subject_id": ineligible_group.id}, headers=owner_headers).status_code == 422
    assert client.post(manage, json=target, headers=owner_headers).status_code == 201
    assert client.post(f"{files}/{second.id}/share", json=target, headers=owner_headers).status_code == 201
    group_notices, _ = await notifications.list_for_user(user_id=other.id)
    assert [item.action_url for item in group_notices] == [notice.action_url]
    assert not (await notifications.list_for_user(user_id=owner.id))[0]
    assert client.get(view, headers=other_headers).json()["version_id"] == second.id
    assert {"subject_kind": "group", "subject_id": group.id, "subject_name": group.name} in client.get(roster, headers=owner_headers).json()["shares"]

    assert await access.remove_group_member(team.id, group.id, other.id)
    assert client.get(view, headers=other_headers).status_code == 404
    await access.add_group_member(team_id=team.id, group_id=group.id, user_id=late.id)
    assert client.get(view, headers=late_headers).status_code == 200
    assert not (await notifications.list_for_user(user_id=late.id))[0]
    assert client.put(audience, json={"audience": "team_wide"}, headers=owner_headers).status_code == 204
    assert client.get(roster, headers=owner_headers).json()["audience"] == "team_wide"
    assert client.get(view, headers=other_headers).status_code == 200
    assert client.get(view, headers=stranger_headers).status_code == 404
    assert len((await notifications.list_for_user(user_id=other.id))[0]) == 1
    assert client.put(audience, json={"audience": "custom"}, headers=owner_headers).status_code == 204
    assert client.get(roster, headers=owner_headers).json()["audience"] == "custom"
    assert client.get(view, headers=other_headers).status_code == 404
    assert client.get(view, headers=recipient_headers).status_code == 200
    assert client.get(view, headers=late_headers).status_code == 200

    assert client.delete(f"{files}/{second.id}/share/group/{group.id}", headers=owner_headers).status_code == 204
    assert client.get(view, headers=late_headers).status_code == 404
    assert client.post(manage, json=target, headers=owner_headers).status_code == 201
    assert client.put(audience, json={"audience": "team_wide"}, headers=owner_headers).status_code == 204
    await access.create_or_update_resource_grant(
        team_id=team.id, resource_kind=ARTIFACT_RESOURCE_KIND, resource_id=second.id,
        principal_type="workspace", principal_id=workspace, permission="read", created_by=owner.id,
    )  # fmt: skip

    with monkeypatch.context() as disabled:
        disabled.setenv("BOWHEAD_CONTENT_SHARING", "0")
        reset_content_sharing_cache()
        try:
            assert client.get(view, headers=late_headers).status_code == 404
            assert client.put(audience, json={"audience": "team_wide"}, headers=owner_headers).status_code == 403
            assert client.put(audience, json={"audience": "private"}, headers=owner_headers).status_code == 204
        finally:
            reset_content_sharing_cache()
    assert client.put(audience, json={"audience": "private"}, headers=owner_headers).status_code == 204
    private = client.get(roster, headers=owner_headers).json()
    assert private["audience"] == "private" and private["shares"] == []
    for headers in (recipient_headers, late_headers, other_headers, stranger_headers):
        assert client.get(view, headers=headers).status_code == 404


async def test_a_share_follows_the_file_and_recipients_read_only_its_latest_version(app_client) -> None:
    """File versions R5: one share covers every version; a recipient reads only the latest, an older ID is a 404."""
    client, store, access = app_client
    version = await _versioned_file(store)
    v1 = await version("t1")
    share = client.post(f"/api/v1/files/{v1.id}/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER)
    assert share.status_code == 201, share.text
    v2 = await version("t2", v1.id)
    assert client.get(f"/api/v1/files/{v2.id}", headers=_H_PEER).status_code == 200
    assert client.get(f"/api/v1/files/{v1.id}", headers=_H_PEER).status_code == 404
    shared = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()["items"]
    assert [item["file_id"] for item in shared] == [v2.id], "The shared file is listed once, at its latest version"
    assert client.get(f"/api/v1/files/{v2.id}/shares", headers=_H_OWNER).json()["shares"] == [{"subject_kind": "workspace", "subject_id": _WS}]

    # A share made before versions (stored on that version) still follows the file.
    v3 = await version("t3", v2.id)
    await access.create_or_update_resource_grant(
        team_id=_TEAM, resource_kind=ARTIFACT_RESOURCE_KIND, resource_id=v2.id, principal_type="team", principal_id=_TEAM,
        permission="read", created_by=_OWNER,
    )  # fmt: skip
    assert client.get(f"/api/v1/files/{v3.id}", headers=_H_PEER).status_code == 200
    assert client.get(f"/api/v1/files/{v2.id}", headers=_H_PEER).status_code == 404

    # Revoking from any version removes the share on every version.
    assert client.delete(f"/api/v1/files/{v3.id}/share/workspace/{_WS}", headers=_H_OWNER).status_code == 204
    assert client.delete(f"/api/v1/files/{v3.id}/share/team/{_TEAM}", headers=_H_OWNER).status_code == 204
    assert client.get(f"/api/v1/files/{v3.id}", headers=_H_PEER).status_code == 404
    assert client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()["items"] == []


async def test_a_share_survives_pruning_and_the_purge_of_older_versions(app_client) -> None:
    """File versions B4 x R5: a share is stored on the version recipients read, and pruning keeps a version holding a grant.

    Stored on the already-pruned first version, the grant would resolve through a row the 30-day purge removes.
    """
    client, store, access = app_client
    version = await _versioned_file(store)
    v = [await version("t1")]
    for turn in ("t2", "t3", "t4"):
        v.append(await version(turn, v[-1].id))
    assert await store.prune_versions(await store.prune_version_candidates([v[0].id]), kb_protected=set()) == 1
    share = client.post(f"/api/v1/files/{v[3].id}/share", json={"subject_kind": "workspace", "subject_id": _WS}, headers=_H_OWNER)
    assert share.status_code == 201, share.text
    grants = await access.list_resource_grants(_TEAM, resource_kind=ARTIFACT_RESOURCE_KIND)
    assert [grant.resource_id for grant in grants] == [v[3].id], "The grant names the version recipients read"
    for turn in ("t5", "t6", "t7"):
        v.append(await version(turn, v[-1].id))
    candidates = await store.prune_version_candidates([v[0].id])
    assert set(candidates) == {v[1].id, v[2].id}, "The version holding the grant is kept"
    assert await store.prune_versions(candidates, kb_protected=set()) == 2
    import aiosqlite

    async with aiosqlite.connect(store.db_path) as db:  # The 30-day purge of pruned rows.
        await db.execute("DELETE FROM artifacts WHERE deleted_at IS NOT NULL")
        await db.commit()
    assert client.get(f"/api/v1/files/{v[6].id}", headers=_H_PEER).status_code == 200
    assert client.get(f"/api/v1/files/{v[3].id}", headers=_H_PEER).status_code == 404
    shared = client.get("/api/v1/files/?shared_with_me=1", headers=_H_PEER).json()["items"]
    assert [item["file_id"] for item in shared] == [v[6].id]
