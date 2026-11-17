"""The file store query behind the conversation result lookup.

docs/sprints/result-lookup-2026-11-15/tech-design.md §5: a listed file is one the
user can open in this conversation and one that ``revise_files`` resolution accepts.
"""

from importlib import import_module

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.results_artifact_policy import SANDBOX_STDOUT_PRODUCER

pytestmark = pytest.mark.asyncio

OTHER_STORES = (
    "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, operation_kind TEXT);"
    "CREATE TABLE IF NOT EXISTS turn_attachments (turn_id TEXT, artifact_id TEXT, seq INTEGER, PRIMARY KEY (turn_id, artifact_id));"
    "CREATE TABLE IF NOT EXISTS session_attachments (session_id TEXT, artifact_id TEXT, pinned_at TEXT, detached_at TEXT, "
    "PRIMARY KEY (session_id, artifact_id));"
    "CREATE TABLE IF NOT EXISTS artifact_purge_claims (artifact_id TEXT PRIMARY KEY);"
)
TURNS = [
    ("t1", "s1", 1, None),
    ("t_compact", "s1", 2, "context_compaction"),
    ("t2", "s1", 3, None),
    ("t3", "s1", 4, None),
    ("t9", "s_other", 1, None),
]


RESULTS_MEMBERSHIP = (
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT);"
    "CREATE TABLE IF NOT EXISTS canvases (id TEXT PRIMARY KEY, session_id TEXT);"
    "INSERT INTO sessions(id, workspace_id, user_id) VALUES ('s1', 'w1', 'u');"
    "INSERT INTO canvases(id, session_id) VALUES ('cv1', 's1');"
)


async def _store(tmp_path, *, results_membership: bool) -> ArtifactStore:
    contract = import_module("bowhead_core.migrations.versions.0228_artifact_versions")
    artifact_store = ArtifactStore(db_path=str(tmp_path / "art.db"), backend=LocalBackend(root_dir=tmp_path / "blob"))
    await artifact_store.initialize()
    if results_membership:
        from bowhead_core.turn_log_store import TurnLogStore

        await TurnLogStore(artifact_store.db_path).initialize()
    async with aiosqlite.connect(artifact_store.db_path) as db:
        await db.executescript(OTHER_STORES + (RESULTS_MEMBERSHIP if results_membership else ""))
        for column, declaration in contract.COLUMNS:
            await db.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        for statement in (contract.LINEAGE_INDEX, contract.VERSION_OF_UNIQUE, contract.VERSION_IMMUTABLE):
            await db.execute(statement)
        await db.executemany("INSERT INTO turns(id, session_id, seq, operation_kind) VALUES (?, ?, ?, ?)", TURNS)
        await db.commit()
    return artifact_store


@pytest.fixture
async def store(tmp_path) -> ArtifactStore:
    """A content store with file versions and only the tables the indexed membership sources need."""
    return await _store(tmp_path, results_membership=False)


@pytest.fixture
async def results_store(tmp_path) -> ArtifactStore:
    """The same store with sessions, canvases and Turn events, as the Results membership reads them."""
    return await _store(tmp_path, results_membership=True)


async def _put(
    store: ArtifactStore, name: str, *, turn: str | None = "t1", user: str = "u", kind: str = "deliverable", target: str | None = None, **more
):
    fields = {
        "content": name.encode(),
        "filename": name,
        "mime": "text/html",
        "created_by": user,
        "source": "python_sandbox",
        "kind": kind,
        "created_by_turn": turn,
        "workspace_id": "w1",
        **more,
    }
    return await store.put_bytes(version_of_target=target, **fields)


async def _names(store: ArtifactStore, **filters) -> tuple[list[str], int]:
    found, total = await store.find_session_results(session_id="s1", user_id="u", **filters)
    return [artifact.filename for artifact in found], total


