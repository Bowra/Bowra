"""Route-level followups release the source snapshot before domain writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
from bowhead_core.events import get_event_registry
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_gateway.clarification_invalidation import publish_pending_clarification_invalidations
from bowhead_gateway.resource_impact_http import pause_affected_schedules
from bowhead_gateway.routes.files_presign import _atomic_insert_pending
from test_sqlite_domain_stores import domain_directory as domain_directory


@pytest.mark.asyncio
async def test_failed_domain_dispatch_recovers_its_canonical_dismissal(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    sessions = SessionStore(core)
    turn_id = "t_failed_domain_dismissal"
    await sessions.record_turn(turn_id=turn_id, session_id="s_1", user_text="Choose a measure")
    assert await sessions.claim_chat_spawn_dispatch_for_worker(turn_id=turn_id, owner_id="lost-owner") == "claimed"
    clarifications = ClarificationStore(core)
    request = await clarifications.put(
        turn_id=turn_id,
        user_id="u_1",
        workspace_id="ws_1",
        token="measure",
        rationale="Choose",
        candidates=[ClarificationCandidate(value="a", label="A")],
    )
    await clarifications.invalidate_one(request.request_id, target_status="expired", reason="clarification_ttl_expired", actor="system")
    assert await sessions.mark_failed(turn_id=turn_id, error="Worker interrupted", error_kind="interrupted")
    async with open_shared_ctx(domain_database_path(core, "execution")) as db:
        await db.execute("UPDATE chat_turn_spawn_dispatch SET state='failed' WHERE turn_id=?", (turn_id,))
        await db.commit()
    logs = TurnLogStore(core)
    assert await logs.record(turn_id=turn_id, event_type="partial_answer", payload={"text": "stale owner"}) is None
    app = SimpleNamespace(state=SimpleNamespace(clarification_store=clarifications, turn_log_store=logs))
    registry = get_event_registry()
    previous_sink = registry._default_log_sink
    sink = OrderedTurnLogSink(logs)
    registry.set_log_sink(sink)
    try:
        assert await publish_pending_clarification_invalidations(app) == 1
        assert await publish_pending_clarification_invalidations(app) == 0
        await sink.drain()
    finally:
        registry.set_log_sink(previous_sink)
    events = await logs.list_events(turn_id)
    assert [(event.id, event.event_type, event.payload["status"]) for event in events] == [
        (f"evt_clarification_invalidation_{request.request_id}", "clarification.dismissed", "expired")
    ]
    assert await clarifications.count_pending_invalidations() == 0
    async with (
        open_shared_ctx(domain_database_path(core, "execution")) as db,
        db.execute("SELECT t.status,d.state FROM turns t JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id WHERE t.id=?", (turn_id,)) as cursor,
    ):
        assert await cursor.fetchone() == ("failed", "failed")


@pytest.mark.asyncio
async def test_schedule_followup_releases_the_completed_source_snapshot(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    job = await ScheduledJobStore(core).create(user_id="u_1", workspace_id="ws_1", name="Gateway followup", cron="0 9 * * *", query="Inspect totals")
    async with open_shared_ctx(core) as db:
        await db.execute("BEGIN")
        cursor = await db.execute("SELECT id FROM sessions UNION ALL SELECT id FROM sessions")
        assert await cursor.fetchone() is not None
        await db.commit()
        await pause_affected_schedules(db, [job.id])
    async with (
        connect(domain_database_path(core, "execution")) as db,
        db.execute("SELECT status,pause_reason FROM main.scheduled_jobs WHERE id=?", (job.id,)) as cursor,
    ):
        assert await cursor.fetchone() == ("paused", "resource_dependency_changed")


@pytest.mark.asyncio
async def test_pending_presign_resolves_content_and_keeps_binding_local(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    now = datetime.now(UTC)
    assert await _atomic_insert_pending(
        str(core),
        aid="art_domain_pending",
        filename="pending.txt",
        mime="text/plain",
        size_bytes=7,
        backend_path="synthetic/pending",
        user_id="u_1",
        workspace_id="ws_1",
        session_id="s_1",
        created_at_iso=now.isoformat(),
        expires_at_iso=(now + timedelta(hours=1)).isoformat(),
    )
    async with (
        connect(domain_database_path(core, "content")) as db,
        db.execute(
            "SELECT a.status,b.session_id FROM main.artifacts a JOIN main.artifact_upload_bindings b ON b.artifact_id=a.id WHERE a.id=?",
            ("art_domain_pending",),
        ) as cursor,
    ):
        assert await cursor.fetchone() == ("pending_upload", "s_1")


@pytest.mark.asyncio
async def test_buffered_diagnostics_cannot_reappear_after_account_retirement(domain_directory: Path) -> None:
    import hashlib
    import sqlite3

    from bowhead_core.account_deletion import request_account_deletion
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.llm_request_record import prepare_request_records, write_prepared_request_records
    from bowhead_core.session_store import SessionStore
    from bowhead_core.user_store import UserStore
    from bowhead_router.request_record import RequestRecordEntry
    from bowhead_router.usage import UsageContext
    from cryptography.fernet import Fernet

    core = domain_directory / "bowhead.db"
    async with connect(core) as db:
        await db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES ('u_late','team_1','late@example.test','t')")
        await db.execute("INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES ('tm_late','u_late','team_1','member','t')")
        await db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES ('ws_1','u_late','member','t')")
        await db.commit()
    sessions = SessionStore(core)
    await sessions.ensure_session(session_id="s_late", workspace_id="ws_1", user_id="u_late")
    await sessions.record_turn(turn_id="t_late", session_id="s_late", user_text="Private request")
    body = b'{"model":"m","messages":[]}'
    entry = RequestRecordEntry(
        usage=UsageContext(team_id="team_1", user_id="u_late", workspace_id="ws_1", turn_id="t_late", reason="turn"),
        provider="p",
        model="m",
        family="openai",
        mode="invoke",
        canonical_body=body,
        request_digest=hashlib.sha256(body).hexdigest(),
        usage_event_id=None,
        subagent_id=None,
        created_at=datetime.now(UTC),
    )
    prepared = prepare_request_records(ReferenceCipher(Fernet.generate_key().decode()), [entry], {"t_late": "s_late"})
    await request_account_deletion(UserStore(core), "u_late")
    path = domain_database_path(core, "observability")
    async with connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await write_prepared_request_records(db, prepared) == 0
        await db.commit()
    rejected = (
        ("INSERT INTO llm_request_records VALUES (" + ",".join("?" for _ in prepared[0].record) + ")", prepared[0].record),
        ("INSERT INTO llm_request_chunks VALUES (?,?,?,?,?,?)", prepared[0].chunks[0]),
        ("INSERT INTO llm_request_changes VALUES (?,?,?,?,?)", ("lrc_late", prepared[0].record[0], "tool_result_capped", "opaque", "t")),
    )
    for sql, parameters in rejected:
        async with connect(path) as db:
            await db.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.IntegrityError, match="request diagnostic source is no longer available"):
                await db.execute(sql, parameters)
            assert not db.in_transaction
    async with connect(path) as db, db.execute("SELECT COUNT(*) FROM llm_request_chunks WHERE turn_id='t_late'") as cursor:
        assert await cursor.fetchone() == (0,)
