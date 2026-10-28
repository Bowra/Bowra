"""Space instructions routes: Space-scoped access, versioning and actionable write errors."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import httpx2
import pytest
from bowhead_core.space_instructions import MAX_CONTENT_CHARS, SpaceInstructionsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes.space_instructions import router
from fastapi import FastAPI

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialize_migrated_core_database) -> FastAPI:
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    db = tmp_path / "bowhead.db"
    initialize_migrated_core_database(db)
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    member = await users.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!")
    team_admin = await users.create_user(team_id=team.id, email="team-admin@example.com", password="Passw0rd!")
    outsider = await users.create_user(team_id=team.id, email="outsider@example.com", password="Passw0rd!")
    auditor = await users.create_user(team_id=team.id, email="auditor@example.com", password="Passw0rd!")
    async with aiosqlite.connect(users.db_path) as conn:
        await conn.execute(
            "UPDATE team_members SET role = 'team_admin' WHERE team_id = ? AND user_id = ?",
            (team.id, team_admin.id),
        )
        await conn.execute(
            "UPDATE team_members SET role = 'audit_admin' WHERE team_id = ? AND user_id = ?",
            (team.id, auditor.id),
        )
        await conn.commit()
    ws_id = await users.create_local_workspace(team_id=team.id, name="Pharma", creator_user_id=admin.id)
    await users.add_member(workspace_id=ws_id, user_id=member.id, role="member")
    await users.add_member(workspace_id=ws_id, user_id=auditor.id, role="member")

    class _Settings:
        auth_mode = "dev"

    app = FastAPI()
    app.include_router(router)
    app.state.settings = _Settings()
    app.state.user_store = users
    app.state.space_instructions_store = SpaceInstructionsStore(db)
    app.state.ids = {"admin": admin.id, "member": member.id, "team_admin": team_admin.id, "auditor": auditor.id, "outsider": outsider.id, "ws": ws_id}
    return app


async def _call(app: FastAPI, method: str, path: str, who: str, **kwargs):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.request(method, path, headers={"x-bowhead-user-id": app.state.ids[who]}, **kwargs)


def _path(app: FastAPI, suffix: str = "") -> str:
    return f"/api/v1/space-instructions/{app.state.ids['ws']}{suffix}"


@pytest.mark.asyncio
async def test_admin_saves_versions_and_members_read_current(app: FastAPI) -> None:
    empty = await _call(app, "GET", _path(app), "member")
    assert empty.status_code == 200
    assert empty.json() | {"max_chars": None} == {
        "workspace_id": app.state.ids["ws"],
        "version": 0,
        "content": "",
        "updated_by_name": None,
        "updated_at": None,
        "max_chars": None,
        "can_edit": False,
        "archived": False,
        "administrators": ["admin@example.com"],
    }

    first = await _call(app, "PUT", _path(app), "admin", json={"content": "  Audience: sales leads.  ", "expected_version": 0})
    assert first.status_code == 200
    assert (first.json()["version"], first.json()["content"], first.json()["can_edit"]) == (1, "Audience: sales leads.", True)
    second = await _call(app, "PUT", _path(app), "admin", json={"content": "Audience: executives.", "expected_version": 1})
    assert second.json()["version"] == 2

    read = await _call(app, "GET", _path(app), "member")
    assert (read.json()["version"], read.json()["content"], read.json()["can_edit"]) == (2, "Audience: executives.", False)

    history = await _call(app, "GET", _path(app, "/versions"), "admin")
    assert [(item["version"], item["content"]) for item in history.json()["versions"]] == [
        (2, "Audience: executives."),
        (1, "Audience: sales leads."),
    ]
    assert history.json()["versions"][0]["created_by_name"] == "admin"


@pytest.mark.asyncio
async def test_restore_appends_a_new_version_with_its_source(app: FastAPI) -> None:
    await _call(app, "PUT", _path(app), "admin", json={"content": "v1", "expected_version": 0})
    await _call(app, "PUT", _path(app), "admin", json={"content": "v2", "expected_version": 1})
    restored = await _call(app, "PUT", _path(app), "admin", json={"content": "v1", "expected_version": 2, "restored_from": 1})
    assert (restored.json()["version"], restored.json()["content"]) == (3, "v1")
    history = await _call(app, "GET", _path(app, "/versions"), "admin")
    assert history.json()["versions"][0]["restored_from"] == 1
    missing = await _call(app, "PUT", _path(app), "admin", json={"content": "v9", "expected_version": 3, "restored_from": 9})
    assert (missing.status_code, missing.json()["detail"]) == (404, {"code": "restore_source_missing", "version": 9})


@pytest.mark.asyncio
async def test_clearing_keeps_history(app: FastAPI) -> None:
    await _call(app, "PUT", _path(app), "admin", json={"content": "Use quarterly views.", "expected_version": 0})
    cleared = await _call(app, "PUT", _path(app), "admin", json={"content": "   ", "expected_version": 1})
    assert (cleared.json()["version"], cleared.json()["content"]) == (2, "")
    history = await _call(app, "GET", _path(app, "/versions"), "admin")
    assert [item["content"] for item in history.json()["versions"]] == ["", "Use quarterly views."]


@pytest.mark.asyncio
async def test_write_errors_carry_the_condition_to_resolve(app: FastAPI) -> None:
    await _call(app, "PUT", _path(app), "admin", json={"content": "v1", "expected_version": 0})
    stale = await _call(app, "PUT", _path(app), "admin", json={"content": "other", "expected_version": 0})
    assert (stale.status_code, stale.json()["detail"]) == (409, {"code": "stale_version", "current_version": 1})
    same = await _call(app, "PUT", _path(app), "admin", json={"content": " v1 ", "expected_version": 1})
    assert (same.status_code, same.json()["detail"]["code"]) == (409, "unchanged")
    too_long = await _call(app, "PUT", _path(app), "admin", json={"content": "x" * (MAX_CONTENT_CHARS + 1), "expected_version": 1})
    assert (too_long.status_code, too_long.json()["detail"]) == (
        422,
        {"code": "too_long", "length": MAX_CONTENT_CHARS + 1, "limit": MAX_CONTENT_CHARS},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["member", "auditor"])
async def test_members_and_other_team_roles_cannot_write_or_list_history(app: FastAPI, who: str) -> None:
    write = await _call(app, "PUT", _path(app), who, json={"content": "x", "expected_version": 0})
    assert write.status_code == 403
    history = await _call(app, "GET", _path(app, "/versions"), who)
    assert history.status_code == 403


@pytest.mark.asyncio
async def test_team_admin_manages_instructions_without_joining_the_space(app: FastAPI) -> None:
    read = await _call(app, "GET", _path(app), "team_admin")
    assert (read.status_code, read.json()["can_edit"]) == (200, True)
    saved = await _call(app, "PUT", _path(app), "team_admin", json={"content": "Use fiscal quarters.", "expected_version": 0})
    assert (saved.status_code, saved.json()["version"]) == (200, 1)
    history = await _call(app, "GET", _path(app, "/versions"), "team_admin")
    assert [item["content"] for item in history.json()["versions"]] == ["Use fiscal quarters."]


@pytest.mark.asyncio
async def test_non_members_cannot_read(app: FastAPI) -> None:
    response = await _call(app, "GET", _path(app), "outsider")
    assert response.status_code == 403
    other_team = await _call(app, "GET", "/api/v1/space-instructions/ws_unknown", "member")
    assert other_team.status_code == 404


@pytest.mark.asyncio
async def test_archived_space_rejects_writes(app: FastAPI) -> None:
    async with aiosqlite.connect(app.state.user_store.db_path) as conn:
        await conn.execute("UPDATE workspaces SET status='archived' WHERE id=?", (app.state.ids["ws"],))
        await conn.commit()
    response = await _call(app, "PUT", _path(app), "admin", json={"content": "x", "expected_version": 0})
    assert (response.status_code, response.json()["detail"]) == (409, {"code": "workspace_archived"})


@pytest.mark.asyncio
async def test_archived_space_reports_why_editing_is_unavailable(app: FastAPI) -> None:
    async with aiosqlite.connect(app.state.user_store.db_path) as conn:
        await conn.execute("UPDATE workspaces SET status='archived' WHERE id=?", (app.state.ids["ws"],))
        await conn.commit()
    response = await _call(app, "GET", _path(app), "admin")
    assert (response.json()["can_edit"], response.json()["archived"]) == (False, True)


@pytest.mark.asyncio
async def test_document_generation_uses_the_version_its_originating_turn_received(app: FastAPI, tmp_path: Path) -> None:
    """Every model call of a document job sees the originating Turn's instructions, not today's."""
    from types import SimpleNamespace

    from bowhead_gateway.document_service import DocumentService, _with_space_block

    store = app.state.space_instructions_store
    await store.save(workspace_id=app.state.ids["ws"], content="Write for executives.", created_by="u", expected_version=0)
    await store.snapshot_for_turn(turn_id="t_origin", workspace_id=app.state.ids["ws"])
    await store.save(workspace_id=app.state.ids["ws"], content="Write for engineers.", created_by="u", expected_version=1)
    # Only the job -> originating Turn lookup matters here; admission triggers guard the real table.
    jobs_db = tmp_path / "jobs.db"
    async with aiosqlite.connect(jobs_db) as conn:
        await conn.execute("CREATE TABLE document_jobs (id TEXT PRIMARY KEY, origin_turn_id TEXT NOT NULL)")
        await conn.execute("INSERT INTO document_jobs VALUES ('doc_1', 't_origin')")
        await conn.commit()
    service = object.__new__(DocumentService)
    service.state = SimpleNamespace(space_instructions_store=store)
    service.delivery = SimpleNamespace(path=str(jobs_db))
    block = await service._space_instructions_block("doc_1")
    assert "Write for executives." in block
    assert "Write for engineers." not in _with_space_block("SYS", block)
    assert await service._space_instructions_block("doc_missing") == ""


@pytest.mark.asyncio
async def test_readers_learn_who_administers_the_space(app: FastAPI) -> None:
    read = await _call(app, "GET", _path(app), "member")
    assert read.json()["administrators"] == ["admin@example.com"]


@pytest.mark.asyncio
async def test_saving_adopted_wording_records_its_suggestion(app: FastAPI) -> None:
    from types import SimpleNamespace

    async with aiosqlite.connect(app.state.user_store.db_path) as conn:
        await conn.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES('s_src',?,?,'2026-10-16','2026-10-16')",
            (app.state.ids["member"], app.state.ids["ws"]),
        )
        await conn.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_src','s_src',1,'q','2026-10-16')")
        await conn.commit()
    here = [{"provenance_refs": ["t_src"]}]
    candidates = {
        "fbc_ok": SimpleNamespace(id="fbc_ok", team_id=None, target_kind="semantic", state="accepted", evidence=here),
        "fbc_open": SimpleNamespace(id="fbc_open", team_id=None, target_kind="semantic", state="open", evidence=here),
        "fbc_elsewhere": SimpleNamespace(id="fbc_elsewhere", team_id=None, target_kind="semantic", state="accepted", evidence=[]),
    }
    team_id = (await app.state.user_store.get_workspace(app.state.ids["ws"]))["team_id"]
    for candidate in candidates.values():
        candidate.team_id = team_id

    class _Candidates:
        async def get(self, candidate_id: str):
            return candidates.get(candidate_id)

    app.state.feedback_candidate_store = _Candidates()
    missing = await _call(app, "PUT", _path(app), "admin", json={"content": "x", "expected_version": 0, "source_candidate_id": "fbc_gone"})
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "source_candidate_missing")
    still_open = await _call(app, "PUT", _path(app), "admin", json={"content": "x", "expected_version": 0, "source_candidate_id": "fbc_open"})
    assert (still_open.status_code, still_open.json()["detail"]["code"]) == (409, "source_candidate_not_adopted")
    elsewhere = await _call(app, "PUT", _path(app), "admin", json={"content": "x", "expected_version": 0, "source_candidate_id": "fbc_elsewhere"})
    assert (elsewhere.status_code, elsewhere.json()["detail"]["code"]) == (409, "source_candidate_other_space")
    saved = await _call(app, "PUT", _path(app), "admin", json={"content": "x", "expected_version": 0, "source_candidate_id": "fbc_ok"})
    assert saved.status_code == 200
    history = await _call(app, "GET", _path(app, "/versions"), "admin")
    assert history.json()["versions"][0]["source_candidate_id"] == "fbc_ok"
