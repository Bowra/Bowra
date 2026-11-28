"""Document matrix step 1 (S6) lighter polling: digest-guarded delta snapshots and batched row checks.

A full snapshot carries ``projection_digest``; a poll with ``since_seq`` and that digest gets only the newer
cells while nothing else of the caller's projection changed, and a full snapshot otherwise. Covers each digest
input, merging a delta into the previous full answer, hidden rows, older clients, parity of the batched row
check with the single-file check, and the poll cost at 200 × 10 during a run (payload and call counts).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.access_control import AccessControlStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.space_governance import SpaceOperation
from bowhead_gateway.routes import kb_matrix
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_kb_matrix_authz import TEAM, U_MEMBER, U_MEMBER2, WS_A, WS_B, _as, _h, _MembershipStore, _mk_user

from bowhead_core import migrations

QUESTION = {"type": "question", "prompt_version": 1}
U_OTHER = "u_other"


class _Members(_MembershipStore):
    """Membership with per-test roles and display names."""

    def __init__(self) -> None:
        super().__init__()
        self.roles = dict(self.ROLES)
        self.names = {U_MEMBER: "Mei", U_MEMBER2: "Jun"}

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        return self.roles.get((ws_id, user_id))

    async def get_team_member_labels(self, team_id: str, user_ids: list[str]) -> dict[str, str]:
        return {uid: self.names[uid] for uid in user_ids if uid in self.names}


@pytest.fixture(autouse=True)
def _single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


def _variant(value: str) -> list[dict[str, Any]]:
    return [{"raw": None, "normalized": {"value": value}, "is_primary": True, "extracted_by": "tier1", "confidence": 0.9}]


async def _file(files: KbFilesStore, hint: str, ws: str) -> str:
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


@pytest.fixture
async def env(initialize_migrated_kb_files_database, initialize_migrated_kb_matrix_database, tmp_path: Path):
    initialize_migrated_kb_matrix_database(tmp_path / "kb_matrix.db")
    store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    members = _Members()
    app = FastAPI()
    app.include_router(kb_matrix.router)
    app.state.kb_matrix_store = store
    app.state.kb_files_store = files
    app.state.user_store = members
    rows = [await _file(files, f"a{i}", WS_A) for i in range(3)]
    hidden = await _file(files, "b", WS_B)  # Another Space's file kept in the matrix: never readable here.
    matrix = await store.create_matrix(
        name="Polling",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": f, "char_count": 1} for f in [*rows, hidden]],
        columns=[{"name": "Amount", "type": "question", "question": "a?"}, {"name": "Term", "type": "question", "question": "b?"}],
    )
    columns = [c["column_id"] for c in matrix.columns]
    run = await store.create_run_and_enqueue(
        matrix_id=matrix.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "all"},
        initiated_by=U_MEMBER,
        targets=[(f, c) for f in [*rows, hidden] for c in columns],
        skipped_edited=0,
        credits_estimated=8,
        column_versions=dict.fromkeys(columns, 1),
        file_versions={},
    )
    assert run is not None
    for f in [*rows, hidden]:
        assert await store.finalize_cell(
            matrix.matrix_id, f, columns[0], status="done", variants=_variant(f"v-{f}"), prompt_version=1, file_version=1, run_id=run.run_id
        )
    # A verification by Jun: the latest change of one readable cell names a person.
    await store.set_cell_verified(matrix.matrix_id, rows[0], columns[0], workspace_id=WS_A, verified=True, actor_user_id=U_MEMBER2)
    _as(app, U_MEMBER)
    return {
        "client": TestClient(app, raise_server_exceptions=False),
        "app": app,
        "store": store,
        "files": files,
        "members": members,
        "matrix": matrix,
        "run": run,
        "rows": rows,
        "hidden": hidden,
        "columns": columns,
        "route": f"/api/v1/kb/matrices/{matrix.matrix_id}/snapshot",
    }


def _full(env: dict[str, Any]) -> dict[str, Any]:
    r = env["client"].get(env["route"], headers=_h(WS_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "full" and body["projection_digest"]
    return body


def _poll(env: dict[str, Any], held: dict[str, Any]) -> dict[str, Any]:
    since = held["matrix"]["seq"] if "matrix" in held else held["seq"]
    r = env["client"].get(env["route"], headers=_h(WS_A), params={"since_seq": since, "digest": held["projection_digest"]})
    assert r.status_code == 200, r.text
    return r.json()


async def _land(env: dict[str, Any], file_id: str, column_id: str, value: str) -> None:
    store, matrix = env["store"], env["matrix"]
    assert await store.finalize_cell(
        matrix.matrix_id, file_id, column_id, status="done", variants=_variant(value), prompt_version=1, file_version=1, run_id=env["run"].run_id
    )


def _merge(full: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """What a delta client holds after applying a delta to its last full answer."""
    assert delta["kind"] == "delta" and "rows" not in delta and "columns" not in delta
    return {**full, "cells": {**full["cells"], **delta["cells"]}, "runs": delta["runs"], "run": delta["run"]}


def _shown(body: dict[str, Any]) -> dict[str, Any]:
    """Everything a viewer sees, without the cursor and digest fields."""
    return {
        "matrix": {k: v for k, v in body["matrix"].items() if k != "seq"},
        "columns": body["columns"],
        "rows": body["rows"],
        "cells": body["cells"],
        "runs": body["runs"],
    }


# ─── deltas ────────────────────────────────────────────────


async def test_an_unchanged_projection_answers_with_only_the_newer_cells(env) -> None:
    rows, (c1, c2) = env["rows"], env["columns"]
    held = _full(env)
    await _land(env, rows[1], c2, "new value")
    await env["store"].edit_cell_value(
        env["matrix"].matrix_id, rows[2], c1, value="by hand", column=QUESTION, workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER2
    )
    delta = _poll(env, held)
    assert delta["kind"] == "delta"
    assert set(delta["cells"]) == {f"{rows[1]}:{c2}", f"{rows[2]}:{c1}"}
    assert delta["seq"] > held["matrix"]["seq"] and delta["projection_digest"] == held["projection_digest"]
    assert delta["runs"][0]["run_id"] == env["run"].run_id and delta["runs"][0]["cells_done"] == held["runs"][0]["cells_done"]
    # S4's latest change travels with each changed cell.
    assert delta["cells"][f"{rows[2]}:{c1}"]["last_event"]["kind"] == "edited"
    assert delta["cells"][f"{rows[2]}:{c1}"]["last_event"]["actor_name"] == "Jun"
    assert delta["cells"][f"{rows[1]}:{c2}"]["last_event"]["kind"] == "ai_value"
    # Nothing newer: an empty delta.
    assert _poll(env, delta)["cells"] == {}


async def test_a_merged_delta_shows_exactly_what_a_full_snapshot_shows(env) -> None:
    rows, (c1, c2) = env["rows"], env["columns"]
    held = _full(env)
    await _land(env, rows[0], c2, "landed")
    await env["store"].set_cell_verified(env["matrix"].matrix_id, rows[1], c1, workspace_id=WS_A, verified=True, actor_user_id=U_MEMBER2)
    await env["store"].edit_cell_value(
        env["matrix"].matrix_id, rows[2], c1, value="edited", column=QUESTION, workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER2
    )
    # Edited, then reverted: the revert is a write of its own, so the delta carries the reverted cell.
    await env["store"].revert_cell_edited(env["matrix"].matrix_id, rows[2], c1, workspace_id=WS_A, actor_user_id=U_MEMBER2)
    delta = _poll(env, held)
    assert delta["kind"] == "delta"
    assert _shown(_merge(held, delta)) == _shown(_full(env))


async def test_hidden_rows_never_appear_in_deltas_and_their_cells_do_not_move(env) -> None:
    hidden, (c1, c2) = env["hidden"], env["columns"]
    held = _full(env)
    masked = held["cells"][f"{hidden}:{c1}"]
    assert masked == {**kb_matrix._HIDDEN_CELL, "variants": []}
    assert held["rows"][3] == {"file_id": hidden, "file_name": "", "missing": True, "file_version": 1}
    await _land(env, hidden, c1, "secret rewrite")
    await _land(env, hidden, c2, "secret new")
    await _land(env, env["rows"][0], c2, "visible")
    delta = _poll(env, held)
    assert delta["kind"] == "delta"
    assert set(delta["cells"]) == {f"{env['rows'][0]}:{c2}"}
    assert "secret" not in json.dumps(delta, ensure_ascii=False)
    # A full snapshot shows the hidden cells exactly as before the writes, so the delta client agrees with it.
    assert _full(env)["cells"][f"{hidden}:{c1}"] == masked
    assert _shown(_merge(held, delta)) == _shown(_full(env))


async def test_a_stale_or_absent_digest_answers_in_full(env) -> None:
    held = _full(env)
    seq = held["matrix"]["seq"]
    cases = [
        {"since_seq": seq, "digest": "0" * 32},  # Another projection's digest.
        {"since_seq": seq},  # Older clients send no digest.
        {"since_seq": seq + 1, "digest": held["projection_digest"]},  # A cursor from the future.
        {"since_seq": held["matrix"]["structure_seq"] - 1, "digest": held["projection_digest"]},  # Before the structure.
    ]
    for params in cases:
        r = env["client"].get(env["route"], headers=_h(WS_A), params=params)
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "full" and r.json()["rows"], params


# ─── every digest input ────────────────────────────────────


async def _rename(env):
    async with aiosqlite.connect(env["files"]._db_path) as db:
        await db.execute("UPDATE kb_files SET filename='renamed.md' WHERE file_id=?", (env["rows"][1],))
        await db.commit()


async def _new_file_version(env):
    async with aiosqlite.connect(env["files"]._db_path) as db:
        await db.execute("UPDATE kb_files SET version=version+1 WHERE file_id=?", (env["rows"][1],))
        await db.commit()


async def _move_to_another_space(env):
    async with aiosqlite.connect(env["files"]._db_path) as db:
        await db.execute("UPDATE kb_files SET workspace_id=?, scope_id=? WHERE file_id=?", (WS_B, WS_B, env["rows"][1]))
        await db.commit()


async def _remove_row(env):
    assert await env["store"].remove_files(env["matrix"].matrix_id, [env["rows"][2]], workspace_id=WS_A, allow_reviewed=True) is not None


async def _new_prompt_version(env):
    await env["store"].update_column(env["matrix"].matrix_id, env["columns"][1], workspace_id=WS_A, question="b again?")


async def _viewer_role(env):
    env["members"].roles[(WS_A, U_MEMBER)] = "viewer"


async def _denied_by_grant(env, monkeypatch, tmp_path):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = env["app"].state.access_control_store
    await access.create_or_update_resource_grant(
        team_id=TEAM,
        resource_kind="kb_file",
        resource_id=env["rows"][1],
        principal_type="user",
        principal_id=U_MEMBER,
        permission="read",
        effect="deny",
    )


async def _cell_row_removed(env):
    # A cell queued from empty and given back (a skipped row): the row goes and no cell keeps its seq.
    store, matrix = env["store"], env["matrix"]
    assert await store.revert_queued_cells(matrix.matrix_id, [(env["rows"][2], env["columns"][1])]) == 1


async def _actor_renamed(env):
    env["members"].names[U_MEMBER2] = "Jun Li"


async def _source_paused(env):
    # The source check found the file's source gone: display state only, no cell write.
    async with aiosqlite.connect(env["files"]._db_path) as db:
        await db.execute("UPDATE kb_files SET source_absent=1 WHERE file_id=?", (env["rows"][1],))
        await db.commit()


async def _matrix_renamed(env):
    assert await env["store"].rename_matrix(env["matrix"].matrix_id, "Renamed", workspace_id=WS_A)


CHANGES = {
    "rename": _rename,
    "file_version": _new_file_version,
    "hidden_by_move": _move_to_another_space,
    "row_removed": _remove_row,
    "prompt_version": _new_prompt_version,
    "role": _viewer_role,
    "grant_denied": _denied_by_grant,
    "cell_row_removed": _cell_row_removed,
    "actor_name": _actor_renamed,
    "source_state": _source_paused,
    "matrix_renamed": _matrix_renamed,
}


@pytest.mark.parametrize("change", list(CHANGES))
async def test_each_projection_input_changes_the_digest_and_forces_a_full_answer(env, change, monkeypatch, tmp_path) -> None:
    if change == "grant_denied":
        access = AccessControlStore(tmp_path / "access.db")
        await access.initialize()
        env["app"].state.access_control_store = access
    if change == "source_state":
        monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
        monkeypatch.setenv("FEATURE_KB_USE_GUARD", "1")
    held = _full(env)
    if change == "cell_row_removed":
        assert f"{env['rows'][2]}:{env['columns'][1]}" in held["cells"]
    mutate = CHANGES[change]
    await (mutate(env, monkeypatch, tmp_path) if change == "grant_denied" else mutate(env))
    after = _poll(env, held)
    assert after["kind"] == "full", change
    assert after["projection_digest"] != held["projection_digest"]
    assert after == _full(env)
    # What the change shows (and what a delta would have hidden).
    row1 = next(r for r in after["rows"] if r["file_id"] == env["rows"][1])
    expected = {
        "rename": lambda: row1["file_name"] == "renamed.md",
        "file_version": lambda: row1["file_version"] == 2 and after["cells"][f"{env['rows'][1]}:{env['columns'][0]}"]["stale"],
        "hidden_by_move": lambda: row1["missing"] and after["cells"][f"{env['rows'][1]}:{env['columns'][0]}"]["variants"] == [],
        "row_removed": lambda: env["rows"][2] not in {r["file_id"] for r in after["rows"]},
        "prompt_version": lambda: after["columns"][1]["prompt_version"] == 2,
        "role": lambda: after["matrix"]["can_edit"] is False,
        "grant_denied": lambda: row1["missing"] and row1["file_name"] == "",
        "cell_row_removed": lambda: f"{env['rows'][2]}:{env['columns'][1]}" not in after["cells"],
        "actor_name": lambda: after["cells"][f"{env['rows'][0]}:{env['columns'][0]}"]["last_event"]["actor_name"] == "Jun Li",
        "source_state": lambda: after["cells"][f"{env['rows'][1]}:{env['columns'][0]}"]["current_source"]["state"] == "paused",
        "matrix_renamed": lambda: after["matrix"]["name"] == "Renamed",
    }
    assert expected[change]()


@pytest.mark.parametrize("change", ["space", "team", "personal"])
async def test_a_poll_with_a_digest_reprojects_moved_sources(env, change) -> None:
    """The a763f7abcf regressions, for clients that send a digest: a moved source is never kept by a delta."""
    held = _full(env)
    assert held["rows"][0]["file_name"] == "a0.md"
    assignments = {
        "space": "workspace_id='ws_beta',scope_id='ws_beta'",
        "team": "team_id='team_foreign'",
        "personal": "scope='personal',scope_id='u_other',uploaded_by='u_other'",
    }
    async with aiosqlite.connect(env["files"]._db_path) as db:
        await db.execute(f"UPDATE kb_files SET {assignments[change]} WHERE file_id=?", (env["rows"][0],))
        await db.commit()
    after = _poll(env, held)
    assert after["kind"] == "full"
    assert after["rows"][0]["missing"] and after["rows"][0]["file_name"] == ""
    assert after["cells"][f"{env['rows'][0]}:{env['columns'][0]}"]["variants"] == []
    assert "v-" + env["rows"][0] not in json.dumps(after)


# ─── batched row check: parity with the single-file check ──


@pytest.fixture(scope="module")
def files_at_head(tmp_path_factory) -> Path:
    """The ``files`` target at head, so named knowledge bases exist (revision 0239)."""
    path = tmp_path_factory.mktemp("kb-files-head") / "kb_files.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "files"
    command.upgrade(config, "head")
    return path


NOW = "2026-11-27T00:00:00+00:00"
# file_id → (scope, scope_id, workspace_id, team, uploaded_by, deleted, knowledge_base_id)
PARITY_FILES = {
    "kbf_space": ("space", WS_A, WS_A, TEAM, U_MEMBER, False, None),
    "kbf_legacy_space": ("workspace", WS_A, WS_A, TEAM, U_OTHER, False, None),
    "kbf_other_space": ("space", WS_B, WS_B, TEAM, U_OTHER, False, None),
    "kbf_team": ("team", TEAM, "", TEAM, U_OTHER, False, None),
    "kbf_foreign_team": ("team", "team_foreign", "", "team_foreign", U_OTHER, False, None),
    "kbf_personal_other": ("personal", U_OTHER, WS_A, TEAM, U_OTHER, False, None),
    "kbf_personal_own": ("personal", U_MEMBER, WS_A, TEAM, U_MEMBER, False, None),
    "kbf_deleted": ("space", WS_A, WS_A, TEAM, U_MEMBER, True, None),
    "kbf_named_private": ("space", WS_A, WS_A, TEAM, U_OTHER, False, "kbase_private"),
    "kbf_named_shared": ("space", WS_A, WS_A, TEAM, U_OTHER, False, "kbase_shared"),
    "kbf_denied": ("space", WS_A, WS_A, TEAM, U_MEMBER, False, None),
}


@pytest.mark.parametrize("personal_owner", [False, True], ids=["space_matrix", "own_private_matrix"])
@pytest.mark.parametrize("enforce", ["1", "0"], ids=["grants_enforced", "grants_shadow"])
@pytest.mark.parametrize("knowledge_bases", ["1", "0"], ids=["knowledge_bases_on", "knowledge_bases_off"])
async def test_the_batched_row_check_hides_exactly_what_the_single_file_check_refuses(
    files_at_head, tmp_path, monkeypatch, enforce, knowledge_bases, personal_owner
) -> None:
    """``personal_owner`` is the creator's own private matrix (S8): both checks then accept the caller's own
    personal files, and only those."""
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", knowledge_bases)
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", enforce)
    path = tmp_path / "kb_files.db"
    with closing(sqlite3.connect(files_at_head)) as source, closing(sqlite3.connect(path)) as destination:
        source.backup(destination)
    with closing(sqlite3.connect(path)) as db:
        # Stand-in for the runtime's retention guard (revision 0238): nothing is in the trash.
        db.create_function("bowhead_file_retention_unavailable_v1", 1, lambda _artifact_id: 0)
        for file_id, (scope, scope_id, workspace_id, team, uploader, deleted, kb_id) in PARITY_FILES.items():
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,"
                "uploaded_at,parse_status,soft_deleted_at,knowledge_base_id) VALUES (?,?,?,'text/markdown',1,?,?,?,?,?,?,'done',?,?)",
                (file_id, f"art_{file_id}", f"{file_id}.md", scope, scope_id, workspace_id, team, uploader, NOW, NOW if deleted else None, kb_id),
            )
        for kb_id in ("kbase_private", "kbase_shared"):
            db.execute(
                "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,audience,audience_level,owner_user_id,created_at,updated_at,"
                "deleted_at) VALUES (?,?,'space',?,0,?,'members','use',?,?,?,NULL)",
                (kb_id, TEAM, WS_A, kb_id, U_OTHER, NOW, NOW),
            )
        db.execute("INSERT INTO kb_knowledge_base_members VALUES ('kbase_shared','user',?,'use',?,?)", (U_MEMBER, U_OTHER, NOW))
        db.commit()
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id=TEAM,
        resource_kind="kb_file",
        resource_id="kbf_denied",
        principal_type="user",
        principal_id=U_MEMBER,
        permission="read",
        effect="deny",
    )
    app = FastAPI()
    app.state.kb_files_store = KbFilesStore(path)
    app.state.user_store = _Members()
    app.state.access_control_store = access
    request = Request({"type": "http", "app": app, "headers": [], "state": {}, "method": "GET", "path": "/", "query_string": b""})
    _ws, ctx = await kb_matrix._require_workspace_member_strict(request, _mk_user(U_MEMBER), WS_A, operation=SpaceOperation.READ_HISTORY)
    ids = [*PARITY_FILES, "kbf_missing"]

    single: dict[str, Any] = {}
    for file_id in ids:
        with suppress(HTTPException):
            single[file_id] = (await kb_matrix._validate_file_refs(request, ctx, [file_id], personal_owner=personal_owner))[0]
    batched = await kb_matrix._readable_sources(request, ctx, ids, personal_owner=personal_owner)

    assert {k: (v.filename, v.version) for k, v in batched.items()} == {k: (v.filename, v.version) for k, v in single.items()}
    expected = {"kbf_space", "kbf_legacy_space", "kbf_team"} | ({"kbf_named_shared"} if knowledge_bases == "1" else set())
    expected |= {"kbf_personal_own"} if personal_owner else set()
    assert set(batched) == expected | ({"kbf_denied"} if enforce == "0" else set())


# ─── cost at 200 × 10 during a run ─────────────────────────


class _Calls:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def wrap(self, obj: Any, name: str) -> None:
        original = getattr(obj, name)

        async def counted(*args: Any, **kwargs: Any) -> Any:
            self.counts[name] = self.counts.get(name, 0) + 1
            return await original(*args, **kwargs)

        setattr(obj, name, counted)

    def take(self) -> dict[str, int]:
        counts, self.counts = self.counts, {}
        return counts


async def test_a_poll_during_a_run_at_200_by_10_is_a_few_kilobytes_and_one_batch_of_checks(env, tmp_path, monkeypatch) -> None:
    """Step 1 §6 target, measured with the spike's shape: unique 80-character excerpts, an event per cell.

    Main checked each row on its own: two file reads and two grant lookups per row (400 + 400 here) and a
    1.4 MB answer on every poll. Bounds are generous; wall time is reported in the tech design, not asserted.
    """
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    store, files = env["store"], env["files"]
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    env["app"].state.access_control_store = access
    with closing(sqlite3.connect(files._db_path)) as db:
        db.executemany(
            "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,"
            "uploaded_at,parse_status) VALUES (?,?,?,'application/pdf',1000,'workspace',?,?,?,?,?,'done')",
            [(f"kbf_m{i:03d}", f"art_m{i}", f"合同_{i:04d}_供应商框架协议.pdf", WS_A, WS_A, TEAM, U_MEMBER, NOW) for i in range(200)],
        )
        db.commit()
    file_ids = [f"kbf_m{i:03d}" for i in range(200)]
    matrix = await store.create_matrix(
        name="Load",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": f, "char_count": 30_000} for f in file_ids],
        columns=[{"name": f"列{j:02d}", "type": "question", "question": f"第{j}个关键条款是什么？"} for j in range(10)],
    )
    columns = [c["column_id"] for c in matrix.columns]
    await store.insert_cells_bulk(
        matrix.matrix_id,
        [
            {
                "file_id": f,
                "column_id": c,
                "workspace_id": WS_A,
                "team_id": TEAM,
                "status": "done",
                "variants": [
                    {
                        **_variant(f"合同约定{i}-{j}")[0],
                        "source_excerpt": f"本合同第{j}条约定的条款原文，编号{i:04d}-{j:02d}，" + "甲方应于每季度首月十日前支付服务费" * 2,
                        "chunk_id": f"chk_{i:04d}_{j}",
                        "page": j + 1,
                    }
                ],
                "edited_by_user": False,
                "verified": (i + j) % 5 == 0,
                "prompt_version": 1,
                "file_version": 1,
            }
            for i, f in enumerate(file_ids)
            for j, c in enumerate(columns)
        ],
    )
    with closing(sqlite3.connect(store._db_path)) as db:
        cells = db.execute("SELECT file_id, column_id, seq FROM kb_matrix_cells WHERE matrix_id=?", (matrix.matrix_id,)).fetchall()
        db.executemany(
            "INSERT INTO kb_matrix_cell_events VALUES (?,?,?,?,?,?,'ai_value',NULL,NULL,?,NULL,?)",
            [(f"evt_{n}", matrix.matrix_id, f, c, WS_A, TEAM, seq, NOW) for n, (f, c, seq) in enumerate(cells)],
        )
        db.commit()
    run = await store.create_run_and_enqueue(
        matrix_id=matrix.matrix_id,
        workspace_id=WS_A,
        team_id=TEAM,
        scope={"kind": "all"},
        initiated_by=U_MEMBER,
        targets=[(f, c) for f in file_ids for c in columns],
        skipped_edited=0,
        credits_estimated=2000,
        column_versions=dict.fromkeys(columns, 1),
        file_versions=dict.fromkeys(file_ids, 1),
    )
    assert run is not None
    calls = _Calls()
    for name in ("get", "get_many"):
        calls.wrap(files, name)
    for name in ("explain_decision", "explain_decisions"):
        calls.wrap(access, name)
    route = f"/api/v1/kb/matrices/{matrix.matrix_id}/snapshot"

    full = env["client"].get(route, headers=_h(WS_A))
    assert full.status_code == 200 and full.json()["kind"] == "full"
    assert calls.take() == {"get_many": 1, "explain_decisions": 2}
    for k in range(5):  # The engine lands five cells.
        assert await store.finalize_cell(
            matrix.matrix_id,
            file_ids[k * 37],
            columns[k],
            status="done",
            variants=_variant(f"新值{k}"),
            prompt_version=1,
            file_version=1,
            run_id=run.run_id,
        )
    held = full.json()
    delta = env["client"].get(route, headers=_h(WS_A), params={"since_seq": held["matrix"]["seq"], "digest": held["projection_digest"]})
    assert delta.status_code == 200 and delta.json()["kind"] == "delta"
    assert len(delta.json()["cells"]) == 5
    assert calls.take() == {"get_many": 1, "explain_decisions": 2}
    # Measured: full ≈ 1.48 MB, delta ≈ 2.7 KB.
    assert len(full.content) > 1_000_000
    assert len(delta.content) < 8_000
