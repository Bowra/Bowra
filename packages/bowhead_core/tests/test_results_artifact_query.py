from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend


async def _store(tmp_path: Path) -> ArtifactStore:
    store = ArtifactStore(
        db_path=str(tmp_path / "bowhead.db"),
        backend=LocalBackend(root_dir=tmp_path / "files"),
    )
    await store.initialize()
    async with aiosqlite.connect(store.db_path) as db:
        await db.executescript("""
            CREATE TABLE turns (id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE turn_attachments (
                turn_id TEXT, artifact_id TEXT,
                PRIMARY KEY (turn_id, artifact_id)
            );
            CREATE TABLE session_attachments (
                session_id TEXT, artifact_id TEXT, pinned_at TEXT, detached_at TEXT,
                PRIMARY KEY (session_id, artifact_id)
            );
            CREATE TABLE canvases (id TEXT PRIMARY KEY, session_id TEXT);
        """)
        rows = [
            (
                "art_deliverable",
                "交付报告.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "user_1",
                "2026-08-24T12:05:00+00:00",
                "deliverable",
                "trusted",
            ),
            ("art_data", "季度_100%.csv", "text/csv", "user_1", "2026-08-24T12:04:00+00:00", "tool_data", "trusted"),
            (
                "art_upload",
                "输入.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "user_1",
                "2026-08-24T12:03:00+00:00",
                "user_upload",
                "trusted",
            ),
            ("art_internal", "manifest.json", "application/json", "user_1", "2026-08-24T12:02:00+00:00", "tool_data", "trusted"),
            ("art_other", "other.csv", "text/csv", "user_2", "2026-08-24T12:01:00+00:00", "tool_data", "trusted"),
            ("art_pending", "pending.csv", "text/csv", "user_1", "2026-08-24T12:06:00+00:00", "tool_data", "trusted"),
        ]
        await db.executemany(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,kind,trust,producer_kind) "
            "VALUES (?,?,?,1,'fixture','fixture/path',?,?,?,?,NULL)",
            rows,
        )
        await db.execute(
            "UPDATE artifacts SET producer_kind='canvas_export_manifest_b1a_v1' WHERE id='art_internal'",
        )
        await db.execute(
            "UPDATE artifacts SET status='pending_upload' WHERE id='art_pending'",
        )
        await db.executemany(
            "INSERT INTO session_attachments(session_id,artifact_id,pinned_at,detached_at) VALUES ('session_1',?,NULL,NULL)",
            [(row[0],) for row in rows],
        )
        await db.commit()
    return store


