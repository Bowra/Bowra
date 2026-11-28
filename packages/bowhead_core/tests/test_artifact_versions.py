"""File versions in the artifact store (docs/sprints/file-versions-2026-11-10/tech-design.md §4–§5).

A revised deliverable is recorded as a new version of the same file: the writer declares the
target, the store checks its scope inside the insert transaction, and history stays linear.
"""

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, VersionTargetRefused

pytestmark = pytest.mark.asyncio


async def _legacy_store(tmp_path) -> ArtifactStore:
    artifact_store = ArtifactStore(db_path=str(tmp_path / "art.db"), backend=LocalBackend(root_dir=tmp_path / "blob"))
    await artifact_store.initialize()
    async with aiosqlite.connect(artifact_store.db_path) as db:
        # Minimal shapes of tables owned by other stores (turns: session_store; purge claims: migration 0046).
        await db.executescript(
            "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT, status TEXT NOT NULL DEFAULT 'completed', ended_at TEXT);"
            "CREATE TABLE IF NOT EXISTS artifact_purge_claims (artifact_id TEXT PRIMARY KEY);"
        )
        await db.executemany("INSERT INTO turns(id, session_id) VALUES (?, ?)", [("t1", "s1"), ("t2", "s1"), ("t3", "s1"), ("t9", "s9")])
        await db.commit()
    return artifact_store


@pytest.fixture
async def store(tmp_path) -> ArtifactStore:
    """The content domain after migration 0228 (the legacy single-file layout stays frozen without it)."""
    from importlib import import_module

    contract = import_module("bowhead_core.migrations.versions.0228_artifact_versions")
    artifact_store = await _legacy_store(tmp_path)
    async with aiosqlite.connect(artifact_store.db_path) as db:
        for column, declaration in contract.COLUMNS:
            await db.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        for statement in (contract.LINEAGE_INDEX, contract.VERSION_OF_UNIQUE, contract.VERSION_IMMUTABLE):
            await db.execute(statement)
        await db.commit()
    return artifact_store


async def test_the_frozen_legacy_layout_stores_a_declared_version_as_a_new_file(tmp_path) -> None:
    store = await _legacy_store(tmp_path)
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    assert not await store.versions_supported()
    assert (second.version_of, second.lineage_id, second.version_no) == (None, None, None)
    assert {artifact.id for artifact in await store.list_session_deliverables(session_id="s1", user_id="u")} == {first.id, second.id}


async def _put(store: ArtifactStore, *, turn: str = "t1", target: str | None = None, user: str = "u", **overrides):
    fields = {
        "content": b"<html></html>",
        "filename": "report.html",
        "mime": "text/html",
        "created_by": user,
        "source": "python_sandbox",
        "kind": "deliverable",
        "created_by_turn": turn,
        "session_id": "s1",
        "workspace_id": "w1",
        **overrides,
    }
    return await store.put_bytes(version_of_target=target, **fields)


