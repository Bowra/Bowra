"""Published Skill permissions reach the real runner and handler boundaries.

These are operation-name guards only. Source/hash-bound materialization and
approval-context reconstruction remain separate release gates.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_bridge import _wrap_skill_execution_gate, _wrap_skill_read_with_cache
from bowhead_core.subagent import SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope, skill_operation_denial

from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, ToolCall


def _scope() -> ExactSkillRuntimeScope:
    return ExactSkillRuntimeScope(read_names=("method", "runner"), invoke_names=("runner",))


def _profile(scope):
    return SubagentProfile(
        name="peer", llm_agent_type="analyst", tool_whitelist=("skill_invoke",), system_prompt="Use declared skills.", skill_runtime_scope=scope
    )


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(workspace_id="ws_scope", member_uid="m", user_id="u", team_id="team_scope", turn_id="t_scope", datarc=None)


def _binding(*, grants=None) -> ExactAgentExecutionBinding:
    if grants is None:
        grants = [
            {
                "skill_ref": {"kind": "team", "id": f"oskill_{name}", "name": name},
                "revision": "sha256:" + "a" * 64,
                "distribution": "reference",
                "permissions": permissions,
            }
            for name, permissions in [("method", ["read"]), ("runner", ["invoke", "read"])]
        ]
    return ExactAgentExecutionBinding(
        execution_id="aex_scope",
        team_id="team_scope",
        turn_id="t_scope",
        agent_id="osa_scope",
        agent_source_kind="team",
        revision_ref="arv_scope",
        release_id="arl_scope",
        runtime_profile_name="scope-agent",
        capability_fingerprint="cap",
        policy_fingerprint="policy",
        definition={
            "skill_grants": grants,
            "tool_refs": ["skill_read", "skill_invoke", "python_exec"],
            "system_prompt": "Use declared methods.",
            "max_iterations": 3,
        },
        listing={"name": "scope-agent", "description": "Analyze with the declared methods."},
        status="admitted",
    )


def _router(*responses: LLMResponse) -> Router:
    adapter = MockAdapter(provider_name="scope-test")
    adapter.scripted_responses = list(responses)
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="scope-test", type="mock")])
    providers._adapters["scope-test"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("scope-test", "mock")))
    return router


@pytest.mark.parametrize("parallel", ["0", "1"])
async def test_published_permissions_reach_serial_and_parallel_handler_context(monkeypatch, parallel):
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "0")
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", parallel)
    seen = []
    retained = []
    registry = ToolRegistry()

    async def handler(args, ctx):
        await asyncio.sleep(0)
        seen.append((args["name"], ctx.skill_runtime_scope, ctx.role))
        retained.append(ctx.skill_runtime_grants)
        return {"ok": True}

    for name in ("skill_read", "skill_invoke", "python_exec"):
        registry.register(ToolSpec(schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}), handler=handler))
    runner = SubagentRunner(
        router=_router(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="read", name="skill_read", arguments={"name": "method"}),
                    ToolCall(id="invoke", name="skill_invoke", arguments={"raw_arguments": '{"name":"runner","script":"run.py"}'}),
                    ToolCall(id="deny", name="skill_invoke", arguments={"name": "method"}),
                    ToolCall(id="foreign", name="skill_read", arguments={"name": "private-other"}),
                    ToolCall(id="flat", name="python_exec", arguments={"name": "runner", "code": "print(1)"}),
                ],
            ),
            LLMResponse(content="Completed the allowed work."),
        ),
        tool_registry=registry,
    )
    parent = _ctx()
    parent.role = "lead"
    approval_checks = []

    async def maybe_gate(**kwargs):
        approval_checks.append((kwargs["tool_name"], kwargs["ctx"].skill_runtime_scope))
        return

    parent.approval_gate = SimpleNamespace(maybe_gate=maybe_gate)
    result = await runner.run(
        profile_name="scope-agent",
        task="Analyze",
        parent_turn_id=parent.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=parent,
        exact_binding=_binding(),
    )
    assert result.ok
    assert sorted(name for name, _, _ in seen) == ["method", "runner"]
    assert all(scope == _scope() and role == "analyst" for _, scope, role in seen)
    assert parent.skill_runtime_scope is None
    assert parent.skill_runtime_grants is None
    assert len(retained) == 2
    assert all([grant.to_canonical_dict() for grant in grants] == _binding().definition["skill_grants"] for grants in retained)
    assert parent.role == "lead"
    assert sorted(name for name, _ in approval_checks) == ["skill_invoke", "skill_read"]
    assert all(scope == _scope() for _, scope in approval_checks)
    # Resolving the frozen binding again (e.g. a resumed execution) retains the distinction.
    assert (await runner.resolve_by_agent_revision(_binding(), parent)).skill_runtime_scope == _scope()


@pytest.mark.parametrize(
    "tool,args",
    [
        ("skill_read", {"name": "private-other"}),
        ("skill_invoke", {"name": "method"}),
        ("skill_invoke", {"arguments": '{"name":"method","script":"run.py"}'}),
        ("python_exec", {"name": "runner", "code": "print(1)"}),
    ],
)
async def test_registry_rejects_scoped_operations_before_any_handler(tool, args):
    calls = []

    async def handler(args, ctx):
        calls.append(args)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name=tool, description=tool, input_schema={"type": "object"}), handler=handler))
    ctx = _ctx()
    ctx.skill_runtime_scope = _scope()
    result = await registry.execute(tool, args, ctx)
    skill = {"skill_read": "private-other", "python_exec": "python_sandbox"}.get(tool, "method")
    assert result.result == {
        "ok": False,
        "error_kind": "skill_unavailable",
        "skill": skill,
        "operation": "read" if tool == "skill_read" else "invoke",
        "reason": "not_granted",
        "error": result.result["error"],
        "retryable": False,
    }
    assert f"Skill '{skill}'" in result.result["error"]
    assert result.error == "ToolPermissionError: skill_unavailable"
    assert calls == []


async def test_scoped_skill_list_reaches_the_handler():
    calls = []

    async def handler(args, ctx):
        calls.append(args)
        return []

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="skill_list", description="list", input_schema={"type": "object"}), handler=handler))
    ctx = _ctx()
    ctx.skill_runtime_scope = _scope()
    result = await registry.execute("skill_list", {}, ctx)
    assert result.error is None and result.result == [] and calls == [{}]


def test_not_granted_messages_name_the_skill_and_the_fix():
    ctx = SimpleNamespace(skill_runtime_scope=_scope())
    missing = skill_operation_denial(ctx, "skill_read", {"name": "sales-report"})
    assert missing["error"] == "Skill 'sales-report' isn't included in this Agent. Ask the Agent's owner to add it in the Agent's configuration."
    read_only = skill_operation_denial(ctx, "skill_invoke", {"name": "method"})
    assert read_only["reason"] == "not_granted" and "for reading only" in read_only["error"] and "release the Agent again" in read_only["error"]
    unnamed = skill_operation_denial(ctx, "skill_invoke", {})
    assert unnamed["skill"] == "" and "skill_list" in unnamed["error"]


async def test_scope_check_precedes_shared_read_cache():
    calls = []

    async def read(args, ctx):
        calls.append(args["name"])
        return {"body": "method content"}

    handler = _wrap_skill_execution_gate("skill_read", _wrap_skill_read_with_cache(read), SimpleNamespace(get=lambda _: object()))
    ctx = _ctx()
    ctx.skill_runtime_scope = _scope()
    assert (await handler({"name": "method"}, ctx))["body"] == "method content"
    ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=(), invoke_names=())
    assert (await handler({"name": "method"}, ctx))["error_kind"] == "skill_unavailable"
    assert calls == ["method"]


@pytest.mark.parametrize("scope", [ExactSkillRuntimeScope(read_names=(), invoke_names=()), {"read_names": ["method"]}])
def test_empty_or_malformed_scope_does_not_become_legacy(scope):
    ctx = SimpleNamespace(skill_runtime_scope=scope)
    assert skill_operation_denial(ctx, "skill_read", {"name": "method"})["error_kind"] == "skill_unavailable"
    assert skill_operation_denial(ctx, "bi_query", {}) is None


async def test_sibling_dispatch_does_not_inherit_another_agents_scope():
    calls = []

    async def handler(args, ctx):
        await asyncio.sleep(0)
        calls.append(ctx.skill_runtime_scope)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="skill_invoke", description="invoke", input_schema={"type": "object"}), handler=handler))
    runner = SubagentRunner(router=_router(), tool_registry=registry)
    parent = _ctx()
    parent.skill_runtime_scope = ExactSkillRuntimeScope(read_names=(), invoke_names=())
    scope = _scope()
    allowed, legacy = await asyncio.gather(
        *[
            runner._execute_tool(
                ToolCall(id=f"call-{i}", name="skill_invoke", arguments={"name": "runner"}),
                parent,
                TurnEventBus(),
                parent.turn_id,
                f"sub_{i}",
                running_profile="peer",
                execution_profile=_profile(s),
            )
            for i, s in enumerate((scope, None))
        ]
    )
    assert allowed.error is None and legacy.error is None
    assert scope in calls and None in calls
    assert parent.skill_runtime_scope == ExactSkillRuntimeScope(read_names=(), invoke_names=())


async def test_empty_published_grants_remain_deny_all(monkeypatch):
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "0")
    runner = SubagentRunner(router=_router(), tool_registry=ToolRegistry())
    profile = await runner.resolve_by_agent_revision(_binding(grants=[]), _ctx())
    assert profile.skill_runtime_scope == ExactSkillRuntimeScope(read_names=(), invoke_names=())


@pytest.mark.parametrize("permissions", [[], ["invoke"], ["read", "read"], ["use"], [["read"]]])
async def test_malformed_published_permissions_cannot_become_legacy(monkeypatch, permissions):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError

    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "0")
    runner = SubagentRunner(router=_router(), tool_registry=ToolRegistry())
    grant = _binding().definition["skill_grants"][0]
    grant["permissions"] = permissions
    with pytest.raises(AgentExecutionBindingError, match="agent_snapshot_malformed"):
        await runner.resolve_by_agent_revision(_binding(grants=[grant]), _ctx())


async def test_scoped_dispatch_keeps_existing_result_context_handoff():
    async def handler(args, ctx):
        ctx._psi_compose_attempted = True
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="skill_invoke", description="invoke", input_schema={"type": "object"}), handler=handler))
    runner = SubagentRunner(router=_router(), tool_registry=registry)
    parent = _ctx()
    invocation = await runner._execute_tool(
        ToolCall(id="call", name="skill_invoke", arguments={"name": "runner"}),
        parent,
        TurnEventBus(),
        parent.turn_id,
        "sub_1",
        running_profile="peer",
        execution_profile=_profile(_scope()),
    )
    assert invocation.error is None
    assert parent._psi_compose_attempted is True
    assert parent.skill_runtime_scope is None


async def test_internal_dispatch_cannot_drop_inherited_scope_without_a_new_profile():
    calls = []
    approvals = []

    async def maybe_gate(**kwargs):
        approvals.append(kwargs)
        return

    async def handler(args, ctx):
        calls.append(args)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="skill_invoke", description="invoke", input_schema={"type": "object"}), handler=handler))
    runner = SubagentRunner(router=_router(), tool_registry=registry)
    parent = _ctx()
    parent.skill_runtime_scope = ExactSkillRuntimeScope(read_names=(), invoke_names=())
    parent.approval_gate = SimpleNamespace(maybe_gate=maybe_gate)
    invocation = await runner._execute_tool(
        ToolCall(id="call", name="skill_invoke", arguments={"name": "runner"}),
        parent,
        TurnEventBus(),
        parent.turn_id,
        "sub_1",
        running_profile="peer",
    )
    assert invocation.result["error_kind"] == "skill_unavailable"
    assert calls == []
    assert approvals == []


@pytest.mark.parametrize("reason", ["not_granted", "script_runtime_disabled", "use_denied", "pending_reapproval", "source_changed"])
def test_only_a_skill_that_changed_mid_call_is_marked_retryable(reason: str) -> None:
    from bowhead_skill.runtime_scope import skill_unavailable

    result = skill_unavailable("method", "invoke", reason)  # type: ignore[arg-type]
    assert result["retryable"] is (reason == "source_changed")
    # The sentence and the flag agree: only the retryable reason tells the caller to retry.
    assert ("Retry the call" in result["error"]) is result["retryable"]


async def test_denied_skill_step_carries_the_reason_code_and_skill():
    # The step payload is emitted live and persisted as is, so replay shows the same code.
    from bowhead_skill.runtime_scope import skill_unavailable

    calls = []

    async def handler(args, ctx):
        # The Skill is granted; the run-time decision denies it after the step started.
        calls.append(args)
        return skill_unavailable(args["name"], "invoke", "script_runtime_disabled")

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="skill_invoke", description="invoke", input_schema={"type": "object"}), handler=handler))
    runner = SubagentRunner(router=_router(), tool_registry=registry)
    bus = TurnEventBus()
    emitted = []
    emit = bus.emit

    async def capture(event):
        emitted.append(event)
        return await emit(event)

    bus.emit = capture  # type: ignore[method-assign]
    parent = _ctx()
    invocation = await runner._execute_tool(
        ToolCall(id="deny", name="skill_invoke", arguments={"name": "runner"}),
        parent,
        bus,
        parent.turn_id,
        "sub_1",
        running_profile="peer",
        execution_profile=_profile(_scope()),
    )
    assert invocation.result["reason"] == "script_runtime_disabled"
    assert len(calls) == 1
    (step,) = [event.payload for event in emitted if event.event_type == "step_done" and event.payload.get("call_id") == "deny"]
    assert step["state"] == "failure"
    assert step["error"] == {
        "type": "skill_unavailable",
        "code": "skill_unavailable_script_runtime_disabled",
        "message_zh": invocation.result["error"],
        "is_transient": False,
        "skill": "runner",
    }
