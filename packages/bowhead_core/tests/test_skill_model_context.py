"""Real persisted preparation through native transports; no actual model calls.

These facts prove preparation only, including when a later ticket/send fails.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import aiosqlite
import pytest
import respx
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.invocation import physical_invocation_scope
from bowhead_router.types import Message
from test_skill_model_access import access_case as _access_case
from test_skill_model_access import (
    adapter_for,
    call,
    receiver,
    request,
    revoke,
    router_for,
    success,
)
from test_skill_model_access import claimed_case as _claimed_case
from test_skill_model_access import model_case as _model_case
from test_skill_model_access import receiver_case as _receiver_case
from test_skill_model_access import start_case as _start_case

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


def contexts(case):
    with sqlite3.connect(case.case.access.source_db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM chat_reference_model_contexts ORDER BY created_at,context_id")]


async def dispatch(case, req=None):
    req = req or request(case)
    req.input_admission_window = 1000000
    adapter = adapter_for("openai")
    try:
        body = adapter._build_body(req, stream=False)
        await case.policy.validate_dispatch(req, body, provider="primary", model="large", family="openai")
    finally:
        await adapter.close()
    return req


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_context_is_committed_before_http_and_next_round_has_new_identity(model_case, family, mode):
    case = model_case
    adapter = adapter_for(family)
    router = router_for(adapter)
    req = request(case)
    seen = []

    def respond(http_request):
        rows = contexts(case)
        assert len(rows) == len(seen) + 1
        assert rows[-1]["kind"] == "context_assembled"
        assert rows[-1]["required_content_hash"] == case.policy.content_hash
        assert rows[-1]["composition_digest"] == case.case.access.composition_digest
        assert req.input_context_id not in http_request.content.decode()
        assert "BODY_method" not in str(rows) and "TASK" not in str(rows)
        seen.append(rows[-1])
        return success(family, mode)

    try:
        with receiver(case.case.handle.subagent_id), respx.mock() as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            assert await call(router, req, mode) == "OK"
            assert await call(router, request(case), mode) == "OK"
        assert seen[0]["context_id"] != seen[1]["context_id"]
        assert seen[0]["input_hash"] == seen[1]["input_hash"]
    finally:
        await adapter.close()


async def test_duplicate_context_compares_contents_without_new_fact_or_renewal(model_case):
    case = model_case
    with receiver(case.case.handle.subagent_id):
        req = await dispatch(case)
        original = contexts(case)
        await dispatch(case, req)
        assert contexts(case) == original
        # Another target/sampling plan is not a new assembled logical input.
        req.temperature = 0.3
        await dispatch(case, req)
        assert contexts(case) == original
        req.messages = [*req.messages, Message(role="user", content="changed task")]
        with pytest.raises(LLMInputAdmissionError):
            await dispatch(case, req)
        assert contexts(case) == original


async def test_concurrent_same_logical_context_has_one_fact(model_case):
    case = model_case
    with receiver(case.case.handle.subagent_id):
        req = request(case)
        await asyncio.gather(dispatch(case, req), dispatch(case, req))
    assert len(contexts(case)) == 1


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_router_retry_keeps_one_preparation_fact(model_case, mode):
    import httpx2

    case = model_case
    adapter = adapter_for("openai")
    observations = []

    def respond(_):
        observations.append(contexts(case))
        return httpx2.Response(503, json={"error": {"message": "temporary"}}) if len(observations) == 1 else success("openai", mode)

    try:
        with receiver(case.case.handle.subagent_id), respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            assert await call(router_for(adapter), request(case), mode) == "OK"
            assert route.call_count == 2
        assert len(observations[0]) == 1 and observations[1] == observations[0]
    finally:
        await adapter.close()


async def test_provider_fallback_preserves_logical_preparation_identity(model_case):
    import httpx2

    case = model_case
    primary = adapter_for("openai")
    secondary = adapter_for("anthropic")
    observations = []

    def respond(http_request):
        observations.append(contexts(case))
        if http_request.url.path.endswith("/messages"):
            return success("anthropic", "invoke")
        return httpx2.Response(503, json={"error": {"message": "temporary"}})

    try:
        with receiver(case.case.handle.subagent_id), respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            assert await call(router_for(primary, secondary), request(case)) == "OK"
            assert route.call_count == 3
        assert len(observations[0]) == 1 and all(rows == observations[0] for rows in observations)
    finally:
        await primary.close()
        await secondary.close()


async def test_preparation_survives_ticket_failure_without_claiming_provider_use(model_case):
    case = model_case
    calls = []

    class Hook:
        async def begin(self, spec):
            calls.append(contexts(case))
            assert len(calls[-1]) == 1
            raise LLMInputAdmissionError("ticket_unavailable")

    adapter = adapter_for("openai")
    try:
        with receiver(case.case.handle.subagent_id), respx.mock(assert_all_called=False) as mock, physical_invocation_scope(Hook()):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            with pytest.raises(LLMInputAdmissionError):
                await call(router_for(adapter), request(case))
            assert route.call_count == 0 and len(calls) == 1
        assert contexts(case) == calls[0]
    finally:
        await adapter.close()


@pytest.mark.parametrize("failure", ["insert", "commit", "cancel", "final_fence"])
async def test_failed_persistence_rolls_back_before_ticket_and_send(model_case, monkeypatch, failure):
    case = model_case
    if failure == "insert":
        with sqlite3.connect(case.case.access.source_db_path) as db:
            db.execute(
                "CREATE TRIGGER injected_context_failure BEFORE INSERT ON chat_reference_model_contexts BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
            )
    elif failure == "commit":

        async def fail_commit(self):
            raise RuntimeError("PRIVATE")

        monkeypatch.setattr(aiosqlite.Connection, "commit", fail_commit)
    elif failure == "cancel":
        from aiosqlite.context import contextmanager

        original = aiosqlite.Connection.execute

        @contextmanager
        async def cancel_after_insert(self, sql, parameters=None):
            result = await original(self, sql, parameters or ())
            if sql.startswith("INSERT INTO chat_reference_model_contexts"):
                raise asyncio.CancelledError()
            return result

        monkeypatch.setattr(aiosqlite.Connection, "execute", cancel_after_insert)
    else:
        # 14033edd0: the final worker fence moved onto the access object
        # (`access.require_worker_in_connection`), which also covers Plan-step fences.
        access_type = type(case.case.access)
        original = access_type.require_worker_in_connection

        async def fail_after_write(self, db):
            await original(self, db)
            raise ValueError("PRIVATE")

        monkeypatch.setattr(access_type, "require_worker_in_connection", fail_after_write)

    class Hook:
        async def begin(self, spec):
            pytest.fail("failed preparation must not reserve a physical ticket")

    adapter = adapter_for("openai")
    try:
        with receiver(case.case.handle.subagent_id), respx.mock(assert_all_called=False) as mock, physical_invocation_scope(Hook()):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else LLMInputAdmissionError) as caught:
                await call(router_for(adapter), request(case))
            assert route.call_count == 0
            assert "PRIVATE" not in str(caught.value)
            assert contexts(case) == []
    finally:
        await adapter.close()


async def test_existing_fact_does_not_authorize_reuse_after_revocation(model_case):
    case = model_case
    with receiver(case.case.handle.subagent_id):
        req = await dispatch(case)
        original = contexts(case)
        await revoke(case)
        with pytest.raises(LLMInputAdmissionError):
            await dispatch(case, req)
    assert contexts(case) == original


@pytest.mark.parametrize("change", ["context_id", "input", "receiver"])
async def test_changed_request_or_receiver_during_preparation_cannot_commit(model_case, monkeypatch, change):
    import bowhead_core.skill_read_access as module

    case = model_case
    req = request(case)
    original = module._load_current_in_connection

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "context_id":
            req.input_context_id = "ric_" + "f" * 32
        elif change == "input":
            req.messages = [*req.messages, Message(role="user", content="PRIVATE")]
        else:
            case.case.ctx.reference_skill_access = None
        return result

    monkeypatch.setattr(module, "_load_current_in_connection", changed)
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError):
        await dispatch(case, req)
    assert contexts(case) == []


@pytest.mark.parametrize("change", ["handles", "bytes"])
async def test_physical_preparation_independently_checks_original_handles_and_bytes(model_case, change):
    import hashlib

    case = model_case
    if change == "handles":
        access = replace(case.case.access, handles=case.case.access.handles[::-1])
        case.case.ctx.reference_skill_access = access
        case.policy = replace(case.policy, access=access)
    else:
        text = case.policy.content.replace("BODY_method", "FOREIGN_METHOD")
        case.policy = replace(case.policy, content=text, content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest())
    # Enter the physical gate directly, without relying on Router's earlier check.
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError):
        await dispatch(case)
    assert contexts(case) == []


async def test_lost_commit_ack_stops_send_even_when_preparation_was_saved(model_case, monkeypatch):
    case = model_case
    original = aiosqlite.Connection.commit

    async def lost_ack(self):
        await original(self)
        raise RuntimeError("PRIVATE_COMMIT_ACK")

    monkeypatch.setattr(aiosqlite.Connection, "commit", lost_ack)
    adapter = adapter_for("openai")
    try:
        with receiver(case.case.handle.subagent_id), respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            with pytest.raises(LLMInputAdmissionError):
                await call(router_for(adapter), request(case))
            assert route.call_count == 0
        assert len(contexts(case)) == 1 and contexts(case)[0]["kind"] == "context_assembled"
    finally:
        await adapter.close()


@pytest.mark.parametrize("value", [None, True, "ric_private", "ric_" + "g" * 32])
async def test_missing_or_malformed_context_identity_is_not_rebuilt(model_case, value):
    case = model_case
    req = replace(request(case), input_context_id=value)
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError):
        await dispatch(case, req)
    assert contexts(case) == []


async def test_purge_keeps_metadata_tombstone_and_prevents_reuse(model_case, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from bowhead_core.chat_reference_retention import ReferenceRetentionResult, purge_expired_reference_material
    from test_reference_retention import _advance

    case = model_case
    with receiver(case.case.handle.subagent_id):
        req = await dispatch(case)
        _advance(monkeypatch, datetime.now(UTC) + timedelta(days=366))
        assert await purge_expired_reference_material(case.case.access.source_db_path) == ReferenceRetentionResult(1, 0)
        assert contexts(case)[0]["retention_state"] == "purged"
        with pytest.raises(LLMInputAdmissionError):
            await dispatch(case, req)
    assert len(contexts(case)) == 1


@pytest.mark.parametrize("operation", ["replace", "update", "delete", "independent_purge"])
async def test_sql_preparation_identity_and_parent_lifecycle_are_guarded(model_case, operation):
    case = model_case
    with receiver(case.case.handle.subagent_id):
        await dispatch(case)
    with sqlite3.connect(case.case.access.source_db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            {
                "replace": "INSERT OR REPLACE INTO chat_reference_model_contexts SELECT * FROM chat_reference_model_contexts",
                "update": "UPDATE chat_reference_model_contexts SET input_hash='sha256:' || substr(input_hash,9) || 'a'",
                "delete": "DELETE FROM chat_reference_model_contexts",
                "independent_purge": "UPDATE chat_reference_model_contexts SET retention_state='purged'",
            }[operation]
        )


@pytest.mark.parametrize(
    "field,value",
    [("created_generation", 2), ("composition_digest", "sha256:" + "f" * 64), ("execution_id", "different"), ("context_id", "ric_" + "g" * 32)],
)
async def test_sql_context_scope_must_match_running_composition(model_case, field, value):
    case = model_case
    with receiver(case.case.handle.subagent_id):
        await dispatch(case)
    row = {**contexts(case)[0], "context_id": "ric_" + "f" * 32, field: value}
    with sqlite3.connect(case.case.access.source_db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO chat_reference_model_contexts (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values())
        )