async def test_versions_chain_after_the_latest_and_the_inventory_lists_only_it(store: ArtifactStore) -> None:
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    third = await _put(store, turn="t3", target=second.id)
    assert (first.version_of, first.lineage_id, first.version_no) == (None, None, None)
    assert (second.version_of, second.lineage_id, second.version_no) == (first.id, first.id, 2)
    assert (third.version_of, third.lineage_id, third.version_no) == (second.id, first.id, 3)
    reread = await store.get(third.id)
    assert reread is not None and (reread.lineage_root, reread.version_no) == (first.id, 3)
    listed = await store.list_session_deliverables(session_id="s1", user_id="u")
    assert [artifact.id for artifact in listed] == [third.id]
    assert await store.latest_live_version_id(first) == third.id


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"user": "v"}, "version_target_not_found"),
        ({"turn": "t9", "session_id": "s9"}, "version_target_out_of_scope"),
        ({"kind": "tool_data"}, "version_target_out_of_scope"),
        ({"producer_kind": "template_document_v1"}, "version_target_out_of_scope"),
        ({"workspace_id": "w2"}, "version_target_out_of_scope"),
        ({"mime": "application/pdf", "filename": "report.pdf"}, "version_target_format_changed"),
    ],
)
async def test_targets_outside_the_scope_are_refused_before_any_bytes(store: ArtifactStore, setup: dict, reason: str) -> None:
    target = await _put(store, **setup)
    with pytest.raises(VersionTargetRefused) as refused:
        await _put(store, turn="t2", target=target.id)
    assert refused.value.reason == reason
    async with aiosqlite.connect(store.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM artifacts")).fetchone())[0]
    assert count == 1, "A refused version writes no row"


async def test_only_the_latest_version_can_be_revised(store: ArtifactStore) -> None:
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    with pytest.raises(VersionTargetRefused) as refused:
        await _put(store, turn="t3", target=first.id)
    assert (refused.value.reason, refused.value.latest_id) == ("version_target_not_latest", second.id)
    for new_row in ({"kind": "tool_data"}, {"session_id": None}):
        with pytest.raises(VersionTargetRefused) as scoped:
            await _put(store, turn="t3", target=second.id, **new_row)
        assert scoped.value.reason == "version_target_out_of_scope"


async def test_a_deleted_latest_version_keeps_history_linear(store: ArtifactStore) -> None:
    """Review P1: `version_of` is the highest row, deleted or not, so the UNIQUE index never blocks the next version."""
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    third = await _put(store, turn="t3", target=second.id)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-11-10T00:00:00Z' WHERE id=?", (third.id,))
        await db.commit()
    with pytest.raises(VersionTargetRefused) as gone:
        await _put(store, turn="t3", target=third.id)
    assert gone.value.reason == "version_target_not_found"
    fourth = await _put(store, turn="t3", target=second.id)
    assert (fourth.version_of, fourth.lineage_id, fourth.version_no) == (third.id, first.id, 4)


async def test_the_database_keeps_versions_linear_and_immutable(store: ArtifactStore) -> None:
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,version_of,lineage_id,version_no) "
                "VALUES ('art_fork','report.html','text/html',1,'python_sandbox','p','2026-11-10',?,?,2)",
                (first.id, first.id),
            )
        with pytest.raises(aiosqlite.IntegrityError, match="artifact_version_immutable"):
            await db.execute("UPDATE artifacts SET lineage_id='art_other' WHERE id=?", (second.id,))


async def test_revise_files_targets_are_classified_and_report_recorded_versions(store: ArtifactStore) -> None:
    """W3: a session deliverable is versionable, an older version is refused with the latest, an upload is a new file."""
    from bowhead_core.delivered_files import (
        FileTarget,
        _version_eligibility,
        file_target_context_line,
        recorded_versions,
        render_delivered_files_block,
        revised_files_facts,
    )

    first = await _put(store)
    upload = await _put(store, kind="user_upload", source="upload", filename="input.csv", mime="text/csv")
    assert await _version_eligibility(store, first, session_id="s1", user_id="u") == (True, None)
    assert await _version_eligibility(store, upload, session_id="s1", user_id="u") == (False, None)

    target = FileTarget(artifact_id=first.id, name="report.html", type="html", versionable=True, lineage_root=first.id)
    second = await _put(store, turn="t2", target=first.id)
    assert await _version_eligibility(store, first, session_id="s1", user_id="u") == (False, second.id)

    recorded = await recorded_versions(store, [target], "t2")
    declared = type("Inv", (), {"tool_name": "python_exec", "error": None, "nested": [], "arguments": {"purpose": "deliverable", "deliverable_outputs": ["report.html"]}, "result": {"output_artifacts": [{"artifact_id": second.id, "name": "report.html"}]}})()  # fmt: skip
    facts = revised_files_facts([target], [declared], recorded)
    assert facts["new_files"] == [
        {"artifact_id": second.id, "name": "report.html", "version": 2, "version_of": first.id, "recorded_as_version": True}
    ]
    other = FileTarget(artifact_id=upload.id, name="input.csv", type="csv")
    line = file_target_context_line([target, other])
    # Regression check N10: the same filename is recorded automatically; revises is only for a renamed file.
    assert "original's filename is recorded as its next version automatically" in line
    assert f'`revises={{"<new name>": "{first.id}"}}`' in line and "《input.csv》 cannot be versioned" in line
    listed = await store.list_session_deliverables(session_id="s1", user_id="u")
    assert f"artifact_id={second.id}" in render_delivered_files_block(listed) and "version=2" in render_delivered_files_block(listed)


