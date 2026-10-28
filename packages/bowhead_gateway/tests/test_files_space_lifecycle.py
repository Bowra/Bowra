from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import aiosqlite
import jwt
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import files, files_local_receiver, files_presign
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def lifecycle_ctx(tmp_path):
    db_path = tmp_path / "bowhead.db"
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Files Lifecycle", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="files-lifecycle@example.com",
        password="Passw0rd!",
    )
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Archived Files",
        creator_user_id=user.id,
    )

    sessions = SessionStore(db_path)
    await sessions.initialize()
    session = await sessions.ensure_session(
        session_id=None,
        workspace_id=workspace_id,
        user_id=user.id,
    )
    backend = LocalBackend(root_dir=tmp_path / "files")
    artifacts = ArtifactStore(
        backend=backend,
        db_path=db_path,
    )
    await artifacts.initialize()
    artifact = await artifacts.put_bytes(
        content=b"archived history",
        filename="history.txt",
        mime="text/plain",
        source="upload",
        created_by=user.id,
        session_id=session.id,
        kind="user_upload",
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                artifact.id,
                workspace_id,
                user.id,
                session.id,
                "2026-07-29T00:00:00+00:00",
            ),
        )
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.include_router(files_local_receiver.router)
    app.include_router(files_presign.router)
    app.state.artifact_store = artifacts
    app.state.artifact_backend = backend
    app.state.session_store = sessions
    app.state.user_store = users
    app.state.local_presign_secret = "files-lifecycle-secret-0123456789"
    app.state.settings = SimpleNamespace(
        dev_default_user=user.id,
        auth_mode="dev",
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, artifacts, workspace_id, user.id, session.id, artifact.id


async def test_archived_space_file_history_remains_readable(lifecycle_ctx) -> None:
    client, _artifacts, workspace_id, user_id, _session_id, artifact_id = lifecycle_ctx
    headers = {
        "X-Bowhead-User-Id": user_id,
        "X-Space-Id": workspace_id,
    }

    listed = client.get("/api/v1/files/", headers=headers)
    usages = client.get(f"/api/v1/files/{artifact_id}/usages", headers=headers)
    preview = client.get(f"/api/v1/files/{artifact_id}/preview", headers=headers)
    shares = client.get(f"/api/v1/files/{artifact_id}/shares", headers=headers)
    usage = client.get("/api/v1/files/usage", headers=headers)
    download = client.get(f"/api/v1/files/{artifact_id}", headers=headers)

    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["file_id"] == artifact_id
    assert usages.status_code == 200, usages.text
    assert preview.status_code == 200, preview.text
    assert preview.json()["text"] == "archived history"
    assert shares.status_code == 200, shares.text
    assert usage.status_code == 200, usage.text
    assert download.status_code == 200, download.text
    assert download.content == b"archived history"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("delete", "/api/v1/files/{artifact_id}", None),
        ("post", "/api/v1/files/{artifact_id}/restore", None),
        (
            "post",
            "/api/v1/files/{artifact_id}/category",
            {"category": "other"},
        ),
        (
            "post",
            "/api/v1/files/{artifact_id}/share",
            {"subject_kind": "workspace", "subject_id": "{workspace_id}"},
        ),
        (
            "delete",
            "/api/v1/files/{artifact_id}/share/workspace/{workspace_id}",
            None,
        ),
        (
            "post",
            "/api/v1/files/purge",
            {"artifact_ids": ["{artifact_id}"]},
        ),
    ],
)
async def test_archived_space_file_mutations_are_rejected_before_write(
    lifecycle_ctx,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    client, artifacts, workspace_id, user_id, _session_id, artifact_id = lifecycle_ctx
    values = {
        "artifact_id": artifact_id,
        "workspace_id": workspace_id,
    }
    body = None
    if payload is not None:
        body = {
            key: (
                [item.format(**values) for item in value] if isinstance(value, list) else value.format(**values) if isinstance(value, str) else value
            )
            for key, value in payload.items()
        }

    response = client.request(
        method.upper(),
        path.format(**values),
        json=body,
        headers={
            "X-Bowhead-User-Id": user_id,
            "X-Space-Id": workspace_id,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"
    persisted = await artifacts.get(artifact_id, include_deleted=True)
    assert persisted is not None
    assert persisted.deleted_at is None
    assert persisted.category is None


async def test_archived_space_upload_is_rejected_before_artifact_write(
    lifecycle_ctx,
) -> None:
    client, artifacts, workspace_id, user_id, session_id, artifact_id = lifecycle_ctx

    response = client.post(
        "/api/v1/files/upload",
        params={"session_id": session_id},
        files={"file": ("new.txt", b"new content", "text/plain")},
        headers={
            "X-Bowhead-User-Id": user_id,
            "X-Space-Id": workspace_id,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"
    listed, total, _size = await artifacts.list_for_user(
        user_id=user_id,
        sources=None,
        limit=20,
        offset=0,
    )
    assert total == 1
    assert [item.id for item in listed] == [artifact_id]


async def test_archived_space_presign_and_confirm_are_rejected(
    lifecycle_ctx,
) -> None:
    client, artifacts, workspace_id, user_id, session_id, artifact_id = lifecycle_ctx
    headers = {
        "X-Bowhead-User-Id": user_id,
        "X-Space-Id": workspace_id,
    }

    presign = client.post(
        "/api/v1/files/presign",
        json={
            "filename": "new.txt",
            "mime": "text/plain",
            "size_bytes": 100,
            "session_id": session_id,
        },
        headers=headers,
    )
    confirm = client.post(
        f"/api/v1/files/presign/{artifact_id}/confirm",
        json={},
        headers={"X-Bowhead-User-Id": user_id},
    )

    for response in (presign, confirm):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "workspace_archived"
    listed, total, _size = await artifacts.list_for_user(
        user_id=user_id,
        sources=None,
        limit=20,
        offset=0,
    )
    assert total == 1
    assert [item.id for item in listed] == [artifact_id]


async def test_archived_space_local_receiver_rechecks_persisted_binding(
    lifecycle_ctx,
) -> None:
    client, _artifacts, _workspace_id, _user_id, _session_id, artifact_id = lifecycle_ctx
    token = jwt.encode(
        {
            "aud": "local-presign",
            "aid": artifact_id,
            "filename": "history.txt",
            "mime": "text/plain",
            "size_max": 100,
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        "files-lifecycle-secret-0123456789",
        algorithm="HS256",
    )

    response = client.post(
        f"/api/v1/files/local-receiver/{artifact_id}",
        content=b"new content",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"


async def test_removed_member_cannot_list_or_read_binding_backed_history(
    lifecycle_ctx,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="bowhead_gateway.routes.files")
    client, artifacts, _workspace_id, user_id, _session_id, artifact_id = lifecycle_ctx
    async with aiosqlite.connect(artifacts.db_path) as db:
        await db.execute(
            "DELETE FROM workspace_members WHERE user_id=?",
            (user_id,),
        )
        await db.commit()

    headers = {"X-Bowhead-User-Id": user_id}
    listed = client.get("/api/v1/files/", headers=headers)
    download = client.get(f"/api/v1/files/{artifact_id}", headers=headers)
    usage = client.get("/api/v1/files/usage", headers=headers)

    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 0
    assert listed.json()["items"] == []
    assert download.status_code == 404, download.text
    assert download.json()["detail"]["error"]["code"] == "file_not_found"
    assert "reason=workspace_access_denied" in caplog.text
    assert usage.status_code == 200, usage.text
    assert usage.json() == {"assets": 0, "run_data": 0}


async def test_usage_drops_only_the_spaces_the_member_was_removed_from(lifecycle_ctx) -> None:
    """用量按归属整批判：多个空间、多个产物时，只扣掉被移出的那个空间（单个产物走的是单条路径，证明不了批量那条）。"""
    client, artifacts, workspace_id, user_id, _session_id, _artifact_id = lifecycle_ctx
    users = client.app.state.user_store
    sessions = client.app.state.session_store
    team_id = await users.get_user_team_id(user_id)
    other_workspace = await users.create_local_workspace(team_id=team_id, name="Still Member", creator_user_id=user_id)

    async def run_data_in(ws: str, label: str, size: int) -> None:
        for i in range(3):
            session = await sessions.ensure_session(session_id=None, workspace_id=ws, user_id=user_id)
            turn_id = f"turn_{label}_{i}"
            await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text="t")
            await artifacts.put_bytes(
                content=b"x" * size,
                filename=f"{label}{i}.csv",
                mime="text/csv",
                created_by=user_id,
                kind="tool_data",
                created_by_turn=turn_id,
            )

    await run_data_in(workspace_id, "removed", 10)
    await run_data_in(other_workspace, "kept", 100)
    headers = {"X-Bowhead-User-Id": user_id}
    before = client.get("/api/v1/files/usage", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json() == {"assets": len(b"archived history"), "run_data": 330}

    async with aiosqlite.connect(artifacts.db_path) as db:
        await db.execute("DELETE FROM workspace_members WHERE user_id=? AND workspace_id=?", (user_id, workspace_id))
        await db.commit()
    after = client.get("/api/v1/files/usage", headers=headers)
    assert after.status_code == 200, after.text
    assert after.json() == {"assets": 0, "run_data": 300}


async def test_usage_access_checks_do_not_open_the_db_per_space(lifecycle_ctx, monkeypatch) -> None:
    """每个归属各判一次空间授权；判定数据走请求级缓存，开库次数不随用户所在空间数增长。"""
    client, artifacts, _workspace_id, user_id, _session_id, _artifact_id = lifecycle_ctx
    users = client.app.state.user_store
    sessions = client.app.state.session_store
    team_id = await users.get_user_team_id(user_id)
    headers = {"X-Bowhead-User-Id": user_id}

    async def one_space_with_run_data(i: int) -> None:
        ws = await users.create_local_workspace(team_id=team_id, name=f"Space {i}", creator_user_id=user_id)
        session = await sessions.ensure_session(session_id=None, workspace_id=ws, user_id=user_id)
        await sessions.record_turn(turn_id=f"turn_space_{i}", session_id=session.id, user_text="t")
        await artifacts.put_bytes(
            content=b"x", filename=f"s{i}.csv", mime="text/csv", created_by=user_id, kind="tool_data", created_by_turn=f"turn_space_{i}"
        )

    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    async def usage_opens() -> int:
        opened["n"] = 0
        monkeypatch.setattr(aiosqlite, "connect", counting)
        try:
            assert client.get("/api/v1/files/usage", headers=headers).status_code == 200
        finally:
            monkeypatch.setattr(aiosqlite, "connect", real_connect)
        return opened["n"]

    for i in range(2):
        await one_space_with_run_data(i)
    await usage_opens()  # Warm up: pools/caches built on the first process request do not count toward steady state.
    few = await usage_opens()
    for i in range(2, 6):
        await one_space_with_run_data(i)
    many = await usage_opens()
    assert many == few, f"2 个空间开库 {few} 次，6 个空间开库 {many} 次"


async def test_purge_rejects_the_whole_batch_when_any_file_is_in_a_read_only_space(lifecycle_ctx) -> None:
    """永久删除按归属整批判写权限：一批里只要有一个在只读（已归档）空间，整单拒绝，排在前面的可写文件也不删。"""
    client, artifacts, _workspace_id, user_id, _session_id, archived_artifact = lifecycle_ctx
    users = client.app.state.user_store
    sessions = client.app.state.session_store
    writable_ws = await users.create_local_workspace(team_id=await users.get_user_team_id(user_id), name="Writable", creator_user_id=user_id)
    writable = []
    for i in range(3):
        session = await sessions.ensure_session(session_id=None, workspace_id=writable_ws, user_id=user_id)
        await sessions.record_turn(turn_id=f"turn_purge_{i}", session_id=session.id, user_text="t")
        art = await artifacts.put_bytes(
            content=b"x", filename=f"p{i}.csv", mime="text/csv", created_by=user_id, kind="tool_data", created_by_turn=f"turn_purge_{i}"
        )
        writable.append(art.id)
    async with aiosqlite.connect(artifacts.db_path) as db:
        await db.execute(
            f"UPDATE artifacts SET deleted_at='2026-09-01T00:00:00+00:00' WHERE id IN ({','.join('?' * (len(writable) + 1))})",  # noqa: S608
            (*writable, archived_artifact),
        )
        await db.commit()

    response = client.post("/api/v1/files/purge", json={"artifact_ids": [*writable, archived_artifact]}, headers={"X-Bowhead-User-Id": user_id})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"
    for artifact_id in writable:
        assert await artifacts.get(artifact_id, include_deleted=True) is not None
