"""Actual receipt/recovery transactions; status reads never dispatch or decrypt.

The aggregate unknown state and the durable pending row intentionally differ:
only the original callback can still provide its authoritative late receipt.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_projection import read_reference_projection
from bowhead_core.chat_reference_runtime_store import settle_reference_primary_in_connection
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.skill_model_status import read_reference_model_status, read_reference_model_status_in_connection
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.types import TokenUsage
from test_skill_model_invocation import access_case as _access_case
from test_skill_model_invocation import claimed_case as _claimed_case
from test_skill_model_invocation import invocations, receiver, scope, spec_for, write_primary_output_fixture
from test_skill_model_invocation import model_case as _model_case
from test_skill_model_invocation import receiver_case as _receiver_case
from test_skill_model_invocation import start_case as _start_case

access_case = _access_case
claimed_case = _claimed_case
model_case = _model_case
receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


def args(case):
    access = case.case.access
    return {
        "db_path": access.source_db_path,
        "scope": ReferenceRequestScope(**access.scope.model_dump(exclude={"policy_epoch"})),
        "turn_id": access.turn_id,
    }


def edit(case, sql, params=()):
    with sqlite3.connect(case.case.access.source_db_path) as db:
        db.execute(sql, params)


async def begin(case):
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        return hook, spec, await hook.begin(spec)


async def projection(case):
    access = case.case.access
    return await read_reference_projection(
        access.source_db_path,
        scope=access.scope,
        turn_id=access.turn_id,
        visible_resource_identities=frozenset(),
        visibility_complete=True,
    )


async def test_distinguishes_preparation_pending_and_confirmed_usage(model_case):
    case = model_case
    fresh = await read_reference_model_status(**args(case))
    assert fresh.state == "not_started" and fresh.context_count == fresh.invocation_count == 0
    assert fresh.input_tokens is fresh.output_tokens is fresh.cached_tokens is None
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        prepared = await read_reference_model_status(**args(case))
        assert prepared.state == "prepared" and prepared.context_count == 1 and prepared.invocation_count == 0
        attempt = await hook.begin(spec)
        pending = await read_reference_model_status(**args(case))
        assert pending.state == "pending" and pending.pending_count == pending.invocation_count == 1
        pending_facts = await projection(case)
        assert pending_facts["model_invocations"][0]["status"] == "pending"
        assert pending.input_tokens is pending.output_tokens is pending.cached_tokens is None
        assert await attempt.complete(usage=TokenUsage(input_tokens=12, output_tokens=7, cached_tokens=3), response_digest="a" * 64)
    settled = await read_reference_model_status(**args(case))
    assert settled.state == "settled" and settled.completed_count == 1 and settled.pending_count == 0
    assert (settled.input_tokens, settled.output_tokens, settled.cached_tokens) == (12, 7, 3)
    encoded = json.dumps(asdict(settled))
    assert all(value not in encoded for value in ("method", "support", "year", "primary", "large", "rmi_", "ric_", "sha256", "cost"))


@pytest.mark.parametrize("last", ["completed", "pending", "unknown"])
async def test_aggregate_does_not_turn_partial_usage_into_complete_or_double_count_rejection(model_case, last):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        rejected = await hook.begin(spec)
        assert await rejected.confirmed_not_applied(evidence_code="outbound_param_rejected/temperature/v1", evidence_digest="a" * 64)
        zero = await read_reference_model_status(**args(case))
        assert zero.state == "settled" and zero.not_applied_count == 1 and zero.input_tokens == zero.output_tokens == zero.cached_tokens == 0
        first = await hook.begin(spec)
        assert await first.complete(usage=TokenUsage(input_tokens=4, output_tokens=2), response_digest="b" * 64)
        # Same context; two distinct physical requests are counted once each.
        second = await hook.begin(spec)
        if last == "completed":
            assert await second.complete(usage=TokenUsage(input_tokens=6, output_tokens=3), response_digest="c" * 64)
        elif last == "unknown":
            await second.unknown(reason_code="timeout")
    result = await read_reference_model_status(**args(case))
    assert result.context_count == 1 and result.invocation_count == 3 and result.not_applied_count == 1
    assert result.state == {"completed": "settled", "pending": "pending", "unknown": "delivery_unknown"}[last]
    assert (result.input_tokens, result.output_tokens) == ((10, 5) if last == "completed" else (None, None))


@pytest.mark.parametrize("stop", ["expired", "cancelled", "purged"])
async def test_recovery_fences_worker_keeps_original_pending_and_accepts_actual_late_receipt(model_case, stop):
    case = model_case
    access = case.case.access
    store = case.case.ctx.session_store
    hook, spec, attempt = await begin(case)
    original = invocations(case)
    if stop == "cancelled":
        result = await store.request_cancel_turn(
            team_id=access.scope.team_id,
            user_id=access.scope.user_id,
            workspace_id=access.scope.workspace_id,
            turn_id=access.turn_id,
            expected_execution_generation=1,
            cancel_request_id="stop_original",
        )
        assert result is not None
    elif stop == "purged":
        edit(case, "UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id=?", (access.turn_id,))
        assert not (await read_reference_model_status(**args(case))).material_available
    edit(
        case,
        "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), access.turn_id),
    )
    before_recovery = await read_reference_model_status(**args(case))
    assert before_recovery.state == "delivery_unknown" and before_recovery.pending_count == 1 and before_recovery.delivery_unknown_count == 0
    assert before_recovery.input_tokens is None
    before_facts = await projection(case)
    assert before_facts["model_invocations"][0]["status"] == "delivery_unknown"
    assert before_facts["model_invocations"][0]["stored_status"] == "pending"
    assert "model_delivery_unknown" in before_facts["evidence_completeness"]["reason_codes"]
    assert await store.recover_expired_chat_spawn_dispatches() == [access.turn_id]
    assert await store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(access.source_db_path) as db:
        expected = "cancelled" if stop == "cancelled" else "failed"
        assert db.execute("SELECT status FROM turns WHERE id=?", (access.turn_id,)).fetchone() == (expected,)
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (access.turn_id,)).fetchone() == (expected,)
        events = db.execute("SELECT event_type,payload_json FROM turn_events WHERE turn_id=?", (access.turn_id,)).fetchall()
        assert sum(kind == "done" for kind, _ in events) == 1
        assert next(json.loads(payload)["status"] for kind, payload in events if kind == "done") == expected
    rows = invocations(case)
    assert len(rows) == 1 and rows[0]["invocation_id"] == original[0]["invocation_id"]
    assert rows[0]["status"] == "pending" and rows[0]["ended_at"] is rows[0]["input_tokens"] is None
    assert (await read_reference_model_status(**args(case))).state == "delivery_unknown"
    assert await attempt.complete(usage=TokenUsage(input_tokens=8, output_tokens=2), response_digest="d" * 64)
    late = await read_reference_model_status(**args(case))
    assert late.state == "settled" and late.input_tokens == 8 and late.output_tokens == 2
    late_facts = await projection(case)
    assert late_facts["model_invocations"][0]["status"] == late_facts["model_invocations"][0]["stored_status"] == "completed"
    assert late_facts["projection_etag"] != before_facts["projection_etag"]
    assert "model_delivery_unknown" not in late_facts["evidence_completeness"]["reason_codes"]
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError):
        await hook.begin(spec)
    with receiver(case.case.handle.subagent_id), scope(case) as fresh, pytest.raises(LLMInputAdmissionError):
        await fresh.begin(spec)
    assert len(invocations(case)) == 1
    # Recording a receipt cannot resurrect the failed/cancelled task.
    with sqlite3.connect(access.source_db_path) as db:
        assert db.execute("SELECT status FROM turns WHERE id=?", (access.turn_id,)).fetchone() == (expected,)


@pytest.mark.parametrize("fence", ["cancel", "turn", "agent", "dispatch", "generation", "session", "purged"])
async def test_inactive_worker_never_displays_pending_progress(model_case, fence):
    case = model_case
    await begin(case)
    sql = {
        "cancel": "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='2026-09-14T00:00:00+00:00'",
        "turn": "UPDATE turns SET status='failed' WHERE id='t_reference'",
        "agent": "UPDATE turn_agent_execution SET status='failed',ended_at='2026-09-14T00:00:00+00:00' WHERE turn_id='t_reference'",
        "dispatch": "UPDATE chat_turn_spawn_dispatch SET state='failed'",
        "generation": "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2",
        "session": "UPDATE sessions SET active_turn_id=NULL WHERE id='s_reference'",
        "purged": "UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'",
    }[fence]
    edit(case, sql)
    result = await read_reference_model_status(**args(case))
    assert result.state == "delivery_unknown" and result.input_tokens is None
    facts = await projection(case)
    assert facts["model_invocations"][0]["status"] == "delivery_unknown"
    assert facts["model_invocations"][0]["stored_status"] == "pending"
    assert invocations(case)[0]["status"] == "pending"


@pytest.mark.parametrize("change", ["team", "user", "workspace", "session", "no_session", "turn", "member", "deleted"])
async def test_reader_requires_current_owned_scope_without_disclosing_other_task_status(model_case, change):
    case = model_case
    await begin(case)
    request_args = args(case)
    if change in ("team", "user", "workspace", "session", "no_session"):
        field = "session_id" if change == "no_session" else change + "_id"
        request_args["scope"] = request_args["scope"].model_copy(update={field: None if change == "no_session" else "other"})
    elif change == "turn":
        request_args["turn_id"] = "other"
    elif change == "member":
        edit(
            case,
            "UPDATE workspace_members SET status='inactive' WHERE user_id=? AND workspace_id=?",
            (request_args["scope"].user_id, request_args["scope"].workspace_id),
        )
    else:
        edit(case, "UPDATE sessions SET deleted_at='2026-09-14T00:00:00+00:00' WHERE id='s_reference'")
    with pytest.raises(ValueError, match="^reference_model_status_unavailable$"):
        await read_reference_model_status(**request_args)


async def test_reader_uses_single_snapshot_across_concurrent_original_settlement(model_case, monkeypatch):
    import bowhead_core.skill_model_status as module

    case = model_case
    _, _, attempt = await begin(case)
    # Production startup enables WAL; this isolated migration fixture does not.
    edit(case, "PRAGMA journal_mode=WAL")
    original = module._row
    fired = False

    async def after_parent(db, sql, params):
        nonlocal fired
        result = await original(db, sql, params)
        if "r.created_generation FROM turns t" in sql and not fired:
            fired = True
            assert await attempt.complete(usage=TokenUsage(input_tokens=5, output_tokens=2), response_digest="e" * 64)
        return result

    monkeypatch.setattr(module, "_row", after_parent)
    snapshot = await read_reference_model_status(**args(case))
    assert fired and snapshot.state == "pending" and snapshot.input_tokens is None
    after = await read_reference_model_status(**args(case))
    assert after.state == "settled" and after.input_tokens == 5


async def test_another_valid_owned_session_cannot_read_this_turn(model_case):
    case = model_case
    await begin(case)
    request_args = args(case)
    owned = request_args["scope"]
    await case.case.ctx.session_store.ensure_session(session_id="s_other_owned", user_id=owned.user_id, workspace_id=owned.workspace_id)
    request_args["scope"] = owned.model_copy(update={"session_id": "s_other_owned"})
    with pytest.raises(ValueError, match="^reference_model_status_unavailable$"):
        await read_reference_model_status(**request_args)


async def test_retention_policy_tightening_is_visible_before_cleaner_and_cannot_be_undone(model_case, monkeypatch):
    import bowhead_core.skill_model_status as module
    from bowhead_core.reference_retention_policy import set_retention_policy

    case = model_case
    _, _, attempt = await begin(case)
    assert await attempt.complete(usage=TokenUsage(input_tokens=4, output_tokens=1), response_digest="a" * 64)
    access = case.case.access
    future = datetime.now(UTC) + timedelta(days=2)
    with sqlite3.connect(access.source_db_path) as db:
        original = db.execute("SELECT expires_at,retention_state FROM chat_reference_plans WHERE turn_id=?", (access.turn_id,)).fetchone()
    assert datetime.fromisoformat(original[0]) > future

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(module, "datetime", FutureClock)
    assert (await read_reference_model_status(**args(case))).material_available
    owner = case.case.case.fixture.case.fixture.owner_id
    # The Agent publication fixture has a resource owner, but no team owner.
    edit(case, "UPDATE teams SET created_by_user_id=? WHERE id=?", (owner, access.scope.team_id))
    await set_retention_policy(access.source_db_path, team_id=access.scope.team_id, user_id=owner, days=1, expected_version=0)
    tightened = await read_reference_model_status(**args(case))
    assert not tightened.material_available
    assert tightened.state == "settled" and tightened.input_tokens == 4
    await set_retention_policy(access.source_db_path, team_id=access.scope.team_id, user_id=owner, days=180, expected_version=1)
    assert not (await read_reference_model_status(**args(case))).material_available
    with sqlite3.connect(access.source_db_path) as db:
        assert db.execute("SELECT expires_at,retention_state FROM chat_reference_plans WHERE turn_id=?", (access.turn_id,)).fetchone() == original


async def test_generic_receipts_remain_readable_after_resource_revocation_without_reading_material(model_case):
    from test_skill_model_access import revoke

    case = model_case
    _, _, attempt = await begin(case)
    assert await attempt.complete(usage=TokenUsage(input_tokens=2, output_tokens=1), response_digest="a" * 64)
    original = invocations(case)
    await revoke(case)
    root = case.case.case.fixture.root
    for name in ("method", "support"):
        (root / name / "method.md").unlink()
    status = await read_reference_model_status(**args(case))
    assert status.state == "settled" and status.input_tokens == 2
    assert invocations(case) == original


async def test_recovery_terminal_write_failure_keeps_original_receipt_for_retry(model_case):
    case = model_case
    _, _, attempt = await begin(case)
    store = case.case.ctx.session_store
    edit(case, "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    original = invocations(case)
    edit(
        case,
        "CREATE TRIGGER fail_reference_status_done BEFORE INSERT ON turn_events WHEN NEW.event_type='done' BEGIN SELECT RAISE(ABORT,'injected terminal failure'); END",
    )
    assert await store.recover_expired_chat_spawn_dispatches() == []
    assert invocations(case) == original
    with sqlite3.connect(case.case.access.source_db_path) as db:
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == ("running",)
        assert db.execute("SELECT COUNT(*) FROM turn_events WHERE turn_id='t_reference'").fetchone() == (0,)
    assert (await read_reference_model_status(**args(case))).state == "delivery_unknown"
    edit(case, "DROP TRIGGER fail_reference_status_done")
    assert await store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await attempt.complete(usage=TokenUsage(input_tokens=7, output_tokens=1), response_digest="a" * 64)
    assert (await read_reference_model_status(**args(case))).input_tokens == 7


@pytest.mark.parametrize("failure", ["missing", "schema", "cancel"])
async def test_unavailable_reader_cannot_create_empty_state_or_swallow_cancellation(model_case, tmp_path, monkeypatch, failure):
    case = model_case
    request_args = args(case)
    if failure != "cancel":
        path = tmp_path / "PRIVATE-missing.db"
        request_args["db_path"] = path
        if failure == "schema":
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE unrelated(value TEXT)")
    else:

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(aiosqlite.Connection, "execute", cancelled)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
        await read_reference_model_status(**request_args)
    if failure == "missing":
        assert not path.exists()


async def test_connection_reader_requires_a_snapshot(model_case):
    case = model_case
    request_args = args(case)
    async with open_shared_ctx(request_args.pop("db_path")) as db:
        with pytest.raises(ValueError):
            await read_reference_model_status_in_connection(db, **request_args)


@pytest.mark.parametrize("outcome", ["pending", "unknown", "completed"])
async def test_primary_completion_helper_rejects_unresolved_before_accepting_agent_output(model_case, outcome):
    case = model_case
    _, _, attempt = await begin(case)
    if outcome == "unknown":
        await attempt.unknown(reason_code="timeout")
    elif outcome == "completed":
        assert await attempt.complete(usage=TokenUsage(input_tokens=1, output_tokens=1), response_digest="a" * 64)
    write_primary_output_fixture(case)
    async with open_shared_ctx(case.case.access.source_db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if outcome == "completed":
                await settle_reference_primary_in_connection(db, turn_id="t_reference", status="completed", now=datetime.now(UTC).isoformat())
            else:
                with pytest.raises(ValueError, match="^reference_model_invocation_unresolved$"):
                    await settle_reference_primary_in_connection(db, turn_id="t_reference", status="completed", now=datetime.now(UTC).isoformat())
        finally:
            await db.rollback()