async def test_the_files_library_lists_a_file_once_and_deletes_and_restores_it_whole(store: ArtifactStore) -> None:
    """A2: every version of a file is one entry; deleting any version deletes the file, restore brings all back."""
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    other = await _put(store, filename="other.html")
    items, total, _ = await store.list_for_user(user_id="u")
    assert total == 2 and {item.id for item in items} == {second.id, other.id}
    entry = next(item for item in items if item.id == second.id)
    assert (entry.version_no, entry.version_count) == (2, 2)

    assert await store.soft_delete(first.id, user_id="u")
    assert {item.id for item in (await store.list_for_user(user_id="u"))[0]} == {other.id}
    trash, trash_total, _ = await store.list_for_user(user_id="u", include_deleted=True)
    assert trash_total == 1 and (trash[0].id, trash[0].version_count) == (second.id, 2)

    assert await store.restore(second.id, user_id="u")
    assert {item.id for item in (await store.list_for_user(user_id="u"))[0]} == {second.id, other.id}
    assert set(await store.lineage_member_ids(second.id, user_id="u")) == {first.id, second.id}


async def test_files_page_queries_find_versions_through_the_lineage_index(store: ArtifactStore, monkeypatch) -> None:
    """116 (1.7.69): the shared account owns 24,660 artifacts. `COALESCE(lineage_id, id) = …` cannot use an index,
    so each version lookup read every artifact of the owner: 0.5 s for the version counts of a 50-row page, 0.6 s
    for its warnings, and 7 s for the trash count. Every per-row version lookup must use the lineage index or the
    primary key, never the owner index or a scan."""
    import sqlite3

    first = await _put(store)
    await _put(store, turn="t2", target=first.id)
    other = await _put(store, filename="other.html")
    recorded: list[tuple[str, tuple]] = []
    execute = aiosqlite.Connection.execute

    def recording(self, sql, parameters=None):
        recorded.append((sql, tuple(parameters or ())))
        return execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", recording)
    await store.list_for_user(user_id="u")
    await store.lineage_overview([first.id, other.id], user_id="u")
    assert await store.soft_delete(first.id, user_id="u")
    await store.list_for_user(user_id="u", include_deleted=True, limit=1)
    assert await store.restore(first.id, user_id="u")
    monkeypatch.undo()

    lookups = [(sql, params) for sql, params in recorded if any(f"artifacts {alias} " in sql for alias in ("m", "l", "a"))]
    assert len(lookups) >= 5, [sql for sql, _ in lookups]
    with sqlite3.connect(store.db_path) as db:
        for sql, params in lookups:
            plan = [row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
            for alias in ("m", "l", "a"):
                assert not any(line.startswith((f"SCAN {alias}", f"SEARCH {alias} USING INDEX idx_artifacts_user")) for line in plan), (sql, plan)


async def test_a_document_reference_to_any_version_refuses_deleting_the_file(store: ArtifactStore) -> None:
    from bowhead_core.documents.retention import DocumentArtifactInUse

    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("CREATE TABLE document_artifact_refs (artifact_id TEXT, released_at TEXT)")
        await db.execute("INSERT INTO document_artifact_refs(artifact_id, released_at) VALUES (?, NULL)", (first.id,))
        await db.commit()
    with pytest.raises(DocumentArtifactInUse) as in_use:
        await store.soft_delete(second.id, user_id="u")
    assert in_use.value.artifact_id == first.id
    assert {item.id for item in (await store.list_for_user(user_id="u"))[0]} == {second.id}, "Nothing was deleted"


async def test_files_page_warnings_cover_every_version(store: ArtifactStore) -> None:
    """A2 follow-up: a report attachment on an older version still warns before the file (all versions) is deleted."""
    first = await _put(store, canvas_id="cv_report")
    second = await _put(store, turn="t2", target=first.id)
    plain = await _put(store, filename="plain.html")
    overview = await store.lineage_overview([second.id, plain.id], user_id="u")
    assert set(overview[second.id][0]) == {first.id, second.id} and overview[second.id][1] is True
    assert overview[plain.id] == ([plain.id], False)


async def test_newer_versions_finds_the_files_newest_live_version(store: ArtifactStore) -> None:
    """B (R3, R6): one rule resolves a held version ID to its file's newest live version."""
    from bowhead_core.artifact_store import newer_version_facts

    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    third = await _put(store, turn="t3", target=second.id)
    newer = await store.newer_versions({first.id: (None, None), second.id: (None, None), third.id: (None, None)})
    assert set(newer) == {first.id, second.id}
    assert (newer[first.id].latest.id, newer[first.id].version_no, newer[first.id].pruned) == (third.id, 1, False)
    assert await newer_version_facts(store, [first.id, third.id]) == {first.id: {"id": third.id, "version": 3}}

    # A pruned version (soft-deleted or purged) still finds its file through the holder's stored root.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-11-13T00:00:00Z' WHERE id=?", (second.id,))
        await db.execute("DELETE FROM artifacts WHERE id=?", (first.id,))
        await db.commit()
    gone = await store.newer_versions({first.id: (first.id, 1), second.id: (None, None)})
    assert gone[first.id].pruned and gone[first.id].latest.id == third.id
    assert gone[second.id].pruned and gone[second.id].version_no == 2
    assert await store.newer_versions({first.id: (None, None)}) == {}, "Without a stored root a purged row has no file"


async def test_a_lower_live_version_is_not_newer(store: ArtifactStore) -> None:
    """Legacy A1 data: a deleted latest version left an earlier one live; a card for the deleted one gains nothing."""
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-11-13T00:00:00Z' WHERE id=?", (second.id,))
        await db.commit()
    assert await store.newer_versions({second.id: (None, None)}) == {}


async def test_a_turns_card_shows_only_its_newest_version_with_its_file_identity(store: ArtifactStore) -> None:
    """R3 within-Turn hiding: two versions written in one Turn deliver one card, which carries lineage and number."""
    from types import SimpleNamespace

    from bowhead_core.agents.lead_orchestrator import _collect_inline_artifacts

    first = await _put(store)
    second = await _put(store, target=first.id, content=b"<html>fixed</html>")
    canvas = SimpleNamespace(list_sections_appended_by_turn=lambda turn_id: _async([]))
    cards = await _collect_inline_artifacts(SimpleNamespace(canvas_store=canvas, artifact_store=store, parent_bus=None), "t1")
    files = [card for card in cards if card["kind"] == "file"]
    assert [(card["id"], card["lineage_id"], card["version_no"]) for card in files] == [(second.id, first.id, 2)]


async def _async(value):
    return value


async def test_a_run_saving_the_same_bytes_again_keeps_its_own_version(store: ArtifactStore) -> None:
    """Regression check M3: a second identical save in one run is not a user-visible duplicate version."""
    first = await _put(store)
    second = await _put(store, turn="t2", target=first.id, content=b"<html>v2</html>")
    again = await _put(store, turn="t2", target=second.id, content=b"<html>v2</html>")
    assert again.id == second.id
    changed = await _put(store, turn="t2", target=second.id, content=b"<html>v2 fixed</html>")
    assert (changed.version_of, changed.version_no) == (second.id, 3), "A genuine second edit is still a version"
    later = await _put(store, turn="t3", target=changed.id, content=b"<html>v2 fixed</html>")
    assert later.id != changed.id and later.version_no == 4, "Identical bytes from a later Turn stay a recorded version"


async def _chain(store: ArtifactStore, turns: list[str]) -> list:
    async with aiosqlite.connect(store.db_path) as db:
        await db.executemany("INSERT OR IGNORE INTO turns(id, session_id) VALUES (?, 's1')", [(turn,) for turn in turns])
        # Minimal shape of AccessControlStore's share grants, which live in the same database.
        await db.execute("CREATE TABLE IF NOT EXISTS resource_grants (resource_kind TEXT, resource_id TEXT, revoked_at TEXT)")
        await db.commit()
    versions = []
    for index, turn in enumerate(turns):
        target = versions[-1].id if versions else None
        versions.append(await _put(store, turn=turn, target=target, content=f"<html>{index}</html>".encode()))
    return versions


async def _live(store: ArtifactStore, ids) -> set[str]:
    async with aiosqlite.connect(store.db_path) as db:
        ph = ",".join("?" for _ in ids)
        rows = await (await db.execute(f"SELECT id FROM artifacts WHERE id IN ({ph}) AND deleted_at IS NULL", list(ids))).fetchall()
    return {row[0] for row in rows}


async def test_pruning_keeps_the_newest_three_versions_and_never_the_latest(store: ArtifactStore) -> None:
    """B4: older versions beyond three are soft-deleted; only KB-checked IDs are pruned."""
    v = await _chain(store, ["t1", "t2", "t3", "t4", "t5"])
    candidates = await store.prune_version_candidates([v[0].id])
    assert set(candidates) == {v[0].id, v[1].id}
    assert await store.prune_versions([v[0].id], kb_protected=set()) == 1, "Only KB-checked IDs are pruned"
    assert await store.prune_versions(candidates, kb_protected={v[1].id}) == 0, "A KB-referenced version stays"
    assert await _live(store, [x.id for x in v]) == {v[1].id, v[2].id, v[3].id, v[4].id}
    assert await store.prune_version_candidates() == [v[1].id], "The nightly run sees every file"


async def test_pruning_fails_closed_without_share_grants(store: ArtifactStore) -> None:
    """Share grants protect the version they name; when they cannot be read, nothing is pruned (Q6)."""
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP TABLE resource_grants")
        await db.commit()
    assert await store.prune_version_candidates([v[0].id]) == []
    assert await store.prune_versions([v[0].id], kb_protected=set()) == 0


async def test_same_run_drafts_are_pruned_before_counting_the_three(store: ArtifactStore) -> None:
    """Regression check M3: a version superseded within its own Turn never reached the user and cannot push out v1."""
    v = await _chain(store, ["t1", "t2", "t2"])
    assert await store.prune_version_candidates([v[0].id]) == [v[1].id]


async def test_a_file_started_in_the_turn_keeps_its_rewrites_as_versions(store: ArtifactStore) -> None:
    """Owner decision D2 (same-name-results-versions-2026-11-17): a helper's rewrites of a file it created in
    this Turn stay as earlier versions within the newest three; only older ones are pruned."""
    v = await _chain(store, ["t1", "t1", "t1", "t1"])
    assert await store.prune_version_candidates([v[0].id]) == [v[0].id]
    items, total, _ = await store.list_for_user(user_id="u")
    assert total == 1 and (items[0].id, items[0].version_no, items[0].version_count) == (v[3].id, 4, 4)
    assert [item.id for item in await store.list_session_deliverables(session_id="s1", user_id="u")] == [v[3].id]


async def test_a_dry_run_names_the_version_the_next_save_pushes_out(store: ArtifactStore) -> None:
    """pptx inline edit §13.7: the editor says which version goes before the person saves."""
    v = await _chain(store, ["t1", "t2"])
    assert await store.versions_pruned_by_next_version(v[1].id) == [], "Three versions after the save: none goes"
    v.append(await _put(store, turn="t3", target=v[1].id, content=b"<html>3</html>"))
    assert await store.versions_pruned_by_next_version(v[2].id) == [(v[0].id, 1)]
    assert await _live(store, [x.id for x in v]) == {x.id for x in v}, "A dry run changes nothing"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO resource_grants VALUES ('artifact', ?, NULL)", (v[0].id,))
        await db.commit()
    assert await store.versions_pruned_by_next_version(v[2].id) == [], "A shared version stays, so nothing is said"


async def test_the_first_turn_counts_even_after_its_first_version_was_pruned(store: ArtifactStore) -> None:
    """A file from t1 revised twice in t2: t2's superseded version is a draft, also once v1 is gone."""
    v = await _chain(store, ["t1", "t2", "t2"])
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at = '2026-11-17T00:00:00Z' WHERE id = ?", (v[0].id,))
        await db.commit()
    assert await store.prune_version_candidates([v[0].id]) == [v[1].id]


@pytest.mark.parametrize("protection", ["canvas_mention", "canvas_attachment", "document", "citation", "snapshot", "share"])
async def test_versions_in_use_are_never_pruned(store: ArtifactStore, protection: str) -> None:
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    old = v[0].id
    async with aiosqlite.connect(store.db_path) as db:
        if protection == "share":
            # A share grant resolves through the version it names; purging that row would end the share.
            await db.execute("INSERT INTO resource_grants VALUES ('artifact', ?, '2026-11-13T00:00:00Z')", (old,))
            await db.commit()
            assert await store.prune_version_candidates([old]) == [old], "A revoked share protects nothing"
            await db.execute("INSERT INTO resource_grants VALUES ('artifact', ?, NULL)", (old,))
        elif protection == "canvas_mention":
            await db.execute("CREATE TABLE IF NOT EXISTS canvas_sections (id TEXT, kind TEXT, payload TEXT)")
            await db.execute("INSERT INTO canvas_sections VALUES ('sec_1', 'narrative', ?)", (f'{{"artifact_id": "{old}"}}',))
        elif protection == "canvas_attachment":
            await db.execute("UPDATE artifacts SET canvas_id = 'cv_1' WHERE id = ?", (old,))
        elif protection == "document":
            await db.execute("CREATE TABLE IF NOT EXISTS document_artifact_refs (artifact_id TEXT, released_at TEXT)")
            await db.execute("INSERT INTO document_artifact_refs VALUES (?, NULL)", (old,))
        elif protection == "citation":
            await db.execute("CREATE TABLE IF NOT EXISTS turn_citations (artifact_id TEXT)")
            await db.execute("INSERT INTO turn_citations VALUES (?)", (old,))
        else:
            await db.execute("CREATE TABLE IF NOT EXISTS canvas_export_snapshots (id TEXT, expires_at TEXT)")
            await db.execute("CREATE TABLE IF NOT EXISTS canvas_export_snapshot_assets (snapshot_id TEXT, artifact_id TEXT)")
            await db.execute("INSERT INTO canvas_export_snapshots VALUES ('snap', '2099-01-01T00:00:00Z')")
            await db.execute("INSERT INTO canvas_export_snapshot_assets VALUES ('snap', ?)", (old,))
        await db.commit()
    assert await store.prune_version_candidates([old]) == []


async def test_a_run_record_mentioning_a_version_does_not_protect_it(store: ArtifactStore) -> None:
    """Regression check on 1.7.40: each revision's sandbox runs record the versions they read and wrote in code blocks.

    Counting those as uses kept every version, so pruning never ran on a revised HTML report.
    """
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    old = v[0].id
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS canvas_sections (id TEXT, kind TEXT, payload TEXT)")
        for kind in ("code_block", "sql_block"):
            await db.execute(
                "INSERT INTO canvas_sections VALUES (?, ?, ?)",
                (f"sec_{kind}", kind, f'{{"input_artifact_ids": ["{old}"], "output_artifact_ids": ["{v[1].id}"]}}'),
            )
        await db.commit()
    assert await store.prune_version_candidates([old]) == [old]
    assert await store.prune_versions([old], kb_protected=set()) == 1


@pytest.mark.parametrize("status", ["running", "awaiting_user", "clarification_pending", "approval_pending"])
async def test_pruning_waits_while_a_turn_of_the_session_is_running_or_paused(store: ArtifactStore, status: str) -> None:
    """116 dogfood N12: a Turn lists the session's files at its start and checks them again when it publishes.

    Pruning v1 while the Turn that wrote v4 was still running failed that Turn at its end, so a file
    waits until no Turn of its session is running or paused.
    """
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO turns(id, session_id, status) VALUES ('t_other', 's9', 'running')")
        await db.commit()
    assert not await store.version_prune_deferred(v[0].id), "A Turn of another session does not hold this file"
    candidates = await store.prune_version_candidates([v[0].id])
    assert candidates == [v[0].id]
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE turns SET status = ? WHERE id = 't4'", (status,))
        await db.commit()
    assert await store.version_prune_deferred(v[0].id)
    assert await store.prune_version_candidates([v[0].id]) == []
    assert await store.prune_versions(candidates, kb_protected=set()) == 0, "The write transaction rechecks the session"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE turns SET status = 'completed' WHERE id = 't4'")
        await db.commit()
    assert not await store.version_prune_deferred(v[0].id)
    assert await store.prune_versions(candidates, kb_protected=set()) == 1


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="seconds")


