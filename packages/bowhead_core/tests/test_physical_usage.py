"""Shared Router calls retain scoped physical attempts and exact input slots."""

from __future__ import annotations

import json
import sqlite3

import httpx2
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_projection import _task_physical_receipts
from bowhead_core.physical_usage import PhysicalUsageObserver, source_fragments
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.invocation import physical_observation_scope
from bowhead_router.usage import usage_context
from test_chat_reference_storage import case as case
from test_skill_model_access import adapter_for, call, router_for, success

from bowhead_router import LLMRequest, Message, ToolCall

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def rows(case):
    with sqlite3.connect(case.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM llm_usage_events ORDER BY created_at,id")]


def request():
    return LLMRequest(
        model="large",
        messages=[
            Message(role="user", content="Analyze the input"),
            Message(role="assistant", content="", tool_calls=[ToolCall("call_source", "skill_read", {"name": "private_method"})]),
            Message(role="tool", tool_call_id="call_source", content="private-method-body"),
        ],
    )


def context(case):
    return usage_context(team_id=case.scope.team_id, user_id=case.scope.user_id, workspace_id=case.scope.workspace_id, turn_id="t_ref", reason="turn")


def observer(case):
    return PhysicalUsageObserver(str(case.path), cipher=case.cipher, turn_id="t_ref", generation=1)


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_actual_adapter_commits_before_send_and_keeps_encrypted_exact_sources(case, family, mode):
    adapter = adapter_for(family)
    router = router_for(adapter)
    seen = []

    def respond(req):
        saved = rows(case)
        assert saved[-1]["receipt_state"] == "pending"
        assert "private-method-body" not in json.dumps(saved)
        assert saved[-1]["agent_type"] == "analyst"
        seen.append(json.loads(req.content))
        return success(family, mode)

    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            await call(router, request(), mode)
    finally:
        await adapter.close()
    saved = rows(case)
    assert len(saved) == 1
    assert saved[0]["receipt_state"] == "completed"
    receipt = json.loads(saved[0]["receipt_json"])
    assert receipt["sources"][0]["tool_call_id"] == "call_source"
    assert receipt["source_coverage"] == "exact"
    assert "physical_receipt" not in json.dumps(seen)
    # Receipts keep only content hashes; the retired source ciphertext column is gone.
    assert "source_ciphertext" not in saved[0]
    async with open_shared_ctx(case.path) as db:
        projection = await _task_physical_receipts(db, scope=case.scope, turn_id="t_ref", plan=None)
    assert len(projection["entries"]) == 1
    assert projection["entries"][0]["usage"] == {"input": 1, "output": 1, "cached": 0}
    assert "private" not in json.dumps(projection)


@pytest.mark.parametrize("mutation", ["drop", "rewrite", "duplicate", "wrong_id", "user_text_only"])
async def test_unmatched_or_forged_wire_slots_are_not_source_receipts(mutation):
    req = request()
    body = {"messages": [{"role": "tool", "tool_call_id": "call_source", "content": "private-method-body"}]}
    if mutation == "drop":
        body["messages"] = []
    elif mutation == "rewrite":
        body["messages"][0]["content"] += " changed"
    elif mutation == "duplicate":
        body["messages"] *= 2
    elif mutation == "wrong_id":
        body["messages"][0]["tool_call_id"] = "other"
    else:
        body["messages"][0]["role"] = "user"
    assert source_fragments(req, body, "openai") == ([], [], False)


@pytest.mark.parametrize("bad_scope", ["user", "turn", "generation"])
async def test_scope_rejection_sends_nothing(case, bad_scope):
    obs = observer(case)
    if bad_scope == "generation":
        obs.generation = 2
    adapter = adapter_for("openai")
    try:
        with context(case), physical_observation_scope(obs), respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").respond(200)
            with (
                usage_context(user_id="other" if bad_scope == "user" else None, turn_id="other" if bad_scope == "turn" else None),
                pytest.raises(LLMInputAdmissionError),
            ):
                await call(router_for(adapter), request(), "invoke")
            assert not route.called
    finally:
        await adapter.close()
    assert not rows(case)


async def test_no_output_timeout_is_not_charged_and_is_retried(case):
    from bowhead_router.errors import InvocationTargetsExhaustedError

    adapter = adapter_for("openai")
    router = router_for(adapter)
    router.max_retries = 1
    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=httpx2.ReadTimeout("synthetic timeout"))
            with pytest.raises(InvocationTargetsExhaustedError):
                await call(router, request(), "invoke")
            assert route.call_count == 2
    finally:
        await adapter.close()
    receipts = rows(case)
    assert [row["receipt_state"] for row in receipts] == ["not_applied", "not_applied"]
    assert {row["outcome_code"] for row in receipts} == {"provider_no_response_timeout/v1"}
    assert all(row["tokens_input"] == row["tokens_output"] == 0 for row in receipts)


@pytest.mark.parametrize("changed", ["input", "output", "cached", "response", "outcome"])
async def test_duplicate_settlement_requires_the_same_response_usage_and_outcome(case, changed):
    from bowhead_core.physical_usage import _UsageAttempt
    from bowhead_router.types import TokenUsage

    obs = observer(case)
    adapter = adapter_for("openai")
    try:
        with context(case), physical_observation_scope(obs), respx.mock() as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            await call(router_for(adapter), request(), "invoke")
    finally:
        await adapter.close()
    original = rows(case)[0]
    attempt = _UsageAttempt(obs, original["id"])
    usage = TokenUsage(input_tokens=original["tokens_input"], output_tokens=original["tokens_output"], cached_tokens=original["tokens_cached"])
    assert await attempt.complete(usage=usage, response_digest=original["response_digest"])
    assert rows(case)[0] == original
    if changed in {"input", "output", "cached"}:
        setattr(usage, f"{changed}_tokens", getattr(usage, f"{changed}_tokens") + 1)
    with pytest.raises(ValueError, match="physical_usage_settlement_conflict"):
        await attempt._settle(
            "completed",
            usage=usage,
            response_digest="changed-response" if changed == "response" else original["response_digest"],
            code="changed-outcome" if changed == "outcome" else None,
        )
    assert rows(case)[0] == original