async def test_a_listed_file_is_one_the_user_can_open_and_resolution_accepts(store: ArtifactStore) -> None:
    await _put(store, "report.html")
    await _put(store, "rows.csv", kind="sandbox_output")
    await _put(store, "extract.csv", kind="tool_data", source="bi_fetch_rows")
    upload = await _put(store, "budget.xlsx", turn=None, kind="user_upload", source="upload")
    await _put(store, "stdout.txt", kind="sandbox_output", producer_kind=SANDBOX_STDOUT_PRODUCER)
    await _put(store, "internal.docx", producer_kind="template_document_internal_v1")
    await _put(store, "other-user.html", user="v")
    await _put(store, "other-session.html", turn="t9")
    claimed = await _put(store, "claimed.html")
    pending = await _put(store, "pending.html")
    deleted = await _put(store, "deleted.html")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO turn_attachments(turn_id, artifact_id, seq) VALUES ('t2', ?, 0)", (upload.id,))
        await db.execute("INSERT INTO artifact_purge_claims(artifact_id) VALUES (?)", (claimed.id,))
        await db.execute("UPDATE artifacts SET status = 'pending_upload' WHERE id = ?", (pending.id,))
        await db.commit()
    await store.soft_delete(deleted.id, user_id="u")

    assert sorted((await _names(store))[0]) == ["budget.xlsx", "extract.csv", "report.html", "rows.csv"]
    assert await _names(store, kinds=("deliverable", "deck_preview", "canvas_export", "user_upload")) == (["budget.xlsx", "report.html"], 2)
    assert await _names(store, kinds=("user_upload",)) == (["budget.xlsx"], 1)
    assert sorted((await _names(store, kinds=("tool_data", "sandbox_output", "web_clip")))[0]) == ["extract.csv", "rows.csv"]
    assert await _names(store, sources=("bi_fetch_rows",)) == (["extract.csv"], 1)
    assert await _names(store, name="REPORT") == (["report.html"], 1)
    assert await _names(store, name="nothing-like-this") == ([], 0)
    # Every listed identifier is a member the way revise_files resolution checks it.
    for artifact in (await store.find_session_results(session_id="s1", user_id="u"))[0]:
        assert await store.list_for_session(session_id="s1", user_id="u", artifact_id=artifact.id, limit=1)


async def test_a_name_fragment_is_matched_literally(store: ArtifactStore) -> None:
    await _put(store, "100%_final.html")
    await _put(store, "100x-final.html")
    assert await _names(store, name="100%_") == (["100%_final.html"], 1)
    assert await _names(store, name="_") == (["100%_final.html"], 1)


async def test_a_file_is_one_row_as_its_newest_live_version(store: ArtifactStore) -> None:
    first = await _put(store, "report.html", session_id="s1")
    second = await _put(store, "report.html", turn="t2", target=first.id, session_id="s1")
    third = await _put(store, "report.html", turn="t3", target=second.id, session_id="s1")
    found, total = await store.find_session_results(session_id="s1", user_id="u")
    assert [(artifact.id, artifact.version_no) for artifact in found] == [(third.id, 3)]
    assert total == 1
    # Deleting a file deletes every version of it, so no older version is listed in its place.
    await store.soft_delete(third.id, user_id="u")
    assert await store.find_session_results(session_id="s1", user_id="u") == ([], 0)


async def test_the_total_counts_what_matches_beyond_the_rows_returned(store: ArtifactStore) -> None:
    for number in range(7):
        await _put(store, f"store-{number:02d}.html")
    found, total = await store.find_session_results(session_id="s1", user_id="u", limit=3)
    assert len(found) == 3 and total == 7
    assert [artifact.filename for artifact in found] == ["store-06.html", "store-05.html", "store-04.html"]


async def test_a_turn_position_counts_ordinary_turns_only(store: ArtifactStore) -> None:
    early = await _put(store, "early.html")
    late = await _put(store, "late.html", turn="t2")
    upload = await _put(store, "budget.xlsx", turn=None, kind="user_upload", source="upload")
    pinned = await _put(store, "pinned.xlsx", turn=None, kind="user_upload", source="upload")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO turn_attachments(turn_id, artifact_id, seq) VALUES ('t2', ?, 0)", (upload.id,))
        await db.execute("INSERT INTO session_attachments(session_id, artifact_id, pinned_at) VALUES ('s1', ?, 'now')", (pinned.id,))
        await db.commit()
    found, _total = await store.find_session_results(session_id="s1", user_id="u")
    positions = await store.session_turn_positions(session_id="s1", artifacts=found)
    # The compaction Turn between them has a sequence number and no position.
    assert positions == {early.id: 1, late.id: 2, upload.id: 2}
    assert pinned.id in {artifact.id for artifact in found}


async def test_a_file_reached_through_the_canvas_is_listed(results_store: ArtifactStore) -> None:
    exported = await _put(results_store, "report.pdf", turn=None, kind="canvas_export", source="canvas_export_skill", canvas_id="cv1")
    found, total = await results_store.find_session_results(session_id="s1", user_id="u", kinds=("canvas_export",))
    assert [artifact.id for artifact in found] == [exported.id] and total == 1