async def test_a_turn_left_paused_after_its_questions_ended_stops_holding_pruning(store: ArtifactStore) -> None:
    """On 116 (1.7.45), 252 sessions kept a Turn paused from months earlier.

    Their questions had expired or been dismissed, so those Turns can never continue, yet they
    kept every version of the session's files. A paused Turn holds pruning only while it can
    still continue: it paused within a day, a question or approval is still open (a scheduled
    task's question stays open until the task's next run), or one was answered within the hour.
    """
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    async with aiosqlite.connect(store.db_path) as db:
        await db.executescript(
            "CREATE TABLE clarification_requests (request_id TEXT PRIMARY KEY, turn_id TEXT, status TEXT, chosen_at TEXT);"
            "CREATE TABLE approvals (id TEXT PRIMARY KEY, turn_id TEXT, status TEXT, decided_at TEXT);"
        )
        await db.execute("UPDATE turns SET status = 'clarification_pending', ended_at = ? WHERE id = 't4'", (_ago(hours=2),))
        await db.commit()
    assert await store.version_prune_deferred(v[0].id), "Paused two hours ago: the question can still be answered"

    async def holds(question: tuple[str, str | None] | None = None, approval: tuple[str, str | None] | None = None) -> bool:
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE turns SET ended_at = ? WHERE id = 't4'", (_ago(days=40),))
            await db.execute("DELETE FROM clarification_requests")
            await db.execute("DELETE FROM approvals")
            if question is not None:
                await db.execute("INSERT INTO clarification_requests VALUES ('q1', 't4', ?, ?)", question)
            if approval is not None:
                await db.execute("INSERT INTO approvals VALUES ('a1', 't4', ?, ?)", approval)
            await db.commit()
        deferred = await store.version_prune_deferred(v[0].id)
        assert deferred == (await store.prune_version_candidates([v[0].id]) == []), "Both checks apply the same rule"
        return deferred

    assert not await holds(), "No question was recorded (a plan proposal continues in a new Turn)"
    assert not await holds(question=("expired", None))
    assert not await holds(question=("dismissed", None))
    assert not await holds(question=("resolved", _ago(hours=3))), "Answered long ago: that Turn already continued"
    assert await holds(question=("pending", None)), "A scheduled task's question stays open past a day"
    assert await holds(question=("resolved", _ago(minutes=5))), "Just answered: the Turn is starting again"
    assert await holds(approval=("pending", None))
    assert await holds(approval=("approved", _ago(minutes=5)))
    assert not await holds(approval=("approved", _ago(hours=3)))
    assert not await holds(approval=("expired", None))
    assert await store.prune_versions([v[0].id], kb_protected=set()) == 1


