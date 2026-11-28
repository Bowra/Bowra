"""Knowledge bases (0239) against the migrated split layout: backfill, dual write, chunk key and erasure."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.script import ScriptDirectory
from bowhead_cli.migrate import upgrade
from bowhead_core._db import run_sqlite_authority_write
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.embedder import EMBED_DIM
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.ingestion import KnowledgeAction, canonical_hash
from bowhead_core.kb.knowledge_base_backfill import backfill_chunk_knowledge_base_ids, chunk_key_backfill_completed, state_key
from bowhead_core.kb.knowledge_base_store import default_for_scope
from bowhead_core.kb.maintenance import KbMaintenanceStore
from bowhead_core.kb.store import KbStore
from bowhead_core.kb.worker import KbWorker
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.user_store import UserStore
from bowhead_gateway.account_deletion_cleanup import AccountExternalCleanup, _reviewed_secondary
from bowhead_gateway.kb_maintenance import backfill_chunk_knowledge_base_keys

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]
MIGRATION = import_module("bowhead_core.migrations.versions.0239_knowledge_bases")
NEW_OBJECTS = {
    "kb_knowledge_bases",
    "uq_kb_knowledge_bases_default",
    "idx_kb_knowledge_bases_home",
    "kb_knowledge_base_members",
    "idx_kb_knowledge_base_members_principal",
    "idx_kb_files_knowledge_base",
}
SEEDED = (
    # file_id, scope, scope_id, workspace_id, uploaded_by, team, soft-deleted
    ("kbf_mem_ws1", "personal", "u_mem", "ws_1", "u_mem", "team_a", False),
    ("kbf_mem_ws2", "personal", "u_mem", "ws_2", "u_mem", "team_a", False),
    ("kbf_mem_legacy", "personal", "u_mem", "", "u_mem", "team_a", True),
    ("kbf_space", "space", "ws_1", "ws_1", "u_mem", "team_a", False),
    ("kbf_space_legacy", "workspace", "ws_1", "ws_1", "u_view", "team_a", False),
    ("kbf_team", "team", "team_a", "", "u_admin", "team_a", False),
    ("kbf_team_gone", "team", "team_a", "", "u_admin", "team_a", True),
    ("kbf_other_team", "team", "team_b", "", "u_mem", "team_b", False),
)


def _schema(path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as db:
        return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    """A split layout at 0238 with knowledge rows, then upgraded by the startup coordinator to 0239.

    The second upgrade stops at this revision, so later revisions (other targets) do not show up as its changes.
    """
    directory = tmp_path_factory.mktemp("knowledge-bases") / "sqlite"
    url = f"sqlite:///{directory / 'bowhead.db'}"
    with pytest.MonkeyPatch.context() as previous:
        previous.setattr(ScriptDirectory, "get_heads", lambda _scripts: [MIGRATION.down_revision])
        upgrade(url)
    with closing(connect_sync(directory / "kb_files.db")) as db:
        for file_id, scope, scope_id, workspace_id, uploaded_by, team, deleted in SEEDED:
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,"
                "uploaded_at,parse_status,soft_deleted_at) VALUES (?,?,?,'text/plain',1,?,?,?,?,?,'2026-11-01T00:00:00+00:00','done',?)",
                (file_id, f"art_{file_id}", f"{file_id}.txt", scope, scope_id, workspace_id, team, uploaded_by, "2026-11-02" if deleted else None),
            )
        db.commit()
    before = {filename: _schema(directory / filename) for filename in DOMAIN_TARGET_FILES.values()}
    with pytest.MonkeyPatch.context() as current:
        current.setattr(ScriptDirectory, "get_heads", lambda _scripts: [MIGRATION.revision])
        upgrade(url)
    return SimpleNamespace(directory=directory, before=before)


@pytest.fixture
def ctx(tmp_path, layout, monkeypatch):
    directory = tmp_path / "sqlite"
    shutil.copytree(layout.directory, directory, ignore=shutil.ignore_patterns("*.lock"))
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(
        directory=directory,
        core=str(directory / "bowhead.db"),
        files=KbFilesStore(directory / "kb_files.db"),
        kb_store=KbStore(str(tmp_path / "chroma")),
    )


def _rows(ctx, sql="SELECT file_id,scope,scope_id,team_id,knowledge_base_id FROM kb_files", params=()):
    with closing(sqlite3.connect(ctx.directory / "kb_files.db")) as db:
        return db.execute(sql, params).fetchall()


def _knowledge_bases(ctx) -> dict[str, tuple]:
    return {row[0]: row[1:] for row in _rows(ctx, "SELECT id,team_id,home,home_id,is_default,owner_user_id FROM kb_knowledge_bases")}


def test_split_layout_upgrade_changes_only_the_files_target(layout):
    for filename in DOMAIN_TARGET_FILES.values():
        after = _schema(layout.directory / filename)
        if filename != "kb_files.db":
            assert after == layout.before[filename], filename
            continue
        before = dict(layout.before[filename])
        assert set(after) - set(before) == NEW_OBJECTS
        assert after.pop("kb_files") == before.pop("kb_files")[:-1] + ", knowledge_base_id TEXT REFERENCES kb_knowledge_bases(id))"
        assert {name: sql for name, sql in after.items() if name in before} == before
    state = json.loads((layout.directory / "sqlite-layout.json").read_text())
    assert {target["head"] for target in state["targets"].values()} == {MIGRATION.revision}
    with closing(sqlite3.connect(layout.directory / "kb_files.db")) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        rows = db.execute("SELECT file_id,scope,scope_id,team_id,knowledge_base_id FROM kb_files").fetchall()
        bases = db.execute("SELECT COUNT(*) FROM kb_knowledge_bases").fetchone()
    assert len(rows) == len(SEEDED)
    for _file_id, scope, scope_id, team, knowledge_base_id in rows:
        assert knowledge_base_id == default_for_scope(scope, scope_id, team)
    # My knowledge (one for three Spaces), the Space (both spellings), Team A and Team B.
    assert bases == (4,)


async def test_each_writer_sets_its_scope_default_in_the_same_transaction(ctx):
    files = ctx.files
    space_id = await files.create(
        artifact_id="art_new_space",
        filename="a.txt",
        mime_type="text/plain",
        size_bytes=1,
        scope="space",
        scope_id="ws_new",
        workspace_id="ws_new",
        team_id="team_a",
        uploaded_by="u_mem",
    )
    space = await files.get(space_id)
    assert space is not None and space.knowledge_base_id == default_for_scope("space", "ws_new", "team_a")
    # The empty home's default was created lazily, without a name and with the fixed Space audience.
    assert _rows(
        ctx,
        "SELECT home,home_id,is_default,name,audience,audience_level,owner_user_id FROM kb_knowledge_bases WHERE id=?",
        (space.knowledge_base_id,),
    ) == [("space", "ws_new", 1, None, "space", "edit", None)]

    action = KnowledgeAction("ref-personal", "kb_ui", "u_new", "team_a", "ws_1")
    values = {
        "artifact_id": "art_new_personal",
        "filename": "b.txt",
        "mime_type": "text/plain",
        "size_bytes": 1,
        "scope": "personal",
        "scope_id": "u_new",
        "workspace_id": "ws_1",
        "team_id": "team_a",
        "uploaded_by": "u_new",
    }
    personal_id, created, operation_id = await files.create_admitted(action=action, source_fingerprint="v1", **values)
    assert created
    personal = await files.get(personal_id)
    my_knowledge = default_for_scope("personal", "u_new", "team_a")
    assert personal is not None and personal.knowledge_base_id == my_knowledge
    assert _knowledge_bases(ctx)[my_knowledge] == ("team_a", "personal", "u_new", 1, "u_new")
    # Receipts for default adds keep their original shape, so an older receipt still replays.
    receipt = await files.get_operation(operation_id)
    destination = {"scope": "personal", "scope_id": "u_new", "team_id": "team_a"}
    assert receipt is not None and json.loads(receipt["destination_json"]) == destination
    assert receipt["intent_hash"] == canonical_hash({"source": "art_new_personal", "version": "v1", "destination": destination})
    assert (await files.create_admitted(action=action, source_fingerprint="v1", **values))[:2] == (personal_id, False)

    # Today's scope change moves the file to the target home's default, creating it when missing.
    await files.update_scope(space_id, "personal", "u_mem", workspace_id="ws_new", team_id="team_a")
    assert (await files.get(space_id)).knowledge_base_id == default_for_scope("personal", "u_mem", "team_a")
    await files.update_scope(space_id, "team", "team_c", workspace_id="", team_id="team_c")
    moved = await files.get(space_id)
    assert moved.knowledge_base_id == default_for_scope("team", "team_c", "team_c")
    assert _knowledge_bases(ctx)[moved.knowledge_base_id] == ("team_c", "team", "team_c", 1, None)
    with closing(sqlite3.connect(ctx.directory / "kb_files.db")) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT COUNT(*) FROM kb_files WHERE knowledge_base_id IS NULL").fetchone() == (0,)


async def test_reads_treat_a_stale_or_unknown_knowledge_base_as_the_scope_default(ctx):
    with closing(connect_sync(ctx.directory / "kb_files.db")) as db:
        db.execute(
            "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,audience,audience_level,owner_user_id,created_at,updated_at) "
            "VALUES ('kbase_finance000000000000000000','team_a','space','ws_1',0,'Finance','members','use','u_mem','t','t')"
        )
        db.execute("UPDATE kb_files SET knowledge_base_id='kbase_finance000000000000000000' WHERE file_id IN ('kbf_space','kbf_team')")
        db.commit()
    files = await ctx.files.get_many(["kbf_space", "kbf_team", "kbf_mem_ws1"])
    effective = await ctx.files.effective_knowledge_base_ids(files)
    assert effective == {
        "kbf_space": "kbase_finance000000000000000000",  # a named knowledge base in the file's own home
        "kbf_team": default_for_scope("team", "team_a", "team_a"),  # a Space knowledge base cannot hold a Team file
        "kbf_mem_ws1": default_for_scope("personal", "u_mem", "team_a"),
    }


def _legacy_chunk(file_id: str, scope: str, scope_id: str, workspace_id: str, team_id: str, index: int = 1) -> KbChunk:
    """A chunk written before 0239: no knowledge base key."""
    return KbChunk(
        chunk_id=make_chunk_id(file_id, index, f"{file_id} text {index}"),
        file_id=file_id,
        content=f"{file_id} text {index}",
        embedding=[0.1, 0.2, 0.3],
        metadata=ChunkMetadata(
            file_id=file_id,
            file_name=f"{file_id}.txt",
            scope="space" if scope == "workspace" else scope,  # type: ignore[arg-type]
            scope_owner=scope_id,
            workspace_id=workspace_id,
            team_id=team_id,
            chunk_index=index,
        ),
    )


async def _keys(ctx, file_id: str) -> set:
    file = await ctx.files.get(file_id)
    collection = await ctx.kb_store._file_collection(file)
    return {meta.get("knowledge_base_id") for meta in collection.get(where={"file_id": file_id}, include=["metadatas"])["metadatas"]}


async def test_chunk_backfill_is_resumable_idempotent_and_per_team(ctx):
    for file_id, scope, scope_id, workspace_id, _uploader, team, _deleted in SEEDED:
        file = await ctx.files.get(file_id)
        await ctx.kb_store.add_chunks(file, [_legacy_chunk(file_id, scope, scope_id, workspace_id, team, index) for index in (1, 2)])
    first = await backfill_chunk_knowledge_base_ids(ctx.files, ctx.kb_store, "team_a", page_size=2, max_files=3)
    assert (first.completed, first.files, first.chunks_updated, first.after) == (False, 3, 6, "kbf_mem_ws2")
    assert not await chunk_key_backfill_completed(ctx.files._db_path, "team_a", ctx.kb_store.index_version)
    # A file moves while the job is paused: the move itself writes the target default into its chunks.
    await ctx.files.update_scope("kbf_space", "team", "team_a", workspace_id="", team_id="team_a")
    await ctx.kb_store.update_chunks_scope("kbf_space", "space", "ws_1", "ws_1", "team_a", "team", "team_a", "", "team_a")
    done = await backfill_chunk_knowledge_base_ids(ctx.files, ctx.kb_store, "team_a")
    assert (done.completed, done.files) == (True, 7)
    for file_id, *_ in SEEDED:
        file = await ctx.files.get(file_id)
        expected = {default_for_scope(file.scope, file.scope_id, file.team_id)} if file.team_id == "team_a" else {None}
        assert await _keys(ctx, file_id) == expected, file_id  # soft-deleted rows keep chunks, so they get the key too
    again = await backfill_chunk_knowledge_base_ids(ctx.files, ctx.kb_store, "team_a")
    assert again == done
    assert await chunk_key_backfill_completed(ctx.files._db_path, "team_a", ctx.kb_store.index_version)
    assert not await chunk_key_backfill_completed(ctx.files._db_path, "team_b", ctx.kb_store.index_version)
    # Another index version is a different set of chunks: completion does not carry over.
    assert not await chunk_key_backfill_completed(ctx.files._db_path, "team_a", "v2")
    state = await KbMaintenanceStore(ctx.files._db_path).state_value(state_key("team_a"))
    assert state["completed_at"] and state["after"] == "kbf_team_gone"


async def test_chunk_backfill_redoes_a_file_that_moves_during_its_write(ctx):
    file = await ctx.files.get("kbf_space")
    await ctx.kb_store.add_chunks(file, [_legacy_chunk("kbf_space", "space", "ws_1", "ws_1", "team_a")])
    real = ctx.kb_store.set_chunks_knowledge_base
    calls = []

    async def move_after_first_write(target_file, knowledge_base_id):
        written = await real(target_file, knowledge_base_id)
        if target_file.file_id != "kbf_space":
            return written
        calls.append(knowledge_base_id)
        if len(calls) == 1:
            # In place within the Space collection, as a Space → My knowledge move is.
            await ctx.files.update_scope("kbf_space", "personal", "u_mem", workspace_id="ws_1", team_id="team_a")
        return written

    ctx.kb_store.set_chunks_knowledge_base = move_after_first_write
    progress = await backfill_chunk_knowledge_base_ids(ctx.files, ctx.kb_store, "team_a")
    mine = default_for_scope("personal", "u_mem", "team_a")
    assert progress.completed and calls == [default_for_scope("space", "ws_1", "team_a"), mine]
    assert await _keys(ctx, "kbf_space") == {mine}


async def test_maintenance_entry_runs_the_team_backfill_in_its_current_index(ctx):
    runtime = SimpleNamespace(kb_store=ctx.kb_store)
    app = SimpleNamespace(
        state=SimpleNamespace(
            kb_files_store=ctx.files,
            user_store=SimpleNamespace(get_team_llm_config_with_version=AsyncMock(return_value=({}, 1))),
            team_kb_runtime_registry=SimpleNamespace(get=AsyncMock(return_value=runtime)),
        )
    )
    progress = await backfill_chunk_knowledge_base_keys(app, "team_a")
    assert progress.completed and progress.files == 7
    with pytest.raises(ValueError, match="team required"):
        await backfill_chunk_knowledge_base_keys(app, "")


async def test_the_agent_mode_waits_for_the_backfill_which_a_readiness_check_starts(ctx):
    """C7 (slice 5): one state row says whether the Team's current index carries the key; asking starts the backfill once."""
    import asyncio

    from bowhead_gateway.kb_maintenance import chunk_key_backfill_state

    runtime = SimpleNamespace(kb_store=ctx.kb_store)
    app = SimpleNamespace(
        state=SimpleNamespace(
            kb_files_store=ctx.files,
            user_store=SimpleNamespace(get_team_llm_config_with_version=AsyncMock(return_value=({}, 1))),
            team_kb_runtime_registry=SimpleNamespace(get=AsyncMock(return_value=runtime)),
        )
    )
    assert await chunk_key_backfill_state(app, "team_a", start=False) == "preparing"
    assert not hasattr(app.state, "kb_chunk_key_backfill_tasks")
    assert await chunk_key_backfill_state(app, "team_a") == "preparing"
    task = app.state.kb_chunk_key_backfill_tasks["team_a"]
    assert await chunk_key_backfill_state(app, "team_a") == "preparing"
    assert app.state.kb_chunk_key_backfill_tasks.get("team_a", task) is task  # one task per Team
    await asyncio.wait_for(task, 10)
    assert await chunk_key_backfill_state(app, "team_a") == "ready"
    app.state.team_kb_runtime_registry.get = AsyncMock(return_value=None)
    assert await chunk_key_backfill_state(app, "team_a") == "unavailable"