@pytest.mark.unit
@pytest.mark.asyncio
async def test_results_artifact_query_counts_filters_and_keysets(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    snapshot = await store.results_high_water(session_id="session_1", user_id="user_1")

    first = await store.query_results_for_session(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
        normalized_query="",
        limit=2,
    )
    second = await store.query_results_for_session(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
        normalized_query="",
        cursor_sort_tuple=first.entries[-1].sort_tuple,
        limit=2,
    )
    previous = await store.query_results_for_session(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
        normalized_query="",
        cursor_sort_tuple=second.entries[0].sort_tuple,
        direction="previous",
        limit=2,
    )

    assert snapshot.high_water_artifact_id == "art_deliverable"
    assert first.authorized_total == 3
    assert first.matched_total == 3
    assert first.group_counts.deliverables == 1
    assert first.group_counts.evidence == 2
    assert first.group_counts.data == 1
    assert first.group_counts.code == 0
    assert first.group_counts.input == 1
    assert [entry.artifact.id for entry in first.entries] == ["art_deliverable", "art_data"]
    assert first.has_more_in_direction
    assert [entry.artifact.id for entry in second.entries] == ["art_upload"]
    assert [entry.artifact.id for entry in previous.entries] == ["art_deliverable", "art_data"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_results_artifact_query_freezes_snapshot_and_searches_literal_symbols(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    snapshot = await store.results_high_water(session_id="session_1", user_id="user_1")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,kind,trust) "
            "VALUES ('art_new','new.csv','text/csv',1,'fixture','fixture/new','user_1','2026-08-24T12:06:00+00:00','tool_data','trusted')",
        )
        await db.execute(
            "INSERT INTO session_attachments(session_id,artifact_id,pinned_at,detached_at) VALUES ('session_1','art_new',NULL,NULL)",
        )
        await db.commit()

    frozen = await store.query_results_for_session(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
        normalized_query="100%",
        limit=10,
    )
    inputs = await store.query_results_for_session(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
        normalized_query="",
        secondary="input",
        limit=10,
    )
    new_count = await store.count_new_results_for_session(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
        normalized_query="",
    )

    assert frozen.authorized_total == 3
    assert [entry.artifact.id for entry in frozen.entries] == ["art_data"]
    assert frozen.group_counts.data == 1
    assert frozen.group_counts.deliverables == 0
    assert frozen.group_counts.input == 0
    assert [entry.artifact.id for entry in inputs.entries] == ["art_upload"]
    # Facet counts describe the query/lens universe, not the currently
    # selected primary/secondary filter.
    assert inputs.group_counts.deliverables == 1
    assert inputs.group_counts.evidence == 2
    assert inputs.group_counts.data == 1
    assert inputs.group_counts.input == 1
    assert new_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_results_membership_digest_rejects_old_artifact_attached_after_snapshot(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    snapshot = await store.results_high_water(session_id="session_1", user_id="user_1")
    before = await store.results_snapshot_membership_digest(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,kind,trust) "
            "VALUES ('art_old_late','late.csv','text/csv',1,'fixture','fixture/late','user_1',"
            "'2026-08-24T11:00:00+00:00','tool_data','trusted')",
        )
        await db.execute(
            "INSERT INTO session_attachments(session_id,artifact_id,pinned_at,detached_at) "
            "VALUES ('session_1','art_old_late','2026-08-24T13:00:00+00:00',NULL)",
        )
        await db.commit()
    after = await store.results_snapshot_membership_digest(
        session_id="session_1",
        user_id="user_1",
        snapshot=snapshot,
    )

    assert before != after


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_closure_scope_precedes_count_search_direct_lens_and_exact_key(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    closure = (
        "art_data",
        "art_other",
        "art_internal",
        "art_pending",
    )
    snapshot = await store.results_high_water(
        session_id="session_1",
        user_id="viewer_not_artifact_owner",
        authorized_artifact_ids=closure,
    )
    page = await store.query_results_for_session(
        session_id="session_1",
        user_id="viewer_not_artifact_owner",
        snapshot=snapshot,
        normalized_query="",
        authorized_artifact_ids=closure,
        direct_report_artifact_ids=("art_other",),
        limit=10,
    )
    direct = await store.query_results_for_session(
        session_id="session_1",
        user_id="viewer_not_artifact_owner",
        snapshot=snapshot,
        normalized_query="",
        authorized_artifact_ids=closure,
        direct_report_artifact_ids=("art_other",),
        report_direct_only=True,
        limit=10,
    )
    exact_inside = await store.get_result_artifact_for_scope(
        artifact_id="art_other",
        session_id="session_1",
        user_id="viewer_not_artifact_owner",
        authorized_artifact_ids=closure,
    )
    exact_outside = await store.get_result_artifact_for_scope(
        artifact_id="art_upload",
        session_id="session_1",
        user_id="viewer_not_artifact_owner",
        authorized_artifact_ids=closure,
    )

    assert snapshot.high_water_artifact_id == "art_data"
    assert page.authorized_total == 2
    assert [entry.artifact.id for entry in page.entries] == [
        "art_other",
        "art_data",
    ]
    assert [entry.artifact.id for entry in direct.entries] == ["art_other"]
    assert exact_inside is not None and exact_inside.created_by == "user_2"
    assert exact_outside is None
