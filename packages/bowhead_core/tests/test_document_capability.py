"""Canonical admission and exact Capability receipts on a migrated database."""

import json

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.store import DocumentRuntimeAdmission
from bowhead_core.subagent import _system_profile_snapshot
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def runtime(ctx, *, turn_id="t_doc", scope=SCOPE):
    registry = AgentRegistry.bootstrap()
    profile = manifest_to_profile(registry.get("lead"))
    binding = await AgentExecutionBindingStore(ctx.path).admit_system_execution(
        team_id=scope.team_id,
        user_id=scope.user_id,
        turn_id=turn_id,
        profile_name="lead",
        dispatch_key="document-admission:" + turn_id,
        definition=_system_profile_snapshot(profile),
        manifest_fingerprint="sha256:" + "a" * 64,
        override_version=None,
        system_required=True,
    )
    return DocumentRuntimeAdmission("sha256:" + "b" * 64, 1, binding, True)


async def test_exact_admission_atomic_and_no_publication_from_receipt(ctx):
    admission = await runtime(ctx)
    receipt = await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=ctx.proposal, runtime=admission)
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute(
            """SELECT c.status,c.output_contract,c.input_artifact_refs_json,a.status
            FROM document_dispatches d JOIN turn_capability_execution c ON c.id=d.capability_execution_id
            JOIN turn_agent_execution a ON a.id=c.agent_execution_id WHERE d.job_id=?""",
            (receipt.job_id,),
        ) as cursor:
            state, contract, inputs, parent = await cursor.fetchone()
        assert (state, contract, parent) == ("completed", "document.admission.v1", "completed")
        assert json.loads(inputs) == [ctx.artifact.id]
        async with db.execute("SELECT count(*) FROM document_executions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=ctx.proposal, runtime=admission) == receipt


async def test_exact_admission_rolls_back_with_document_receipt(ctx):
    admission = await runtime(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("CREATE TRIGGER fail_dispatch BEFORE INSERT ON document_dispatches BEGIN SELECT RAISE(ABORT,'test failure'); END")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="test failure"):
        await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=ctx.proposal, runtime=admission)
    async with open_shared_ctx(ctx.path) as db:
        for table in ("document_jobs", "document_command_receipts", "turn_capability_execution"):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
        async with db.execute("SELECT status FROM turn_agent_execution") as cursor:
            assert (await cursor.fetchone())[0] == "admitted"


async def test_canonical_request_required_for_native_admission(ctx):
    admission = await runtime(ctx)
    proposal = ctx.proposal.model_copy(update={"evidence": ctx.proposal.evidence.model_copy(update={"text": "Invented request"})})
    with pytest.raises(DocumentRequestError):
        await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="native", proposal=proposal, runtime=admission)