async def test_receipts_are_immutable_and_turn_deletion_erases_them(case):
    adapter = adapter_for("openai")
    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            await call(router_for(adapter), request(), "invoke")
    finally:
        await adapter.close()
    with sqlite3.connect(case.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE llm_usage_events SET tokens_input=42")
        db.execute("DELETE FROM turns WHERE id='t_ref'")
    assert rows(case) == []


async def test_lost_reservation_ack_stops_before_transport(case, monkeypatch):
    import aiosqlite

    original = aiosqlite.Connection.commit

    async def lost_ack(db):
        await original(db)
        raise RuntimeError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(aiosqlite.Connection, "commit", lost_ack)
    adapter = adapter_for("openai")
    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").respond(200)
            with pytest.raises(LLMInputAdmissionError):
                await call(router_for(adapter), request(), "invoke")
            assert route.call_count == 0
    finally:
        await adapter.close()
    assert rows(case)[0]["receipt_state"] == "pending"


async def test_authority_rejection_is_still_authoritative(case):
    from bowhead_router.invocation import physical_invocation_scope

    class Authority:
        async def begin(self, spec):
            raise LLMInputAdmissionError("synthetic authorization rejection")

    adapter = adapter_for("openai")
    try:
        with (
            context(case),
            physical_observation_scope(observer(case)),
            physical_invocation_scope(Authority()),
            respx.mock(assert_all_called=False) as mock,
        ):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").respond(200)
            with pytest.raises(LLMInputAdmissionError):
                await call(router_for(adapter), request(), "invoke")
            assert route.call_count == 0
    finally:
        await adapter.close()
    assert rows(case)[0]["receipt_state"] == "not_applied"


async def test_cancellation_after_send_keeps_unknown_receipt(case):
    import asyncio

    entered = asyncio.Event()

    async def stall(req):
        entered.set()
        await asyncio.Event().wait()

    adapter = adapter_for("openai")
    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=stall)
            task = asyncio.create_task(call(router_for(adapter), request(), "invoke"))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert route.call_count == 0  # Transport entered but has not yielded a response.
    finally:
        await adapter.close()
    assert len(rows(case)) == 1
    assert rows(case)[0]["receipt_state"] == "unknown"


async def test_known_and_missing_usage_do_not_create_duplicate_statistics(case):
    from bowhead_gateway.llm_usage_recorder import LLMUsageRecorder
    from bowhead_router.usage import set_usage_sink

    recorder = LLMUsageRecorder(case.users)
    previous = set_usage_sink(recorder.sink)
    adapter = adapter_for("openai")
    partial = []
    try:
        with context(case), usage_context(turn_usage_sink=partial.append), physical_observation_scope(observer(case)), respx.mock() as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(
                side_effect=[
                    success("openai", "invoke"),
                    httpx2.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}),
                ]
            )
            assert await call(router_for(adapter), request(), "invoke") == "OK"
            assert await call(router_for(adapter), request(), "invoke") == "OK"
    finally:
        set_usage_sink(previous)
        await adapter.close()
    async with open_shared_ctx(case.path) as db:
        projection = await _task_physical_receipts(db, scope=case.scope, turn_id="t_ref", plan=None)
    assert len(projection["entries"]) == 2
    assert projection["entries"][0]["usage"] == {"input": 1, "output": 1, "cached": 0}
    assert projection["entries"][1]["state"] == "usage_missing"
    assert projection["entries"][1]["usage"] is None
    assert len(partial) == 1 and partial[0].tokens_total == 2
    assert recorder.stats["buffered"] == 0


async def test_failed_receipt_does_not_poison_independent_calls_or_replay_the_request(case, monkeypatch):
    from contextlib import asynccontextmanager

    from bowhead_router.invocation import InvocationOutcomeUnknownError

    from bowhead_core import physical_usage

    original = physical_usage.open_shared_ctx
    fail_settlement = True

    @asynccontextmanager
    async def connection(path):
        nonlocal fail_settlement
        async with original(path) as db:
            execute = db.execute

            def checked(sql, parameters=()):
                nonlocal fail_settlement
                if fail_settlement and sql.startswith("UPDATE llm_usage_events SET receipt_state"):
                    fail_settlement = False
                    raise sqlite3.OperationalError("database is locked")
                return execute(sql, parameters)

            db.execute = checked
            try:
                yield db
            finally:
                db.execute = execute

    monkeypatch.setattr(physical_usage, "open_shared_ctx", connection)
    adapter = adapter_for("openai")
    obs = observer(case)
    try:
        with context(case), physical_observation_scope(obs), respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            with pytest.raises(InvocationOutcomeUnknownError):
                await call(router_for(adapter), request(), "invoke")
            assert route.call_count == 1
            await call(router_for(adapter), request(), "invoke")
            assert route.call_count == 2
    finally:
        await adapter.close()
    saved = rows(case)
    assert [row["receipt_state"] for row in saved] == ["unknown", "completed"]
    assert saved[0]["outcome_code"] == "durable_settlement_failed"
