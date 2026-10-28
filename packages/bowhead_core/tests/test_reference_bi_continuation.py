"""Actual reference worker -> durable BI card -> same-Turn native continuation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.events import EventBusRegistry
from bowhead_core.language_context import LanguageContext
from bowhead_core.orchestrator import Orchestrator
from bowhead_core.skill_read_resolution import build_reference_skill_sources
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.types import TurnRequest
from bowhead_gateway.turn_service import (
    _prepare_reference_bi_resume,
    admit_legacy_chat_execution,
    build_resume_turn_request,
    spawn_resume_turn,
    spawn_turn,
)
from cryptography.fernet import Fernet
from test_reference_continuation_boundary import workspace_question
from test_skill_model_invocation import adapter_for, router_for
from test_skill_reference_runtime import claimed_case as _claimed_case
from test_skill_reference_runtime import receiver_case as _receiver_case
from test_skill_reference_runtime import rows
from test_skill_reference_runtime import start_case as _start_case

claimed_case, receiver_case, start_case = _claimed_case, _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    # A serial case (setup+call) took up to ~55s; full xdist load multiplies this, so the global 120s timeout is insufficient.
    pytest.mark.timeout(600),
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["agent_baseline", "canonical_input"], indirect=True),
]


def answer_response(request):
    usage = {"prompt_tokens": 13, "completion_tokens": 3}
    if json.loads(request.content).get("stream"):
        events = [
            {"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
        ]
        content = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx2.Response(200, text=content, headers={"content-type": "text/event-stream"})
    return httpx2.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}], "usage": usage})


@pytest.fixture
async def continuation_case(claimed_case, monkeypatch):
    base = claimed_case
    case = base.fixture.case
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    adapter = adapter_for("openai")
    router = router_for(adapter)
    registry = build_default_registry()
    events = EventBusRegistry(default_log_sink=OrderedTurnLogSink(TurnLogStore(base.path)))
    clarifications = ClarificationStore(str(base.path))
    await clarifications.initialize()
    orchestrator = Orchestrator(
        router=router,
        datarc=None,
        session_store=case.store,
        tool_registry=registry,
        event_registry=events,
        user_store=case.fixture.users,
        clarification_store=clarifications,
    )
    orchestrator.dispatch_owner_id = "mixed-worker"
    orchestrator.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    orchestrator.reference_skill_sources_factory = lambda scope: build_reference_skill_sources(
        base.path, scope=scope, builtin_index=base.fixture.sources.builtin_index, builtin_root=base.fixture.root
    )
    request = TurnRequest(
        **case.scope.identity(),
        member_uid=case.fixture.member_id,
        turn_id="t_reference",
        text=case.args["user_text"],
        reference_scope=case.scope,
        planning_mode="off",
        team_router=router,
        language_context=LanguageContext(ui_language="zh-CN", output_language="zh-CN"),
        datarc_client=object(),
        accessible_datarc_workspaces=({"id": "project_a", "name": "A"}, {"id": "project_b", "name": "B"}),
    )
    try:
        yield SimpleNamespace(
            base=base, path=base.path, case=case, orchestrator=orchestrator, request=request, clarifications=clarifications, adapter=adapter
        )
    finally:
        from bowhead_gateway.routes.chat import _inflight_tasks

        pending = [task for task in _inflight_tasks if task.get_name().endswith(":t_reference") and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await adapter.close()


async def settle_worker(task):
    from bowhead_gateway.routes.chat import _inflight_tasks

    monitor = next(t for t in _inflight_tasks if t.get_name() == "turn_dispatch_lease:t_reference")
    await asyncio.wait_for(asyncio.gather(task, monitor), timeout=30)


async def pause(case):
    bus = await admit_legacy_chat_execution(case.orchestrator, case.request)
    task = await spawn_turn(
        case.orchestrator, case.request, team_id=case.request.team_id, admitted_legacy_bus=bus, reference_skill_sources=case.base.fixture.sources
    )
    await settle_worker(task)
    # This is the result consumed by Gateway billing/metrics, independently of
    # the already persisted Turn totals used by the stream/replay projection.
    assert (task.result().tokens_input, task.result().tokens_output) == (100, 20)
    saved = rows(case, "turn_events")
    cards = [json.loads(row["payload_json"]) for row in saved if row["event_type"] == "clarification_required"]
    assert len(cards) == 1
    assert (await case.case.store.get_turn("t_reference")).status == "clarification_pending"
    assert rows(case, "turn_agent_execution")[0]["status"] == "running"
    assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "ready"
    assert await case.case.store.is_turn_resume_ready("t_reference")
    return await case.clarifications.get(cards[0]["clarification_id"])


async def buffer_plain_steer(case, text):
    """模拟另一个请求在模型调用进行中写入普通 steer。

    必须在线程里写：respx 的 side_effect 跑在 Turn worker 所在的事件循环线程上。
    若在这里同步 ``sqlite3.connect().execute()``，恰逢租约心跳等 aiosqlite 写事务
    （BEGIN 与 COMMIT 之间要回到事件循环）持有写锁时，事件循环被本调用堵住、对方
    永远提交不了，sqlite 忙等 5 秒后抛 ``database is locked``。worker 随之按「物理调用
    结果未知」失败；默认 structlog 用 rich 展开整条调用栈的局部变量渲染这条异常，
    耗时远超用例本身，表现为整文件挂死。生产中 steer 来自另一个请求，同样不会阻塞
    worker 的事件循环。
    """

    def write():
        with sqlite3.connect(case.path, timeout=30) as db:
            db.execute(
                "UPDATE sessions SET steer_buffer_json=? WHERE id=? AND active_turn_id=?",
                (
                    json.dumps(
                        [{"text": text, "attachment_ids": [], "attachment_refs": []}],
                        ensure_ascii=False,
                    ),
                    case.request.session_id,
                    case.request.turn_id,
                ),
            )

    await asyncio.to_thread(write)


async def resolve_request(case, question):
    resolved, won = await case.clarifications.resolve_once(question.request_id, "project_b")
    assert won
    request = build_resume_turn_request(resolved, datarc_client=object(), team_router=case.request.team_router)
    request.active_datarc_project = "project_b"
    request.accessible_datarc_workspaces = case.request.accessible_datarc_workspaces
    return request


async def test_gateway_resumes_the_original_agent_without_replaying_its_first_call(continuation_case):
    case = continuation_case
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return workspace_question(request)
        return answer_response(request)

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        question = await pause(case)
        before = rows(case, "chat_reference_resolutions")
        execution = rows(case, "turn_agent_execution")[0]["id"]
        # Resume must consume retained package bytes, even when the original
        # live files have disappeared since the first model request.
        for path in case.base.fixture.root.rglob("*.md"):
            path.unlink()
        request = await resolve_request(case, question)
        from bowhead_core.chat_reference_projection import read_reference_projection

        resolved_facts = await read_reference_projection(
            case.path,
            scope=case.case.scope,
            turn_id="t_reference",
            visible_resource_identities=frozenset(),
            visibility_complete=True,
        )
        assert resolved_facts["continuations"][0]["model_invocation_ids"] == []
        floor = max(row["seq"] for row in rows(case, "turn_events"))
        spawned = await spawn_resume_turn(
            case.orchestrator, request, team_id=request.team_id, initial_seq=floor, resume_notice={"clarification_id": question.request_id}
        )
        assert spawned.execution_generation == 2
        await settle_worker(spawned.task)
        assert route.call_count == 2
    messages = bodies[1]["messages"]
    calls = [message for message in messages if message.get("tool_calls")]
    assert len(calls) == 1 and calls[0]["tool_calls"][0]["id"] == "select-context"
    assert any(message.get("tool_call_id") == "select-context" and "project_b" in message["content"] for message in messages)
    assert "Continue the original task with the workspace selected in the tool context." in messages[-1]["content"]
    turn = await case.case.store.get_turn("t_reference")
    assert (turn.status, turn.final_answer, turn.tokens_input, turn.tokens_output) == ("completed", "OK", 113, 23)
    assert rows(case, "chat_reference_resolutions") == before
    assert rows(case, "turn_agent_execution")[0]["id"] == execution
    assert rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
    invocations = rows(case, "chat_reference_model_invocations")
    assert sorted((row["created_generation"], row["status"]) for row in invocations) == [(1, "completed"), (2, "completed")]
    assert [row["kind"] for row in rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
    assert not await case.case.store.is_turn_resume_ready("t_reference")
    from bowhead_core.skill_model_status import read_reference_model_status_in_connection

    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN")
        status = await read_reference_model_status_in_connection(
            db, scope=ReferenceRequestScope(**case.case.scope.model_dump(exclude={"policy_epoch"})), turn_id="t_reference"
        )
    assert (status.invocation_count, status.completed_count, status.input_tokens, status.output_tokens) == (2, 2, 113, 23)
    facts = await read_reference_projection(
        case.path,
        scope=case.case.scope,
        turn_id="t_reference",
        visible_resource_identities=frozenset(),
        visibility_complete=True,
    )
    continuation = rows(case, "chat_reference_bi_continuations")[0]
    assert facts["continuations"] == [
        {
            "contract": "reference-bi-workspace.v1",
            "clarification_id": continuation["clarification_id"],
            "execution_id": execution,
            "status": "resolved",
            "resume_generation": 2,
            "source_turn_id": "t_reference",
            "association_basis": "persisted_binding",
            "model_invocation_ids": [row["invocation_id"] for row in invocations if row["created_generation"] == 2],
            "task_physical_receipt_ids": [
                row["id"]
                for row in rows(case, "llm_usage_events")
                if row["turn_id"] == "t_reference" and row["receipt_json"] and json.loads(row["receipt_json"])["generation"] == 2
            ],
        }
    ]
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT active_turn_id FROM sessions WHERE id='s_reference'").fetchone() == (None,)


async def test_bi_resume_restores_canonical_source_attachments(continuation_case, monkeypatch):
    case = continuation_case
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)

    import bowhead_core.session_store as session_store

    original = session_store._read_chat_turn_user_source_in_tx

    async def attached_source(*args, **kwargs):
        source = await original(*args, **kwargs)
        assert source is not None
        return replace(source, attachment_ids=("art_resume",))

    monkeypatch.setattr(session_store, "_read_chat_turn_user_source_in_tx", attached_source)

    sources = await _prepare_reference_bi_resume(case.orchestrator, request)

    assert sources is not None
    assert request.attachments == ["art_resume"]


async def test_bi_pause_defers_plain_steer_until_resumed_generation_finishes(continuation_case):
    case = continuation_case
    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            await buffer_plain_steer(case, "继续比较华东区")
            return workspace_question(request)
        return answer_response(request)

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        question = await pause(case)
        with sqlite3.connect(case.path) as db:
            active, buffer_json = db.execute(
                "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
                (case.request.session_id,),
            ).fetchone()
            assert active == case.request.turn_id
            assert [item["text"] for item in json.loads(buffer_json)] == ["继续比较华东区"]
            assert db.execute(
                "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (case.request.turn_id,),
            ).fetchone() == ("terminal",)

        request = await resolve_request(case, question)
        spawned = await spawn_resume_turn(
            case.orchestrator,
            request,
            team_id=request.team_id,
            initial_seq=max(row["seq"] for row in rows(case, "turn_events")),
            resume_notice={"clarification_id": question.request_id},
        )
        assert spawned.execution_generation == 2
        await settle_worker(spawned.task)
        follow_up = await case.case.store.get_chat_follow_up(
            parent_turn_id=case.request.turn_id,
        )
        assert follow_up is not None
        source = await case.case.store.get_chat_turn_user_source(
            turn_id=follow_up[0],
            user_id=case.request.user_id,
            workspace_id=case.request.workspace_id,
        )
        assert source is not None and source.user_text == "继续比较华东区"
        assert route.call_count == 2


async def test_bi_pause_owner_loss_retains_plain_steer_for_resume(continuation_case):
    case = continuation_case

    async def respond(request):
        await buffer_plain_steer(case, "补充北区")
        return workspace_question(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        question = await pause(case)
    with sqlite3.connect(case.path) as db:
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET state='spawned',owner_id='lost-worker',lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id=?",
            (case.request.turn_id,),
        )
    import os

    from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher

    assert case.request.turn_id in await case.case.store.recover_expired_chat_spawn_dispatches(
        reference_store=ChatReferenceStore(
            ReferenceCipher(os.environ["BOWHEAD_FERNET_KEY"]),
        ),
    )
    with sqlite3.connect(case.path) as db:
        active, buffer_json = db.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
            (case.request.session_id,),
        ).fetchone()
        assert active == case.request.turn_id
        assert [item["text"] for item in json.loads(buffer_json)] == ["补充北区"]
        assert db.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (case.request.turn_id,),
        ).fetchone() == ("terminal",)
    await resolve_request(case, question)
    assert (
        await case.case.store.advance_turn_execution_generation(
            turn_id=case.request.turn_id,
            reference_clarification_id=question.request_id,
        )
        == 2
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "retention",
        "session_delete",
        "clarification_expired",
        "clarification_dismissed",
        "user_rephrase",
        "resolved_answer_corrupt",
        "checkpoint_corrupt",
    ],
)
async def test_bi_deferred_plain_steer_is_settled_when_pause_becomes_unrecoverable(
    continuation_case,
    boundary,
):
    case = continuation_case

    async def respond(request):
        await buffer_plain_steer(case, "补充已受理")
        return workspace_question(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        question = await pause(case)

    if boundary == "session_delete":
        with sqlite3.connect(case.path) as db:
            assert (
                db.execute(
                    "UPDATE sessions SET deleted_at='2026-09-16T00:00:00+00:00' WHERE id=? AND deleted_at IS NULL",
                    (case.request.session_id,),
                ).rowcount
                == 1
            )
    elif boundary == "retention":
        with sqlite3.connect(case.path) as db:
            db.execute(
                "UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id=?",
                (case.request.turn_id,),
            )
    elif boundary == "user_rephrase":
        await case.clarifications.invalidate_one(question.request_id, target_status="dismissed", reason="user_rephrase", actor="user")
    elif boundary.startswith("clarification_"):
        with sqlite3.connect(case.path) as db:
            assert (
                db.execute(
                    "UPDATE clarification_requests SET status=? WHERE turn_id=? AND status='pending'",
                    (
                        boundary.removeprefix("clarification_"),
                        case.request.turn_id,
                    ),
                ).rowcount
                == 1
            )
    elif boundary == "resolved_answer_corrupt":
        await resolve_request(case, question)
        with sqlite3.connect(case.path) as db:
            assert (
                db.execute(
                    "UPDATE clarification_requests SET chosen_values_json='[\"project_a\"]' WHERE request_id=? AND status='resolved'",
                    (question.request_id,),
                ).rowcount
                == 1
            )
    else:
        with sqlite3.connect(case.path) as db:
            db.execute("DROP TRIGGER trg_reference_bi_continuation_immutable")
            assert (
                db.execute(
                    "UPDATE chat_reference_bi_continuations SET ciphertext='corrupt' WHERE turn_id=?",
                    (case.request.turn_id,),
                ).rowcount
                == 1
            )

    import os

    from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher

    recovery_store = ChatReferenceStore(
        ReferenceCipher(os.environ["BOWHEAD_FERNET_KEY"]),
    )
    assert await case.case.store.recover_expired_chat_spawn_dispatches(
        reference_store=recovery_store,
    ) == [case.request.turn_id]
    assert (
        await case.case.store.recover_expired_chat_spawn_dispatches(
            reference_store=recovery_store,
        )
        == []
    )
    assert not await case.case.store.is_turn_resume_ready(case.request.turn_id)
    turn = await case.case.store.get_turn(case.request.turn_id)
    assert turn is not None
    assert (turn.status, turn.error_kind) == (
        "cancelled" if boundary == "user_rephrase" else "failed",
        "user_cancel" if boundary == "user_rephrase" else "reference_continuation_unavailable",
    )
    with sqlite3.connect(case.path) as db:
        active, buffer_json = db.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
            (case.request.session_id,),
        ).fetchone()
        assert active is None and json.loads(buffer_json) == []
        assert db.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (case.request.turn_id,),
        ).fetchone() == ("terminal" if boundary == "user_rephrase" else "failed",)
        children = db.execute(
            "SELECT id,status,user_text FROM turns WHERE session_id=? AND id<>? ORDER BY seq",
            (case.request.session_id, case.request.turn_id),
        ).fetchall()
    if boundary != "session_delete":
        assert len(children) == 1
        assert children[0][1:] == ("failed", "补充已受理")
    else:
        assert children == []


async def test_revoked_original_skill_keeps_answer_without_starting_another_model(continuation_case):
    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)
        await case.base.fixture.sources.instances.upsert(
            user_id=request.user_id, name="method", source_type="builtin", source_path=str(case.base.fixture.root / "method"), enabled=False
        )
        floor = max(row["seq"] for row in rows(case, "turn_events"))
        with pytest.raises(ValueError):
            await spawn_resume_turn(
                case.orchestrator, request, team_id=request.team_id, initial_seq=floor, resume_notice={"clarification_id": question.request_id}
            )
        assert route.call_count == 1
        turn = await case.case.store.get_turn("t_reference")
        assert (turn.status, turn.execution_generation, turn.tokens_input, turn.tokens_output) == ("clarification_pending", 2, 100, 20)
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "ready"
        assert (await case.clarifications.get(question.request_id)).status == "resolved"
        await case.base.fixture.sources.instances.upsert(
            user_id=request.user_id, name="method", source_type="builtin", source_path=str(case.base.fixture.root / "method"), enabled=True
        )
        route.mock(side_effect=answer_response)
        spawned = await spawn_resume_turn(
            case.orchestrator,
            request,
            team_id=request.team_id,
            initial_seq=max(row["seq"] for row in rows(case, "turn_events")),
            resume_notice={"clarification_id": question.request_id},
        )
        assert spawned.execution_generation == 3
        await settle_worker(spawned.task)
        assert route.call_count == 2
        assert sorted((row["created_generation"], row["status"]) for row in rows(case, "chat_reference_model_invocations")) == [
            (1, "completed"),
            (3, "completed"),
        ]
        assert (await case.case.store.get_turn("t_reference")).status == "completed"


async def test_expired_preworker_reservation_restores_same_paused_turn(continuation_case):
    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        await resolve_request(case, question)
        generation = await case.case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=question.request_id)
        assert generation == 2 and rows(case, "chat_reference_bi_continuations")[0]["state"] == "reserved"
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id='t_reference'")
        assert "t_reference" in await case.case.store.recover_expired_chat_spawn_dispatches()
        assert (await case.case.store.get_turn("t_reference")).status == "clarification_pending"
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "ready"
        assert await case.case.store.is_turn_resume_ready("t_reference")
        assert route.call_count == 1


async def test_unknown_resume_response_never_reopens_the_question(continuation_case):
    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)
        route.mock(side_effect=httpx2.ReadTimeout("response lost"))
        spawned = await spawn_resume_turn(
            case.orchestrator,
            request,
            team_id=request.team_id,
            initial_seq=max(row["seq"] for row in rows(case, "turn_events")),
            resume_notice={"clarification_id": question.request_id},
        )
        await settle_worker(spawned.task)
        assert route.call_count == 2
        assert next(row for row in rows(case, "chat_reference_model_invocations") if row["created_generation"] == 2)["status"] == "delivery_unknown"
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
        assert not await case.case.store.is_turn_resume_ready("t_reference")
        assert await case.case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=question.request_id) is None
        assert (await case.case.store.get_turn("t_reference")).status in {"failed", "cancelled"}


async def test_duplicate_resume_attaches_and_cannot_rollback_a_started_call(continuation_case):
    import os

    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher

    case = continuation_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(request):
        entered.set()
        await release.wait()
        return answer_response(request)

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)
        route.mock(side_effect=blocked)
        floor = max(row["seq"] for row in rows(case, "turn_events"))
        first = await spawn_resume_turn(
            case.orchestrator, request, team_id=request.team_id, initial_seq=floor, resume_notice={"clarification_id": question.request_id}
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            again = await spawn_resume_turn(case.orchestrator, request, team_id=request.team_id, initial_seq=floor)
            assert again.task is first.task and again.execution_generation == 2
            owner = rows(case, "chat_turn_spawn_dispatch")[0]["owner_id"]
            assert not await case.case.store.rollback_turn_resume_generation(turn_id="t_reference", expected_execution_generation=2, owner_id=owner)
            rebuilt = ReferencePrimaryRuntime(
                scope=request.reference_scope,
                turn_id="t_reference",
                generation=2,
                owner_id="another-worker",
                bindings=case.case.bindings,
                cipher=ReferenceCipher(os.environ["BOWHEAD_FERNET_KEY"]),
                skill_sources=case.base.fixture.sources,
                allow_bi_workspace_continuation=True,
            )
            with pytest.raises(ValueError):
                await rebuilt.claim_bi_resume(clarification_id=question.request_id)
            assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "started"
            assert len(rows(case, "chat_reference_model_invocations")) == 2
        finally:
            release.set()
            await settle_worker(first.task)
        assert route.call_count == 2
        assert (await case.case.store.get_turn("t_reference")).status == "completed"


@pytest.mark.parametrize("change", ["scope", "question", "answer", "purge"])
async def test_changed_pause_is_rejected_before_claim_or_http(continuation_case, change):
    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)
        if change == "scope":
            request.workspace_id = "foreign"
        elif change == "question":
            request.reference_clarification_id = "foreign"
        elif change == "answer":
            request.active_datarc_project = "project_a"
        else:
            with sqlite3.connect(case.path) as db:
                db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
            checkpoint = rows(case, "chat_reference_bi_continuations")[0]
            assert checkpoint["retention_state"] == "purged" and checkpoint["ciphertext"] is None and checkpoint["state"] == "closed"
        with pytest.raises(ValueError):
            await spawn_resume_turn(
                case.orchestrator, request, team_id=request.team_id, initial_seq=max(row["seq"] for row in rows(case, "turn_events"))
            )
        assert route.call_count == 1
        assert (await case.case.store.get_turn("t_reference")).execution_generation == 1


@pytest.mark.parametrize("raw", ["[]", '{"reference_continuation": []}', '"bad"'])
async def test_malformed_saved_card_is_not_resume_ready(continuation_case, raw):
    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE clarification_requests SET resume_context_json=? WHERE request_id=?", (raw, question.request_id))
        assert not await case.case.store.is_turn_resume_ready("t_reference")
        assert route.call_count == 1


@pytest.mark.parametrize("change", ["purge", "claimed_worker"])
async def test_unrecoverable_resume_lease_closes_original_execution(continuation_case, change):
    import os

    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher

    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)
        assert await case.case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=question.request_id) == 2
        if change == "claimed_worker":
            runtime = ReferencePrimaryRuntime(
                scope=case.case.scope,
                turn_id="t_reference",
                generation=2,
                owner_id="lost-worker",
                bindings=case.case.bindings,
                cipher=ReferenceCipher(os.environ["BOWHEAD_FERNET_KEY"]),
                skill_sources=case.orchestrator.reference_skill_sources_factory(
                    ReferenceRequestScope(**case.case.scope.model_dump(exclude={"policy_epoch"}))
                ),
                allow_bi_workspace_continuation=True,
            )
            await runtime.claim_bi_resume(clarification_id=request.reference_clarification_id)
            from bowhead_core.turn_runner import _reference_resume_attachment_snapshot

            assert (
                await _reference_resume_attachment_snapshot(runtime, ["original-file"], [{"artifact_id": "original-file", "mime": "image/png"}])
                is None
            )
        with sqlite3.connect(case.path) as db:
            if change == "purge":
                db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
            db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id='t_reference'")
        assert "t_reference" in await case.case.store.recover_expired_chat_spawn_dispatches()
        assert (await case.case.store.get_turn("t_reference")).status == "failed"
        assert rows(case, "turn_agent_execution")[0]["status"] == "failed"
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
        assert not await case.case.store.is_turn_resume_ready("t_reference")
        assert route.call_count == 1


@pytest.mark.parametrize("revoke", [False, True])
async def test_actual_http_resume_revalidates_scope_and_runs_original_worker(continuation_case, monkeypatch, revoke):
    import jwt
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore
    from bowhead_gateway.datarc_context import DatarcTurnContext
    from bowhead_gateway.routes.clarifications import router
    from fastapi import FastAPI

    from bowhead_gateway import datarc_context, turn_service

    case = continuation_case
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/clarifications")
    app.state.settings = SimpleNamespace(auth_mode="prod")
    app.state.clarification_store = case.clarifications
    app.state.orchestrator = case.orchestrator
    app.state.user_store = case.case.fixture.users
    app.state.access_control_store = AccessControlStore(case.path)
    app.state.terminal_protocol_store = TerminalProtocolStore(case.path)
    app.state.turn_log_store = TurnLogStore(case.path)
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "reference-test-key-32-characters-only")
    monkeypatch.delenv("BOWHEAD_JWT_SECRETS", raising=False)
    token = jwt.encode({"sub": case.request.user_id, "team_id": case.request.team_id}, "reference-test-key-32-characters-only", algorithm="HS256")

    async def connector(*args, **kwargs):
        assert kwargs["team_id"] == case.request.team_id and kwargs["pinned_project"] == "project_b"
        return DatarcTurnContext(
            active_datarc_project="project_b", datarc_client=object(), accessible_datarc_workspaces=case.request.accessible_datarc_workspaces
        )

    async def team_model(*args, **kwargs):
        return turn_service.TeamModelRuntimeResolution(router=case.request.team_router, result="team_safe_success", reason="test_provider")

    monkeypatch.setattr(datarc_context, "resolve_datarc_turn_context", connector)
    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", team_model)
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        await resolve_request(case, question)
        if revoke:
            await case.base.fixture.sources.instances.upsert(
                user_id=case.request.user_id, name="method", source_type="builtin", source_path=str(case.base.fixture.root / "method"), enabled=False
            )
        route.mock(side_effect=answer_response)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + token}
        ) as http:
            response = await http.post(f"/api/v1/clarifications/{question.request_id}/resume_turn")
            if revoke:
                assert response.status_code == 409 and "clarification_resume_unavailable" in response.text
                assert route.call_count == 1
                assert (await case.case.store.get_turn("t_reference")).status == "clarification_pending"
                assert (await case.clarifications.get(question.request_id)).status == "resolved"
                return
            assert response.status_code == 200, response.text
            assert response.json()["execution_generation"] == 2
            from bowhead_gateway.routes.chat import _inflight_tasks

            worker = next(task for task in _inflight_tasks if task.get_name() == "turn_resume:t_reference")
            await settle_worker(worker)
            repeat = await http.post(f"/api/v1/clarifications/{question.request_id}/resume_turn")
            assert repeat.status_code == 200 and repeat.json() == response.json()
        assert route.call_count == 2
        assert (await case.case.store.get_turn("t_reference")).status == "completed"


async def test_stop_during_resume_preserves_original_physical_receipt(continuation_case, monkeypatch):
    case = continuation_case
    entered, release = asyncio.Event(), asyncio.Event()
    worker_done = asyncio.Event()
    store = case.case.store
    real_heartbeat = store.heartbeat_chat_spawn_dispatch
    hold_heartbeats = False
    heartbeats_in_flight = 0
    heartbeats_idle = asyncio.Event()
    heartbeats_idle.set()

    async def heartbeat(**kwargs):
        # This case covers the interleaving where the provider response wins
        # before the worker observes Stop. The lease monitor polls the durable
        # cancel every second and would otherwise race the worker's own fenced
        # write; under CPU load it often wins and cancels the worker locally,
        # which is a separate (valid) Stop path. Defer only the non-settling
        # heartbeats that start after Stop until this worker has finished.
        nonlocal heartbeats_in_flight
        if hold_heartbeats and not kwargs.get("settling"):
            await worker_done.wait()
        heartbeats_in_flight += 1
        heartbeats_idle.clear()
        try:
            return await real_heartbeat(**kwargs)
        finally:
            heartbeats_in_flight -= 1
            if heartbeats_in_flight == 0:
                heartbeats_idle.set()

    async def blocked(request):
        entered.set()
        await release.wait()
        return answer_response(request)

    monkeypatch.setattr(store, "heartbeat_chat_spawn_dispatch", heartbeat)
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        request = await resolve_request(case, question)
        route.mock(side_effect=blocked)
        spawned = await spawn_resume_turn(
            case.orchestrator,
            request,
            team_id=request.team_id,
            initial_seq=max(row["seq"] for row in rows(case, "turn_events")),
            resume_notice={"clarification_id": question.request_id},
        )
        spawned.task.add_done_callback(lambda _task: worker_done.set())
        try:
            await asyncio.wait_for(entered.wait(), 10)
            # A heartbeat already reading the lease must finish before Stop
            # commits, so none can observe the durable cancel early.
            hold_heartbeats = True
            await asyncio.wait_for(heartbeats_idle.wait(), 30)
            assert (
                await case.case.store.request_cancel_turn(
                    team_id=request.team_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    turn_id=request.turn_id,
                    cancel_request_id="reference-resume-stop",
                    expected_execution_generation=2,
                )
                == "cancelled"
            )
        finally:
            release.set()
            from bowhead_gateway.routes.chat import _inflight_tasks

            monitor = next(task for task in _inflight_tasks if task.get_name() == "turn_dispatch_lease:t_reference")
            outcomes = await asyncio.wait_for(asyncio.gather(spawned.task, monitor, return_exceptions=True), 30)
        # Stop can win before a successful provider response. Its obsolete
        # worker hands off instead of forging a terminal; expiry repairs it.
        assert isinstance(outcomes[0], RuntimeError) and "durability_recovery_handoff" in str(outcomes[0])
        with sqlite3.connect(case.path) as db:
            db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id='t_reference'")
        assert "t_reference" in await case.case.store.recover_expired_chat_spawn_dispatches()
        assert len(rows(case, "chat_reference_model_invocations")) == 2
        assert all(row["status"] == "completed" for row in rows(case, "chat_reference_model_invocations"))
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
        assert (await case.case.store.get_turn("t_reference")).status == "cancelled"
        assert not await case.case.store.is_turn_resume_ready("t_reference")
        assert route.call_count == 2


async def test_sql_model_preparation_requires_started_original_pause(continuation_case):
    from uuid import uuid4

    case = continuation_case
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        await resolve_request(case, question)
        assert await case.case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=question.request_id) == 2
        original = rows(case, "chat_reference_model_contexts")[0]
        original.update(context_id="ric_" + uuid4().hex, created_generation=2)
        with sqlite3.connect(case.path) as db, pytest.raises(sqlite3.IntegrityError, match="reference model context"):
            db.execute(
                "INSERT INTO chat_reference_model_contexts(" + ",".join(original) + ") VALUES (" + ",".join("?" for _ in original) + ")",
                tuple(original.values()),
            )
        assert route.call_count == 1 and len(rows(case, "chat_reference_model_contexts")) == 1


@pytest.mark.parametrize("pick", ["project_b", "__bowhead_clarification_skip__"])
async def test_resume_preserves_additional_user_instruction_and_skip(continuation_case, pick):
    case = continuation_case
    bodies = []

    def answer(request):
        bodies.append(json.loads(request.content))
        return answer_response(request)

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        question = await pause(case)
        resolved, won = await case.clarifications.resolve_once(question.request_id, pick, user_directive="只分析 2026 年盈利能力。")
        assert won
        request = build_resume_turn_request(resolved, datarc_client=object(), team_router=case.request.team_router)
        request.active_datarc_project = "project_b" if pick == "project_b" else None
        request.accessible_datarc_workspaces = case.request.accessible_datarc_workspaces
        route.mock(side_effect=answer)
        spawned = await spawn_resume_turn(
            case.orchestrator,
            request,
            team_id=request.team_id,
            initial_seq=max(row["seq"] for row in rows(case, "turn_events")),
            resume_notice={"clarification_id": question.request_id},
        )
        await settle_worker(spawned.task)
        assert route.call_count == 2 and len(bodies) == 1
        last = bodies[0]["messages"][-1]["content"]
        assert "Additional instruction from the user:\n只分析 2026 年盈利能力。" in last
        if pick != "project_b":
            assert "I skipped the workspace selection." in last
        assert (await case.case.store.get_turn("t_reference")).status == "completed"
