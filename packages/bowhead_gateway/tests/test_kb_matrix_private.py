"""Only-me matrices (document matrix step 1, S8; ``FEATURE_KB_MATRIX_PRIVATE``).

Runs on the matrix head schema (``kb_matrices.visibility``, revision 0240). Covers creation with the switch
on and off, the 404 every matrix route gives a non-creator (parametrized over the router, so a new route
cannot be missed), list and Home filtering without an admin exception, personal files only in the creator's
private matrix (per-file and batched checks; graduation stays strict), the visibility change and its
refusals, the creator's preview, navigation roles, and the snapshot header.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.kb.source_location import SourceChunk
from bowhead_gateway.routes import home, kb_matrix
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_kb_matrix_authz import TEAM, U_MEMBER, U_MEMBER2, U_TEAM_ADMIN, U_VIEWER, U_WS_ADMIN, WS_A, _as, _h, _MembershipStore, _mk_user

if TYPE_CHECKING:
    from pathlib import Path

NAMES = {U_MEMBER: "Mei", U_MEMBER2: "Jun"}
QUESTION = {"type": "question", "prompt_version": 1}


class _Members(_MembershipStore):
    """WS_A also has the Team admin as a member, so "admins have no exception" covers both admin kinds."""

    ROLES = {**_MembershipStore.ROLES, (WS_A, U_TEAM_ADMIN): "member"}

    async def get_team_member_labels(self, team_id: str, user_ids: list[str]) -> dict[str, str]:
        return {uid: NAMES[uid] for uid in user_ids if uid in NAMES}


class _KbStore:
    async def get_ordered_chunks_for_file(self, file: Any) -> list[tuple[str, str, int]]:
        return [("chk_1", "text", 1)]

    async def get_ordered_source_chunks(self, file: Any) -> list[SourceChunk]:
        return [SourceChunk("chk_1", "text", 1)]


@pytest.fixture(autouse=True)
def _switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("FEATURE_KB_MATRIX_PRIVATE", "1")


@pytest.fixture
async def env(initialize_migrated_kb_files_database, initialize_migrated_kb_matrix_database, tmp_path: Path):
    initialize_migrated_kb_matrix_database(tmp_path / "kb_matrix.db")
    store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    app = FastAPI()
    app.include_router(kb_matrix.router)
    app.state.kb_matrix_store = store
    app.state.kb_files_store = files
    app.state.kb_store = _KbStore()
    app.state.user_store = _Members()

    async def add(hint: str, scope: str, owner: str) -> str:
        scope_id = owner if scope == "personal" else WS_A
        return await files.create(
            artifact_id=hint,
            filename=f"{hint}.md",
            mime_type="text/markdown",
            size_bytes=1,
            scope=scope,
            scope_id=scope_id,
            workspace_id=WS_A,
            team_id=TEAM,
            uploaded_by=owner,
        )

    space = await add("space", "space", U_MEMBER)
    mine = await add("mine", "personal", U_MEMBER)
    theirs = await add("theirs", "personal", U_MEMBER2)
    columns = [{"name": "Amount", "type": "question", "question": "a?"}]
    private = await store.create_matrix(
        name="Mine",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": space, "char_count": 1}, {"file_id": mine, "char_count": 1}],
        columns=columns,
        visibility="private",
    )
    shared = await store.create_matrix(
        name="Shared", workspace_id=WS_A, team_id=TEAM, created_by=U_MEMBER, files=[{"file_id": space, "char_count": 1}], columns=columns
    )
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        app=app,
        store=store,
        files=files,
        private=private,
        shared=shared,
        col=private.columns[0]["column_id"],
        space=space,
        mine=mine,
        theirs=theirs,
    )


def _error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error = response.json()["detail"]["error"]
    assert error["code"] == code, error
    return error


def _url(env: SimpleNamespace, path: str = "", matrix: Any = None) -> str:
    return f"/api/v1/kb/matrices/{(matrix or env.private).matrix_id}{path}"


# ─── creation ──────────────────────────────────────────────


def _create(env: SimpleNamespace, **body: Any) -> Any:
    return env.client.post(
        "/api/v1/kb/matrices",
        json={"workspace_id": WS_A, "name": "New", "columns": [{"name": "A", "type": "question", "question": "a?"}], **body},
        headers=_h(WS_A),
    )


async def test_creation_stores_only_me_and_accepts_the_creators_personal_files(env) -> None:
    _as(env.app, U_MEMBER)
    r = _create(env, visibility="private", file_ids=[env.space, env.mine])
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "private" and "ignored_fields" not in r.json()
    created = await env.store.get_matrix(r.json()["matrix_id"])
    assert created is not None and created.visibility == "private"
    assert [f["file_id"] for f in created.files] == [env.space, env.mine]
    # Someone else's personal file is still a missing file, even in an only-me matrix.
    _error(_create(env, visibility="private", file_ids=[env.theirs]), 404, "matrix_source_file_unavailable")
    # Space matrices (the default) keep refusing personal files.
    _error(_create(env, file_ids=[env.mine]), 422, "personal_files_not_allowed")
    r = _create(env, file_ids=[env.space])
    assert r.status_code == 200 and r.json()["visibility"] == "space"


async def test_with_the_switch_off_creation_ignores_the_option_and_reports_it(env, monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_KB_MATRIX_PRIVATE", "0")
    _as(env.app, U_MEMBER)
    r = _create(env, visibility="private", file_ids=[env.space])
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "space" and r.json()["ignored_fields"] == ["visibility"]
    created = await env.store.get_matrix(r.json()["matrix_id"])
    assert created is not None and created.visibility == "space"
    # Nothing private is created off: personal files are refused as in a Space matrix.
    _error(_create(env, visibility="private", file_ids=[env.mine]), 422, "personal_files_not_allowed")


# ─── gating: every matrix route answers a non-creator like a missing matrix ──

# A valid request for each route, so the answer comes from the handler and not from body validation.
_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/api/v1/kb/matrices/{matrix_id}/graduate"): {"name": "Snapshot", "mode": "template"},
    ("PATCH", "/api/v1/kb/matrices/{matrix_id}"): {"name": "Renamed"},
    ("PUT", "/api/v1/kb/matrices/{matrix_id}/files"): {"file_ids": []},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/files"): {"file_ids": []},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/files/remove"): {"file_ids": []},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/columns"): {"name": "B", "type": "question", "question": "b?"},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/columns/draft"): {"question": "b?"},
    ("PATCH", "/api/v1/kb/matrices/{matrix_id}/columns/{column_id}"): {"name": "Renamed"},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/runs/estimate"): {"kind": "all_pending"},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/runs"): {"kind": "all_pending"},
    ("PATCH", "/api/v1/kb/matrices/{matrix_id}/cells/{file_id}/{column_id}"): {"op": "set_verified", "verified": True},
    ("POST", "/api/v1/kb/matrices/{matrix_id}/cells/verify"): {"verified": True},
}
_MATRIX_ROUTES = sorted(
    (method, route.path) for route in kb_matrix.router.routes if "{matrix_id}" in route.path for method in getattr(route, "methods", ())
)


def test_the_gate_covers_routes_with_and_without_bodies() -> None:
    # Every route that takes a body has one above; a new route with a body must be added here.
    assert set(_BODIES) <= set(_MATRIX_ROUTES)
    assert ("GET", "/api/v1/kb/matrices/{matrix_id}/visibility") in _MATRIX_ROUTES
    assert len(_MATRIX_ROUTES) >= 20


@pytest.mark.parametrize("caller", [U_MEMBER2, U_WS_ADMIN, U_TEAM_ADMIN], ids=["member", "space_admin", "team_admin"])
@pytest.mark.parametrize(("method", "path"), _MATRIX_ROUTES, ids=[f"{m} {p.removeprefix('/api/v1/kb')}" for m, p in _MATRIX_ROUTES])
async def test_a_private_matrix_answers_every_route_like_a_missing_one_for_anyone_but_its_creator(env, method, path, caller) -> None:
    _as(env.app, caller)
    url = path.format(matrix_id=env.private.matrix_id, column_id=env.col, file_id=env.space, run_id="mxrun_missing")
    r = env.client.request(method, url, json=_BODIES.get((method, path)), headers=_h(WS_A))
    _error(r, 404, "matrix_not_found")


@pytest.mark.parametrize("switch", ["1", "0"], ids=["switch_on", "switch_off"])
async def test_reads_and_the_list_hide_private_matrices_from_everyone_else_with_the_switch_on_or_off(env, monkeypatch, switch) -> None:
    monkeypatch.setenv("FEATURE_KB_MATRIX_PRIVATE", switch)
    for caller in (U_MEMBER2, U_VIEWER, U_WS_ADMIN, U_TEAM_ADMIN):
        _as(env.app, caller)
        for path in ("/snapshot", "/export.xlsx", "/visibility", f"/cells/{env.space}/{env.col}/events"):
            _error(env.client.get(_url(env, path), headers=_h(WS_A)), 404, "matrix_not_found")
        listed = env.client.get("/api/v1/kb/matrices", params={"workspace_id": WS_A}, headers=_h(WS_A))
        assert [i["matrix_id"] for i in listed.json()["items"]] == [env.shared.matrix_id], caller
    _as(env.app, U_MEMBER)
    listed = env.client.get("/api/v1/kb/matrices", params={"workspace_id": WS_A}, headers=_h(WS_A)).json()["items"]
    assert {i["matrix_id"]: i["visibility"] for i in listed} == {env.private.matrix_id: "private", env.shared.matrix_id: "space"}
    snapshot = env.client.get(_url(env, "/snapshot"), headers=_h(WS_A))
    assert snapshot.status_code == 200, snapshot.text
    header = snapshot.json()["matrix"]
    assert header["visibility"] == "private" and header["can_change_visibility"] is (switch == "1")


async def test_usage_events_of_a_private_matrix_come_only_from_its_creator(env, monkeypatch) -> None:
    """S9's ``opened`` page load and the other usage calls sit behind the same gate."""
    recorded: list[tuple[str, str]] = []

    async def record(_metrics_log: Any, actor: Any, *, matrix_id: str, **_kwargs: Any) -> None:
        recorded.append((actor.user_id, matrix_id))

    for name in ("track_matrix_opened", "track_matrix_exported", "track_matrix_cells_verified"):
        monkeypatch.setattr(kb_matrix.matrix_usage, name, record)
    for caller in (U_MEMBER2, U_WS_ADMIN, U_TEAM_ADMIN):
        _as(env.app, caller)
        _error(env.client.get(_url(env, "/snapshot"), params={"opened": "true"}, headers=_h(WS_A)), 404, "matrix_not_found")
        _error(env.client.get(_url(env, "/export.xlsx"), headers=_h(WS_A)), 404, "matrix_not_found")
        _error(env.client.post(_url(env, "/cells/verify"), json={"verified": True}, headers=_h(WS_A)), 404, "matrix_not_found")
    assert recorded == []
    _as(env.app, U_MEMBER)
    assert env.client.get(_url(env, "/snapshot"), params={"opened": "true"}, headers=_h(WS_A)).status_code == 200
    assert recorded == [(U_MEMBER, env.private.matrix_id)]


