"""Exercise recovery and language guidance through the real subagent runner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace

import pytest
from bowhead_core.agent_run_limits import reset_tool_call_budget, set_tool_call_budget
from bowhead_core.events import TurnEventBus
from bowhead_core.language_context import LanguageContext
from bowhead_core.resource_disable import DisableState
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec, _caller_sandbox_entry
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, ToolCall, ToolSchema
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from bowhead_skill.sandbox_call_shape import sandbox_inputs_hint

pytestmark = pytest.mark.unit


class _ScriptedRouter:
    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def invoke(self, _agent_type: str, request: LLMRequest, **_kwargs: object) -> LLMResponse:
        self.requests.append(deepcopy(request))
        return next(self.responses)

    async def stream(self, agent_type: str, request: LLMRequest, **kwargs: object) -> AsyncIterator[LLMChunk]:
        response = await self.invoke(agent_type, request, **kwargs)
        for call in response.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        if response.content:
            yield LLMChunk(delta=response.content)
        yield LLMChunk(finish_reason="tool_use" if response.tool_calls else "stop")


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_guidance",
        member_uid="m_guidance",
        user_id="u_guidance",
        turn_id="t_guidance",
        datarc=None,
        language_context=LanguageContext(ui_language="en-US", output_language="en-US", data_language="zh-CN"),
    )


def _profile(monkeypatch: pytest.MonkeyPatch, tools: tuple[str, ...]) -> SubagentProfile:
    profile = SubagentProfile(
        name="tool-guidance-test",
        llm_agent_type="analyst",
        tool_whitelist=tools,
        system_prompt="Complete the assigned task using available Tools and verified results.",
        max_iterations=8,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    return profile


@pytest.mark.parametrize("distinct_arguments", [False, True], ids=["same-call-refusal", "tool-circuit-breaker"])
async def test_failed_tool_recovers_with_an_available_tool(monkeypatch: pytest.MonkeyPatch, distinct_arguments: bool) -> None:
    attempted: list[str] = []

    async def validate(_args: dict, _ctx: ToolExecutionContext) -> dict:
        attempted.append("validate")
        return {"ok": False, "error": "document_revision_missing"}

    async def inspect(_args: dict, _ctx: ToolExecutionContext) -> dict:
        attempted.append("inspect")
        return {"ok": True, "revision": "v2"}

    registry = ToolRegistry()
    for name, handler in (("document_validate", validate), ("document_inspect", inspect)):
        registry.register(ToolSpec(schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}), handler=handler))
    failure_limit = 3 if distinct_arguments else 2
    responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id=f"fail_{index}", name="document_validate", arguments={"revision": str(index) if distinct_arguments else "missing"})
            ],
        )
        for index in range(failure_limit + 1)
    ]
    router = _ScriptedRouter(
        *responses,
        LLMResponse(content="", tool_calls=[ToolCall(id="inspect", name="document_inspect", arguments={})]),
        LLMResponse(content="The current document revision is v2; validation remains incomplete."),
    )
    profile = _profile(monkeypatch, ("document_validate", "document_inspect"))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Inspect the document revision.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=_context(),
    )

    assert result.ok, result.error
    assert attempted == ["validate"] * failure_limit + ["inspect"]
    refusal = next(message for message in router.requests[-1].messages if message.tool_call_id == f"fail_{failure_limit}")
    assert refusal.content.startswith("circuit_broken:" if distinct_arguments else "soft_refuse:")
    assert "available" in refusal.content
    assert "bi_query" not in refusal.content and "python_sandbox" not in refusal.content
    assert "v2" in result.final_answer


@pytest.mark.parametrize(
    ("tool_name", "registered", "visible", "unconfigured", "expect_constraint"),
    [
        ("bi_query", True, True, False, True),
        ("bi_query", False, True, False, False),
        ("bi_query", True, False, False, False),
        ("bi_query", True, True, True, False),
        ("bi_schema", True, True, False, True),
        ("bi_custom_lookup", True, True, False, True),
        ("document_inspect", True, True, False, False),
    ],
    ids=["query-only", "unregistered", "hidden", "unconfigured", "schema-only", "new-connector-tool", "non-bi"],
)
async def test_language_guidance_follows_advertised_connector_tools(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, registered: bool, visible: bool, unconfigured: bool, expect_constraint: bool
) -> None:
    async def read(_args: dict, _ctx: ToolExecutionContext) -> dict:
        return {"ok": True}

    registry = ToolRegistry()
    if registered:
        registry.register(
            ToolSpec(schema=ToolSchema(name=tool_name, description="Read", input_schema={"type": "object"}), handler=read, visible_to_llm=visible)
        )
    profile = _profile(monkeypatch, (tool_name,))
    ctx = _context()
    if unconfigured:
        ctx.unconfigured_capability_tools = frozenset({tool_name})
    router = _ScriptedRouter(LLMResponse(content="No data was requested."))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name, task="Acknowledge the task.", parent_turn_id="t_guidance", parent_bus=TurnEventBus(), parent_ctx=ctx
    )

    assert result.ok, result.error
    request = router.requests[0]
    assert [schema.name for schema in request.tools] == ([tool_name] if registered and visible and not unconfigured else [])
    prompt = "\n".join([request.system or "", *(message.content or "" for message in request.messages)])
    assert ("Tool 调用约束" in prompt) is expect_constraint
    assert "data_language=zh-CN" in prompt
    assert "bi_concept_resolve" not in prompt
    assert "bi_schema()" not in prompt


async def test_legacy_query_limit_closes_out_without_suggesting_unavailable_tools() -> None:
    attempted = 0

    async def query(_args: dict, _ctx: ToolExecutionContext) -> dict:
        nonlocal attempted
        attempted += 1
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="bi_query", description="Query", input_schema={"type": "object"}), handler=query))
    ctx = _context()
    ctx._psi_bi_query_count = 20
    budget_token = set_tool_call_budget(None)
    try:
        invocation = await SubagentRunner(router=_ScriptedRouter(), tool_registry=registry)._execute_tool(
            ToolCall(id="budget", name="bi_query", arguments={"text": "sales"}),
            ctx,
            TurnEventBus(),
            "t_guidance",
            "sub_guidance",
            running_profile="custom",
        )
    finally:
        reset_tool_call_budget(budget_token)

    assert attempted == 0
    assert invocation.error.startswith("budget_exhausted:")
    assert "verified" in invocation.error
    assert "python_sandbox" not in invocation.error and "subagent_return" not in invocation.error


@pytest.mark.parametrize("availability", ["available", "unregistered", "hidden", "unconfigured"])
async def test_capability_protocol_follows_the_advertised_tools(monkeypatch: pytest.MonkeyPatch, availability: str) -> None:
    async def read(_args: dict, _ctx: ToolExecutionContext) -> dict:
        return {"ok": True}

    registry = ToolRegistry()
    if availability != "unregistered":
        registry.register(
            ToolSpec(
                schema=ToolSchema(name="bi_query", description="Query", input_schema={"type": "object"}),
                handler=read,
                visible_to_llm=availability != "hidden",
            )
        )
    profile = replace(_profile(monkeypatch, ("bi_query",)), capability_protocol_block=True)
    monkeypatch.setitem(PROFILES, profile.name, profile)
    ctx = _context()
    if availability == "unconfigured":
        ctx.unconfigured_capability_tools = frozenset({"bi_query"})
    router = _ScriptedRouter(LLMResponse(content="No data was requested."))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name, task="Acknowledge the task.", parent_turn_id="t_guidance", parent_bus=TurnEventBus(), parent_ctx=ctx
    )

    assert result.ok, result.error
    request = router.requests[0]
    assert [schema.name for schema in request.tools] == (["bi_query"] if availability == "available" else [])
    assert ("# Data query protocol" in request.system) is (availability == "available")


@pytest.mark.parametrize(
    ("flat_entry", "sandbox_permission", "expected_entry"),
    [
        ("available", "allowed", "python_exec"),
        ("unregistered", "allowed", "skill_invoke"),
        ("hidden", "allowed", "skill_invoke"),
        ("unconfigured", "allowed", "skill_invoke"),
        ("available", "disabled", None),
        ("unregistered", "allowlisted-other", None),
        ("available", "read-only", None),
        ("available", "scope-other", None),
    ],
)
async def test_sandbox_guidance_respects_the_skill_permission_without_removing_the_invoker(
    monkeypatch: pytest.MonkeyPatch, flat_entry: str, sandbox_permission: str, expected_entry: str | None
) -> None:
    entries: list[str | None] = []
    invoked: list[str] = []

    async def query(_args: dict, ctx: ToolExecutionContext) -> dict:
        entry = _caller_sandbox_entry(ctx)
        entries.append(entry)
        return {"ok": True, "hint": sandbox_inputs_hint(entry, artifact_id="art_guidance")}

    async def invoke(args: dict, _ctx: ToolExecutionContext) -> dict:
        invoked.append(args["name"])
        return {"ok": True, "checked": args["name"]}

    registry = ToolRegistry()
    for name, handler in (("bi_query", query), ("skill_invoke", invoke), ("python_exec", invoke)):
        if name == "python_exec" and flat_entry == "unregistered":
            continue
        registry.register(
            ToolSpec(
                schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}),
                handler=handler,
                visible_to_llm=name != "python_exec" or flat_entry != "hidden",
            )
        )
    profile = _profile(monkeypatch, ("bi_query", "python_exec", "skill_invoke"))
    if sandbox_permission == "allowlisted-other":
        profile = replace(profile, skill_invoke_allowlist=("document-check",))
    elif sandbox_permission in {"allowed", "read-only", "scope-other"}:
        read_names = ("document-check",) if sandbox_permission == "scope-other" else ("document-check", "python_sandbox")
        invoke_names = read_names if sandbox_permission == "allowed" else ("document-check",)
        profile = replace(profile, skill_runtime_scope=ExactSkillRuntimeScope(read_names, invoke_names))
    monkeypatch.setitem(PROFILES, profile.name, profile)
    ctx = _context()
    ctx.datarc = object()  # type: ignore[assignment]  # The fake query handler performs no I/O.
    ctx.active_datarc_project = "project_guidance"
    if flat_entry == "unconfigured":
        ctx.unconfigured_capability_tools = frozenset({"python_exec"})
    if sandbox_permission == "disabled":
        ctx.resource_disable_state = DisableState(team_builtin_plugins=frozenset({"code-computation"}))
    router = _ScriptedRouter(
        LLMResponse(content="", tool_calls=[ToolCall(id="query", name="bi_query", arguments={})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="check", name="skill_invoke", arguments={"name": "document-check"})]),
        LLMResponse(content="The document was checked."),
    )
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Query the data and check the document.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )

    assert result.ok, result.error
    assert "skill_invoke" in {schema.name for schema in router.requests[0].tools}
    assert invoked == ["document-check"]
    query_feedback = next(message.content for message in router.requests[-1].messages if message.tool_call_id == "query")
    assert entries == [expected_entry], query_feedback
    if expected_entry is not None:
        assert f"{expected_entry}(" in query_feedback
    else:
        assert "python_exec(" not in query_feedback and "skill_invoke(" not in query_feedback