async def test_new_chunks_carry_the_knowledge_base_of_the_current_row(ctx, tmp_path):
    document = tmp_path / "doc.md"
    document.write_text("# Budget\n\nQuarterly budget is three million.\n", encoding="utf-8")
    file_id = await ctx.files.create(
        artifact_id=str(document),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=document.stat().st_size,
        scope="space",
        scope_id="ws_1",
        workspace_id="ws_1",
        team_id="team_a",
        uploaded_by="u_mem",
    )

    async def embed(texts):
        # The file moves to My knowledge while it is being embedded; the worker writes under the new identity.
        await ctx.files.update_scope(file_id, "personal", "u_mem", workspace_id="ws_1", team_id="team_a")
        return [[0.1] * EMBED_DIM for _ in texts]

    worker = KbWorker(files_store=ctx.files, store=ctx.kb_store, embedder=SimpleNamespace(embed_batch=embed))
    await worker.parse_and_index(file_id)
    row = await ctx.files.get(file_id)
    assert row.parse_status == "done"
    assert await _keys(ctx, file_id) == {default_for_scope("personal", "u_mem", "team_a")}


async def test_account_erasure_removes_my_knowledge_and_hands_over_shared_knowledge_bases(ctx):
    users = UserStore(ctx.core)
    await users.initialize()
    team = await users.create_team(name="Knowledge", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="owner@example.test", password="Passw0rd!")
    leaving = await users.create_user(team_id=team.id, email="leaving@example.test", password="Passw0rd!")

    async def make_owner(db):
        await db.execute("UPDATE teams SET created_by_user_id=? WHERE id=?", (owner.id, team.id))

    await run_sqlite_authority_write(ctx.core, make_owner)

    async def add(file_id_hint, scope, scope_id, uploaded_by):
        return await ctx.files.create(
            artifact_id=f"art_{file_id_hint}",
            filename=f"{file_id_hint}.txt",
            mime_type="text/plain",
            size_bytes=1,
            scope=scope,
            scope_id=scope_id,
            workspace_id="ws_1",
            team_id=team.id,
            uploaded_by=uploaded_by,
        )

    own = await add("own", "personal", leaving.id, leaving.id)
    # Someone else's file an administrator moved into the leaving person's personal scope.
    moved = await add("moved", "personal", leaving.id, owner.id)
    shared = await add("shared", "space", "ws_1", leaving.id)
    stale = await add("stale", "space", "ws_1", owner.id)
    named = "kbase_named000000000000000000"
    with closing(connect_sync(ctx.directory / "kb_files.db")) as db:
        # A home-mismatched value naming the leaving person's My knowledge must not block its deletion.
        db.execute("UPDATE kb_files SET knowledge_base_id=? WHERE file_id=?", (default_for_scope("personal", leaving.id, team.id), stale))
        db.execute(
            "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,audience,audience_level,owner_user_id,created_at,updated_at) "
            "VALUES (?,?,'space','ws_1',0,'Finance','members','use',?,'t','t')",
            (named, team.id, leaving.id),
        )
        db.executemany(
            "INSERT INTO kb_knowledge_base_members VALUES (?,?,?,?,?,'t')",
            [
                (named, "user", leaving.id, "edit", owner.id),
                (named, "user", owner.id, "manage", leaving.id),
                (named, "group", "grp_1", "use", leaving.id),
            ],
        )
        db.commit()

    async def retire(db):
        await db.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source,state) "
            "VALUES (?,'adel_knowledge_bases','2026-09-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','request','purging')",
            (leaving.id,),
        )

    await run_sqlite_authority_write(ctx.core, retire)
    app = SimpleNamespace(state=SimpleNamespace(user_store=users, kb_store=ctx.kb_store))
    await AccountExternalCleanup(app)(leaving.id, "adel_knowledge_bases")

    bases = _knowledge_bases(ctx)
    assert default_for_scope("personal", leaving.id, team.id) not in bases
    assert bases[named] == (team.id, "space", "ws_1", 0, owner.id)
    assert default_for_scope("space", "ws_1", team.id) in bases
    assert _rows(ctx, "SELECT principal_type,principal_id,level,added_by FROM kb_knowledge_base_members ORDER BY principal_type") == [
        ("group", "grp_1", "use", "erased"),
        ("user", owner.id, "manage", "erased"),
    ]
    remaining = {row[0]: row for row in _rows(ctx, "SELECT file_id,scope,knowledge_base_id FROM kb_files WHERE team_id=?", (team.id,))}
    # Personal files leave by owner, including one someone else uploaded.
    assert own not in remaining and moved not in remaining
    assert remaining[stale] == (stale, "space", None)
    assert remaining[shared] == (shared, "space", default_for_scope("space", "ws_1", team.id))
    with closing(sqlite3.connect(ctx.directory / "kb_files.db")) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_pre_split_secondary_schema_is_reviewed_without_the_appended_objects():
    tables = {"kb_files": ["file_id", "knowledge_base_id"], "kb_knowledge_bases": ["id"], "kb_knowledge_base_members": ["knowledge_base_id"]}
    assert _reviewed_secondary("kb_files.db", tables, set(tables)) == tables
    assert _reviewed_secondary("kb_files.db", tables, {"kb_files"}) == {"kb_files": ["file_id"]}
    from bowhead_core.account_deletion import AccountDeletionError

    for actual in ({"kb_files", "kb_knowledge_bases"}, {"kb_files", "unexpected"}, set()):
        with pytest.raises(AccountDeletionError):
            _reviewed_secondary("kb_files.db", tables, actual)