async def test_home_lists_active_runs_of_private_matrices_for_their_creator_only(env) -> None:
    for matrix in (env.private, env.shared):
        await env.store.create_run(
            matrix_id=matrix.matrix_id,
            workspace_id=WS_A,
            team_id=TEAM,
            scope={"kind": "all_pending"},
            initiated_by=U_MEMBER,
            cells_total=1,
            skipped_edited=0,
            credits_estimated=1,
        )
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(kb_matrix_store=env.store)))
    scope = home._WorkspaceScope(workspace_id=WS_A, workspace_name="A", query_workspace_ids=(WS_A,))
    for caller, titles in ((U_MEMBER, {"Mine", "Shared"}), (U_MEMBER2, {"Shared"}), (U_WS_ADMIN, {"Shared"}), (U_TEAM_ADMIN, {"Shared"})):
        items = await home._collect_active_matrices(request, _mk_user(caller), scope)
        assert {item.title for item in items} == titles, caller


# ─── personal files ────────────────────────────────────────


async def test_personal_files_are_accepted_per_file_only_in_the_creators_private_matrix(env) -> None:
    _as(env.app, U_MEMBER)
    extra = await env.files.create(
        artifact_id="mine2",
        filename="mine2.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="personal",
        scope_id=U_MEMBER,
        workspace_id=WS_A,
        team_id=TEAM,
        uploaded_by=U_MEMBER,
    )
    r = env.client.post(_url(env, "/files"), json={"file_ids": [extra]}, headers=_h(WS_A))
    assert r.status_code == 200 and r.json()["added"] == [extra], r.text
    _error(env.client.post(_url(env, "/files"), json={"file_ids": [env.theirs]}, headers=_h(WS_A)), 404, "matrix_source_file_unavailable")
    _error(
        env.client.post(_url(env, "/files", env.shared), json={"file_ids": [extra]}, headers=_h(WS_A)),
        422,
        "personal_files_not_allowed",
    )
    # Cell writes and history on a personal row go through the same per-file check.
    await env.store.edit_cell_value(
        env.private.matrix_id, env.mine, env.col, value="1", column=QUESTION, workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER
    )
    r = env.client.patch(_url(env, f"/cells/{env.mine}/{env.col}"), json={"op": "set_verified", "verified": True}, headers=_h(WS_A))
    assert r.status_code == 200 and r.json()["cell"]["verified"] is True, r.text
    r = env.client.get(_url(env, f"/cells/{env.mine}/{env.col}/events"), headers=_h(WS_A))
    assert [e["kind"] for e in r.json()["items"]] == ["verified", "edited"]


