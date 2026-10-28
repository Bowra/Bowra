from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aiosqlite
import pytest
from bowhead_core._db import bootstrap_shared_db, open_shared_ctx
from bowhead_core.memory_note_anchors import MemoryNoteAnchorsStore
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.memory_pack_snapshots import MemoryPackSnapshotsStore
from bowhead_core.memory_privacy import (
    MemoryDestructiveOperationStore,
    MemoryIdempotencyConflictError,
    MemoryMutationContext,
    MemoryWriteBlockedError,
    PrivacyDeletionLedger,
    PrivacyDeletionNotReadyError,
    ensure_memory_write_allowed,
)
from bowhead_core.notification_store import NotificationStore
from bowhead_core.user_memory import UserMemoryStore
from bowhead_core.user_settings_store import UserSettingsStore

from bowhead_memory import MemoryCandidateStore


async def _privacy_store(tmp_path):
    db_path = tmp_path / "shared.db"
    ledger_path = tmp_path / "privacy-ledger.db"
    await bootstrap_shared_db(str(db_path))
    memory = UserMemoryStore(db_path)
    await memory.initialize()
    notes = MemoryNotesStore(db_path)
    await notes.initialize()
    anchors = MemoryNoteAnchorsStore(db_path)
    await anchors.initialize()
    candidates = MemoryCandidateStore(db_path=str(db_path))
    await candidates.initialize()
    packs = MemoryPackSnapshotsStore(db_path)
    await packs.initialize()
    settings = UserSettingsStore(db_path)
    await settings.init()
    ledger = PrivacyDeletionLedger(
        ledger_path,
        hmac_key="test-ledger-secret",
    )
    await ledger.initialize()
    privacy = MemoryDestructiveOperationStore(
        db_path,
        ledger=ledger,
        fingerprint_key="test-fingerprint-secret",
        require_secure_delete_baseline=False,
    )
    await privacy.initialize()
    return {
        "db_path": db_path,
        "ledger_path": ledger_path,
        "memory": memory,
        "notes": notes,
        "anchors": anchors,
        "candidates": candidates,
        "packs": packs,
        "settings": settings,
        "privacy": privacy,
    }


@pytest.mark.asyncio
async def test_full_delete_scrubs_all_personal_memory_and_commits_receipt(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_delete"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="report_style",
        value="先结论后依据",
    )
    await stores["memory"].upsert_operational_memory(
        user_id=user_id,
        kind="satisfaction",
        key="turn_secret",
        value={"comment": "private comment"},
        source="turn_feedback",
    )
    await stores["notes"].upsert_content(
        user_id=user_id,
        content="我负责零售业务",
    )
    await stores["anchors"].insert_or_revive(
        user_id=user_id,
        bullet_text="我负责零售业务",
        canvas_id="c_1",
        section_id="sec_1",
        verifier_result_hash="secret-hash",
    )
    candidate = await stores["candidates"].enqueue(
        user_id=user_id,
        kind="preference",
        key="chart_style",
        value="折线图",
        reason="来自旧对话",
        source_turn_id="t_secret",
    )
    await stores["candidates"].set_decision(
        cid=candidate.id,
        status="rejected",
        decided_by=user_id,
    )
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            """
            INSERT INTO memory_mutation_idempotency(
                user_id, idempotency_key, request_fingerprint,
                result_json, created_at
            )
            VALUES (?, ?, 'reset_v3', '{}', datetime('now'))
            """,
            (user_id, str(uuid4())),
        )
        await db.commit()
    await stores["packs"].save(
        turn_id="t_pack",
        user_id=user_id,
        team_id=None,
        workspace_id=None,
        content="完整的 Memory Pack 私密正文",
        sections_breakdown={"preferences": 1},
        pack_version="v1",
    )

    operation = await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=str(uuid4()),
    )

    assert operation["status"] == "completed"
    assert operation["phase"] == "finalize"
    assert operation["logical_scrub_completed"] is True
    assert operation["physical_purge_completed"] is True
    assert operation["deletion_receipt_committed"] is True
    assert operation["dreaming_paused"] is True
    async with aiosqlite.connect(stores["db_path"]) as db:
        for table, where in (
            ("memory_entries", "subject_id='usr_delete'"),
            ("memory_notes", "subject_id='usr_delete'"),
            ("memory_note_anchors", "user_id='usr_delete'"),
            ("memory_pack_snapshots", "user_id='usr_delete'"),
            ("memory_candidate_suppressions", "user_id='usr_delete'"),
            ("memory_mutation_idempotency", "user_id='usr_delete'"),
        ):
            row = await db.execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE {where}",
            )
            assert row[0][0] == 0
        candidate = (
            await db.execute_fetchall(
                """
                SELECT status, key, value_json, reason, source_turn_id,
                       decision_kind
                FROM memory_candidates
                WHERE user_id=?
                """,
                (user_id,),
            )
        )[0]
        assert candidate == (
            "rejected",
            "",
            "null",
            None,
            None,
            "permanent_delete",
        )
        paused = await db.execute_fetchall(
            """
            SELECT value FROM user_settings
            WHERE user_id=? AND key='memory_dreaming_paused'
            """,
            (user_id,),
        )
        assert paused[0][0] == "true"
        state = (
            await db.execute_fetchall(
                """
                SELECT current_epoch, barrier_status, last_completed_delete_at
                FROM memory_privacy_state
                WHERE user_id=?
                """,
                (user_id,),
            )
        )[0]
        assert state[0] == 1
        assert state[1] == "idle"
        assert state[2] is not None
    async with aiosqlite.connect(stores["ledger_path"]) as ledger_db:
        receipts = await ledger_db.execute_fetchall(
            """
            SELECT receipt_id, status, delete_scope, subject_hmac
            FROM privacy_deletion_receipts
            """,
        )
    assert len(receipts) == 1
    assert receipts[0][0].startswith("pdr_")
    assert receipts[0][1:3] == ("committed", "full_user")
    assert user_id not in json.dumps(receipts)


