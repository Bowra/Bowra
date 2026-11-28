"""Delivery evidence and reviewed cleanup against the migrated, split SQLite layout."""

import json
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_pooled_read_ctx, run_sqlite_authority_write
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.user_store import UserStore

from bowhead_core import file_library, file_retention


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    directory = tmp_path_factory.mktemp("file-library-template") / "sqlite"
    url = f"sqlite:///{directory / 'bowhead.db'}"
    migration = import_module("bowhead_core.migrations.versions.0238_file_library")
    with pytest.MonkeyPatch.context() as previous:
        previous.setattr(ScriptDirectory, "get_heads", lambda _scripts: [migration.down_revision])
        upgrade(url)
    content = directory / "bowhead_content.db"
    with closing(connect_sync(content)) as db:
        db.execute("""INSERT INTO artifacts(id,filename,mime,size,source,path,created_at)
            VALUES('art_legacy_library','legacy.txt','text/plain',4,'upload','legacy.txt','2024-01-01')""")
        db.commit()
    upgrade(url)
    with closing(connect_sync(content)) as db:
        assert db.execute("SELECT filename,deleted_at FROM artifacts WHERE id='art_legacy_library'").fetchone() == ("legacy.txt", None)
        assert db.execute("SELECT COUNT(*) FROM file_retention_state").fetchone() == (0,)
    return directory


@pytest.fixture
async def library_ctx(tmp_path, layout, monkeypatch):
    monkeypatch.setenv("FEATURE_FILE_LIBRARY", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    directory = tmp_path / "sqlite"
    shutil.copytree(layout, directory, ignore=shutil.ignore_patterns("*.lock"))
    core = str(directory / "bowhead.db")
    us = UserStore(core)
    await us.initialize()
    team = await us.create_team(name="Files", max_seats=10)
    user = await us.create_user(team_id=team.id, email="files@example.test", password="Passw0rd!")

    async def make_owner(db):
        await db.execute("UPDATE teams SET created_by_user_id=? WHERE id=?", (user.id, team.id))

    await run_sqlite_authority_write(core, make_owner)
    space = await us.create_local_workspace(team_id=team.id, name="Files", creator_user_id=user.id)
    ss = SessionStore(core)
    await ss.initialize()
    session = await ss.ensure_session(session_id=None, workspace_id=space, user_id=user.id)
    turn = "t_filelibrary0001"
    await ss.record_turn(turn_id=turn, session_id=session.id, user_text="Prepare a file")
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "objects"), db_path=core)
    await store.initialize()

    async def save(name, kind):
        return await store.put_bytes(
            content=name.encode(),
            filename=name,
            mime="text/plain",
            source="sandbox_output",
            kind=kind,
            created_by=user.id,
            workspace_id=space,
            created_by_turn=turn,
        )

    delivered = await save("delivered.txt", "tool_data")
    transient = await save("scratch.txt", "tool_data")
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()

    async def finish(db):
        await db.execute("UPDATE turns SET status='completed',ended_at=? WHERE id=?", (old, turn))
        await db.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES(?,?,99,'lead.final.payload',?,?)",
            ("evt_filelibrary0001", turn, json.dumps({"inline_artifacts": [{"kind": "file", "id": delivered.id}]}), old),
        )

    await run_sqlite_authority_write(domain_database_path(core, "execution"), finish)
    return SimpleNamespace(
        core=core,
        store=store,
        user=user.id,
        team=team.id,
        kb=KbFilesStore(directory / "kb_files.db"),
        sid=session.id,
        delivered=delivered.id,
        transient=transient.id,
        space=space,
        users=us,
        sessions=ss,
    )


@pytest.mark.anyio
async def test_delivery_and_reviewed_cleanup(library_ctx):
    c = library_ctx
    async with open_pooled_read_ctx(c.store.db_path) as db:
        deliveries = await file_library.deliveries(db, c.user)
        assert [d.artifact_id for d in deliveries] == [c.delivered]
        rows = await file_library.versions(db, c.user, roots={c.delivered})
        assert [r.art.id for r in rows] == [c.delivered]
    reviewed = await file_retention.review(c.core, c.kb, c.team, c.user, 30, 0)
    assert reviewed["impact"]["eligible"]["count"] == 1
    assert reviewed["impact"]["delivered"]["count"] == 1
    result = await file_retention.apply(c.core, c.team, c.user, reviewed["review_id"], True)
    assert result["days"] == 30 and result["version"] == 1
    assert await file_retention.apply(c.core, c.team, c.user, reviewed["review_id"], True) == result
    with pytest.raises(file_retention.PolicyError, match="stale"):
        await file_retention.apply(c.core, c.team, c.user, reviewed["review_id"], False)
    assert (await file_retention.collect(c.store, c.kb))["files"] == 1
    assert await c.store.get(c.delivered)
    assert await c.store.restore(c.transient, user_id=c.user)
    assert (await file_retention.collect(c.store, c.kb))["files"] == 0
    assert await c.store.get(c.transient)


