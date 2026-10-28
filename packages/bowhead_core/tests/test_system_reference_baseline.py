"""Explicit system Skill grants use real reference preparation and original packs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core.agent_dependency_compiler import parse_exact_skill_grants
from bowhead_core.chat_reference_preparation import ReferenceStartPreparationError, prepare_reference_system_start_admission
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import AgentResourceRef, ReferenceNode
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import PROFILES, SubagentProfile
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from cryptography.fernet import Fernet
from test_reference_start_preparation import activate, raw_document
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case
from test_skill_reference_invocation import tools_for
from test_skill_reference_runtime import rows

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


async def configure(fixture, monkeypatch, *, methods=False, legacy=False):
    with sqlite3.connect(fixture.case.fixture.db_path) as db:
        definition = json.loads(
            db.execute(
                "SELECT definition_json FROM team_subagent_revision WHERE id=?", (fixture.case.args["reference_start"].expected.revision_ref,)
            ).fetchone()[0]
        )
    grants = parse_exact_skill_grants(definition["skill_grants"])
    profile = SubagentProfile(
        name="compute",
        llm_agent_type="analyst",
        tool_whitelist=("skill_read", "skill_invoke", "bi_query"),
        system_prompt="Use original financial methods.",
        skill_runtime_grants=None if legacy else grants,
        skill_runtime_scope=None
        if legacy
        else ExactSkillRuntimeScope(
            read_names=tuple(sorted(grant.name for grant in grants)),
            invoke_names=tuple(sorted(grant.name for grant in grants if "invoke" in grant.permissions)),
        ),
    )
    monkeypatch.setitem(PROFILES, "compute", profile)
    projected = await fixture.case.runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=fixture.case.ctx)
    document = raw_document(fixture, skills=methods)
    for index, node in enumerate(document.nodes):
        if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef):
            document.nodes[index] = node.model_copy(
                update={"resource": AgentResourceRef(kind="agent", source_kind="system", id="system:compute", version=projected.revision_ref)}
            )
    return document, profile


async def prepare(fixture, document):
    return await prepare_reference_system_start_admission(
        fixture.case.fixture.db_path, input_document=document, scope=fixture.scope, runner=fixture.case.runner, sources=fixture.sources
    )


@pytest.mark.parametrize(
    ("receiver_case", "methods"),
    [("admission_unused_invoke", False), ("admission_unused_invoke", True), ("admission_empty", False)],
    indirect=["receiver_case"],
)
async def test_system_baseline_keeps_full_original_grants_and_real_invocation(receiver_case, monkeypatch, methods):
    fixture = receiver_case
    activate(fixture)
    document, profile = await configure(fixture, monkeypatch, methods=methods)
    admission = await prepare(fixture, document)
    assert admission.system_required and admission.expected.assignment_id is None
    fixture.case.args.update(reference_start=admission, user_text=document.display_text())
    await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    scope = ReferenceScope(**fixture.scope.model_dump(exclude={"session_id"}), session_id="s_reference", policy_epoch=admission.policy_epoch)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    claim = await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="system-worker",
    )
    assert claim.outcome == "claimed"
    runtime = ReferencePrimaryRuntime(
        scope=scope, turn_id="t_reference", owner_id="system-worker", bindings=fixture.case.bindings, cipher=cipher, skill_sources=fixture.sources
    )
    registry, calls, _ = tools_for(fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    runner = fixture.case.runner
    runner.router, runner.tool_registry = router_for(adapter), registry
    ctx = ToolExecutionContext(**scope.identity(), turn_id="t_reference", member_uid=scope.user_id, datarc=None, session_store=fixture.case.store)
    bodies = []
    invoke = any("invoke" in grant.permissions for grant in profile.skill_runtime_grants)
    original = (fixture.root / "unused/scripts/runner.py").read_bytes() if invoke else None
    if invoke:
        (fixture.root / "unused/scripts/runner.py").unlink()
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", "1")

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if invoke and len(bodies) == 1:
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "original-system-call",
                                        "type": "function",
                                        "function": {
                                            "name": "skill_invoke",
                                            "arguments": json.dumps({"name": "unused", "script": "runner.py", "input": {}}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )
        return success("openai", "stream" if body.get("stream") else "invoke")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
            assert result.ok and result.final_answer == "OK"
            assert route.call_count == (2 if invoke else 1)
        assert len(calls) == (1 if invoke else 0)
        if invoke:
            assert calls[0][2] == original and not calls[0][1].exists()
            assert "CALCULATED 42" in json.dumps(bodies[-1])
        assert ("BODY_method" in json.dumps(bodies[0])) is methods
        saved = SimpleNamespace(path=fixture.case.fixture.db_path)
        assert rows(saved, "turn_agent_execution")[0]["agent_source_kind"] == "system"
        assert rows(saved, "turn_agent_execution")[0]["status"] == "completed"
        assert all(row["status"] == "completed" for row in rows(saved, "chat_reference_model_invocations"))
    finally:
        await adapter.close()


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("kind", ["legacy", "narrowed", "revoked"])
async def test_system_baseline_never_infers_missing_or_wider_grants(receiver_case, monkeypatch, kind):
    fixture = receiver_case
    document, profile = await configure(fixture, monkeypatch, methods=True, legacy=kind == "legacy")
    if kind == "narrowed":
        monkeypatch.setitem(PROFILES, "compute", replace(profile, skill_runtime_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=())))
        projected = await fixture.case.runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=fixture.case.ctx)
        document = document.model_copy(
            update={
                "nodes": [
                    node.model_copy(update={"resource": node.resource.model_copy(update={"version": projected.revision_ref})})
                    if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
                    else node
                    for node in document.nodes
                ]
            }
        )
    elif kind == "revoked":
        await fixture.sources.instances.upsert(
            user_id=fixture.scope.user_id, name="method", source_type="builtin", source_path=str(fixture.root / "method"), enabled=False
        )
    with pytest.raises(ReferenceStartPreparationError):
        await prepare(fixture, document)


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_revoked_unselected_system_dependency_cannot_be_admitted(receiver_case, monkeypatch):
    from test_reference_start_admission import _assert_no_acceptance

    fixture = receiver_case
    activate(fixture)
    document, _ = await configure(fixture, monkeypatch, methods=True)
    admission = await prepare(fixture, document)
    await fixture.sources.instances.upsert(
        user_id=fixture.scope.user_id, name="unused", source_type="builtin", source_path=str(fixture.root / "unused"), enabled=False
    )
    fixture.case.args.update(reference_start=admission, user_text=document.display_text())
    with pytest.raises(ValueError):
        await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_system_primary_uses_shared_gateway_workspace_continuation(receiver_case, monkeypatch):
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.skill_read_resolution import build_reference_skill_sources
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import build_default_registry
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_core.types import TurnRequest
    from bowhead_gateway.turn_service import spawn_resume_turn
    from test_reference_bi_continuation import answer_response, pause, resolve_request, settle_worker, workspace_question

    fixture = receiver_case
    activate(fixture)
    document, _ = await configure(fixture, monkeypatch)
    admission = await prepare(fixture, document)
    fixture.case.args.update(reference_start=admission, user_text=document.display_text())
    await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    fixture.case.scope = ReferenceScope(
        **fixture.scope.model_dump(exclude={"session_id"}), session_id="s_reference", policy_epoch=admission.policy_epoch
    )
    path = fixture.case.fixture.db_path
    clarifications = ClarificationStore(path)
    registry = build_default_registry()
    adapter = adapter_for("openai")
    router = router_for(adapter)
    orchestrator = Orchestrator(
        router=router,
        datarc=None,
        session_store=fixture.case.store,
        tool_registry=registry,
        event_registry=EventBusRegistry(default_log_sink=OrderedTurnLogSink(TurnLogStore(path))),
        user_store=fixture.case.fixture.users,
        clarification_store=clarifications,
    )
    orchestrator.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=fixture.case.fixture.users)
    orchestrator.dispatch_owner_id = "system-gateway-worker"
    orchestrator.reference_skill_sources_factory = lambda scope: build_reference_skill_sources(
        path, scope=scope, builtin_index=fixture.sources.builtin_index, builtin_root=fixture.root
    )
    request = TurnRequest(
        **fixture.case.scope.identity(),
        member_uid=fixture.scope.user_id,
        turn_id="t_reference",
        text=document.display_text(),
        reference_scope=fixture.case.scope,
        planning_mode="off",
        team_router=router,
        datarc_client=object(),
        accessible_datarc_workspaces=({"id": "project_a", "name": "A"}, {"id": "project_b", "name": "B"}),
    )
    case = SimpleNamespace(
        base=SimpleNamespace(fixture=fixture), case=fixture.case, path=path, request=request, orchestrator=orchestrator, clarifications=clarifications
    )
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
            question = await pause(case)
            resumed = await resolve_request(case, question)
            route.mock(side_effect=answer_response)
            spawned = await spawn_resume_turn(
                orchestrator,
                resumed,
                team_id=resumed.team_id,
                initial_seq=max(row["seq"] for row in rows(case, "turn_events")),
                resume_notice={"clarification_id": question.request_id},
            )
            await settle_worker(spawned.task)
            assert route.call_count == 2
        assert (await fixture.case.store.get_turn("t_reference")).status == "completed"
        assert rows(case, "turn_agent_execution")[0]["agent_source_kind"] == "system"
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
    finally:
        import asyncio

        from bowhead_gateway.routes.chat import _inflight_tasks

        pending = [task for task in _inflight_tasks if task.get_name().endswith(":t_reference") and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await adapter.close()
