"""Real legacy reference admission and native physical receipts; HTTP is mocked."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.skill_model_invocation import ReferenceModelInvocationHook, reference_model_invocation_scope
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.invocation import PhysicalInvocationSpec, canonical_transport_digest, physical_invocation_scope
from bowhead_router.types import LLMRequest, Message, TokenUsage
from test_skill_model_access import call, receiver, request, revoke, router_for, success
from test_skill_model_context import access_case as _access_case
from test_skill_model_context import claimed_case as _claimed_case
from test_skill_model_context import contexts
from test_skill_model_context import model_case as _model_case
from test_skill_model_context import receiver_case as _receiver_case
from test_skill_model_context import start_case as _start_case

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


def adapter_for(family):
    cls = AnthropicCompatibleAdapter if family == "anthropic" else OpenAICompatibleAdapter
    return cls(
        base_url="https://api.anthropic.example.com" if family == "anthropic" else "https://api.openai.com/v1",
        api_key="test-only",
        provider_name="primary",
        provider_options={"models": [{"id": "large", **({"api_surface": "responses"} if family == "responses" else {})}]},
    )


def invocations(case):
    with sqlite3.connect(case.case.access.source_db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM chat_reference_model_invocations ORDER BY created_at,invocation_id")]


def scope(case):
    return reference_model_invocation_scope(access=case.case.access, ctx=case.case.ctx)


async def spec_for(case, req=None):
    req = req or request(case)
    req.input_admission_window = 1000000
    adapter = adapter_for("openai")
    try:
        body = adapter._build_body(req, stream=False)
        receipt = await case.policy.validate_dispatch(req, body, provider="primary", model="large", family="openai")
        return PhysicalInvocationSpec("primary", "large", "openai_chat/v1", "invoke", canonical_transport_digest(body), receipt)
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_native_send_has_committed_identity_and_exact_receipt(model_case, family, mode):
    case = model_case
    adapter = adapter_for(family)
    sent = []

    def respond(http_request):
        rows = invocations(case)
        assert len(rows) == 1 and rows[0]["status"] == "pending"
        assert rows[0]["context_id"] == contexts(case)[0]["context_id"]
        assert rows[0]["request_digest"] == canonical_transport_digest(json.loads(http_request.content))
        assert rows[0]["input_tokens"] is None and rows[0]["output_tokens"] is None
        assert rows[0]["invocation_id"] not in http_request.content.decode()
        sent.append(rows[0])
        return success(family, mode)

    try:
        with receiver(case.case.handle.subagent_id), scope(case), respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            assert await call(router_for(adapter), request(case), mode) == "OK"
            assert route.call_count == 1
        row = invocations(case)[0]
        assert row["status"] == "completed" and row["input_tokens"] == 1 and row["output_tokens"] == 1
        assert len(row["response_digest"]) == 64 and row["ended_at"]
        assert row["invocation_id"] == sent[0]["invocation_id"]
        assert len(contexts(case)) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "anthropic"])
async def test_confirmed_parameter_rejection_gets_new_physical_id_same_context(model_case, family, monkeypatch):
    from bowhead_router.adapters import _chi_sniff
    from bowhead_router.outbound import reset_learned_for_test

    reset_learned_for_test()
    case = model_case
    adapter = adapter_for(family)
    # This is a test-owned structured capture; the production manifest stays
    # empty until real provider evidence is reviewed through its existing path.
    monkeypatch.setattr(
        _chi_sniff,
        "_DURABLE_REJECTION_MANIFEST_V1",
        frozenset(
            {
                (
                    "anthropic_compatible" if family == "anthropic" else "openai_compatible",
                    "anthropic-compatible-v1" if family == "anthropic" else "openai-compatible-v1",
                    "api.anthropic.example.com" if family == "anthropic" else "api.openai.com",
                    "temperature",
                    "invalid_request_error",
                )
            }
        ),
    )
    sent = []

    def respond(http_request):
        sent.append(json.loads(http_request.content))
        if len(sent) == 1:
            return httpx2.Response(
                400, json={"error": {"message": "unsupported temperature for this model", "param": "temperature", "type": "invalid_request_error"}}
            )
        return success(family, "invoke")

    try:
        with receiver(case.case.handle.subagent_id), scope(case), respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            assert await call(router_for(adapter), request(case)) == "OK"
        rows = invocations(case)
        assert [row["status"] for row in rows] == ["not_applied", "completed"]
        assert len({row["invocation_id"] for row in rows}) == 2
        assert len({row["context_id"] for row in rows}) == 1
        assert rows[0]["input_tokens"] == rows[0]["output_tokens"] == rows[0]["cached_tokens"] == 0
        assert rows[0]["request_digest"] != rows[1]["request_digest"]
    finally:
        await adapter.close()
        reset_learned_for_test()


@pytest.mark.parametrize("failure", ["timeout", "network", "unclassified", "missing_usage", "invalid_usage", "cancel", "unregistered_rejection"])
async def test_unknown_or_cancelled_send_blocks_fallback_and_rebuilt_hook(model_case, failure):
    case = model_case
    adapter = adapter_for("openai")
    fallback = adapter_for("anthropic")

    sent = []

    def respond(_):
        sent.append(True)
        if failure == "unregistered_rejection":
            return httpx2.Response(400, json={"error": {"message": "PRIVATE", "param": "temperature", "type": "invalid_request_error"}})
        if failure == "timeout":
            raise httpx2.ReadTimeout("PRIVATE")
        if failure == "network":
            raise httpx2.ConnectError("PRIVATE")
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "unclassified":
            return httpx2.Response(503, json={"error": {"message": "PRIVATE"}})
        body = {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        if failure == "invalid_usage":
            body["usage"] = {"prompt_tokens": True, "completion_tokens": 1}
        return httpx2.Response(200, json=body)

    try:
        with receiver(case.case.handle.subagent_id), respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            with scope(case), pytest.raises(asyncio.CancelledError if failure == "cancel" else InvocationOutcomeUnknownError):
                await call(router_for(adapter, fallback), request(case))
            assert len(sent) == 1
            row = invocations(case)[0]
            assert row["status"] == "delivery_unknown"
            assert row["input_tokens"] is None and row["output_tokens"] is None and row["response_digest"] is None
            assert "PRIVATE" not in str(row)
            with scope(case), pytest.raises(LLMInputAdmissionError):
                await call(router_for(adapter, fallback), request(case))
            assert len(sent) == 1
    finally:
        await adapter.close()
        await fallback.close()


@pytest.mark.parametrize("change", ["revoke", "cancel", "purge", "lease"])
async def test_late_provider_receipt_settles_original_only_after_authority_lost(model_case, change):
    from datetime import UTC, datetime, timedelta

    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        attempt = await hook.begin(spec)
        if change == "revoke":
            await revoke(case)
        else:
            with sqlite3.connect(case.case.access.source_db_path) as db:
                if change == "purge":
                    db.execute("UPDATE chat_reference_plans SET retention_state='purged'")
                elif change == "cancel":
                    db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=?", (datetime.now(UTC).isoformat(),))
                else:
                    db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
        assert await attempt.complete(usage=TokenUsage(input_tokens=3, output_tokens=2), response_digest="a" * 64)
        assert invocations(case)[0]["status"] == "completed"
        with pytest.raises(LLMInputAdmissionError):
            await hook.begin(spec)
        assert len(invocations(case)) == 1
        if change == "purge":
            assert invocations(case)[0]["retention_state"] == "purged"


async def test_duplicate_receipt_compares_content_and_unknown_cannot_erase_success(model_case):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        attempt = await hook.begin(await spec_for(case))
        assert await attempt.complete(usage=TokenUsage(input_tokens=3, output_tokens=2), response_digest="a" * 64)
        original = invocations(case)
        assert await attempt.complete(usage=TokenUsage(input_tokens=3, output_tokens=2), response_digest="a" * 64)
        assert invocations(case) == original
        assert not await attempt.complete(usage=TokenUsage(input_tokens=4, output_tokens=2), response_digest="a" * 64)
        await attempt.unknown(reason_code="PRIVATE")
        assert invocations(case) == original


@pytest.mark.parametrize("change", ["missing", "context", "input_hash", "generation", "scope"])
async def test_physical_gate_requires_exact_persisted_preparation_receipt(model_case, change):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        proof = spec.input_receipt
        if change == "missing":
            proof = None
        elif change == "context":
            proof = replace(proof, context_id="ric_" + "f" * 32)
        elif change == "input_hash":
            proof = replace(proof, input_hash="sha256:" + "f" * 64)
        elif change == "generation":
            proof = replace(proof, generation=True)
        else:
            proof = replace(proof, turn_id="other")
        with pytest.raises(LLMInputAdmissionError):
            await hook.begin(replace(spec, input_receipt=proof))
        assert invocations(case) == []


async def test_unresolved_pending_request_blocks_concurrent_or_rebuilt_sender(model_case):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        first = await hook.begin(spec)
        with pytest.raises(LLMInputAdmissionError):
            await hook.begin(spec)
        rebuilt = ReferenceModelInvocationHook(access=case.case.access, ctx=case.case.ctx)
        with pytest.raises(LLMInputAdmissionError):
            await rebuilt.begin(spec)
        assert len(invocations(case)) == 1
        await first.unknown(reason_code="interrupted")
        with pytest.raises(LLMInputAdmissionError):
            await rebuilt.begin(spec)


async def test_existing_physical_scope_is_preserved_and_ordinary_following_call_has_no_reference_hook(model_case):
    case = model_case

    class Existing:
        async def begin(self, spec):
            pytest.fail("existing hook must not be invoked by scope construction")

    with physical_invocation_scope(Existing()), pytest.raises(LLMInputAdmissionError), scope(case):
        pass
    adapter = adapter_for("openai")
    try:
        with receiver(case.case.handle.subagent_id), respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            with scope(case):
                assert await call(router_for(adapter), request(case)) == "OK"
            assert await call(router_for(adapter), LLMRequest(model="large", messages=[Message(role="user", content="ordinary")])) == "OK"
        assert len(invocations(case)) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("operation", ["replace", "delete", "rewrite", "terminal_rewrite"])
async def test_sql_invocation_identity_and_terminal_receipt_are_immutable(model_case, operation):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        attempt = await hook.begin(await spec_for(case))
        if operation == "terminal_rewrite":
            await attempt.complete(usage=TokenUsage(input_tokens=1, output_tokens=2), response_digest="a" * 64)
    with sqlite3.connect(case.case.access.source_db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            {
                "replace": "INSERT OR REPLACE INTO chat_reference_model_invocations SELECT * FROM chat_reference_model_invocations",
                "delete": "DELETE FROM chat_reference_model_invocations",
                "rewrite": "UPDATE chat_reference_model_invocations SET context_id='ric_' || printf('%032d',0)",
                "terminal_rewrite": "UPDATE chat_reference_model_invocations SET input_tokens=10",
            }[operation]
        )


async def test_sql_not_applied_requires_all_three_exact_zero_counts(model_case):
    from datetime import UTC, datetime

    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        await hook.begin(await spec_for(case))
    with sqlite3.connect(case.case.access.source_db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE chat_reference_model_invocations SET status='not_applied',response_digest=?,input_tokens=NULL,output_tokens=0,cached_tokens=0,ended_at=?",
            ("a" * 64, datetime.now(UTC).isoformat()),
        )


@pytest.mark.parametrize("failure", ["insert", "commit", "commit_ack", "cancel", "final_fence"])
async def test_failed_physical_admission_cannot_reach_http(model_case, monkeypatch, failure):
    from contextlib import asynccontextmanager

    import bowhead_core.skill_model_invocation as module
    from aiosqlite.context import contextmanager

    case = model_case
    original = module.open_shared_ctx

    @asynccontextmanager
    async def faulty(path):
        async with original(path) as db:
            execute = db.execute
            commit = db.commit

            @contextmanager
            async def failing_execute(sql, params=None):
                if failure == "insert" and sql.startswith("INSERT INTO chat_reference_model_invocations"):
                    raise RuntimeError("PRIVATE_INSERT")
                result = await execute(sql, params or ())
                if failure == "cancel" and sql.startswith("INSERT INTO chat_reference_model_invocations"):
                    raise asyncio.CancelledError()
                return result

            async def failing_commit():
                if failure == "commit_ack":
                    await commit()
                if failure in {"commit", "commit_ack"}:
                    raise RuntimeError("PRIVATE_COMMIT")
                await commit()

            db.execute = failing_execute
            db.commit = failing_commit
            yield db

    monkeypatch.setattr(module, "open_shared_ctx", faulty)
    if failure == "final_fence":

        async def expired(self, db):
            raise ValueError("PRIVATE_FENCE")

        # 14033edd0: the final worker fence moved onto the access object.
        monkeypatch.setattr(type(case.case.access), "require_worker_in_connection", expired)
    adapter = adapter_for("openai")
    try:
        with receiver(case.case.handle.subagent_id), scope(case), respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else LLMInputAdmissionError):
                await call(router_for(adapter), request(case))
            assert route.call_count == 0
        rows = invocations(case)
        assert len(rows) == (1 if failure == "commit_ack" else 0)
        if rows:
            assert rows[0]["status"] == "pending" and rows[0]["input_tokens"] is None
    finally:
        await adapter.close()


async def test_lost_settlement_ack_uses_exact_saved_receipt_without_repeating_http(model_case, monkeypatch):
    import aiosqlite

    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        attempt = await hook.begin(await spec_for(case))
        original = aiosqlite.Connection.commit

        async def lost_ack(self):
            await original(self)
            raise RuntimeError("PRIVATE_ACK")

        monkeypatch.setattr(aiosqlite.Connection, "commit", lost_ack)
        assert await attempt.complete(usage=TokenUsage(input_tokens=3, output_tokens=2), response_digest="a" * 64)
        assert invocations(case)[0]["status"] == "completed"
        # Replay must compare the exact contents, including usage and response.
        assert not await attempt.complete(usage=TokenUsage(input_tokens=3, output_tokens=2), response_digest="b" * 64)


async def test_unrecognized_non_application_is_unknown_and_cannot_authorize_retry(model_case):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        spec = await spec_for(case)
        attempt = await hook.begin(spec)
        assert not await attempt.confirmed_not_applied(evidence_code="PRIVATE success", evidence_digest="a" * 64)
        assert invocations(case)[0]["status"] == "delivery_unknown"
        with pytest.raises(LLMInputAdmissionError):
            await hook.begin(spec)


@pytest.mark.parametrize("change", ["revoke", "helper", "context", "marker", "handles"])
async def test_saved_preparation_is_not_live_authority_at_physical_admission(model_case, change):
    case = model_case
    with receiver(case.case.handle.subagent_id):
        spec = await spec_for(case)
    if change == "revoke":
        await revoke(case)
    if change == "context":
        case.case.ctx.reference_skill_access = None
    if change == "marker":
        case.case.ctx.reference_skill_required = False

        # The recognition probe restores a missing requirement on a reference
        # Turn. A marker lost after that await still cannot authorize a send.
        async def lose_requirement():
            case.case.ctx.reference_skill_required = False

        case.case.ctx.ensure_reference_skill_context = lose_requirement
    if change == "handles":
        case.case.access = replace(case.case.access, handles=case.case.access.handles[::-1])
        case.case.ctx.reference_skill_access = case.case.access
    with receiver("helper" if change == "helper" else case.case.handle.subagent_id), scope(case) as hook, pytest.raises(LLMInputAdmissionError):
        await hook.begin(spec)
    assert len(contexts(case)) == 1 and invocations(case) == []


async def test_real_subagent_runner_keeps_original_task_receiver_and_physical_receipt(model_case):
    from test_skill_model_access import test_existing_subagent_runner_uses_the_actual_receiver_for_native_dispatch as run_exact_receiver_scenario

    # The existing integration supplies the original source task/exact binding
    # and lets SubagentRunner establish its own ContextVar; it also checks wire.
    with scope(model_case):
        await run_exact_receiver_scenario(model_case)
    rows = invocations(model_case)
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    assert rows[0]["subagent_id"] == model_case.case.handle.subagent_id
    assert rows[0]["execution_id"] == model_case.case.handle.execution_id


@pytest.mark.parametrize("change", ["context", "receiver"])
async def test_sql_physical_receipt_must_match_its_context_receiver(model_case, change):
    case = model_case
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        attempt = await hook.begin(await spec_for(case))
        await attempt.complete(usage=TokenUsage(input_tokens=1, output_tokens=1), response_digest="a" * 64)
    row = {
        **invocations(case)[0],
        "invocation_id": "rmi_" + "f" * 32,
        "status": "pending",
        "ended_at": None,
        "response_digest": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
    }
    row["context_id" if change == "context" else "subagent_id"] = "different"
    with sqlite3.connect(case.case.access.source_db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO chat_reference_model_invocations (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values())
        )


def write_primary_output_fixture(case):
    import hashlib
    from datetime import UTC, datetime

    access = case.case.access
    # Give the existing completion guard a valid parent output shape. These are
    # SQL contract fixtures, not a claim that an unresolved model produced output.
    content = b'{"final_answer":"fixture","payload":{}}'
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    encrypted = access.cipher.seal(
        scope=access.scope, purpose="reference_output", identity=f"{access.turn_id}/{access.handles[0].execution_id}", content=content
    )
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(access.source_db_path) as db:
        expiry = db.execute("SELECT expires_at FROM chat_reference_plans WHERE turn_id=?", (access.turn_id,)).fetchone()[0]
        for kind, key, ciphertext in [("delegate_started", "primary:dispatch", None), ("output_received", "primary:output", encrypted)]:
            db.execute(
                "INSERT INTO chat_reference_evidence(turn_id,evidence_key,schema_version,resolution_seq,execution_id,created_generation,kind,subagent_id,content_hash,ciphertext,retention_state,created_at,expires_at) VALUES (?,?,1,1,?,1,?,?,?,?,'available',?,?)",
                (access.turn_id, key, access.handles[0].execution_id, kind, access.handles[0].subagent_id, digest, ciphertext, now, expiry),
            )
        db.execute("UPDATE turn_agent_execution SET status='completed',ended_at=? WHERE id=?", (now, access.handles[0].execution_id))


@pytest.mark.parametrize("outcome", ["pending", "delivery_unknown", "completed"])
async def test_turn_completion_requires_resolved_model_invocations(model_case, outcome):
    case = model_case
    access = case.case.access
    with receiver(case.case.handle.subagent_id), scope(case) as hook:
        attempt = await hook.begin(await spec_for(case))
        if outcome == "completed":
            await attempt.complete(usage=TokenUsage(input_tokens=1, output_tokens=1), response_digest="a" * 64)
        elif outcome == "delivery_unknown":
            await attempt.unknown(reason_code="transport_timeout")
    write_primary_output_fixture(case)
    with sqlite3.connect(access.source_db_path) as db:
        if outcome == "completed":
            db.execute("UPDATE turns SET status='completed' WHERE id=?", (access.turn_id,))
            assert db.execute("SELECT status FROM turns WHERE id=?", (access.turn_id,)).fetchone() == ("completed",)
        else:
            with pytest.raises(sqlite3.IntegrityError, match="reference model invocation unresolved"):
                db.execute("UPDATE turns SET status='completed' WHERE id=?", (access.turn_id,))