@pytest.mark.asyncio
async def test_full_delete_redacts_only_the_users_memory_notifications(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_notifications"
    notifications = NotificationStore(str(stores["db_path"]))
    await notifications.initialize()
    memory_item = await notifications.upsert_item(
        team_id="team_1",
        category="memory",
        type="memory.candidate",
        priority="p2",
        title="有新的记忆建议待确认",
        body="私密候选预览",
        subject_type="memory_candidates",
        subject_id=user_id,
        actor_user_id=None,
        action_url="/memory",
        actions=[],
        metadata={"createdCount": 1},
        dedupe_key=f"memory.candidate:{user_id}",
    )
    unrelated_item = await notifications.upsert_item(
        team_id="team_1",
        category="system",
        type="scheduled.failed",
        priority="p1",
        title="任务失败",
        body="unrelated body",
        subject_type="scheduled_job",
        subject_id="sj_keep",
        actor_user_id=None,
        action_url="/scheduled",
        actions=[],
        metadata={"keep": True},
        dedupe_key="scheduled.failed:sj_keep",
    )
    await notifications.add_recipients(
        memory_item.id,
        [(user_id, "owner")],
    )
    await notifications.add_recipients(
        unrelated_item.id,
        [(user_id, "owner")],
    )

    await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=str(uuid4()),
    )

    async with aiosqlite.connect(stores["db_path"]) as db:
        rows = await db.execute_fetchall(
            """
            SELECT id, state, body, metadata_json, resolved_reason
            FROM notification_items
            ORDER BY id
            """,
        )
    by_id = {row[0]: row[1:] for row in rows}
    assert by_id[memory_item.id] == (
        "resolved",
        "Memory suggestions removed",
        "{}",
        "memory_permanent_delete",
    )
    assert by_id[unrelated_item.id] == (
        "open",
        "unrelated body",
        '{"keep": true}',
        None,
    )


