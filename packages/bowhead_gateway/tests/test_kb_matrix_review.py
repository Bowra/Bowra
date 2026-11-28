"""Document matrix step 1 (S4) review routes: bulk verify, cell history and the snapshot's last change.

Runs on the matrix head schema, so every cell write records its history. Covers scope by column and rows,
the skipped buckets, unreadable rows left untouched, member rank, a shared batch id, history order, the
50-event limit, actor names and ``last_event`` in the snapshot and in PATCH answers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_store import CELL_EVENT_LIMIT, KbMatrixStore
from bowhead_gateway.routes import kb_matrix
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_kb_matrix_authz import TEAM, U_MEMBER, U_MEMBER2, U_VIEWER, WS_A, WS_B, _as, _h, _MembershipStore

if TYPE_CHECKING:
    from pathlib import Path

NAMES = {U_MEMBER: "Mei", U_MEMBER2: "Jun"}
QUESTION = {"type": "question", "prompt_version": 1}


class _NamedMembers(_MembershipStore):
    async def get_team_member_labels(self, team_id: str, user_ids: list[str]) -> dict[str, str]:
        assert team_id == TEAM
        return {uid: NAMES[uid] for uid in user_ids if uid in NAMES}


@pytest.fixture(autouse=True)
def _single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


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
    app.state.user_store = _NamedMembers()

    async def add(hint: str, ws: str) -> str:
        return await files.create(
            artifact_id=hint,
            filename=f"{hint}.md",
            mime_type="text/markdown",
            size_bytes=1,
            scope="workspace",
            scope_id=ws,
            workspace_id=ws,
            team_id=TEAM,
            uploaded_by=U_MEMBER,
        )

    rows = [await add(f"a{i}", WS_A) for i in range(3)]
    # A row the caller cannot read: another Space's file kept in the matrix.
    hidden = await add("b", WS_B)
    matrix = await store.create_matrix(
        name="Review",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": f, "char_count": 1} for f in [*rows, hidden]],
        columns=[{"name": "Amount", "type": "question", "question": "a?"}, {"name": "Term", "type": "question", "question": "b?"}],
    )
    c1, c2 = (c["column_id"] for c in matrix.columns)
    # c1's question changed: cells written under version 1 are stale, version 2 ones are current.
    await store.update_column(matrix.matrix_id, c1, workspace_id=WS_A, question="a again?")

    async def land(file_id: str, col: str, version: int) -> None:
        await store.enqueue_cells(
            matrix.matrix_id, [(file_id, col)], run_id="mxrun_t", workspace_id=WS_A, team_id=TEAM, column_versions={col: version}, file_versions={}
        )
        variants = [{"raw": None, "normalized": {"value": "v"}, "is_primary": True, "extracted_by": "tier1", "confidence": 0.9}]
        assert await store.finalize_cell(
            matrix.matrix_id, file_id, col, status="done", variants=variants, prompt_version=version, file_version=1, run_id="mxrun_t"
        )

    a0, a1, a2 = rows
    await land(a0, c1, 2)  # current → verifies
    await land(a0, c2, 1)
    await store.edit_cell_value(matrix.matrix_id, a0, c2, value="人工", column=QUESTION, workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER2)
    await land(a1, c1, 1)  # stale
    await land(a1, c2, 1)
    await store.enqueue_cells(
        matrix.matrix_id, [(a1, c2)], run_id="mxrun_f", workspace_id=WS_A, team_id=TEAM, column_versions={c2: 1}, file_versions={}
    )
    await store.fail_cells(matrix.matrix_id, [(a1, c2)], fail_reason="timeout")  # not done
    await land(a2, c1, 2)
    await store.set_cell_verified(matrix.matrix_id, a2, c1, workspace_id=WS_A, verified=True, actor_user_id=U_MEMBER2)  # already
    await land(hidden, c1, 2)  # unreadable: never touched
    return {
        "client": TestClient(app, raise_server_exceptions=False),
        "app": app,
        "store": store,
        "matrix": matrix,
        "rows": rows,
        "hidden": hidden,
        "c1": c1,
        "c2": c2,
    }


async def _batch_events(store: KbMatrixStore, batch_id: str) -> list[tuple[str, str, str, str]]:
    async with (
        aiosqlite.connect(store._db_path) as db,
        db.execute(
            "SELECT file_id, column_id, kind, actor_user_id FROM kb_matrix_cell_events WHERE batch_id = ? ORDER BY cell_seq", (batch_id,)
        ) as cur,
    ):
        return [tuple(r) for r in await cur.fetchall()]


def _verify(env: dict[str, Any], body: dict[str, Any]) -> Any:
    return env["client"].post(f"/api/v1/kb/matrices/{env['matrix'].matrix_id}/cells/verify", json=body, headers=_h(WS_A))


async def test_bulk_verify_a_column_counts_every_skip_and_leaves_unreadable_rows_alone(env, monkeypatch) -> None:
    store, m, (a0, a1, a2), c1 = env["store"], env["matrix"], env["rows"], env["c1"]
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(kb_matrix, "emit_audit", lambda event, payload: audits.append((event, payload)))
    _as(env["app"], U_MEMBER)
    r = _verify(env, {"column_id": c1, "verified": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 1
    assert body["skipped"] == {"not_done": 0, "stale": 1, "already": 1, "unavailable": 1}
    assert await _batch_events(store, body["batch_id"]) == [(a0, c1, "verified", U_MEMBER)]
    # The audit line carries ids and counts only.
    assert audits == [
        (
            "kb.matrix_cells_bulk_verified",
            {
                "matrix_id": m.matrix_id,
                "column_id": c1,
                "verified": True,
                "changed": 1,
                "batch_id": body["batch_id"],
                "skipped_not_done": 0,
                "skipped_stale": 1,
                "skipped_already": 1,
                "skipped_unavailable": 1,
            },
        )
    ]
    hidden = await store.get_cell(m.matrix_id, env["hidden"], c1)
    assert hidden is not None and not hidden.verified
    stale = await store.get_cell(m.matrix_id, a1, c1)
    assert stale is not None and not stale.verified

    # The whole matrix: the edited cell verifies too, the failed and empty cells are not done.
    r = _verify(env, {"verified": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 1
    assert body["skipped"] == {"not_done": 2, "stale": 1, "already": 2, "unavailable": 2}
    assert await _batch_events(store, body["batch_id"]) == [(a0, env["c2"], "verified", U_MEMBER)]

    # Named rows, undoing: every verified cell of those rows, under one new batch.
    r = _verify(env, {"file_ids": [a2, a0, a2], "verified": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 3
    assert body["skipped"] == {"not_done": 0, "stale": 0, "already": 1, "unavailable": 0}
    assert {(e[0], e[1], e[2]) for e in await _batch_events(store, body["batch_id"])} == {
        (a2, c1, "verify_reverted"),
        (a0, c1, "verify_reverted"),
        (a0, env["c2"], "verify_reverted"),
    }
    # A row the caller cannot read is counted, never written.
    r = _verify(env, {"file_ids": [env["hidden"]], "column_id": c1, "verified": True})
    assert r.json() == {"changed": 0, "skipped": {"not_done": 0, "stale": 0, "already": 0, "unavailable": 1}, "batch_id": r.json()["batch_id"]}


async def test_bulk_verify_refuses_bad_requests_with_specific_codes(env) -> None:
    _as(env["app"], U_MEMBER)
    for body, status, code in (
        ({"column_id": env["c1"]}, 422, "verified_required"),
        ({"file_ids": [], "verified": True}, 422, "matrix_verify_files_empty"),
        ({"file_ids": [env["rows"][0], "kbf_elsewhere"], "verified": True}, 422, "matrix_verify_files_not_in_matrix"),
        ({"column_id": "mxcol_missing", "verified": True}, 404, "matrix_column_not_found"),
    ):
        r = _verify(env, body)
        assert r.status_code == status, r.text
        assert r.json()["detail"]["error"]["code"] == code
    r = _verify(env, {"file_ids": ["kbf_elsewhere"], "verified": True})
    assert r.json()["detail"]["error"] == {"code": "matrix_verify_files_not_in_matrix", "count": 1}


async def test_bulk_verify_needs_member_rank(env) -> None:
    _as(env["app"], U_VIEWER)
    r = _verify(env, {"column_id": env["c1"], "verified": True})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"]["code"] == "workspace_member_role_required"
    cell = await env["store"].get_cell(env["matrix"].matrix_id, env["rows"][0], env["c1"])
    assert cell is not None and not cell.verified


async def test_cell_history_newest_first_with_names_and_a_limit(env) -> None:
    store, m, a0, c1 = env["store"], env["matrix"], env["rows"][0], env["c1"]
    route = f"/api/v1/kb/matrices/{m.matrix_id}/cells/{a0}/{c1}/events"
    _as(env["app"], U_VIEWER)
    r = env["client"].get(route, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    assert [(e["kind"], e["actor_name"], e["run_id"]) for e in r.json()["items"]] == [("ai_value", None, "mxrun_t")]
    assert r.json()["more"] is False
    for i in range(CELL_EVENT_LIMIT):
        await store.set_cell_verified(m.matrix_id, a0, c1, workspace_id=WS_A, verified=i % 2 == 0, actor_user_id=U_MEMBER if i % 2 else U_MEMBER2)
    r = env["client"].get(route, headers=_h(WS_A))
    items = r.json()["items"]
    assert len(items) == CELL_EVENT_LIMIT and r.json()["more"] is True
    # Newest first: the last write was an undo by Mei; the oldest shown is the second toggle.
    assert (items[0]["kind"], items[0]["actor_name"]) == ("verify_reverted", "Mei")
    assert (items[-1]["kind"], items[-1]["actor_name"]) == ("verified", "Jun")
    assert set(items[0]) == {"event_id", "kind", "actor_name", "at", "run_id", "batch_id"}


async def test_cell_history_answers_like_missing_for_unreadable_rows(env) -> None:
    m, c1 = env["matrix"], env["c1"]
    _as(env["app"], U_MEMBER)
    base = f"/api/v1/kb/matrices/{m.matrix_id}/cells"
    r = env["client"].get(f"{base}/{env['hidden']}/{c1}/events", headers=_h(WS_A))
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "matrix_source_file_unavailable"
    r = env["client"].get(f"{base}/{env['rows'][0]}/mxcol_missing/events", headers=_h(WS_A))
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "matrix_column_not_found"
    r = env["client"].get(f"{base}/kbf_elsewhere/{c1}/events", headers=_h(WS_A))
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "matrix_file_not_found"


async def test_snapshot_and_patch_carry_the_last_change(env) -> None:
    m, (a0, a1, _a2), c1, c2 = env["matrix"], env["rows"], env["c1"], env["c2"]
    _as(env["app"], U_MEMBER)
    r = env["client"].patch(f"/api/v1/kb/matrices/{m.matrix_id}/cells/{a1}/{c1}", json={"op": "set_verified", "verified": False}, headers=_h(WS_A))
    assert r.status_code == 200, r.text
    # A no-op undo keeps the AI value as the last change.
    assert r.json()["cell"]["last_event"]["kind"] == "ai_value"
    r = env["client"].patch(f"/api/v1/kb/matrices/{m.matrix_id}/cells/{a1}/{c1}", json={"op": "edit_value", "value": "新"}, headers=_h(WS_A))
    last = r.json()["cell"]["last_event"]
    assert (last["kind"], last["actor_name"]) == ("edited", "Mei") and last["at"]

    cells = env["client"].get(f"/api/v1/kb/matrices/{m.matrix_id}/snapshot", headers=_h(WS_A)).json()["cells"]
    assert (cells[f"{a1}:{c1}"]["last_event"]["kind"], cells[f"{a1}:{c1}"]["last_event"]["actor_name"]) == ("edited", "Mei")
    assert (cells[f"{a0}:{c2}"]["last_event"]["kind"], cells[f"{a0}:{c2}"]["last_event"]["actor_name"]) == ("edited", "Jun")
    assert cells[f"{a0}:{c1}"]["last_event"] == {"kind": "ai_value", "actor_name": None, "at": cells[f"{a0}:{c1}"]["last_event"]["at"]}
    # The unreadable row shows neither its value nor who changed it.
    assert "last_event" not in cells[f"{env['hidden']}:{c1}"]