def http_library(c):
    from bowhead_gateway.routes import file_library as routes
    from bowhead_gateway.routes import files
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.state.artifact_store = c.store
    app.state.kb_files_store = c.kb
    app.state.user_store = c.users
    app.state.session_store = c.sessions
    app.include_router(routes.router)
    app.include_router(files.router, prefix="/api/v1/files")
    return TestClient(app, headers={"X-Bowhead-User-Id": c.user, "X-Space-Id": c.space})


@pytest.mark.anyio
async def test_library_http_filters_focus_and_permissions(library_ctx, monkeypatch):
    c = library_ctx
    monkeypatch.setenv("FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE", "1")
    with http_library(c) as client:
        resp = client.get("/api/v1/files/library")
        assert resp.status_code == 200, resp.text
        assert [i["file_id"] for i in resp.json()["items"]] == [c.delivered]
        assert resp.json()["items"][0]["delivered_at"]
        lookup = client.get("/api/v1/files/library/items/" + c.transient)
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()["views"] == ["all", "run"]
        assert not client.get("/api/v1/files/library?session_id=other").json()["items"]
        first = client.get("/api/v1/files/library?view=all&limit=1").json()
        second = client.get("/api/v1/files/library", params={"view": "all", "limit": 1, "cursor": first["next_cursor"]}).json()
        assert {first["items"][0]["file_id"], second["items"][0]["file_id"]} == {c.delivered, c.transient}
        assert second["next_cursor"] is None
        assert client.get("/api/v1/files/library", params={"view": "run", "cursor": first["next_cursor"]}).status_code == 422
        assert client.get("/api/v1/files/library/summary").json()["counts"] == {"all": 2, "run": 2, "deliverables": 1}
        other = await c.users.create_user(team_id=c.team, email="outsider@example.test", password="Passw0rd!")
        assert client.get("/api/v1/files/library/items/" + c.delivered, headers={"X-Bowhead-User-Id": other.id}).status_code in (403, 404)
        monkeypatch.setenv("FEATURE_FILE_LIBRARY", "0")
        assert client.get("/api/v1/files/library").status_code == 404


@pytest.mark.anyio
async def test_changed_reference_and_review_conflict(library_ctx):
    c = library_ctx
    first = await file_retention.review(c.core, c.kb, c.team, c.user, 30, 0)
    second = await file_retention.review(c.core, c.kb, c.team, c.user, 0, 0)
    await file_retention.apply(c.core, c.team, c.user, first["review_id"], True)
    with pytest.raises(file_retention.PolicyError, match="stale"):
        await file_retention.apply(c.core, c.team, c.user, second["review_id"], True)
    await c.sessions.pin_result(user_id=c.user, target_kind="file", target_id=c.transient)
    assert (await file_retention.collect(c.store, c.kb))["files"] == 0
    assert await c.store.get(c.transient)
    with pytest.raises(file_retention.PolicyError, match="forbidden"):
        await file_retention.review(c.core, c.kb, c.team, "another-user", 30, 1)


@pytest.mark.anyio
async def test_retention_rechecks_before_purge_and_counts_acknowledged_bytes(library_ctx):
    c = library_ctx
    assert (await file_retention.collect(c.store, c.kb))["files"] == 1
    review = await file_retention.review(c.core, c.kb, c.team, c.user, 0, 0)
    await file_retention.apply(c.core, c.team, c.user, review["review_id"], True)
    assert (await c.store.hard_delete_old_soft_deleted(days=0))["rows_removed"] == 0
    assert await c.store.restore(c.transient, user_id=c.user)
    assert await c.store.soft_delete(c.transient, user_id=c.user)
    result = await c.store.hard_delete_old_soft_deleted(days=0)
    assert result["rows_removed"] == 1
    assert result["bytes_reclaimed"] == len("scratch.txt")


@pytest.mark.anyio
async def test_knowledge_original_and_late_reference(library_ctx):
    import sqlite3

    c = library_ctx

    async def add_reference():
        return await c.kb.create(
            artifact_id=c.transient,
            filename="scratch.txt",
            mime_type="text/plain",
            size_bytes=11,
            scope="space",
            scope_id=c.space,
            workspace_id=c.space,
            team_id=c.team,
            uploaded_by=c.user,
        )

    reviewed = await file_retention.review(c.core, c.kb, c.team, c.user, 30, 0)
    reference = await add_reference()
    await file_retention.apply(c.core, c.team, c.user, reviewed["review_id"], True)
    assert (await file_retention.collect(c.store, c.kb))["files"] == 0
    await c.kb.soft_delete(reference)
    assert (await file_retention.collect(c.store, c.kb))["files"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="Restore the file"):
        await add_reference()
    await c.store.restore(c.transient, user_id=c.user)
    assert await add_reference()