@pytest.mark.parametrize(
    "answer_question,repair_request,outer_entry",
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, False),
        (False, True, True),
        (False, "quote", True),
        (True, "quote", True),
    ],
)
async def test_lead_model_tool_call_reaches_exact_document_dispatch(ctx, monkeypatch, answer_question, repair_request, outer_entry):
    from types import SimpleNamespace

    # Pre-continuation executor readiness; same-Turn continuation has its own suites.
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    from unittest.mock import AsyncMock

    import httpx2
    from bowhead_core.documents.executor import PROFILE
    from bowhead_core.documents.tool import register_document_tool
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.team_llm_config import REQUIRED_AGENT_TYPES
    from bowhead_core.team_router_registry import TeamRouterRegistry
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
    from bowhead_core.user_store import UserStore
    from bowhead_gateway.document_service import DocumentService
    from cryptography.fernet import Fernet
    from test_subagent import _router

    from bowhead_router import LLMResponse, ToolCall, ToolSchema

    turn_id = "t_doc"
    task = ctx.source.user_text
    arguments = ctx.proposal.model_dump(mode="json")
    for item in arguments["inputs"]:
        item.pop("current_message_position")
    prefix = []
    if answer_question:
        from test_document_recovery import question, turn

        parent = await question(ctx)
        task = "Use the same quarter last year."
        turn_id = await turn(ctx, "answer", text=task, session_id="s_doc", attach=False)
        prefix = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="list_questions", name="template_document_submit", arguments={"action": "list_questions"})],
                finish_reason="tool_calls",
            )
        ]
        arguments = {"action": "answer", "job_id": parent, "evidence": {"text": task}}

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(ctx.path.parent))
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://executor")
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "test-only")
    users = UserStore(ctx.path)
    await users.update_team_llm_config(
        SCOPE.team_id,
        {
            "schema_version": 2,
            "providers": {"team": {"type": "openai_compatible", "base_url": "http://provider/v1", "api_key": "test-only", "models": ["test"]}},
            "agents": {
                role: (
                    {"default": {"primary": {"provider": "team", "model": "test"}}}
                    if role == "analyst"
                    else {"primary": {"provider": "team", "model": "test"}}
                )
                for role in REQUIRED_AGENT_TYPES
            },
        },
    )
    service = DocumentService(SimpleNamespace(artifact_store=ctx.artifacts, user_store=users, team_router_registry=TeamRouterRegistry()))
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    model_runtime = await resolve_team_model_runtime(service.state, SCOPE.team_id, entrypoint="tool_continuation")
    assert model_runtime.available, model_runtime
    client_type = httpx2.AsyncClient

    def transport(request):
        assert request.url.path == "/document-runs/capabilities"
        return httpx2.Response(200, json={"profile": PROFILE, "image_id": "sha256:" + "b" * 64})

    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: client_type(transport=httpx2.MockTransport(transport), **kwargs))
    registry = ToolRegistry()
    register_document_tool(registry, service=lambda: service)
    fallback = AsyncMock(return_value={"ok": True, "filename": "unapproved.pptx"})
    registry.register(ToolSpec(schema=ToolSchema(name="subagent_spawn", description="Delegate", input_schema={"type": "object"}), handler=fallback))
    if repair_request:
        invalid = {key: value for key, value in arguments.items() if key != "evidence"}
        if repair_request == "quote":
            invalid["evidence"] = {"text": "Not quoted from this request"}
        prefix.append(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="fallback", name="subagent_spawn", arguments={"profile": "analyst"}),
                    ToolCall(id="invalid_document", name="template_document_submit", arguments=invalid),
                ],
                finish_reason="tool_calls",
            )
        )
    router = _router(
        *prefix,
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="call_document", name="template_document_submit", arguments=arguments)],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="Your private draft has been requested. Publication requires your review and explicit confirmation."),
    )
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=users)
    runner._registry = AgentRegistry.bootstrap()
    context = ToolExecutionContext(
        workspace_id=SCOPE.workspace_id,
        team_id=SCOPE.team_id,
        user_id=SCOPE.user_id,
        member_uid="member",
        turn_id=turn_id,
        session_id="s_doc",
        role="lead",
        datarc=None,
    )
    bus = TurnEventBus()
    emitted = AsyncMock(wraps=bus.emit)
    monkeypatch.setattr(bus, "emit", emitted)
    if outer_entry:
        from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
        from bowhead_core.agents.turn_context import SerializableTurnContext

        captured = []
        run = runner.run
        adapter = router._registry._adapters["p1"]

        async def stream_response(request):
            from bowhead_core.documents.turn_policy import NATIVE_DOCUMENT_GUIDANCE
            from bowhead_router.types import LLMChunk

            assert request.system.count(NATIVE_DOCUMENT_GUIDANCE) == 1
            response = await adapter.invoke(request)
            for tool_call in response.tool_calls:
                yield LLMChunk(tool_call_delta=tool_call)
            yield LLMChunk(delta=response.content, finish_reason=response.finish_reason, usage=response.usage)

        # The generic mock stream discards tools; preserve the provider boundary.
        monkeypatch.setattr(adapter, "stream", stream_response)

        async def observe_run(**kwargs):
            value = await run(**kwargs)
            assert value.final_answer.startswith("Your template request has been accepted."), [
                (item.tool_name, item.error, item.result) for item in value.tool_invocations
            ]
            captured.append(value)
            return value

        monkeypatch.setattr(runner, "run", observe_run)
        runtime = LeadRuntime(router=router, tool_registry=registry, parent_bus=bus, parent_ctx=context, subagent_runner=runner)
        final = await _lead_loop(
            SerializableTurnContext(user_id=SCOPE.user_id, workspace_id=SCOPE.workspace_id, session_id="s_doc", turn_id=turn_id, role="user"),
            task,
            runtime,
        )
        result = captured[0]
        assert final.final_answer == result.final_answer
        answers = [call.args[0].payload["text"] for call in emitted.await_args_list if call.args[0].event_type == "final_answer"]
        assert answers == [result.final_answer]
    else:
        result = await runner.run(profile_name="lead", task=task, parent_turn_id=turn_id, parent_bus=bus, parent_ctx=context)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_dispatches") as cursor:
        assert (await cursor.fetchone())[0] == 1, [(item.error, item.result) for item in result.tool_invocations]
    if answer_question:
        jobs = await service.jobs.list_for_session(scope=SCOPE, session_id="s_doc")
        child = next(job for job in jobs if job.id != parent)
        value = await service.delivery.get(scope=SCOPE, job_id=child.id)
        assert ctx.source.user_text in value["request_text"] and task in value["request_text"]
        assert result.tool_invocations[0].result["questions"] == [{"job_id": parent, "question": "Which comparison period?"}]
    assert context.document_admission_binding is None
    assert result.final_answer.startswith("Your template request has been accepted.")
    adapter = router._registry._adapters["p1"]
    assert adapter._script_index == len(adapter.scripted_responses) - 1
    if repair_request:
        fallback.assert_not_awaited()
        assert next(inv for inv in result.tool_invocations if inv.tool_name == "subagent_spawn").result["error"] == "document_workflow_required"
    if repair_request:
        invalid_step = next(
            call.args[0]
            for call in emitted.await_args_list
            if call.args[0].event_type == "step_done" and call.args[0].payload.get("call_id") == "invalid_document"
        )
        assert invalid_step.payload["error"]["is_transient"] is False
