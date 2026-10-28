from __future__ import annotations

import aiosqlite
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore


async def test_first_turn_facts_are_team_scoped_and_content_free(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    users = UserStore(db_path)
    await users.initialize()
    sessions = SessionStore(db_path)
    await sessions.initialize()

    team = await users.create_team(name="Acme", max_seats=5)
    owner = await users.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="Passw0rd!",
    )
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Operations",
        creator_user_id=owner.id,
    )
    session = await sessions.ensure_session(
        session_id=None,
        workspace_id=workspace_id,
        user_id=owner.id,
    )
    await sessions.record_turn(
        turn_id="t_readiness",
        session_id=session.id,
        user_text="sensitive prompt that must never be returned",
    )

    running = await sessions.first_turn_readiness_facts_for_team(team.id)
    assert running.has_any_normal_turn is True
    assert running.has_completed_turn is False
    assert running.has_successful_turn is False

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE turns SET status='completed', final_answer='  ' WHERE id=?",
            ("t_readiness",),
        )
        await db.commit()
    blank = await sessions.first_turn_readiness_facts_for_team(team.id)
    assert blank.has_completed_turn is True
    assert blank.has_successful_turn is False

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE turns SET final_answer='usable answer' WHERE id=?",
            ("t_readiness",),
        )
        await db.commit()
    successful = await sessions.first_turn_readiness_facts_for_team(team.id)
    assert successful.has_successful_turn is True

    await sessions.set_origin(session.id, "workflow_dryrun")
    dry_run = await sessions.first_turn_readiness_facts_for_team(team.id)
    assert dry_run.has_any_normal_turn is False
    assert dry_run.has_successful_turn is False

    await sessions.set_origin(session.id, "normal")
    await users.set_workspace_status(
        workspace_id=workspace_id,
        team_id=team.id,
        status="archived",
    )
    archived = await sessions.first_turn_readiness_facts_for_team(team.id)
    assert archived.has_any_normal_turn is False

    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_sessions_workspace_origin'",
        ) as cur,
    ):
        assert await cur.fetchone() is not None


async def test_workspace_readiness_queries_only_return_active_team_spaces(
    tmp_path,
) -> None:
    users = UserStore(tmp_path / "users.db")
    await users.initialize()
    team_a = await users.create_team(name="A", max_seats=5)
    team_b = await users.create_team(name="B", max_seats=5)
    owner_a = await users.create_user(
        team_id=team_a.id,
        email="a@example.com",
        password="Passw0rd!",
    )
    owner_b = await users.create_user(
        team_id=team_b.id,
        email="b@example.com",
        password="Passw0rd!",
    )
    active = await users.create_local_workspace(
        team_id=team_a.id,
        name="Active",
        creator_user_id=owner_a.id,
    )
    archived = await users.create_local_workspace(
        team_id=team_a.id,
        name="Archived",
        creator_user_id=owner_a.id,
    )
    await users.set_workspace_status(
        workspace_id=archived,
        team_id=team_a.id,
        status="archived",
    )
    await users.create_local_workspace(
        team_id=team_b.id,
        name="Other Team",
        creator_user_id=owner_b.id,
    )

    assert await users.count_active_workspaces(team_a.id) == 1
    assert await users.list_active_workspace_ids(team_a.id) == [active]


async def test_kb_readiness_aggregates_do_not_load_file_content(initialize_migrated_kb_files_database, tmp_path) -> None:
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    image_id = await files.create(
        artifact_id="art_image",
        filename="scan.png",
        mime_type="image/png",
        size_bytes=100,
        scope="space",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u_a",
    )
    await files.create(
        artifact_id="art_pdf",
        filename="scan.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        scope="space",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u_a",
    )
    async with aiosqlite.connect(tmp_path / "files.db") as db:
        await db.execute(
            "UPDATE kb_files SET use_llm_marker=1 WHERE team_id='team_a' AND mime_type='application/pdf'",
        )
        await db.commit()

    stats = await files.readiness_stats_for_team("team_a")
    assert stats.active_file_count == 2
    assert stats.image_file_count == 1
    assert stats.scanned_pdf_evidence_count == 1
    async with (
        aiosqlite.connect(tmp_path / "files.db") as db,
        db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM kb_files WHERE team_id = ? AND soft_deleted_at IS NULL",
            ("team_a",),
        ) as cur,
    ):
        plan = " ".join(str(value) for row in await cur.fetchall() for value in row)
    assert "idx_kb_files_team" in plan

    await files.soft_delete(image_id)
    stats = await files.readiness_stats_for_team("team_a")
    assert stats.active_file_count == 1
    assert stats.image_file_count == 0

    doc_types = KbDocTypeStore(tmp_path / "doc_types.db")
    await doc_types.initialize()
    await doc_types.create(
        workspace_id="ws_a",
        name="Line items",
        created_by="u_a",
        cardinality="many",
        auto_extract_enabled=True,
    )
    await doc_types.create(
        workspace_id="ws_b",
        name="Single record",
        created_by="u_b",
        cardinality="one",
        auto_extract_enabled=True,
    )
    assert await doc_types.has_many_auto_extract_for_workspaces(["ws_a"]) is True
    assert await doc_types.has_many_auto_extract_for_workspaces(["ws_b"]) is False
    assert await doc_types.has_many_auto_extract_for_workspaces([]) is False