@pytest.mark.asyncio
async def test_full_delete_is_idempotent_and_rejects_key_reuse(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_delete"
    key = str(uuid4())

    first = await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=key,
    )
    second = await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=key,
    )

    assert second["operation_id"] == first["operation_id"]
    async with aiosqlite.connect(stores["ledger_path"]) as db:
        count = (
            await db.execute_fetchall(
                "SELECT COUNT(*) FROM privacy_deletion_receipts",
            )
        )[0][0]
    assert count == 1

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            """
            UPDATE memory_destructive_operations
            SET request_fingerprint='different'
            WHERE id=?
            """,
            (first["operation_id"],),
        )
        await db.commit()
    with pytest.raises(MemoryIdempotencyConflictError):
        await stores["privacy"].permanent_delete_all(
            user_id=user_id,
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_same_idempotency_key_executes_one_delete_at_a_time(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_concurrent"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="private",
        value="delete once",
    )
    entered_purge = asyncio.Event()
    release_purge = asyncio.Event()
    physical_purge_calls = 0
    original_physical_purge = stores["privacy"]._physical_purge

    async def controlled_physical_purge() -> None:
        nonlocal physical_purge_calls
        physical_purge_calls += 1
        entered_purge.set()
        await release_purge.wait()
        await original_physical_purge()

    monkeypatch.setattr(
        stores["privacy"],
        "_physical_purge",
        controlled_physical_purge,
    )
    key = str(uuid4())
    first_task = asyncio.create_task(
        stores["privacy"].permanent_delete_all(
            user_id=user_id,
            idempotency_key=key,
        ),
    )
    await entered_purge.wait()
    second_task = asyncio.create_task(
        stores["privacy"].permanent_delete_all(
            user_id=user_id,
            idempotency_key=key,
        ),
    )
    await asyncio.sleep(0)
    release_purge.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert physical_purge_calls == 1
    assert first["operation_id"] == second["operation_id"]
    assert first["status"] == second["status"] == "completed"


@pytest.mark.asyncio
async def test_write_guard_rejects_pre_boundary_and_active_barrier(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_delete"
    before = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat(timespec="microseconds")
    key = str(uuid4())
    await stores["memory"].upsert_operational_memory(
        user_id=user_id,
        kind="datarc_binding",
        key="member_uid",
        value="private-member",
        source="test",
    )
    existing_candidate = await stores["candidates"].enqueue(
        user_id=user_id,
        kind="preference",
        key="private",
        value="private",
        reason=None,
        source_turn_id=None,
    )
    await stores["packs"].save(
        turn_id="t_private",
        user_id=user_id,
        team_id="team_private",
        workspace_id=None,
        content="private",
        sections_breakdown={},
        pack_version="v1",
    )
    original_prepare = stores["privacy"]._prepare_full_delete
    prepared = await original_prepare(
        user_id=user_id,
        idempotency_key=key,
    )

    assert await stores["privacy"].is_full_delete_active(user_id=user_id)
    assert await stores["memory"].list_for_user(user_id) == []
    assert await stores["notes"].get(user_id=user_id) is None
    assert await stores["candidates"].list_for_user(user_id=user_id) == []
    assert await stores["candidates"].get(existing_candidate.id) is None
    assert await stores["memory"].get_datarc_member_uid(user_id) is None
    assert (
        await stores["packs"].get_by_turn_for_user(
            turn_id="t_private",
            user_id=user_id,
        )
        is None
    )
    assert (
        await stores["packs"].get_by_turn_for_admin(
            turn_id="t_private",
            admin_team_id="team_private",
        )
        is None
    )
    with pytest.raises(MemoryWriteBlockedError):
        await stores["memory"].upsert_product_memory(
            user_id=user_id,
            kind="preference",
            key="blocked",
            value="blocked",
        )
    with pytest.raises(MemoryWriteBlockedError):
        await stores["notes"].upsert_content(
            user_id=user_id,
            content="blocked",
        )
    with pytest.raises(MemoryWriteBlockedError):
        await stores["anchors"].insert_or_revive(
            user_id=user_id,
            bullet_text="blocked",
            canvas_id="c_blocked",
            section_id="s_blocked",
            verifier_result_hash="blocked",
        )
    with pytest.raises(MemoryWriteBlockedError):
        await stores["candidates"].enqueue(
            user_id=user_id,
            kind="preference",
            key="blocked",
            value="blocked",
            reason=None,
            source_turn_id=None,
        )
    with pytest.raises(MemoryWriteBlockedError):
        await stores["packs"].save(
            turn_id="t_blocked",
            user_id=user_id,
            team_id=None,
            workspace_id=None,
            content="blocked",
            sections_breakdown={},
            pack_version="v1",
        )
    with pytest.raises(MemoryWriteBlockedError):
        await stores["settings"].set(
            user_id,
            "memory_dreaming_paused",
            "false",
        )

    async with aiosqlite.connect(stores["db_path"]) as db:
        with pytest.raises(MemoryWriteBlockedError):
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
                context=MemoryMutationContext(started_at=before),
            )

    await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=key,
    )
    assert not await stores["privacy"].is_full_delete_active(
        user_id=user_id,
    )
    async with aiosqlite.connect(stores["db_path"]) as db:
        with pytest.raises(MemoryWriteBlockedError):
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
                context=MemoryMutationContext(
                    started_at=before,
                    privacy_epoch=int(prepared["privacy_epoch"]) - 1,
                ),
            )