@pytest.mark.anyio
async def test_denied_lineages_do_not_consume_the_page(library_ctx, monkeypatch):
    c = library_ctx
    monkeypatch.setenv("FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE", "1")
    foreign = await c.users.create_team(name="Other team", max_seats=2)
    outsider = await c.users.create_user(team_id=foreign.id, email="outside@example.test", password="Passw0rd!")
    space = await c.users.create_local_workspace(team_id=foreign.id, name="Hidden", creator_user_id=outsider.id)

    async def seed(db):
        await db.execute(
            """WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<205)
            INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,workspace_id,kind)
            SELECT printf('art_denied%08d',i),'hidden.csv','text/csv',8,'sandbox_output',printf('hidden/%d',i),?,
                '2099-01-01T00:00:00+00:00',?,'tool_data' FROM n""",
            (c.user, space),
        )

    await run_sqlite_authority_write(c.store.db_path, seed)
    with http_library(c) as client:
        rows = client.get("/api/v1/files/library", params={"view": "all", "limit": 2})
        assert rows.status_code == 200, rows.text
        assert {i["file_id"] for i in rows.json()["items"]} == {c.delivered, c.transient}
        assert rows.json()["next_cursor"] is None
        denied = client.get("/api/v1/files/library/items/art_denied00000001")
        assert denied.status_code == 404
        assert client.get("/api/v1/files/library/summary").json()["counts"]["all"] == 2


@pytest.mark.anyio
async def test_native_receipt_and_replaced_final_cards(library_ctx):
    from bowhead_core.artifact_presentation import CONTRACT

    c = library_ctx
    turn = "t_filelibrary0001"
    receipt = {
        "turn_id": turn,
        "session_id": c.sid,
        "user_id": c.user,
        "workspace_id": c.space,
        "team_id": c.team,
        "tool_name": "present_artifacts",
        "generation": 1,
        "call_key": "a" * 64,
    }
    payload = {
        "ok": True,
        "tool_name": "present_artifacts",
        "result_meta": {
            "artifact_presentation": {"contract": CONTRACT, "origin": "explicit_tool", "items": [{"artifact_id": c.transient}]},
            "artifact_presentation_receipt": receipt,
        },
    }

    async def record(db):
        for seq, kind, data in [(100, "lead.final.payload", {"inline_artifacts": []}), (101, "tool_result", payload)]:
            await db.execute(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES(?,?,?,?,?,?)",
                (f"evt_native{seq}", turn, seq, kind, json.dumps(data), datetime.now(UTC).isoformat()),
            )

    await run_sqlite_authority_write(domain_database_path(c.core, "execution"), record)
    async with open_pooled_read_ctx(c.store.db_path) as db:
        assert [p.artifact_id for p in await file_library.deliveries(db, c.user)] == [c.transient]


@pytest.mark.anyio
async def test_team_policy_http_requires_review_and_admin(library_ctx, monkeypatch):
    from bowhead_core.entitlements import CommunityResolver
    from bowhead_gateway.routes import team_settings
    from test_team_chart_styles_routes import SECRET, _as

    c = library_ctx
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    member = await c.users.create_user(team_id=c.team, email="team-member@example.test", password="Passw0rd!")
    owner = await c.users.get_user(c.user)
    with http_library(c) as client:
        client.app.include_router(team_settings.router)
        client.app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
        client.app.state.entitlements = CommunityResolver(mode="local", user_store=c.users)
        url = "/api/v1/team-settings/file-retention"
        client.cookies.update(_as(member))
        assert client.get(url).status_code == 403
        client.cookies.update(_as(owner))
        initial = client.get(url)
        assert initial.status_code == 200, initial.text
        assert initial.json()["effective_days"] == 30
        for invalid in (3, False, 0.0):
            assert client.post(url + "/review", json={"days": invalid, "expected_version": 0}).status_code == 422
        reviewed = client.post(url + "/review", json={"days": 90, "expected_version": 0})
        assert reviewed.status_code == 200, reviewed.text
        assert client.get(url).json()["version"] == 0
        changed = client.put(url, json={"review_id": reviewed.json()["review_id"], "include_existing": False})
        assert changed.status_code == 200, changed.text
        assert changed.json()["days"] == 90 and changed.json()["version"] == 1


@pytest.mark.anyio
async def test_trash_deadlines_and_restore_are_version_specific(library_ctx):
    c = library_ctx
    now = datetime.now(UTC)
    stamps = {c.transient: now - timedelta(days=9), c.delivered: now - timedelta(days=2)}

    async def trash(db):
        await db.executemany("UPDATE artifacts SET deleted_at=? WHERE id=?", [(stamp.isoformat(), aid) for aid, stamp in stamps.items()])

    await run_sqlite_authority_write(c.store.db_path, trash)
    with http_library(c) as client:
        response = client.get("/api/v1/files/library", params={"view": "trash"})
        assert response.status_code == 200, response.text
        deadlines = {r["file_id"]: datetime.fromisoformat(r["recover_until"]) for r in response.json()["items"]}
        assert deadlines == {aid: stamp + timedelta(days=30) for aid, stamp in stamps.items()}
        assert not client.get("/api/v1/files/library").json()["items"]
    await c.store.restore(c.delivered, user_id=c.user)
    with http_library(c) as client:
        assert client.get("/api/v1/files/library/summary").json()["counts"] == {"all": 1, "deliverables": 1, "run": 1, "trash": 1}