async def test_a_reference_added_after_listing_still_protects(store: ArtifactStore) -> None:
    """The write transaction recomputes and rechecks: a version referenced meanwhile is kept."""
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    candidates = await store.prune_version_candidates([v[0].id])
    assert candidates == [v[0].id]
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET canvas_id = 'cv_1' WHERE id = ?", (v[0].id,))
        await db.commit()
    assert await store.prune_versions(candidates, kb_protected=set()) == 0


async def test_restoring_a_deleted_file_does_not_bring_back_pruned_versions(store: ArtifactStore) -> None:
    v = await _chain(store, ["t1", "t2", "t3", "t4"])
    assert await store.prune_versions([v[0].id], kb_protected=set()) == 1
    trash, total, _ = await store.list_for_user(user_id="u", include_deleted=True)
    assert total == 0, "A pruned version of a live file is not in the trash"
    assert await store.soft_delete(v[3].id, user_id="u")
    trash, total, _ = await store.list_for_user(user_id="u", include_deleted=True)
    assert total == 1 and (trash[0].id, trash[0].version_count) == (v[3].id, 3), "The trash counts what restore brings back"
    assert await store.restore(v[3].id, user_id="u")
    assert await _live(store, [x.id for x in v]) == {v[1].id, v[2].id, v[3].id}
    assert await store.soft_delete(v[3].id, user_id="u")
    assert await store.force_restore([v[2].id]) == 3, "KB reconciliation restores the file it was deleted with, not pruned versions"
    assert await _live(store, [x.id for x in v]) == {v[1].id, v[2].id, v[3].id}


