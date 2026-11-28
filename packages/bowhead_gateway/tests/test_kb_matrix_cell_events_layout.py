"""Cell history (0240) against the migrated split layout: only the matrix file changes, and account erasure covers it."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from importlib import import_module
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory
from bowhead_cli.migrate import upgrade
from bowhead_core._db import run_sqlite_authority_write
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.kb.store import KbStore
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.user_store import UserStore
from bowhead_gateway.account_deletion_cleanup import AccountExternalCleanup, _reviewed_secondary

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]
MIGRATION = import_module("bowhead_core.migrations.versions.0240_matrix_cell_events")
QUESTION = {"type": "question", "prompt_version": 1}


def _schema(path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as db:
        return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    """A split layout at the previous head with one matrix, then upgraded by the startup coordinator to 0240."""
    directory = tmp_path_factory.mktemp("matrix-cell-events") / "sqlite"
    url = f"sqlite:///{directory / 'bowhead.db'}"
    with pytest.MonkeyPatch.context() as previous:
        previous.setattr(ScriptDirectory, "get_heads", lambda _scripts: [MIGRATION.down_revision])
        upgrade(url)
    with closing(connect_sync(directory / "kb_matrix.db")) as db:
        db.execute(
            "INSERT INTO kb_matrices(matrix_id,name,workspace_id,team_id,files_json,columns_json,last_seq,structure_seq,created_by,created_at,updated_at) "
            "VALUES ('mx_old','Old','ws_1','team_1','[]','[]',0,0,'u_1','2026-11-01','2026-11-01')"
        )
        db.commit()
    before = {filename: _schema(directory / filename) for filename in DOMAIN_TARGET_FILES.values()}
    with pytest.MonkeyPatch.context() as current:
        current.setattr(ScriptDirectory, "get_heads", lambda _scripts: [MIGRATION.revision])
        upgrade(url)
    return SimpleNamespace(directory=directory, before=before)


def test_split_layout_upgrade_changes_only_the_matrix_target(layout):
    for filename in DOMAIN_TARGET_FILES.values():
        after = _schema(layout.directory / filename)
        if filename != "kb_matrix.db":
            assert after == layout.before[filename], filename
            continue
        before = dict(layout.before[filename])
        assert set(after) - set(before) == {"kb_matrix_cell_events", "idx_kb_matrix_cell_events_cell"}
        assert after.pop("kb_matrices").endswith(", visibility TEXT NOT NULL DEFAULT 'space' CHECK(visibility IN ('space','private')))")
        before.pop("kb_matrices")
        assert {name: sql for name, sql in after.items() if name in before} == before
    state = json.loads((layout.directory / "sqlite-layout.json").read_text())
    assert {target["head"] for target in state["targets"].values()} == {MIGRATION.revision}
    with closing(sqlite3.connect(layout.directory / "kb_matrix.db")) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT matrix_id,visibility FROM kb_matrices").fetchall() == [("mx_old", "space")]


async def test_account_erasure_drops_history_of_the_users_files_and_anonymizes_the_rest(tmp_path, layout, monkeypatch):
    directory = tmp_path / "sqlite"
    shutil.copytree(layout.directory, directory, ignore=shutil.ignore_patterns("*.lock"))
    monkeypatch.chdir(tmp_path)
    core = str(directory / "bowhead.db")
    users = UserStore(core)
    await users.initialize()
    team = await users.create_team(name="Matrix", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="owner@example.test", password="Passw0rd!")
    leaving = await users.create_user(team_id=team.id, email="leaving@example.test", password="Passw0rd!")
    files = KbFilesStore(directory / "kb_files.db")
    await files.initialize()

    async def add(hint, scope, scope_id, uploaded_by):
        return await files.create(
            artifact_id=f"art_{hint}",
            filename=f"{hint}.txt",
            mime_type="text/plain",
            size_bytes=1,
            scope=scope,
            scope_id=scope_id,
            workspace_id="ws_1",
            team_id=team.id,
            uploaded_by=uploaded_by,
        )

    personal = await add("personal", "personal", leaving.id, leaving.id)
    shared = await add("shared", "space", "ws_1", owner.id)
    matrices = KbMatrixStore(directory / "kb_matrix.db")
    await matrices.initialize()
    matrix = await matrices.create_matrix(
        name="Review",
        workspace_id="ws_1",
        team_id=team.id,
        created_by=owner.id,
        files=[{"file_id": personal, "char_count": 1}, {"file_id": shared, "char_count": 1}],
        columns=[{"name": "Amount", "type": "question", "question": "How much?"}],
    )
    col = matrix.columns[0]["column_id"]
    for file_id in (personal, shared):
        await matrices.edit_cell_value(
            matrix.matrix_id, file_id, col, value="1", column=QUESTION, workspace_id="ws_1", team_id=team.id, actor_user_id=leaving.id
        )
    await matrices.set_cell_verified(matrix.matrix_id, shared, col, workspace_id="ws_1", verified=True, actor_user_id=owner.id)

    async def retire(db):
        await db.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source,state) "
            "VALUES (?,'adel_matrix_history','2026-09-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','request','purging')",
            (leaving.id,),
        )

    await run_sqlite_authority_write(core, retire)
    app = SimpleNamespace(state=SimpleNamespace(user_store=users, kb_store=KbStore(str(tmp_path / "chroma"))))
    await AccountExternalCleanup(app)(leaving.id, "adel_matrix_history")

    with closing(sqlite3.connect(directory / "kb_matrix.db")) as db:
        events = db.execute("SELECT file_id,kind,actor_user_id FROM kb_matrix_cell_events ORDER BY cell_seq").fetchall()
        files_json = db.execute("SELECT files_json FROM kb_matrices WHERE matrix_id=?", (matrix.matrix_id,)).fetchone()[0]
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    # The personal file's history left with it; the shared cell keeps its history without the person.
    assert events == [(shared, "edited", "erased"), (shared, "verified", owner.id)]
    assert [entry["file_id"] for entry in json.loads(files_json)] == [shared]


async def test_account_erasure_deletes_the_users_private_matrices_and_keeps_space_ones(tmp_path, layout, monkeypatch):
    """Only-me matrices (S8) leave with their creator's account, with cells, runs and history; Space ones stay."""
    directory = tmp_path / "sqlite"
    shutil.copytree(layout.directory, directory, ignore=shutil.ignore_patterns("*.lock"))
    monkeypatch.chdir(tmp_path)
    core = str(directory / "bowhead.db")
    users = UserStore(core)
    await users.initialize()
    team = await users.create_team(name="Matrix", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="owner@example.test", password="Passw0rd!")
    leaving = await users.create_user(team_id=team.id, email="leaving@example.test", password="Passw0rd!")
    files = KbFilesStore(directory / "kb_files.db")
    await files.initialize()
    personal = await files.create(
        artifact_id="art_personal",
        filename="personal.txt",
        mime_type="text/plain",
        size_bytes=1,
        scope="personal",
        scope_id=leaving.id,
        workspace_id="ws_1",
        team_id=team.id,
        uploaded_by=leaving.id,
    )
    shared = await files.create(
        artifact_id="art_shared",
        filename="shared.txt",
        mime_type="text/plain",
        size_bytes=1,
        scope="space",
        scope_id="ws_1",
        workspace_id="ws_1",
        team_id=team.id,
        uploaded_by=owner.id,
    )
    matrices = KbMatrixStore(directory / "kb_matrix.db")
    await matrices.initialize()
    columns = [{"name": "Amount", "type": "question", "question": "How much?"}]

    async def matrix(created_by, visibility, file_ids):
        m = await matrices.create_matrix(
            name=visibility,
            workspace_id="ws_1",
            team_id=team.id,
            created_by=created_by,
            files=[{"file_id": f, "char_count": 1} for f in file_ids],
            columns=columns,
            visibility=visibility,
        )
        for file_id in file_ids:
            await matrices.edit_cell_value(
                m.matrix_id,
                file_id,
                m.columns[0]["column_id"],
                value="1",
                column=QUESTION,
                workspace_id="ws_1",
                team_id=team.id,
                actor_user_id=leaving.id,
            )
        await matrices.create_run(
            matrix_id=m.matrix_id,
            workspace_id="ws_1",
            team_id=team.id,
            scope={"kind": "all_pending"},
            initiated_by=created_by,
            cells_total=1,
            skipped_edited=0,
            credits_estimated=1,
        )
        return m.matrix_id

    gone = await matrix(leaving.id, "private", [personal, shared])
    kept_space = await matrix(leaving.id, "space", [shared])
    kept_private = await matrix(owner.id, "private", [shared])

    async def retire(db):
        await db.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source,state) "
            "VALUES (?,'adel_private_matrix','2026-09-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','request','purging')",
            (leaving.id,),
        )

    await run_sqlite_authority_write(core, retire)
    app = SimpleNamespace(state=SimpleNamespace(user_store=users, kb_store=KbStore(str(tmp_path / "chroma"))))
    await AccountExternalCleanup(app)(leaving.id, "adel_private_matrix")

    with closing(sqlite3.connect(directory / "kb_matrix.db")) as db:
        left = {
            table: {row[0] for row in db.execute(f"SELECT DISTINCT matrix_id FROM {table}")}  # noqa: S608 -- Fixed table names.
            for table in ("kb_matrices", "kb_matrix_cells", "kb_matrix_runs", "kb_matrix_cell_events")
        }
        creators = dict(db.execute("SELECT matrix_id, created_by FROM kb_matrices"))
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    for table, matrix_ids in left.items():
        assert gone not in matrix_ids, table
        assert {kept_space, kept_private} <= matrix_ids, table
    # Space matrices stay shared resources under their creator, as before.
    assert creators[kept_space] == leaving.id and creators[kept_private] == owner.id


def test_pre_split_matrix_schema_is_reviewed_without_the_appended_objects():
    tables = {"kb_matrices": ["matrix_id", "visibility"], "kb_matrix_cell_events": ["event_id"], "kb_matrix_cells": ["matrix_id"]}
    assert _reviewed_secondary("kb_matrix.db", tables, set(tables)) == tables
    assert _reviewed_secondary("kb_matrix.db", tables, {"kb_matrices", "kb_matrix_cells"}) == {
        "kb_matrices": ["matrix_id"],
        "kb_matrix_cells": ["matrix_id"],
    }
