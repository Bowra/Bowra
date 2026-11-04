"""Exercise recovery and language guidance through the real subagent runner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.language_context import LanguageContext
from bowhead_core.resource_disable import DisableState
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner, _unasked_resolve_question
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec, _caller_sandbox_entry
from bowhead_core.types import ToolInvocation
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, ToolCall, ToolSchema
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from bowhead_skill.sandbox_call_shape import sandbox_inputs_hint

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tool_name", ["bi_concept_resolve", "bi_query", "teammcp.catalog.search", "skill_invoke"])
def test_resolver_question_protocol_requires_its_producer(tool_name: str) -> None:
    question = {"question": "Which sales metric?", "options": ["Net sales", "Gross sales"], "type": "single_select"}
    payload = {"delivery_surface": "chat", "exec_summary": "Sales metrics are available."}
    invocations = [
        ToolInvocation(
            tool_name=tool_name,
            arguments={},
            result={"results": [{"next_action_hint": "call_clarification_ask", "clarification_payload": {"questions": [question]}}]},
            started_at="2026-10-29T00:00:00Z",
            ended_at="2026-10-29T00:00:01Z",
        ),
        ToolInvocation(
            tool_name="subagent_return",
            arguments={"payload": payload},
            result=payload,
            started_at="2026-10-29T00:00:01Z",
            ended_at="2026-10-29T00:00:02Z",
        ),
    ]

    promoted = _unasked_resolve_question(payload, invocations)

    if tool_name == "bi_concept_resolve":
        assert promoted is not None
        assert promoted["error"]["kind"] == "needs_user_input"
        assert promoted["error"]["clarification_payload"] == {"questions": [question]}
    else:
        assert promoted is None


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
async def test_disabled_review_preserves_tool_recovery_guidance(monkeypatch: pytest.MonkeyPatch, distinct_arguments: bool) -> None:
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "0")
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


@pytest.mark.parametrize("covered", [False, True], ids=["required-gap", "equivalent-table"])
async def test_delivery_review_preserves_execution_success_and_reports_requirement_status(monkeypatch: pytest.MonkeyPatch, covered: bool) -> None:
    from bowhead_core.subagent_return_tool import register_subagent_return_tool

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    events = []

    class RecordingBus(TurnEventBus):
        async def emit(self, event):
            events.append(event)
            return await super().emit(event)

    async def chart(_args, _ctx):
        return {"ok": False, "error_kind": "invalid_chart_expression", "path": "/column_units"}

    async def table(_args, _ctx):
        return {"ok": True, "columns": ["Store", "Revenue"], "rows": [["Store 1", 100]]}

    registry = ToolRegistry()
    registry.register(ToolSpec(ToolSchema("add_chart", "", {}), chart))
    registry.register(ToolSpec(ToolSchema("add_table", "", {}), table))
    register_subagent_return_tool(registry)
    review = {
        "requirements": [
            {
                "requirement": "Store performance",
                "status": "fulfilled" if covered else "unfulfilled",
                "evidence_call_ids": ["table"] if covered else [],
            }
        ],
        "issues": [
            {
                "call_id": "chart",
                "resolution": "covered" if covered else "unfulfilled",
                "explanation": "The table contains the requested store ranking." if covered else "The required ranking was not produced.",
                "evidence_call_ids": ["table"] if covered else [],
            }
        ],
    }
    payload = {"delivery_surface": "chat", "exec_summary": "Confirmed annual revenue.", "delivery_review": review}
    responses = [LLMResponse(content="", tool_calls=[ToolCall(id="chart", name="add_chart", arguments={})])]
    if covered:
        responses.append(LLMResponse(content="", tool_calls=[ToolCall(id="table", name="add_table", arguments={})]))
    responses.extend(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="premature", name="subagent_return", arguments={"payload": {"delivery_surface": "chat", "exec_summary": "Done"}})
                ],
            ),
            LLMResponse(content="", tool_calls=[ToolCall(id="reviewed", name="subagent_return", arguments={"payload": payload})]),
        ]
    )
    profile = _profile(monkeypatch, ("add_chart", "add_table", "subagent_return"))
    result = await SubagentRunner(router=_ScriptedRouter(*responses), tool_registry=registry).run(
        profile_name=profile.name,
        task="Deliver store performance.",
        parent_turn_id="t_guidance",
        parent_bus=RecordingBus(),
        parent_ctx=_context(),
    )
    assert result.ok, result.error
    assert result.completion == ("complete" if covered else "partial")
    assert result.payload["exec_summary"] == "Confirmed annual revenue."
    assert result.payload.get("caveats")
    if covered:
        assert "table" in result.payload["caveats"][0]
    terminal = [event.payload for event in events if event.event_type == "tool_result" and event.payload.get("call_id") == "reviewed"]
    assert terminal and terminal[0]["ok"] is True
    assert any(event.event_type == "step_done" and event.payload["state"] == ("success" if covered else "partial") for event in events)


async def test_repeated_terminal_review_is_bounded_without_triggering_tool_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.subagent_return_tool import register_subagent_return_tool

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    registry = ToolRegistry()

    async def chart(_args, _ctx):
        return {"ok": False, "error_kind": "invalid_chart_expression"}

    registry.register(ToolSpec(ToolSchema("add_chart", "", {}), chart))
    register_subagent_return_tool(registry)
    router = _ScriptedRouter(
        LLMResponse(content="", tool_calls=[ToolCall(id="chart", name="add_chart", arguments={})]),
        *[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"return_{i}",
                        name="subagent_return",
                        arguments={"payload": {"delivery_surface": "chat", "exec_summary": "Confirmed annual revenue."}},
                    )
                ],
            )
            for i in range(3)
        ],
    )
    profile = _profile(monkeypatch, ("add_chart", "subagent_return"))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Deliver store performance.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=_context(),
    )
    assert result.ok and result.completion == "partial"
    assert len(router.requests) == 4
    assert len([inv for inv in result.tool_invocations if inv.tool_name == "subagent_return"]) == 3
    assert result.payload["caveats"]


@pytest.mark.parametrize("parallel", ["0", "1"], ids=["sequential", "parallel"])
async def test_terminal_review_waits_for_all_sibling_outcomes(monkeypatch: pytest.MonkeyPatch, parallel: str) -> None:
    import asyncio

    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_delivery_review import DeliveryReviewRefusal

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", parallel)
    registry = ToolRegistry()

    async def chart(_args, _ctx):
        await asyncio.sleep(0.01)
        return {"ok": False, "error_kind": "invalid_chart_expression"}

    registry.register(ToolSpec(ToolSchema("add_chart", "", {}), chart))
    register_subagent_return_tool(registry)
    review = {
        "requirements": [{"requirement": "Store analysis", "status": "unfulfilled", "evidence_call_ids": []}],
        "issues": [{"call_id": "chart", "resolution": "unfulfilled", "explanation": "The sibling chart failed.", "evidence_call_ids": []}],
    }
    router = _ScriptedRouter(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="premature", name="subagent_return", arguments={"payload": {"delivery_surface": "chat", "exec_summary": "Done"}}),
                ToolCall(id="chart", name="add_chart", arguments={}),
            ],
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="reviewed",
                    name="subagent_return",
                    arguments={"payload": {"delivery_surface": "chat", "exec_summary": "Confirmed results retained.", "delivery_review": review}},
                )
            ],
        ),
    )
    profile = _profile(monkeypatch, ("add_chart", "subagent_return"))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Deliver store analysis.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=_context(),
    )
    assert result.ok and result.completion == "partial"
    assert len(router.requests) == 2
    assert isinstance(result.tool_invocations[0].result, DeliveryReviewRefusal)
    assert result.payload["exec_summary"] == "Confirmed results retained."


async def test_default_review_discloses_unverified_plain_text_closeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    registry = ToolRegistry()

    async def validate(_args, _ctx):
        return {"ok": False, "error_kind": "invalid_args"}

    async def inspect(_args, _ctx):
        return {"ok": True, "revision": "v2"}

    registry.register(ToolSpec(ToolSchema("document_validate", "", {}), validate))
    registry.register(ToolSpec(ToolSchema("document_inspect", "", {}), inspect))
    router = _ScriptedRouter(
        LLMResponse(content="", tool_calls=[ToolCall(id="validate", name="document_validate", arguments={})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="inspect", name="document_inspect", arguments={})]),
        LLMResponse(content="The current document revision is v2."),
        LLMResponse(content="The current document revision is v2; validation remains unverified."),
    )
    profile = _profile(monkeypatch, ("document_validate", "document_inspect"))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Inspect and validate the document revision.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=_context(),
    )
    assert result.ok and result.completion == "partial"
    assert len(router.requests) == 4
    assert "v2" in result.final_answer and "unverified" in result.final_answer
    assert [inv.call_id for inv in result.tool_invocations] == ["validate", "inspect"]


@pytest.mark.parametrize("chart_available", [False, True])
async def test_chart_guidance_follows_admitted_tool_availability(monkeypatch: pytest.MonkeyPatch, chart_available: bool) -> None:
    from bowhead_core.canvas_tools import _add_chart_schema

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "0")
    schema = _add_chart_schema()

    async def chart(_args: dict, _ctx: ToolExecutionContext) -> dict:
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=schema, handler=chart, visible_to_llm=chart_available))
    router = _ScriptedRouter(LLMResponse(content="The existing chart remains available."))
    profile = _profile(monkeypatch, ("add_chart",))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Inspect the available chart contract.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=_context(),
    )
    assert result.ok, result.error
    system = router.requests[0].system or ""
    assert ("This execution uses explicit native presentation" in system) == chart_available