@pytest.mark.asyncio
async def test_single_permanent_delete_scrubs_only_confirmed_trash_target(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_single"
    target = await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="target",
        value="delete me",
    )
    survivor = await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="survivor",
        value="keep me",
    )
    assert await stores["memory"].soft_delete(
        user_id=user_id,
        entry_id=target.id,
        expected_version=target.version,
    )

    operation = await stores["privacy"].permanent_delete_item(
        user_id=user_id,
        item_id=target.id,
        idempotency_key=str(uuid4()),
    )

    assert operation["status"] == "completed"
    current = await stores["memory"].list_product_for_user(
        user_id,
        include_trashed=True,
    )
    assert [entry.id for entry in current] == [survivor.id]
    async with aiosqlite.connect(stores["db_path"]) as db:
        tombstone = await db.execute_fetchall(
            """
            SELECT deleted_through_version
            FROM memory_resource_deletion_tombstones
            WHERE user_id=? AND resource_type='memory_entry'
              AND resource_id=?
            """,
            (user_id, target.id),
        )
    assert tombstone == [(target.version + 1,)]


@pytest.mark.asyncio
async def test_permanent_delete_old_reset_never_deletes_newer_memory(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_reset"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="report_style",
        value="old",
    )
    reset = await stores["memory"].reset_v3(user_id=user_id)
    recreated = await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="report_style",
        value="new",
    )

    operation = await stores["privacy"].permanent_delete_reset(
        user_id=user_id,
        snapshot_id=reset.snapshot_id,
        idempotency_key=str(uuid4()),
    )

    assert operation["status"] == "completed"
    current = await stores["memory"].list_product_for_user(user_id)
    assert [(entry.id, entry.parsed_value()) for entry in current] == [
        (recreated.id, "new"),
    ]
    assert (
        await stores["memory"].get_reset_v3_detail(
            snapshot_id=reset.snapshot_id,
            user_id=user_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_external_ledger_replay_scrubs_restored_full_user_backup(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_restored"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="private",
        value="must not return",
    )
    operation = await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=str(uuid4()),
    )

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            """
            INSERT INTO memory_entries(
                id, subject_type, subject_id, kind, key, value,
                locked, source, created_at, updated_at, version,
                memory_role, value_schema, memory_policy_version
            )
            VALUES (
                'mem_restored', 'user', ?, 'preference', 'private',
                '"must not return"', 0, 'user_explicit', ?, ?, 1,
                'product_memory', 'text', 1
            )
            """,
            (
                user_id,
                operation["cutoff_at"],
                operation["cutoff_at"],
            ),
        )
        await db.execute(
            "DELETE FROM memory_applied_deletion_receipts",
        )
        await db.execute(
            """
            DELETE FROM memory_migration_state
            WHERE key='privacy_ledger_replay_complete'
            """,
        )
        await db.commit()

    replay = await stores["privacy"].replay_committed_deletions()

    assert replay["receipts_seen"] == 1
    assert replay["receipts_applied"] == 1
    assert replay["rows_scrubbed"] >= 1
    assert await stores["memory"].list_for_user(user_id) == []
    assert (
        await stores["settings"].get(
            user_id,
            "memory_dreaming_paused",
        )
        == "true"
    )


