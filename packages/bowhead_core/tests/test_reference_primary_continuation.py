"""Lead may continue a completed @Agent primary on its one admitted execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from bowhead_core.chat_reference_delegation import PRIMARY_DISPATCH_LIMIT
from bowhead_core.chat_reference_runtime import ReferenceDispatchRefused
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from test_reference_lead_delegation import lead_case as _lead_case
from test_reference_lead_delegation import start_case as _start_case
from test_reference_primary_runtime import _ctx, _rows, _runtime
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_subagent import _router

from bowhead_router import LLMResponse

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
start_case = _start_case
lead_case = _lead_case
receiver_case = _receiver_case


def _runner(case, *answers: str) -> SubagentRunner:
    router = _router(*(LLMResponse(content=answer) for answer in answers))
    adapter = router._registry._adapters["p1"]  # type: ignore[attr-defined]
    adapter.raise_on_exhaust = True
    requests: list = []
    original = adapter.invoke

    async def invoke(req):
        requests.append(req)
        return await original(req)

    adapter.invoke = invoke
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), team_subagent_store=case.fixture.users)
    runner._test_requests = requests  # type: ignore[attr-defined]
    return runner


async def _dispatch(runtime, case, runner, *, task: str, call_id: str):
    return await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus(), delegation_task=task, delegation_call_id=call_id)


async def test_second_dispatch_continues_on_the_same_execution_and_scope(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    first = await _dispatch(runtime, case, _runner(case, "First half of the work"), task="Do the first half", call_id="call-1")
    assert first.ok
    primary = await runtime.validate_lead()
    assert primary.binding.status == "completed"
    assert await runtime.binding_for_delegation(profile=primary.binding.runtime_profile_name, agent_id=None, role="primary") == primary.binding

    runner = _runner(case, "Second half of the work")
    second = await _dispatch(runtime, case, runner, task="Finish the second half", call_id="call-2")

    assert second.ok and second.final_answer == "Second half of the work"
    assert second.subagent_id == first.subagent_id
    executions = _rows(case, "turn_agent_execution")
    assert [(row["id"], row["status"]) for row in executions] == [(primary.binding.execution_id, "completed")]
    # The first dispatch's ledger is unchanged: one dispatch and one output.
    assert sorted(row["kind"] for row in _rows(case, "chat_reference_evidence")) == ["delegate_started", "output_received"]
    assert len(_rows(case, "chat_reference_resolutions")) == 2
    (continuation,) = _rows(case, "chat_reference_primary_continuations")
    assert continuation["dispatch_seq"] == 2
    assert continuation["execution_id"] == primary.binding.execution_id
    assert continuation["subagent_id"] == first.subagent_id
    assert continuation["call_id"] == "call-2"
    assert continuation["state"] == "completed" and continuation["output_ciphertext"]
    # The continuation sees the user's request, Lead's new task and its own previous result.
    task = _last_task(runner)
    assert task["current_user_request"] == primary.source.input_document.text_content()
    assert task["lead_delegation"] == "Finish the second half"
    assert task["previous_dispatch_result"] == {"available": True, "answer": {"text": "First half of the work"}}


def _last_task(runner) -> dict:
    """The continuation task JSON the selected Agent received."""
    for request in runner._test_requests:
        for message in request.messages:
            content = message.content if isinstance(message.content, str) else ""
            if "previous_dispatch_result" in content:
                return json.JSONDecoder().raw_decode(content, content.index('{"current_user_request"'))[0]
    raise AssertionError("continuation task not sent")


async def test_dispatch_limit_refuses_further_dispatch_without_touching_the_completed_primary(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    for seq in range(1, PRIMARY_DISPATCH_LIMIT + 1):
        result = await _dispatch(runtime, case, _runner(case, f"answer {seq}"), task=f"step {seq}", call_id=f"call-{seq}")
        assert result.ok
    continuations = _rows(case, "chat_reference_primary_continuations")
    assert [row["dispatch_seq"] for row in continuations] == list(range(2, PRIMARY_DISPATCH_LIMIT + 1))
    primary = await runtime.validate_lead()
    with pytest.raises(ValueError, match="reference_primary_dispatch_limit_reached"):
        await runtime.binding_for_delegation(profile=primary.binding.runtime_profile_name, agent_id=None, role="primary")
    # A racing dispatch past the pre-check is refused by the writer before any write.
    with pytest.raises(ReferenceDispatchRefused, match="reference_primary_dispatch_limit_reached"):
        await _dispatch(runtime, case, _runner(case, "too many"), task="one more", call_id="call-extra")
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert len(_rows(case, "chat_reference_primary_continuations")) == PRIMARY_DISPATCH_LIMIT - 1


async def test_database_refuses_a_continuation_past_the_limit(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    for seq in range(1, PRIMARY_DISPATCH_LIMIT + 1):
        assert (await _dispatch(runtime, case, _runner(case, f"answer {seq}"), task=f"step {seq}", call_id=f"call-{seq}")).ok
    row = _rows(case, "chat_reference_primary_continuations")[-1]
    with sqlite3.connect(case.fixture.db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO chat_reference_primary_continuations(turn_id,dispatch_seq,execution_id,created_generation,owner_id,call_id,"
            "subagent_id,task_hash,state,retention_state,started_at) VALUES (?,?,?,?,?,?,?,?,'started','available',?)",
            (
                row["turn_id"],
                PRIMARY_DISPATCH_LIMIT + 1,
                row["execution_id"],
                1,
                row["owner_id"],
                "call-db",
                row["subagent_id"],
                row["task_hash"],
                row["started_at"],
            ),
        )


async def test_completed_primary_cannot_reopen_without_a_continuation_record(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "done"), task="work", call_id="call-1")).ok
    with sqlite3.connect(case.fixture.db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE turn_agent_execution SET status='running',ended_at=NULL WHERE turn_id='t_reference'")


async def test_failed_continuation_keeps_the_completed_primary(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    second = await _dispatch(runtime, case, _runner(case, ""), task="more work", call_id="call-2")
    assert not second.ok
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    (continuation,) = _rows(case, "chat_reference_primary_continuations")
    assert continuation["state"] == "failed" and continuation["failure_code"]
    # The next continuation still reads the last successful answer.
    runner = _runner(case, "third answer")
    assert (await _dispatch(runtime, case, runner, task="retry the rest", call_id="call-3")).ok
    task = _last_task(runner)
    assert task["previous_dispatch_result"]["answer"] == {"text": "first answer"}


async def test_failed_primary_cannot_be_continued(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert not (await _dispatch(runtime, case, _runner(case, ""), task="work", call_id="call-1")).ok
    primary = await runtime.validate_lead()
    assert primary.binding.status == "failed"
    with pytest.raises(ValueError, match="reference_primary_not_completed"):
        await runtime.binding_for_delegation(profile=primary.binding.runtime_profile_name, agent_id=None, role="primary")
    assert not _rows(case, "chat_reference_primary_continuations")


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_continuation_keeps_receiver_and_role_rules(lead_case):
    runtime = await _runtime(lead_case)
    assert (await _dispatch(runtime, lead_case, _runner(lead_case, "done"), task="work", call_id="call-1")).ok
    primary = await runtime.validate_lead()
    for args, code in (
        ({"profile": primary.binding.runtime_profile_name, "agent_id": "another-id", "role": "primary"}, "reference_receiver_changed"),
        ({"profile": "another-agent", "agent_id": None, "role": "primary"}, "reference_receiver_changed"),
        ({"profile": "another-agent", "agent_id": None, "role": None}, "reference_delegation_role_required"),
    ):
        with pytest.raises(ValueError, match=code):
            await runtime.binding_for_delegation(**args)
    assert await runtime.binding_for_delegation(profile=primary.binding.runtime_profile_name, agent_id=None, role="helper") is None
    assert not _rows(lead_case, "chat_reference_primary_continuations")


async def _lead_turn(case, monkeypatch, script: list[tuple[str, str]], *, production_gates: bool = False, responder=None):
    """Run the real Lead loop; each scripted reply answers the next model request in order."""
    from functools import partial

    import anyio
    import httpx2
    import respx
    from bowhead_core.agents.lead_orchestrator import LeadRuntime, run_turn
    from bowhead_core.agents.turn_context import SerializableTurnContext
    from bowhead_core.subagent import PROFILES, SubagentProfile, register_subagent_tool
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from test_skill_model_invocation import adapter_for, router_for

    from bowhead_router import AgentBinding

    runtime = getattr(case, "runtime", None) or await _runtime(case)
    primary = await runtime.validate()
    ctx = _ctx(case)
    ctx.session_store = case.store
    ctx._reference_runtime = runtime
    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    if production_gates:
        monkeypatch.delenv("BOWHEAD_LEAD_POST_PEER_BREAK", raising=False)
        monkeypatch.delenv("BOWHEAD_LEAD_FINAL_ANSWER_GATE", raising=False)
    else:
        monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")
        monkeypatch.setenv("BOWHEAD_LEAD_FINAL_ANSWER_GATE", "0")
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead", llm_agent_type="lead", tool_whitelist=("subagent_spawn",), system_prompt="Delegate then summarize.", max_iterations=8
        ),
    )
    adapter = adapter_for("openai")
    router = router_for(adapter)
    for agent_type in ("lead", "compute"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("primary", "large")))
    requests: list[dict] = []
    replies = list(script)

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        kind, value = responder(body) if responder is not None else replies.pop(0)
        if kind == "slow_text":
            # Keep this run open so a sibling call in the same round overlaps it.
            await anyio.sleep(0.5)
            kind = "text"
        reason = "stop"
        message: dict = {"role": "assistant", "content": value}
        if kind in ("spawn", "spawn2"):
            reason = "tool_calls"
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": index,
                        "id": f"dispatch-{len(requests)}-{index}",
                        "type": "function",
                        "function": {
                            "name": "subagent_spawn",
                            "arguments": json.dumps(
                                {"profile": primary.binding.runtime_profile_name, "task": value, "delegation_role": "primary"}, ensure_ascii=False
                            ),
                        },
                    }
                    for index in range(2 if kind == "spawn2" else 1)
                ],
            }
        elif kind == "return":
            reason = "tool_calls"
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"return-{len(requests)}",
                        "type": "function",
                        "function": {"name": "subagent_return", "arguments": value},
                    }
                ],
            }
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        if not body.get("stream"):
            return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})
        events = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
        return httpx2.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    registry = build_default_registry()
    register_subagent_tool(registry)
    register_subagent_return_tool(registry)
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    log = TurnLogStore(case.fixture.db_path)
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(log), persist_before_live=True)
    ctx.subagent_runner = runner
    ctx.parent_bus = bus
    try:
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await run_turn(
                SerializableTurnContext(**case.scope.identity(), turn_id="t_reference", role="user"),
                primary.source.user_text,
                runtime=LeadRuntime(
                    router=router,
                    tool_registry=registry,
                    parent_bus=bus,
                    parent_ctx=ctx,
                    subagent_runner=runner,
                    session_store=case.store,
                    persist_turn_state=partial(runtime.persist_turn_state, store=case.store),
                ),
            )
    finally:
        await adapter.close()
    case.unused_replies = replies
    if not production_gates:
        assert not replies, "every scripted model reply is consumed"
    return result, bus, log, requests


def _spawn_results(result) -> list[dict]:
    return [inv.result for inv in result.tool_invocations if inv.tool_name == "subagent_spawn"]


# A system primary (like @Coach) returns to Lead; plain Team text is delivered directly.
@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_lead_continues_the_primary_and_live_persisted_replayed_agree(lead_case, monkeypatch):
    case = lead_case
    result, bus, log, requests = await _lead_turn(
        case,
        monkeypatch,
        [
            ("spawn", "Draft the alignment card"),
            ("text", "Card drafted."),
            ("spawn", "Continue with the follow-up work"),
            ("text", "Follow-up finished."),
            ("text", "Card drafted and follow-up finished."),
        ],
    )
    assert result.status == "completed"
    assert "follow-up finished" in result.final_answer.lower()
    executions = _rows(case, "turn_agent_execution")
    assert len(executions) == 1 and executions[0]["status"] == "completed"
    (continuation,) = _rows(case, "chat_reference_primary_continuations")
    assert continuation["state"] == "completed" and continuation["dispatch_seq"] == 2
    # The continuation request carried the first dispatch's result.
    assert "Card drafted." in json.dumps(requests[3], ensure_ascii=False)
    stored = await case.store.get_turn("t_reference")
    assert stored is not None and stored.status == "completed" and stored.final_answer == result.final_answer
    subagent_id = "sub_" + hashlib.sha256(executions[0]["id"].encode()).hexdigest()[:20]
    live = [event for event in bus._events if event.event_type == "peer_dispatched"]
    replay = await log.list_events("t_reference", event_types=("peer_dispatched",))
    assert [event.payload["subagent_id"] for event in live] == [event.payload["subagent_id"] for event in replay] == [subagent_id] * 2
    assert [event.payload["call_id"] for event in live] == [event.payload["call_id"] for event in replay]
    live_final = [
        event.payload["text"] for event in bus._events if event.event_type == "final_answer" and event.payload.get("parent_agent_id") is None
    ]
    replay_final = [
        event.payload["text"]
        for event in await log.list_events("t_reference", event_types=("final_answer",))
        if event.payload.get("parent_agent_id") is None
    ]
    assert live_final == replay_final == [result.final_answer]
    done = await log.list_events("t_reference", event_types=("done",))
    assert [item.payload["status"] for item in done] == ["completed"]
    # Each run has its own runner start/finish and summary, identical live and replayed.
    for event_type, expected in (("agent_spawned", [None, "completed", None, "completed"]), ("lead.subagent_summary", ["succeeded"] * 2)):
        live_rows = [
            (e.payload.get("subagent_id"), e.payload.get("status"))
            for e in bus._events
            if e.event_type == event_type and e.payload.get("subagent_id") == subagent_id
        ]
        replay_rows = [
            (e.payload.get("subagent_id"), e.payload.get("status"))
            for e in await log.list_events("t_reference", event_types=(event_type,))
            if e.payload.get("subagent_id") == subagent_id
        ]
        assert live_rows == replay_rows
        assert [status for _, status in live_rows] == expected


# A system primary (like @Coach) returns to Lead; plain Team text is delivered directly.
@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_lead_past_the_limit_keeps_the_completed_answer(lead_case, monkeypatch):
    case = lead_case
    script: list[tuple[str, str]] = []
    for seq in range(1, PRIMARY_DISPATCH_LIMIT + 1):
        script += [("spawn", f"step {seq}"), ("text", f"Result {seq}.")]
    script += [("spawn", "one more step"), ("text", "Here is what the Agent finished; @-mention it again to continue.")]
    result, bus, log, _ = await _lead_turn(case, monkeypatch, script)
    assert result.status == "completed"
    assert result.final_answer == "Here is what the Agent finished; @-mention it again to continue."
    refused = _spawn_results(result)[-1]
    assert refused["ok"] is False and refused["error_code"] == "reference_primary_dispatch_limit_reached"
    assert "@-mention" in refused["error"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert len(_rows(case, "chat_reference_primary_continuations")) == PRIMARY_DISPATCH_LIMIT - 1
    assert await log.list_events("t_reference", event_types=("turn_failed",)) == []
    # The refused step is a non-transient, localized tool failure in live and replayed events.
    live = [e.payload for e in bus._events if e.event_type == "step_done" and e.payload.get("tool_name") == "subagent_spawn"]
    replay = [e.payload for e in await log.list_events("t_reference", event_types=("step_done",)) if e.payload.get("tool_name") == "subagent_spawn"]
    assert live[-1]["error"] == replay[-1]["error"]
    assert live[-1]["error"]["is_transient"] is False
    assert "reference_primary_dispatch_limit_reached" not in json.dumps(replay, ensure_ascii=False)


# A system primary (like @Coach) returns to Lead; plain Team text is delivered directly.
@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_lead_with_a_failed_primary_ends_with_a_specific_failure(lead_case, monkeypatch):
    case = lead_case
    result, _, log, _ = await _lead_turn(
        case,
        monkeypatch,
        [("spawn", "Do the work"), ("text", ""), ("spawn", "Try again"), ("text", "I could not get a result.")],
    )
    assert result.status == "failed"
    assert result.error == "reference_primary_incomplete"
    # Lead's second dispatch was refused, not run: no continuation exists.
    assert not _rows(case, "chat_reference_primary_continuations")
    spawns = [e.payload for e in await log.list_events("t_reference", event_types=("step_done",)) if e.payload.get("tool_name") == "subagent_spawn"]
    assert spawns[-1]["error"]["type"] == "reference_dispatch_blocked" and spawns[-1]["error"]["is_transient"] is False
    assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"
    failed = await log.list_events("t_reference", event_types=("turn_failed",))
    assert [event.payload["reason"] for event in failed] == ["reference_primary_incomplete"]


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_continuation_keeps_the_selected_skill_scope_of_the_first_dispatch(receiver_case):
    from dataclasses import replace

    import httpx2
    import respx
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from cryptography.fernet import Fernet
    from test_skill_model_invocation import adapter_for, router_for
    from test_skill_start_admission import candidate

    from bowhead_router import AgentBinding

    fixture = receiver_case
    await candidate(fixture, agent_first=True)
    case = fixture.case
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="worker-a",
    )
    runtime = ReferencePrimaryRuntime(
        scope=case.scope, turn_id="t_reference", owner_id="worker-a", bindings=case.bindings, cipher=cipher, skill_sources=fixture.sources
    )
    adapter = adapter_for("openai")
    router = router_for(adapter)
    router.bind(AgentBinding(agent_type="compute", primary=("primary", "large")))
    wires: list[str] = []

    def respond(request):
        wires.append(request.content.decode())
        message = {"role": "assistant", "content": f"answer {len(wires)}"}
        return httpx2.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
        )

    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), team_subagent_store=case.fixture.users)
    try:
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            first = await _dispatch(runtime, case, runner, task="first part", call_id="call-1")
            second = await _dispatch(runtime, case, runner, task="second part", call_id="call-2")
    finally:
        await adapter.close()
    assert first.ok and second.ok
    assert len(wires) == 2
    # Both dispatches carry exactly the selected Skill material, and nothing else changes scope.
    for wire in wires:
        assert "BODY_method" in wire and "BODY_support" in wire
    assert "answer 1" in wires[1]
    invocations = _rows(case, "chat_reference_model_invocations")
    assert len(invocations) == 2 and {row["execution_id"] for row in invocations} == {_rows(case, "turn_agent_execution")[0]["id"]}
    assert {row["status"] for row in invocations} == {"completed"}
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert [row["state"] for row in _rows(case, "chat_reference_primary_continuations")] == ["completed"]


async def test_interrupted_continuation_returns_the_primary_to_completed(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    runner = _runner(case)

    async def broken_run(**_kwargs):
        raise RuntimeError("worker crashed")

    runner.run = broken_run  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="worker crashed"):
        await _dispatch(runtime, case, runner, task="more work", call_id="call-2")
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    (continuation,) = _rows(case, "chat_reference_primary_continuations")
    assert (continuation["state"], continuation["failure_code"]) == ("failed", "reference_continuation_interrupted")


async def test_turn_settlement_closes_a_continuation_left_open(lead_case, monkeypatch):
    import aiosqlite
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_runtime_store import settle_reference_primary_in_connection

    from bowhead_core import chat_reference_runtime

    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    runner = _runner(case)

    async def broken_run(**_kwargs):
        raise RuntimeError("worker lost")

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    runner.run = broken_run  # type: ignore[method-assign]
    # The worker dies before it can settle its own continuation.
    monkeypatch.setattr(chat_reference_runtime, "close_primary_continuation_in_connection", unavailable)
    with pytest.raises(RuntimeError, match="worker lost"):
        await _dispatch(runtime, case, runner, task="more work", call_id="call-2")
    monkeypatch.undo()
    assert _rows(case, "turn_agent_execution")[0]["status"] == "running"
    async with open_shared_ctx(case.fixture.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        await settle_reference_primary_in_connection(db, turn_id="t_reference", status="failed", now="2026-10-20T00:00:00+00:00")
        await db.commit()
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    continuation = _rows(case, "chat_reference_primary_continuations")[0]
    assert (continuation["state"], continuation["failure_code"]) == ("failed", "reference_worker_stopped")


async def test_a_call_admitted_as_the_first_dispatch_never_becomes_a_continuation(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    # A sibling Lead call that saw the reservation and lost the race still invokes
    # once; it is refused, not a Turn-ending durability failure.
    with pytest.raises(ReferenceDispatchRefused, match="reference_primary_running"):
        await runtime.run(
            runner=_runner(case, "duplicate"),
            parent_ctx=_ctx(case),
            parent_bus=TurnEventBus(),
            delegation_task="work",
            delegation_call_id="call-sibling",
            expect_first_dispatch=True,
        )
    assert not _rows(case, "chat_reference_primary_continuations")
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


async def test_concurrent_continuations_run_once_and_refuse_the_sibling(lead_case):
    import asyncio

    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    import anyio

    def slow_runner(call):
        runner = _runner(case, f"answer {call}")
        original = runner.run

        async def run(**kwargs):
            # Keep this continuation running while the sibling reaches the writer.
            await anyio.sleep(0.3)
            return await original(**kwargs)

        runner.run = run  # type: ignore[method-assign]
        return runner

    results = await asyncio.gather(
        *(_dispatch(runtime, case, slow_runner(call), task="more", call_id=call) for call in ("call-a", "call-b")),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) and r.ok for r in results) == 1
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(refused) == 1 and isinstance(refused[0], ReferenceDispatchRefused)
    assert [row["state"] for row in _rows(case, "chat_reference_primary_continuations")] == ["completed"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


# A system primary (like @Coach) returns to Lead; plain Team text is delivered directly.
@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_parallel_continuation_spawns_in_one_lead_round_keep_the_turn(lead_case, monkeypatch):
    case = lead_case
    lead_calls = []

    def responder(body):
        # Lead requests advertise subagent_spawn; the primary's do not.
        if "subagent_spawn" not in json.dumps(body.get("tools", [])):
            return ("slow_text", "Primary result.")
        lead_calls.append(body)
        return [("spawn", "Draft the card"), ("spawn2", "Continue the follow-up"), ("text", "Card drafted and follow-up finished.")][
            len(lead_calls) - 1
        ]

    result, _, log, _ = await _lead_turn(case, monkeypatch, [], responder=responder)
    # Both calls run in one parallel batch; the Turn completes with the answer.
    assert result.status == "completed"
    assert result.final_answer == "Card drafted and follow-up finished."
    continuations = _rows(case, "chat_reference_primary_continuations")
    # The primary's run is held open, so the sibling overlaps it and is refused
    # (by the pre-check or by the writer), never a Turn-ending abort.
    assert [row["state"] for row in continuations] == ["completed"]
    spawns = [e.payload for e in await log.list_events("t_reference", event_types=("step_done",)) if e.payload.get("tool_name") == "subagent_spawn"]
    assert [p["error"]["type"] for p in spawns if p.get("error")] == ["reference_dispatch_blocked"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert await log.list_events("t_reference", event_types=("turn_failed",)) == []


@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_continuation_with_production_lead_gates_completes(lead_case, monkeypatch):
    case = lead_case
    result, _, log, _ = await _lead_turn(
        case,
        monkeypatch,
        [
            ("spawn", "Draft the card"),
            ("text", "Card drafted."),
            ("spawn", "Continue the follow-up"),
            ("text", "Follow-up finished."),
            ("text", "Card drafted and follow-up finished."),
        ],
        production_gates=True,
    )
    assert result.status == "completed" and result.final_answer
    assert await log.list_events("t_reference", event_types=("turn_failed",)) == []
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    continuations = _rows(case, "chat_reference_primary_continuations")
    # The gates may end the Turn after the first result; any continuation that ran settled.
    assert all(row["state"] == "completed" for row in continuations)
    if continuations:
        assert not case.unused_replies


async def test_payload_only_result_reaches_the_continuation(lead_case):
    from bowhead_core.subagent import SubagentResult

    case = lead_case
    runtime = await _runtime(case)
    payload_runner = _runner(case)

    async def payload_only(**kwargs):
        # A structured return (for example a card) with no prose answer.
        return SubagentResult(
            profile=kwargs["profile_name"],
            subagent_id=kwargs["pre_minted_subagent_id"],
            ok=True,
            final_answer="",
            payload={"card": {"id": "card_1"}},
        )

    payload_runner.run = payload_only  # type: ignore[method-assign]
    first = await _dispatch(runtime, case, payload_runner, task="draft a card", call_id="call-1")
    assert first.ok and first.payload == {"card": {"id": "card_1"}}
    runner = _runner(case, "continued from the card")
    assert (await _dispatch(runtime, case, runner, task="continue", call_id="call-2")).ok
    previous = _last_task(runner)["previous_dispatch_result"]
    assert previous["available"] is True and "answer" not in previous
    assert json.loads(previous["payload"]["text"]) == {"card": {"id": "card_1"}}
    # A later payload-only continuation does not hide the earlier results either.
    assert (await _dispatch(runtime, case, payload_runner, task="next card", call_id="call-3")).ok
    assert [row["state"] for row in _rows(case, "chat_reference_primary_continuations")] == ["completed", "completed"]


async def test_cancelled_continuation_returns_the_primary_to_completed(lead_case):
    import anyio

    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    runner = _runner(case)
    started = anyio.Event()

    async def hanging_run(**_kwargs):
        started.set()
        await anyio.sleep_forever()

    runner.run = hanging_run  # type: ignore[method-assign]
    async with anyio.create_task_group() as group:
        group.start_soon(lambda: _dispatch(runtime, case, runner, task="more work", call_id="call-2"))
        await started.wait()
        group.cancel_scope.cancel()
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    continuation = _rows(case, "chat_reference_primary_continuations")[0]
    assert (continuation["state"], continuation["failure_code"]) == ("failed", "reference_continuation_interrupted")


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_lost_authority_fails_only_the_continuation(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    runner = _runner(case, "private continuation output")
    original = runner.run

    async def revoke_after_run(**kwargs):
        result = await original(**kwargs)
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE agent_assignment SET access_state='revoked'")
        return result

    runner.run = revoke_after_run  # type: ignore[method-assign]
    second = await _dispatch(runtime, case, runner, task="more work", call_id="call-2")
    assert not second.ok and second.final_answer == "" and second.payload is None
    continuation = _rows(case, "chat_reference_primary_continuations")[0]
    assert continuation["state"] == "failed" and continuation["failure_code"] == second.error
    assert continuation["output_ciphertext"] is None
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


async def test_reopen_and_close_keep_the_first_completion_time(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    first = _rows(case, "turn_agent_execution")[0]
    assert (await _dispatch(runtime, case, _runner(case, "second answer"), task="more work", call_id="call-2")).ok
    after = _rows(case, "turn_agent_execution")[0]
    assert (after["started_at"], after["ended_at"]) == (first["started_at"], first["ended_at"])
    continuation = _rows(case, "chat_reference_primary_continuations")[0]
    assert continuation["primary_ended_at"] == first["ended_at"]
    assert continuation["started_at"] <= continuation["ended_at"]


async def test_settlement_closes_an_open_continuation_for_every_outcome(lead_case, monkeypatch):
    import aiosqlite
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_runtime_store import close_primary_continuation_in_connection

    from bowhead_core import chat_reference_runtime

    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    runner = _runner(case)

    async def broken_run(**_kwargs):
        raise RuntimeError("worker lost")

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    runner.run = broken_run  # type: ignore[method-assign]
    monkeypatch.setattr(chat_reference_runtime, "close_primary_continuation_in_connection", unavailable)
    with pytest.raises(RuntimeError, match="worker lost"):
        await _dispatch(runtime, case, runner, task="more work", call_id="call-2")
    monkeypatch.undo()
    # Another path already closed the execution; the sweep still closes the row cleanly.
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE turn_agent_execution SET status='failed',ended_at='2026-10-20T00:00:00+00:00',failure_code='x' WHERE turn_id='t_reference'"
        )
    async with open_shared_ctx(case.fixture.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        await close_primary_continuation_in_connection(
            db, turn_id="t_reference", dispatch_seq=None, output=None, failure_code="reference_worker_stopped"
        )
        await db.commit()
    assert [row["state"] for row in _rows(case, "chat_reference_primary_continuations")] == ["failed"]


async def test_clarification_settlement_closes_an_open_continuation_first(lead_case, monkeypatch):
    import aiosqlite
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_runtime_store import settle_reference_primary_in_connection

    from bowhead_core import chat_reference_runtime

    case = lead_case
    runtime = await _runtime(case)
    assert (await _dispatch(runtime, case, _runner(case, "first answer"), task="work", call_id="call-1")).ok
    runner = _runner(case)

    async def broken_run(**_kwargs):
        raise RuntimeError("worker lost")

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    runner.run = broken_run  # type: ignore[method-assign]
    monkeypatch.setattr(chat_reference_runtime, "close_primary_continuation_in_connection", unavailable)
    with pytest.raises(RuntimeError):
        await _dispatch(runtime, case, runner, task="more work", call_id="call-2")
    monkeypatch.undo()
    async with open_shared_ctx(case.fixture.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        # A completed primary cannot pause; the open continuation is closed first.
        with pytest.raises(ValueError, match="reference_continuation_not_supported"):
            await settle_reference_primary_in_connection(db, turn_id="t_reference", status="clarification_pending", now="2026-10-20T00:00:00+00:00")
        await db.commit()
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert [row["state"] for row in _rows(case, "chat_reference_primary_continuations")] == ["failed"]


async def test_continuation_after_a_same_turn_clarification_resume(lead_case, monkeypatch):
    from bowhead_core.clarification_store import ClarificationStore
    from test_reference_lead_delegation import _normal_lead, _runtime_for_resume

    case = lead_case
    await _normal_lead(case, monkeypatch, clarification_tool=True)
    card = _rows(case, "clarification_requests")[0]
    assert (await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")) is not None
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    resumed = await _runtime_for_resume(case)
    await resumed.claim_bi_resume(clarification_id=card["request_id"])
    ctx = _ctx(case)
    first = await resumed.run(
        runner=_runner(case, "resumed answer"), parent_ctx=ctx, parent_bus=TurnEventBus(), delegation_task="work", delegation_call_id="call-1"
    )
    assert first.ok
    second = await resumed.run(
        runner=_runner(case, "continued answer"), parent_ctx=ctx, parent_bus=TurnEventBus(), delegation_task="more", delegation_call_id="call-2"
    )
    assert second.ok and second.final_answer == "continued answer"
    (continuation,) = _rows(case, "chat_reference_primary_continuations")
    assert (continuation["created_generation"], continuation["state"]) == (2, "completed")
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
