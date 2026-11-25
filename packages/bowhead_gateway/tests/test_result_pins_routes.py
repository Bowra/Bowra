"""Result pins routes and resolver (docs/sprints/result-pins-2026-11-24/tech-design.md §4).

A migrated domain layout is built once per module and copied per test. Share rules are the shared-file
viewer's and the report read path's own; their behavior has its own tests, so these cases replace them
with stand-ins and check that the list follows their verdict.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import result_pins
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_FERNET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_TURN = "t_pinroutes001"


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory):
    from bowhead_cli.migrate import upgrade

    core = tmp_path_factory.mktemp("result-pins-routes") / "sqlite" / "bowhead.db"
    upgrade(f"sqlite:///{core}")
    return core.parent


def _app(state: dict) -> TestClient:
    app = FastAPI()
    app.include_router(result_pins.router, prefix="/api/v1/result-pins")
    for key, value in state.items():
        setattr(app.state, key, value)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
async def ctx(tmp_path, migrated_template, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", _FERNET)
    directory = tmp_path / "sqlite"
    shutil.copytree(migrated_template, directory, ignore=shutil.ignore_patterns("*.lock"))
    core = str(directory / "bowhead.db")
    users = UserStore(core)
    await users.initialize()
    team = await users.create_team(name="Pins", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="pins-owner@example.com", password="Passw0rd!")
    peer = await users.create_user(team_id=team.id, email="pins-peer@example.com", password="Passw0rd!")
    ws = await users.create_local_workspace(team_id=team.id, name="Pins", creator_user_id=owner.id)
    ws_other = await users.create_local_workspace(team_id=team.id, name="Other", creator_user_id=owner.id)
    sessions = SessionStore(core)
    await sessions.initialize()
    session = await sessions.ensure_session(session_id=None, workspace_id=ws, user_id=owner.id)
    await sessions.record_turn(turn_id=_TURN, session_id=session.id, user_text="make a deck")
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "files"), db_path=core)
    await artifacts.initialize()
    canvases = CanvasStore(core)
    await canvases.initialize()
    state = {
        "artifact_store": artifacts,
        "session_store": sessions,
        "user_store": users,
        "canvas_store": canvases,
        "settings": SimpleNamespace(dev_default_user=owner.id, auth_mode="dev"),
    }
    return SimpleNamespace(
        client=_app(state),
        state=state,
        artifacts=artifacts,
        sessions=sessions,
        canvases=canvases,
        owner=owner.id,
        peer=peer.id,
        ws=ws,
        ws_other=ws_other,
        session_id=session.id,
    )


def _h(c, user=None, ws=None):
    return {"X-Bowhead-User-Id": user or c.owner, "X-Space-Id": ws or c.ws}


async def _deck(c, content: bytes, **kw):
    fields = {
        "content": content,
        "filename": "deck.pptx",
        "mime": _PPTX,
        "source": "sandbox_output",
        "created_by": c.owner,
        "session_id": c.session_id,
        "kind": "deliverable",
        "created_by_turn": _TURN,
        "workspace_id": c.ws,
    }
    fields.update(kw)
    return await c.artifacts.put_bytes(**fields)


async def _upload(c, content: bytes, *, owner=None, ws=None):
    return await c.artifacts.put_bytes(
        content=content,
        filename="notes.txt",
        mime="text/plain",
        source="upload",
        created_by=owner or c.owner,
        kind="user_upload",
        workspace_id=ws or c.ws,
    )


def _list(c, **params):
    response = c.client.get("/api/v1/result-pins", params=params, headers=_h(c))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _pin(c, kind, target, user=None):
    return c.client.post("/api/v1/result-pins", json={"kind": kind, "id": target}, headers=_h(c, user))


async def test_a_file_pin_follows_the_newest_version_and_opens_in_its_conversation(ctx) -> None:
    c = ctx
    v1 = await _deck(c, b"v1")
    pinned = _pin(c, "file", v1.id)
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() | {"pinned_at": None, "changed_at": None} == {
        "kind": "file",
        "pin_id": v1.id,
        "artifact_id": v1.id,
        "title": "deck.pptx",
        "mime": _PPTX,
        "version_no": 1,
        "session_id": c.session_id,
        "opens": "session",
        "pinned_at": None,
        "changed_at": None,
    }
    v2 = await _deck(c, b"v2", version_of_target=v1.id)
    (item,) = _list(c, workspace_id=c.ws)
    assert (item["pin_id"], item["artifact_id"], item["version_no"]) == (v1.id, v2.id, 2)
    # Pinning another version of the same file keeps one pin.
    assert _pin(c, "file", v2.id).json()["pin_id"] == v1.id
    assert len(_list(c)) == 1
    # Unpinning works from any version, also a deleted one.
    await c.artifacts.soft_delete(v1.id, user_id=c.owner)
    assert c.client.delete(f"/api/v1/result-pins/file/{v1.id}", headers=_h(c)).status_code == 204
    assert _list(c) == []


async def test_an_upload_without_a_conversation_opens_in_files(ctx) -> None:
    up = await _upload(ctx, b"loose")
    assert _pin(ctx, "file", up.id).json()["opens"] == "files"
    (item,) = _list(ctx)
    assert (item["opens"], item["session_id"]) == ("files", None)


async def test_unreadable_and_missing_targets_answer_alike_and_write_nothing(ctx) -> None:
    c = ctx
    theirs = await _upload(c, b"theirs", owner=c.peer)
    for kind, target in (("file", theirs.id), ("file", "art_missing00"), ("report", "cv_missing00")):
        response = _pin(c, kind, target)
        assert response.status_code == 404, (target, response.text)
        assert response.json()["detail"]["error"]["code"] == "result_pin_target_not_found"
    assert await c.sessions.list_result_pins(user_id=c.owner) == []
    assert _pin(c, "chart", "x").status_code == 422
    assert _pin(c, "file", "").status_code == 422


async def test_trash_hides_a_pin_and_restore_brings_it_back(ctx) -> None:
    c = ctx
    deck = await _deck(c, b"deck")
    _pin(c, "file", deck.id)
    await c.artifacts.soft_delete(deck.id, user_id=c.owner)
    assert _list(c) == []
    await c.artifacts.restore(deck.id, user_id=c.owner)
    assert [item["artifact_id"] for item in _list(c)] == [deck.id]


async def test_own_results_of_another_space_are_left_out(ctx) -> None:
    c = ctx
    here = await _upload(c, b"here")
    there = await _upload(c, b"there", ws=c.ws_other)
    _pin(c, "file", here.id)
    assert c.client.post("/api/v1/result-pins", json={"kind": "file", "id": there.id}, headers=_h(c, ws=c.ws_other)).status_code == 200
    assert [item["artifact_id"] for item in _list(c, workspace_id=c.ws)] == [here.id]
    assert {item["artifact_id"] for item in _list(c)} == {here.id, there.id}


async def test_an_own_file_hides_when_its_space_is_no_longer_readable(ctx) -> None:
    c = ctx
    users = c.state["user_store"]
    # The peer is an ordinary member (the team's first user owns the team and reads every Space).
    await users.add_member(workspace_id=c.ws_other, user_id=c.peer, role="member")
    # A file whose Space can be derived (its conversation's); unattributed files keep today's fail-open rule.
    peer_session = await c.sessions.ensure_session(session_id=None, workspace_id=c.ws_other, user_id=c.peer)
    await c.sessions.record_turn(turn_id="t_pinpeer00001", session_id=peer_session.id, user_text="deck")
    mine = await _deck(c, b"peer-deck", created_by=c.peer, session_id=peer_session.id, created_by_turn="t_pinpeer00001", workspace_id=c.ws_other)
    peer = _h(c, c.peer, c.ws_other)
    assert c.client.post("/api/v1/result-pins", json={"kind": "file", "id": mine.id}, headers=peer).status_code == 200
    assert [item["artifact_id"] for item in c.client.get("/api/v1/result-pins", headers=peer).json()["items"]] == [mine.id]
    await users.remove_member(c.ws_other, c.peer)
    assert c.client.get("/api/v1/result-pins", headers=_h(c, c.peer)).json()["items"] == []
    # The row stays, so the pin returns if access returns.
    assert [target for _, target, _ in await c.sessions.list_result_pins(user_id=c.peer)] == [mine.id]


async def test_a_shared_file_follows_the_shared_viewer_rule(ctx, monkeypatch) -> None:
    from bowhead_gateway.routes import files as files_routes

    c = ctx
    page = await _upload(c, b"<html>", owner=c.peer)
    access = {"granted": True}

    async def shared_rule(request, file_id, *, viewer_id=None):
        if access["granted"] and file_id == page.id and viewer_id == c.owner:
            return page
        raise HTTPException(404)

    monkeypatch.setattr(files_routes, "_read_shared_html", shared_rule)
    await c.sessions.pin_result(user_id=c.owner, target_kind="file", target_id=page.id)
    (item,) = _list(c, workspace_id=c.ws_other)  # shared results show in every Space (PRD D6)
    assert (item["pin_id"], item["opens"], item["session_id"]) == (page.id, "shared_view", None)
    access["granted"] = False
    assert _list(c) == []


async def test_reports_list_only_while_they_hold_a_report(ctx) -> None:
    c = ctx
    canvas = await c.canvases.ensure_canvas(session_id=c.session_id, workspace_id=c.ws, user_id=c.owner, title=None)
    await c.canvases.append_section(canvas_id=canvas.id, kind="code_block", payload={"code": "1+1", "call_id": "call_1"})
    # A canvas with code only is not a report: nothing to open from the sidebar.
    assert _pin(c, "report", canvas.id).status_code == 404
    await c.canvases.append_section(canvas_id=canvas.id, kind="narrative", payload={"h1": "Q3 review", "markdown": "text"})
    pinned = _pin(c, "report", canvas.id)
    assert pinned.status_code == 200, pinned.text
    body = pinned.json()
    assert (body["kind"], body["title"], body["first_section_title"], body["session_id"], body["opens"]) == (
        "report",
        "",
        "Q3 review",
        c.session_id,
        "session",
    )
    await c.canvases.clear_sections(canvas_id=canvas.id)
    assert _list(c) == []


async def test_a_shared_report_follows_the_report_read_path(ctx, monkeypatch) -> None:
    from bowhead_gateway.routes import canvas as canvas_routes

    c = ctx
    peer_session = await c.sessions.ensure_session(session_id=None, workspace_id=c.ws, user_id=c.peer)
    canvas = await c.canvases.ensure_canvas(session_id=peer_session.id, workspace_id=c.ws, user_id=c.peer, title="Peer report")
    await c.canvases.append_section(canvas_id=canvas.id, kind="narrative", payload={"h1": "Peer"})
    shared = {peer_session.id}

    async def shared_sessions(request, user_id):
        return frozenset(shared)

    async def can_read(request, item):
        return item.user_id == c.owner or item.session_id in shared

    monkeypatch.setattr(canvas_routes, "_shared_report_session_ids", shared_sessions)
    monkeypatch.setattr(canvas_routes, "_can_read_canvas", can_read)
    assert _pin(c, "report", canvas.id).status_code == 200
    assert [item["title"] for item in _list(c)] == ["Peer report"]
    shared.clear()
    assert _list(c) == []


async def test_the_limit_counts_only_pins_the_person_can_see(ctx, monkeypatch) -> None:
    c = ctx
    monkeypatch.setattr(result_pins, "RESULT_PIN_LIMIT", 2)
    first, second, third = [await _upload(c, f"f{i}".encode()) for i in range(3)]
    assert _pin(c, "file", first.id).status_code == 200
    assert _pin(c, "file", second.id).status_code == 200
    refused = _pin(c, "file", third.id)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == {"code": "result_pin_limit", "limit": 2}
    # Pinning an already pinned result is not refused.
    assert _pin(c, "file", first.id).status_code == 200
    # A hidden pin cannot be unpinned by the person, so it does not block a new one.
    await c.artifacts.soft_delete(second.id, user_id=c.owner)
    assert _pin(c, "file", third.id).status_code == 200


async def test_the_frozen_legacy_layout_answers_with_an_actionable_code(tmp_path) -> None:
    sessions = SessionStore(tmp_path / "legacy.db")
    await sessions.initialize()
    client = _app({"session_store": sessions, "settings": SimpleNamespace(dev_default_user="u", auth_mode="dev")})
    headers = {"X-Bowhead-User-Id": "u"}
    assert client.get("/api/v1/result-pins", headers=headers).json() == {"items": []}
    response = client.post("/api/v1/result-pins", json={"kind": "file", "id": "art_x"}, headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "result_pins_unavailable"
    assert client.delete("/api/v1/result-pins/file/art_x", headers=headers).status_code == 204


async def test_the_list_reads_in_batches(ctx, monkeypatch) -> None:
    c = ctx
    for i in range(20):
        art = await _upload(c, f"batch-{i}".encode())
        await c.sessions.pin_result(user_id=c.owner, target_kind="file", target_id=art.id)
    calls = {"get_many": 0, "latest": 0, "conversations": 0}
    for name, key in (("get_many", "get_many"), ("latest_live_versions", "latest"), ("conversation_ids", "conversations")):
        original = getattr(c.artifacts, name)

        async def counted(*args, _original=original, _key=key, **kwargs):
            calls[_key] += 1
            return await _original(*args, **kwargs)

        monkeypatch.setattr(c.artifacts, name, counted)
    assert len(_list(c)) == 20
    assert calls == {"get_many": 1, "latest": 1, "conversations": 1}


async def test_gc_removes_pins_of_purged_files_and_deleted_reports(ctx) -> None:
    from bowhead_gateway.result_pin_resolver import remove_gone_result_pins

    c = ctx
    kept = await _upload(c, b"kept")
    purged = await _upload(c, b"purged")
    trashed = await _upload(c, b"trashed")
    canvas = await c.canvases.ensure_canvas(session_id=c.session_id, workspace_id=c.ws, user_id=c.owner, title="Gone")
    for kind, target in (("file", kept.id), ("file", purged.id), ("file", trashed.id), ("report", canvas.id), ("report", "cv_never00")):
        await c.sessions.pin_result(user_id=c.owner, target_kind=kind, target_id=target)
    await c.artifacts.soft_delete(purged.id, user_id=c.owner)
    await c.artifacts.hard_delete_old_soft_deleted(days=0)
    await c.artifacts.soft_delete(trashed.id, user_id=c.owner)
    await c.canvases.delete_canvas(canvas_id=canvas.id)
    assert await remove_gone_result_pins(SimpleNamespace(**c.state)) == {"files": 1, "reports": 2}
    assert sorted(target for _, target, _ in await c.sessions.list_result_pins(user_id=c.owner)) == sorted([kept.id, trashed.id])