async def test_the_batched_row_check_shows_the_creators_personal_rows_only_in_their_private_matrix(env) -> None:
    # Rows written directly: someone else's personal file in the private matrix, and the creator's own in a
    # Space matrix (a file moved to personal after it was added). Both stay hidden.
    await env.store.add_files(env.private.matrix_id, [{"file_id": env.theirs}], workspace_id=WS_A)
    await env.store.add_files(env.shared.matrix_id, [{"file_id": env.mine}], workspace_id=WS_A)
    _as(env.app, U_MEMBER)
    rows = env.client.get(_url(env, "/snapshot"), headers=_h(WS_A)).json()["rows"]
    assert {r["file_id"]: r["missing"] for r in rows} == {env.space: False, env.mine: False, env.theirs: True}
    rows = env.client.get(_url(env, "/snapshot", env.shared), headers=_h(WS_A)).json()["rows"]
    assert {r["file_id"]: r["missing"] for r in rows} == {env.space: False, env.mine: True}
    # Bulk verify uses the same batched check: the personal row counts, the other person's is unavailable.
    for file_id in (env.mine, env.space, env.theirs):
        await env.store.edit_cell_value(
            env.private.matrix_id, file_id, env.col, value="1", column=QUESTION, workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER
        )
    r = env.client.post(_url(env, "/cells/verify"), json={"verified": True}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 2 and r.json()["skipped"]["unavailable"] == 1


async def test_graduating_a_private_matrix_names_its_personal_files(env) -> None:
    env.app.state.kb_doc_type_store = object()
    env.app.state.kb_extracted_rows_store = object()
    _as(env.app, U_MEMBER)
    error = _error(
        env.client.post(_url(env, "/graduate"), json={"name": "Snapshot", "mode": "snapshot"}, headers=_h(WS_A)),
        422,
        "matrix_graduation_personal_files",
    )
    assert error["files"] == [{"file_id": env.mine, "name": "mine.md"}] and error["count"] == 1


# ─── visibility change ─────────────────────────────────────


def _set(env: SimpleNamespace, visibility: str, matrix: Any = None) -> Any:
    return env.client.patch(_url(env, "", matrix), json={"visibility": visibility}, headers=_h(WS_A))


async def test_making_a_private_matrix_visible_is_refused_while_it_holds_personal_files(env) -> None:
    _as(env.app, U_MEMBER)
    error = _error(_set(env, "space"), 422, "matrix_personal_files_block_space")
    assert error["files"] == [{"file_id": env.mine, "name": "mine.md"}] and error["count"] == 1 and error["hidden"] == 0
    assert (await env.store.get_matrix(env.private.matrix_id)).visibility == "private"
    r = env.client.post(_url(env, "/files/remove"), json={"file_ids": [env.mine]}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    r = _set(env, "space")
    assert r.status_code == 200 and r.json()["visibility"] == "space", r.text
    _as(env.app, U_MEMBER2)
    assert env.client.get(_url(env, "/snapshot"), headers=_h(WS_A)).status_code == 200


async def test_only_the_creator_may_make_a_matrix_private_and_others_then_lose_access_but_keep_their_work(env, monkeypatch) -> None:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(kb_matrix, "emit_audit", lambda event, payload: audits.append((event, payload)))
    await env.store.edit_cell_value(
        env.shared.matrix_id, env.space, env.col, value="1", column=QUESTION, workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER2
    )
    for caller in (U_MEMBER2, U_WS_ADMIN, U_TEAM_ADMIN):
        _as(env.app, caller)
        _error(_set(env, "private", env.shared), 403, "matrix_creator_required")
    _as(env.app, U_MEMBER)
    preview = env.client.get(_url(env, "/visibility", env.shared), headers=_h(WS_A)).json()
    assert preview["contributors"] == ["Jun"] and preview["contributors_count"] == 1
    assert preview["can_change"] is True and preview["active_runs_by_others"] == 0
    r = _set(env, "private", env.shared)
    assert r.status_code == 200 and r.json() == {"matrix_id": env.shared.matrix_id, "name": "Shared", "visibility": "private"}
    assert ("kb.matrix_visibility_changed", {"matrix_id": env.shared.matrix_id, "visibility": "private"}) in audits
    _as(env.app, U_MEMBER2)
    _error(env.client.get(_url(env, "/snapshot", env.shared), headers=_h(WS_A)), 404, "matrix_not_found")
    _as(env.app, U_MEMBER)
    cells = env.client.get(_url(env, "/snapshot", env.shared), headers=_h(WS_A)).json()["cells"]
    assert cells[f"{env.space}:{env.col}"]["edited"] is True
    assert cells[f"{env.space}:{env.col}"]["last_event"]["actor_name"] == "Jun"


async def test_making_a_matrix_private_waits_for_other_members_runs(env) -> None:
    run = await env.store.create_run(
        matrix_id=env.shared.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=U_MEMBER2,
        cells_total=1,
        skipped_edited=0,
        credits_estimated=1,
    )
    _as(env.app, U_MEMBER)
    assert env.client.get(_url(env, "/visibility", env.shared), headers=_h(WS_A)).json()["active_runs_by_others"] == 1
    _error(_set(env, "private", env.shared), 409, "matrix_visibility_runs_active")
    await env.store.set_run_status(run.run_id, "succeeded")
    assert _set(env, "private", env.shared).status_code == 200


async def test_navigation_roles_are_refused_on_private_matrices(env) -> None:
    _as(env.app, U_MEMBER)
    r = env.client.patch(_url(env, f"/columns/{env.col}"), json={"navigation_role": "description"}, headers=_h(WS_A))
    _error(r, 422, "matrix_navigation_role_private")
    assert env.client.patch(_url(env, f"/columns/{env.col}"), json={"navigation_role": "none"}, headers=_h(WS_A)).status_code == 200
    shared_col = env.shared.columns[0]["column_id"]
    r = env.client.patch(_url(env, f"/columns/{shared_col}", env.shared), json={"navigation_role": "topics"}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    error = _error(_set(env, "private", env.shared), 422, "matrix_navigation_columns_block_private")
    assert error["columns"] == ["Amount"]


async def test_the_visibility_change_is_refused_with_the_switch_off(env, monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_KB_MATRIX_PRIVATE", "0")
    _as(env.app, U_MEMBER)
    _error(_set(env, "private", env.shared), 422, "matrix_visibility_unavailable")
    env.client.post(_url(env, "/files/remove"), json={"file_ids": [env.mine]}, headers=_h(WS_A))
    _error(_set(env, "space"), 422, "matrix_visibility_unavailable")
    # The current value is not a change; renaming in the same request still works.
    r = env.client.patch(_url(env), json={"visibility": "private", "name": "Mine 2"}, headers=_h(WS_A))
    assert r.status_code == 200 and r.json() == {"matrix_id": env.private.matrix_id, "name": "Mine 2", "visibility": "private"}
    assert env.client.get(_url(env, "/visibility"), headers=_h(WS_A)).json()["can_change"] is False


async def test_personal_files_cannot_land_in_a_matrix_that_stopped_being_private(env) -> None:
    """The add re-checks visibility under the structure lock, after a concurrent change to Space visibility."""
    from bowhead_core.kb.matrix_store import MatrixVisibilityError

    await env.store.remove_files(env.private.matrix_id, [env.mine], workspace_id=WS_A, allow_reviewed=True)
    assert await env.store.set_visibility(env.private.matrix_id, "space", workspace_id=WS_A)
    with pytest.raises(MatrixVisibilityError) as caught:
        await env.store.add_files(env.private.matrix_id, [{"file_id": env.mine}], workspace_id=WS_A, require_private=True)
    assert caught.value.code == "matrix_visibility_changed"
    with closing(sqlite3.connect(env.store._db_path)) as db:
        assert db.execute("SELECT visibility FROM kb_matrices WHERE matrix_id=?", (env.private.matrix_id,)).fetchone() == ("space",)
