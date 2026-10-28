"""Actual service, Router transport seam, Stores and shared continuation.

HTTP model responses are controlled. HostWorker executes the original fixed
worker on synthetic bytes; this is not Docker isolation or provider acceptance.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.document_chat_continuation import DocumentResumeReservation, DocumentTurnOwner
from bowhead_core.document_chat_usage import document_usage_projection
from bowhead_core.document_clarification_lifecycle import consume_document_resume_in_tx
from bowhead_core.documents.continuation import DocumentPlanningPause, DocumentRuntimeSnapshot, begin_document_planning_in_tx
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import PROFILE
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.tool import register_document_tool
from bowhead_core.documents.turn_runner import fail_document_turn
from bowhead_core.language_context import LanguageContext
from bowhead_core.physical_usage import PhysicalUsageObserver
from bowhead_core.team_llm_config import REQUIRED_AGENT_TYPES
from bowhead_core.team_router_registry import TeamRouterRegistry
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.document_service import DocumentService
from bowhead_router.invocation import physical_observation_scope
from bowhead_router.usage import usage_context
from cryptography.fernet import Fernet
from test_document_capability import runtime
from test_document_clarification_lifecycle import pause
from test_document_planning_continuation import QUESTION, link
from test_document_pptx_text import _deck
from test_document_runner import IMAGE, HostWorker
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
ASK = json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": QUESTION})


@pytest.fixture
async def native(ctx, monkeypatch, tmp_path):
    root_key = Fernet.generate_key().decode()
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", root_key)
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    content = _deck()
    Path(ctx.artifact.path).write_bytes(content)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE artifacts SET size=? WHERE id=?", (len(content), ctx.artifact.id))
        await db.execute("UPDATE teams SET team_credit_balance=100 WHERE id=?", (SCOPE.team_id,))
        await db.execute("UPDATE users SET credit_balance=100 WHERE id=?", (SCOPE.user_id,))
        await db.commit()
    users = UserStore(ctx.path)
    await users.update_team_llm_config(
        SCOPE.team_id,
        {
            "schema_version": 2,
            "providers": {
                "team": {
                    "type": "openai_compatible",
                    "base_url": "http://provider/v1",
                    "api_key": "test-only",
                    "models": ["test", "fast-test", "selected-test"],
                },
                "selected": {
                    "type": "openai_compatible",
                    "base_url": "http://selected-provider/v1",
                    "api_key": "test-only",
                    "models": ["selected-test"],
                },
            },
            "agents": {
                role: (
                    {"default": {"primary": {"provider": "team", "model": "test"}}}
                    if role == "analyst"
                    else {"primary": {"provider": "team", "model": "fast-test" if role == "echo" else "test"}}
                )
                for role in REQUIRED_AGENT_TYPES
            },
        },
    )
    state = SimpleNamespace(artifact_store=ctx.artifacts, user_store=users, team_router_registry=TeamRouterRegistry())
    service = DocumentService(state)
    worker = HostWorker(tmp_path / "worker")
    worker.url, worker.token = "http://executor", "test-only"
    service.executor = lambda: worker
    result = SimpleNamespace(
        service=service,
        worker=worker,
        calls=[],
        responses=[ASK],
        root_key=root_key,
        planner_profiles=["pptx-grounded-plan-v4", "pptx-grounded-plan-v5"],
    )

    def respond(request):
        if request.url.host == "executor":
            assert request.url.path == "/document-runs/capabilities"
            return httpx2.Response(200, json={"profile": PROFILE, "image_id": IMAGE, "planner_profiles": result.planner_profiles})
        assert request.url.path == "/v1/chat/completions"
        result.calls.append(json.loads(request.content))
        answer = result.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": getattr(result, "finish_reason", "stop")}],
                "usage": {"prompt_tokens": 600, "completion_tokens": 100},
            },
        )

    original_client = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: original_client(**{**kwargs, "transport": httpx2.MockTransport(respond)}))
    tool_ctx = ToolExecutionContext(
        **SCOPE.model_dump(),
        session_id="s_doc",
        turn_id="t_doc",
        role="lead",
        member_uid="",
        datarc=None,
        language_context=LanguageContext(output_language="fr-FR"),
        clarification_store=ClarificationStore(ctx.path),
        execution_generation=1,
        execution_owner_id="turn-document-worker",
    )
    assert (
        await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id=tool_ctx.execution_owner_id, lease_seconds=600) == "claimed"
    )

    async def binding():
        value = await runtime(ctx)
        return value.binding, True

    tool_ctx.document_admission_binding = binding
    registry = ToolRegistry()
    register_document_tool(registry, service=lambda: service)
    result.context, result.registry = tool_ctx, registry
    result.arguments = ctx.proposal.model_dump(mode="json")
    for item in result.arguments["inputs"]:
        item.pop("current_message_position")
    return result


def observe(ctx, native):
    return physical_observation_scope(
        PhysicalUsageObserver(str(ctx.path), cipher=ReferenceCipher(native.root_key), turn_id="t_doc", generation=native.context.execution_generation)
    )


async def start(ctx, native):
    with observe(ctx, native):
        result = (await native.registry.execute("template_document_submit", native.arguments, native.context)).result
    assert result.get("state") == "pause", result
    outcome = native.context.document_turn_state.planning_result
    assert isinstance(outcome, DocumentPlanningPause)
    return outcome


@pytest.mark.parametrize("profiles", [None, [], ["pptx-grounded-plan-v4"], "pptx-grounded-plan-v5"])
async def test_unsupported_worker_refuses_new_native_admission_before_job_or_model(ctx, native, profiles):
    native.planner_profiles = profiles
    with pytest.raises(DocumentRequestError, match="document_runtime_unavailable"):
        await native.service.submit(native.context, ctx.proposal)
    assert native.calls == []
    assert native.worker.runs == []
    async with open_shared_ctx(ctx.path) as db:
        for table in ("document_jobs", "document_dispatches", "document_turn_dispatches", "turn_agent_execution"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0


async def claim_resume(ctx, native, outcome, answer="Quarter two"):
    worker = DocumentTurnOwner(
        **SCOPE.model_dump(),
        session_id="s_doc",
        turn_id="t_doc",
        generation=native.context.execution_generation,
        owner_id=native.context.execution_owner_id,
    )
    async with (
        open_shared_ctx(ctx.path) as db,
        db.execute("SELECT runtime_snapshot_json FROM document_turn_dispatches WHERE job_id=?", (outcome.job_id,)) as cursor,
    ):
        snapshot = DocumentRuntimeSnapshot.model_validate_json((await cursor.fetchone())[0])
    store, card = await pause(ctx, SimpleNamespace(worker=worker, job_id=outcome.job_id, ref=outcome.checkpoint, snapshot=snapshot))
    assert (
        await store.resolve_once(
            card.request_id,
            chosen_value=answer,
            answers=[{"question": QUESTION, "source": "custom", "answer": answer, "skipped": False, "option_index": None}],
        )
    )[1]
    generation = await ctx.sessions.advance_turn_execution_generation(turn_id="t_doc", document_clarification_id=card.request_id)
    assert generation == worker.generation + 1
    native.context.execution_generation, native.context.execution_owner_id = generation, "resumed-document-worker"
    assert (
        await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id=native.context.execution_owner_id, lease_seconds=600)
        == "claimed"
    )
    await TurnLogStore(str(ctx.path)).record(
        turn_id="t_doc", event_type="turn.resumed", payload={"turn_id": "t_doc", "execution_generation": generation}
    )
    worker = worker.model_copy(update={"generation": generation, "owner_id": native.context.execution_owner_id})
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        attempt = await consume_document_resume_in_tx(
            db, worker=worker, reservation=DocumentResumeReservation(card_id=card.request_id, checkpoint=outcome.checkpoint, generation=generation)
        )
        await db.commit()
    return attempt


async def test_actual_tool_writer_retains_atomic_pause_and_zero_root_document_usage(ctx, native, monkeypatch):
    root_usage = []
    with usage_context(**SCOPE.model_dump(), turn_id="t_doc", reason="turn", turn_usage_sink=root_usage.append):
        outcome = await start(ctx, native)
    assert root_usage == []
    assert native.worker.runs == ["inventory", "apply_verify"] and len(native.calls) == 1
    assert "fr-FR" in native.calls[0]["messages"][0]["content"]
    cards = await native.context.clarification_store.get_pending_for_turn("t_doc")
    assert len(cards) == 1 and cards[0].rationale == QUESTION
    assert await native.service.questions(native.context) == []
    from bowhead_core.documents.store import _question

    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        with pytest.raises(DocumentRequestError, match="document_question_unavailable"):
            await _question(db, SCOPE, outcome.job_id)
    assert (await ctx.sessions.get_turn("t_doc")).status == "running"
    assert await document_usage_projection(ctx.path, turn_id="t_doc") == {"tokens_input": 600, "tokens_output": 100, "partial": False}
    assert await DocumentDeliveryStore(ctx.artifacts).claim(scope=SCOPE, job_id=outcome.job_id) is None
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    from bowhead_core.documents.store import DocumentReceipt

    assert await native.service.run_turn(native.context, DocumentReceipt(outcome.job_id, 1)) == outcome
    assert len(native.calls) == 1


async def test_resume_uses_original_context_no_inventory_and_stops_second_question(ctx, native, monkeypatch):
    outcome = await start(ctx, native)
    attempt = await claim_resume(ctx, native, outcome)
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    native.responses = [ASK]
    with observe(ctx, native):
        result = await native.service.resume_turn(native.context, attempt)
    assert result.kind == "incomplete", result
    assert native.worker.runs == ["inventory", "apply_verify", "apply_verify"]
    assert len(native.calls) == 2
    payload = json.loads(native.calls[1]["messages"][-1]["content"])
    assert any(f["path"] == "/clarification/answer" and f["value"] == "Quarter two" for f in payload["facts"])
    assert await document_usage_projection(ctx.path, turn_id="t_doc") == {"tokens_input": 1217, "tokens_output": 209, "partial": False}
    assert await native.context.clarification_store.get_pending_for_turn("t_doc") == []


@pytest.mark.parametrize("selected", [None, "selected-test"], ids=["team-primary", "user-selected"])
async def test_planner_preserves_admitted_model_through_pause_and_picker_change(ctx, native, selected):
    native.context.preferred_provider = "selected" if selected else None
    native.context.preferred_model = selected
    outcome = await start(ctx, native)
    native.context.preferred_provider, native.context.preferred_model = "team", "fast-test"
    recovered = await native.service.submit(native.context, ctx.proposal)
    assert recovered.job_id == outcome.job_id
    attempt = await claim_resume(ctx, native, outcome)
    native.responses = [ASK]
    with observe(ctx, native):
        result = await native.service.resume_turn(native.context, attempt)
    assert result.kind == "incomplete"
    expected = selected or "test"
    provider = "selected" if selected else "team"
    assert [call["model"] for call in native.calls] == [expected, expected]
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute(
            "SELECT d.model_selection_json,j.request_json FROM document_dispatches d JOIN document_jobs j ON j.id=d.job_id"
        ) as cursor:
            selection_json, request_json = await cursor.fetchone()
            assert json.loads(selection_json) == {"provider": provider, "model": expected}
            assert json.loads(request_json)["runtime"]["model_selection"] == json.loads(selection_json)
        async with db.execute("SELECT provider,model FROM document_model_calls ORDER BY attempt_no,call_no") as cursor:
            assert [tuple(row) for row in await cursor.fetchall()] == [(provider, expected), (provider, expected)]


async def test_unknown_selected_planner_refuses_before_job_or_model(ctx, native):
    native.context.preferred_provider, native.context.preferred_model = "team", "not-configured"
    with pytest.raises(DocumentRequestError, match="document_model_unavailable"):
        await native.service.submit(native.context, ctx.proposal)
    assert not native.calls and not native.worker.runs
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_jobs") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_legacy_dispatch_retains_its_fast_route(ctx, native):
    admission = await runtime(ctx)
    previous = await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="legacy-admission", proposal=ctx.proposal, runtime=admission)
    await native.service._invoke(SCOPE, admission.model_config_version, "Legacy planner", "Legacy request", job_id=previous.job_id)
    assert [call["model"] for call in native.calls] == ["fast-test"]


async def test_document_planner_uses_shared_automatic_output_with_explicit_override(ctx, native):
    await start(ctx, native)
    assert native.calls[0]["max_tokens"] == 32_000
    config, _ = await native.service.state.user_store.get_team_llm_config_with_version(SCOPE.team_id)
    config["providers"]["team"]["outbound_params"] = {"max_tokens": {"mode": "fixed", "value": 6000}}
    await native.service.state.user_store.update_team_llm_config(SCOPE.team_id, config)
    # Use the original service against the already admitted job with the current
    # configuration version; this checks transport policy, not recovery admission.
    job_id = native.context.document_turn_state.planning_result.job_id
    _, version = await native.service.state.user_store.get_team_llm_config_with_version(SCOPE.team_id)
    native.responses = [ASK]
    await native.service._invoke(SCOPE, version, "Planner", "Request", job_id=job_id)
    assert native.calls[-1]["max_tokens"] == 6000


@pytest.mark.parametrize("finish,response", [("length", "Private incomplete content"), ("stop", "Private oversized content" * 3000)])
@pytest.mark.parametrize("background", [False, True])
async def test_incomplete_planning_records_actual_limits_without_content_or_retries(ctx, native, monkeypatch, caplog, finish, response, background):
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0" if background else "1")
    native.finish_reason = finish
    native.responses = [response]
    receipt = await native.service.submit(native.context, ctx.proposal)
    if background:
        await native.service._run(SCOPE, receipt.job_id, IMAGE, 1)
    else:
        with observe(ctx, native):
            result = await native.service.run_turn(native.context, receipt)
        assert result.error_code == "planning_incomplete_response"
    assert len(native.calls) == 1 and len(native.worker.runs) == 1
    assert (await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id))["state"] == "failed"
    messages = [item.message for item in caplog.records if item.message.startswith("document.planning_response")]
    assert len(messages) == 1
    assert f"job_id={receipt.job_id}" in messages[0] and f"finish_reason={finish}" in messages[0]
    assert f"response_bytes={len(response.encode())}" in messages[0] and "wire_allowance=32000" in messages[0]
    assert "caller_allowance=automatic" in messages[0] and "response_byte_limit=64000" in messages[0]
    assert "Private" not in messages[0]


async def test_new_background_dispatch_also_uses_the_request_selection(ctx, native, monkeypatch):
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    native.context.preferred_provider, native.context.preferred_model = "selected", "selected-test"
    receipt = await native.service.submit(native.context, ctx.proposal)
    await native.service._run(SCOPE, receipt.job_id, IMAGE, 1)
    assert [call["model"] for call in native.calls] == ["selected-test"]
    assert (await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id))["state"] == "question"


async def test_model_configuration_change_stops_before_first_model_send(ctx, native):
    receipt = await native.service.submit(native.context, ctx.proposal)
    config, _ = await native.service.state.user_store.get_team_llm_config_with_version(SCOPE.team_id)
    await native.service.state.user_store.update_team_llm_config(SCOPE.team_id, config)
    with observe(ctx, native):
        result = await native.service.run_turn(native.context, receipt)
    assert result.kind == "failed" and result.error_code == "document_model_configuration_changed"
    assert native.calls == []


async def test_physical_send_refuses_a_model_other_than_the_frozen_selection(ctx, native):
    from bowhead_core.documents.usage import DocumentModelHook
    from bowhead_router.invocation import PhysicalInvocationSpec

    native.context.preferred_provider, native.context.preferred_model = "team", "selected-test"
    receipt = await native.service.submit(native.context, ctx.proposal)
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=1, owner_id=native.context.execution_owner_id)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await begin_document_planning_in_tx(db, worker=worker, job_id=receipt.job_id, instruction=ctx.source.user_text)
        await db.commit()
    await native.service.delivery.transition(scope=SCOPE, job_id=receipt.job_id, owner=worker.owner_id, state="planning")
    hook = DocumentModelHook(str(ctx.path), SCOPE, receipt.job_id, worker.owner_id, 1)
    with pytest.raises(DocumentRequestError, match="document_model_configuration_changed"):
        await hook.begin(PhysicalInvocationSpec("team", "fast-test", "test.v1", "invoke", "a" * 64))
    assert native.calls == []
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_model_calls") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_turn_ownership_failure_rolls_back_original_admission_without_scanner_job(ctx, native):
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "CREATE TRIGGER fail_turn_owner BEFORE INSERT ON document_turn_dispatches BEGIN SELECT RAISE(ABORT,'controlled ownership failure'); END"
        )
        await db.commit()
    result = (await native.registry.execute("template_document_submit", native.arguments, native.context)).result
    assert not result["ok"] and result["document_terminal"]
    async with open_shared_ctx(ctx.path) as db:
        for table in ("document_jobs", "document_dispatches", "document_turn_dispatches", "document_command_receipts", "document_executions"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
    assert not native.calls and not native.worker.runs


async def test_recovered_initial_inflight_attempt_ends_failed_without_inventory_or_model(ctx, native):
    receipt = await native.service.submit(native.context, ctx.proposal)
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=1, owner_id=native.context.execution_owner_id)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await begin_document_planning_in_tx(db, worker=worker, job_id=receipt.job_id, instruction=ctx.source.user_text)
        await db.commit()
    result = await native.service.run_turn(native.context, receipt)
    assert result.kind == "failed" and result.error_code == "document_execution_interrupted"
    assert (await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id))["state"] == "failed"
    assert not native.calls and not native.worker.runs


async def test_recovered_resumed_observation_ends_failed_without_reissuing_model(ctx, native):
    outcome = await start(ctx, native)
    attempt = await claim_resume(ctx, native, outcome)
    await link(ctx, SimpleNamespace(job_id=outcome.job_id, worker=attempt.worker), worker=attempt.worker, attempt_no=2)
    result = await native.service.resume_turn(native.context, attempt)
    assert result.kind == "failed" and result.error_code == "document_usage_unresolved"
    assert (await native.service.delivery.get(scope=SCOPE, job_id=outcome.job_id))["state"] == "failed"
    assert len(native.calls) == 1 and native.worker.runs == ["inventory", "apply_verify"]
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM credit_ledger WHERE reason='template_document'") as cursor:
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.parametrize("fault", ["unknown", "context_bytes", "card_commit", "long_question"])
async def test_uncertain_or_invalid_initial_execution_never_replays_or_orphans_question(ctx, native, fault):
    if fault == "unknown":
        native.responses = [httpx2.ReadTimeout("controlled transport loss")]
    elif fault == "context_bytes":
        Path(ctx.artifact.path).write_bytes(b"invalid document bytes")
    elif fault == "card_commit":
        async with open_shared_ctx(ctx.path) as db:
            await db.execute(
                "CREATE TRIGGER fail_card BEFORE INSERT ON clarification_document_continuations BEGIN SELECT RAISE(ABORT,'controlled card failure'); END"
            )
            await db.commit()
    else:
        native.responses = [ASK.replace(QUESTION, "q" * 201), ASK]
    with observe(ctx, native):
        result = (await native.registry.execute("template_document_submit", native.arguments, native.context)).result
    if fault == "long_question":
        assert result["state"] == "pause" and len(native.calls) == 2, result
    else:
        assert result["state"] == "failed", result
        assert await native.context.clarification_store.get_pending_for_turn("t_doc") == []
        async with open_shared_ctx(ctx.path) as db:
            async with db.execute("SELECT checkpoint_digest FROM document_planning_attempts") as cursor:
                assert all(row[0] is None for row in await cursor.fetchall())
            async with db.execute("SELECT state FROM document_executions") as cursor:
                assert await cursor.fetchall() == [("failed",)]
    before = len(native.calls)
    assert (await native.registry.execute("template_document_submit", native.arguments, native.context)).result == result
    assert len(native.calls) == before


async def test_prior_ordinary_answers_remain_identical_across_inventory_and_document_resume(ctx, native):
    context = native.context
    store = context.clarification_store
    question = "Who is the audience?"
    card = await store.put(
        turn_id="t_doc",
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        session_id="s_doc",
        token="",
        rationale=question,
        candidates=[],
        kind="ambiguous_turn",
        resume_context={"user_text": ctx.source.user_text},
        bounded_question={"generation": 1, "owner_id": context.execution_owner_id, "questions": [{"question": question}]},
    )
    assert (
        await store.resolve_once(
            card.request_id,
            chosen_value="Board members",
            answers=[{"question": question, "source": "custom", "answer": "Board members", "skipped": False, "option_index": None}],
        )
    )[1]
    assert await ctx.sessions.mark_clarification_pending(turn_id="t_doc", intent="ambiguous", error_kind="needs_clarification")
    await TurnLogStore(str(ctx.path)).record(turn_id="t_doc", event_type="done", payload={"turn_id": "t_doc", "status": "clarification_pending"})
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id="t_doc", owner_id=context.execution_owner_id)
    context.execution_generation = await ctx.sessions.advance_turn_execution_generation(turn_id="t_doc", clarification_id=card.request_id)
    assert context.execution_generation == 2
    context.execution_owner_id = "document-after-ordinary-answer"
    assert (
        await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id=context.execution_owner_id, lease_seconds=600) == "claimed"
    )
    await TurnLogStore(str(ctx.path)).record(
        turn_id="t_doc", event_type="turn.resumed", payload={"turn_id": "t_doc", "execution_generation": context.execution_generation}
    )
    outcome = await start(ctx, native)
    first = json.loads(native.calls[0]["messages"][-1]["content"])
    assert any(f["value"] == "Board members" for f in first["facts"])
    attempt = await claim_resume(ctx, native, outcome)
    native.responses = [ASK]
    with observe(ctx, native):
        result = await native.service.resume_turn(context, attempt)
    assert result.kind == "incomplete", result
    second = json.loads(native.calls[1]["messages"][-1]["content"])
    assert second["facts"][: len(first["facts"])] == first["facts"]
    assert native.worker.runs.count("inventory") == 1


@pytest.mark.parametrize("fault", ["frozen_bytes", "model_change", "runtime", "cancellation"])
async def test_resumed_failure_never_reinventories_or_reissues_unknown_call(ctx, native, fault):
    outcome = await start(ctx, native)
    attempt = await claim_resume(ctx, native, outcome)
    if fault == "frozen_bytes":
        async with open_shared_ctx(ctx.path) as db, db.execute("SELECT path FROM document_input_copies WHERE job_id=?", (outcome.job_id,)) as cursor:
            Path((await cursor.fetchone())[0]).write_bytes(b"changed frozen bytes")
    elif fault == "model_change":
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("UPDATE teams SET llm_config_version=llm_config_version+1 WHERE id=?", (SCOPE.team_id,))
            await db.commit()
    elif fault == "runtime":

        def unavailable():
            raise DocumentRequestError("document_runtime_unavailable")

        native.service.executor = unavailable
    else:
        native.responses = [asyncio.CancelledError()]
    with observe(ctx, native):
        if fault == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await native.service.resume_turn(native.context, attempt)
        else:
            result = await native.service.resume_turn(native.context, attempt)
            assert result.kind == "failed", result
    saved = await native.service.delivery.get(scope=SCOPE, job_id=outcome.job_id)
    assert saved["state"] == "failed", saved
    assert native.worker.runs.count("inventory") == 1
    assert len(native.calls) == (2 if fault == "cancellation" else 1)
    if fault == "cancellation":
        assert saved["usage"]["pending"]
    before = len(native.calls)
    await native.service.resume_turn(native.context, attempt)
    assert len(native.calls) == before


@pytest.mark.skipif(os.environ.get("BOWHEAD_DOCUMENT_NATIVE_SMOKE") != "1", reason="Requires explicit native LibreOffice/Poppler smoke environment")
@pytest.mark.parametrize("answer_text", ["Quarter two", "Use Quarter two for the title; leave the caption and all figures unchanged."])
async def test_native_resumed_draft_confirms_exact_source_and_pdf_without_new_model(ctx, native, answer_text):
    outcome = await start(ctx, native)
    attempt = await claim_resume(ctx, native, outcome, answer=answer_text)
    async with (
        open_shared_ctx(ctx.path) as db,
        db.execute("SELECT context_json FROM document_planning_attempts WHERE job_id=? AND attempt_no=2", (outcome.job_id,)) as cursor,
    ):
        payload = json.loads(json.loads((await cursor.fetchone())[0])["payload"])
    slot = next(s["id"] for s in payload["slots"] if s["text"] == "Quarter one")
    answer = next(f["id"] for f in payload["facts"] if f["path"] == "/clarification/answer")
    native.responses = [
        json.dumps(
            {
                "action": "propose",
                "updates": [{"slot": slot, "parts": [{"ref": answer, "quote": "Quarter two"}]}],
                "keep": [s["id"] for s in payload["slots"] if s["id"] != slot],
            }
        )
    ]
    with observe(ctx, native):
        result = await native.service.resume_turn(native.context, attempt)
    assert result.kind == "review", result
    delivery = native.service.delivery
    with pytest.raises(DocumentRequestError):
        await delivery.read_file(scope=SCOPE, job_id=outcome.job_id, name="candidate.pptx", preview=False)
    saved = await delivery.get(scope=SCOPE, job_id=outcome.job_id)
    assert saved["draft_digest"] == result.draft_digest
    change = saved["draft"]["manifest"]["changes"][0]
    assert change["after"] == "Quarter two"
    assert change["evidence"][0]["pointer"] == "/clarification/answer"
    assert change["evidence"][0]["quote_start"] == answer_text.index("Quarter two")
    assert saved["draft"]["preview"]["text_and_geometry"] == "passed"
    await fail_document_turn(delivery, attempt.worker, outcome.job_id, "document_execution_failed")
    assert await delivery.get(scope=SCOPE, job_id=outcome.job_id) == saved
    published = await delivery.confirm(scope=SCOPE, job_id=outcome.job_id, command_id="review-confirmation", draft_digest=result.draft_digest)
    assert published["state"] == "published"
    await fail_document_turn(delivery, attempt.worker, outcome.job_id, "document_execution_failed")
    assert await delivery.get(scope=SCOPE, job_id=outcome.job_id) == published
    assert await delivery.confirm(scope=SCOPE, job_id=outcome.job_id, command_id="review-confirmation", draft_digest=result.draft_digest) == published
    for name in ("candidate.pptx", "candidate.pdf"):
        content, _ = await delivery.read_file(scope=SCOPE, job_id=outcome.job_id, name=name, preview=False)
        assert hashlib.sha256(content).hexdigest() == saved["draft"]["files"][name]["sha256"]
    assert len(native.calls) == 2 and native.worker.runs == ["inventory", "apply_verify", "apply_verify"]