def test_a_declared_nested_file_or_folder_counts_by_its_saved_name() -> None:
    from types import SimpleNamespace

    from bowhead_core.delivered_files import _declared

    invocation = SimpleNamespace(
        arguments={"code": "...", "purpose": "deliverable", "deliverable_outputs": ["site/", "build/report.pdf", "game.html"]}
    )
    assert _declared(invocation, "site.zip") and _declared(invocation, "report.pdf") and _declared(invocation, "game.html")
    assert not _declared(invocation, "index.html")
    assert not _declared(SimpleNamespace(arguments={"purpose": "analysis", "deliverable_outputs": ["site/"]}), "site.zip")


async def test_a_copy_keeps_its_source_after_its_first_version_is_pruned(store: ArtifactStore) -> None:
    """pptx dogfood 2026-11-25: only three versions are kept, and the first one alone recorded the upload a copy was
    made from. Pruning it with the fourth save cut the link, so the upload no longer offered its copy. Every new
    version now carries the lineage's live source; a lineage without one, or with a deleted one, gets none."""
    upload = await _put(store, source="upload", kind="user_upload", created_by_turn=None, filename="deck.pptx")
    first = await _put(store, derived_from=upload.id)
    versions = [first]
    for turn in ("t2", "t3", "t2"):
        versions.append(await _put(store, turn=turn, target=versions[-1].id))
    assert [version.derived_from for version in versions] == [upload.id] * 4
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at=? WHERE id=?", (datetime.now(UTC).isoformat(), first.id))
        await db.commit()
    later = await _put(store, turn="t3", target=versions[-1].id)
    assert later.derived_from == upload.id, "The pruned first version still names the source"

    plain = [await _put(store, filename="plain.html")]
    plain.append(await _put(store, turn="t2", target=plain[0].id, filename="plain.html"))
    assert plain[1].derived_from is None

    assert await store.soft_delete(upload.id, user_id="u")
    after_delete = await _put(store, turn="t2", target=later.id)
    assert after_delete.derived_from is None, "A deleted source is not carried forward"