@pytest.mark.asyncio
async def test_full_delete_and_replay_remove_per_turn_memory_receipt_rows(
    tmp_path,
) -> None:
    from importlib import import_module

    receipt_ddl = import_module("bowhead_core.migrations.versions.0153_memory_turn_requests").DDL[0]
    stores = await _privacy_store(tmp_path)
    user_id = "usr_receipts"
    insert = (
        "INSERT INTO memory_turn_requests(user_id,turn_id,source,outcome,entry_id,created_at) "
        "VALUES (?, ?, 'remember_tool', 'remembered', 'mem_x', '2026-10-06')"
    )
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(receipt_ddl)
        await db.execute(insert, (user_id, "t_1"))
        await db.execute(insert, ("usr_other", "t_2"))
        await db.commit()

    await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=str(uuid4()),
    )

    async def rows() -> list[tuple[str, str]]:
        async with (
            aiosqlite.connect(stores["db_path"]) as db,
            db.execute("SELECT user_id, turn_id FROM memory_turn_requests ORDER BY user_id") as cursor,
        ):
            return [tuple(r) for r in await cursor.fetchall()]

    assert await rows() == [("usr_other", "t_2")]

    # A restored backup row is scrubbed again by ledger replay.
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(insert, (user_id, "t_1"))
        await db.execute("DELETE FROM memory_applied_deletion_receipts")
        await db.execute("DELETE FROM memory_migration_state WHERE key='privacy_ledger_replay_complete'")
        await db.commit()
    replay = await stores["privacy"].replay_committed_deletions()
    assert replay["receipts_applied"] == 1
    assert await rows() == [("usr_other", "t_2")]


@pytest.mark.asyncio
async def test_startup_recovery_finishes_interrupted_operation(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_interrupted"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="private",
        value="must be removed",
    )
    await stores["privacy"]._prepare_full_delete(
        user_id=user_id,
        idempotency_key=str(uuid4()),
    )

    result = await stores["privacy"].resume_incomplete_operations()

    assert result == {
        "operations_seen": 1,
        "operations_completed": 1,
    }
    assert await stores["memory"].list_for_user(user_id) == []
    assert not await stores["privacy"].is_full_delete_active(
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_replay_does_not_delete_post_delete_memory_when_marker_exists(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_recreated"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="old",
        value="delete",
    )
    await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=str(uuid4()),
    )
    recreated = await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="new",
        value="keep",
    )

    replay = await stores["privacy"].replay_committed_deletions()

    assert replay["receipts_seen"] == 1
    assert replay["receipts_applied"] == 0
    assert replay["rows_scrubbed"] == 0
    current = await stores["memory"].list_product_for_user(user_id)
    assert [item.id for item in current] == [recreated.id]


@pytest.mark.asyncio
async def test_delete_waits_for_export_read_boundary_before_success(
    tmp_path,
) -> None:
    stores = await _privacy_store(tmp_path)
    user_id = "usr_exporting"
    await stores["memory"].upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key="private",
        value="streaming",
    )
    key = str(uuid4())
    async with open_shared_ctx(stores["db_path"]) as export_guard:
        await export_guard.execute("BEGIN")
        await export_guard.execute_fetchall(
            """
            SELECT *
            FROM memory_entries
            WHERE subject_type='user' AND subject_id=?
            """,
            (user_id,),
        )
        with pytest.raises(PrivacyDeletionNotReadyError):
            await stores["privacy"].permanent_delete_all(
                user_id=user_id,
                idempotency_key=key,
            )

    completed = await stores["privacy"].permanent_delete_all(
        user_id=user_id,
        idempotency_key=key,
    )

    assert completed["status"] == "completed"
    assert completed["physical_purge_completed"] is True


@pytest.mark.asyncio
async def test_privacy_ledger_archives_hmac_key_mismatch_and_reinitializes(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger_path = tmp_path / "privacy-ledger.db"
    original = PrivacyDeletionLedger(
        ledger_path,
        hmac_key="original-key",
    )
    await original.initialize()
    mismatched = PrivacyDeletionLedger(
        ledger_path,
        hmac_key="different-key",
    )

    with caplog.at_level(logging.WARNING, logger="bowhead_core.memory_privacy"):
        await mismatched.initialize()

    archived = list(tmp_path.glob("privacy-ledger.db.key-mismatch-*"))
    assert len(archived) == 1
    assert "privacy_ledger.key_mismatch_archived" in caplog.text
    assert await mismatched.healthcheck() is True
