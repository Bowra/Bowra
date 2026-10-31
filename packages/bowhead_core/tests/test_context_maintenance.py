"""Inline maintenance shares actual Router rendering and the original decisions."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.context_maintenance import ContextMaintenance, recoverable_context_target
from bowhead_router.output_budget import ContextCapacityError
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router, _inject_runtime_context
from bowhead_router.types import LLMRequest, LLMResponse, Message, ToolCall, ToolSchema

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "kind,visible,expected", [("context_overflow", False, ("fallback", "small")), ("permission", False, None), ("context_overflow", True, None)]
)
def test_recovery_uses_only_the_final_rejected_target_without_visible_output(kind, visible, expected):
    from bowhead_router.errors import InvocationTargetsExhaustedError, LLMTargetFailure, ProviderFailureKind

    failure = LLMTargetFailure(
        provider="fallback",
        model="small",
        provider_family="openai",
        mode="invoke",
        target_index=2,
        attempt=1,
        kind=ProviderFailureKind(kind),
        retryable=False,
        first_visible_chunk_emitted=visible,
    )
    primary = replace(failure, provider="primary", model="large", target_index=1, kind=ProviderFailureKind.NETWORK, first_visible_chunk_emitted=False)
    assert recoverable_context_target(InvocationTargetsExhaustedError([primary, failure])) == expected


class Decisions:
    def __init__(self, limit=4):
        self.used = 0
        self.budget = SimpleNamespace(step_limit=limit)

    def admit(self):
        if self.used >= self.budget.step_limit:
            return False
        self.used += 1
        return True

    def owned(self):
        return nullcontext()


@pytest.fixture
async def context_case(monkeypatch):
    registry = ProviderRegistry()
    registry.load(
        [
            ProviderConfig(
                name="provider",
                type="openai_compatible",
                base_url="https://example.com/v1",
                models=[{"id": "model", "context_window": 32000, "context_policy": {"trigger_input_tokens": 5000, "keep_recent_tokens": 1000}}],
            )
        ]
    )
    router = Router(registry)
    router.bind(AgentBinding(agent_type="lead", primary=("provider", "model")))
    router.set_context_windows({("provider", "model"): 32000})
    old = [Message(role="user" if i % 2 == 0 else "assistant", content="evidence " * 1500) for i in range(8)]
    current = [
        Message(role="user", content="Continue the requested investigation."),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="read_source", arguments={})]),
        Message(role="tool", name="read_source", tool_call_id="c1", content="Verified source /policy/refunds, amount 175 USD."),
    ]
    request = LLMRequest(
        model="",
        system="Use verified evidence.",
        messages=old + current,
        tools=[ToolSchema(name="read_source", description="Read source.", input_schema={"type": "object"})],
    )
    adapter = registry.get("provider")
    sent = []
    response = LLMResponse(
        content="Goal: investigate refunds. Earlier source is /policy/refunds; the verified amount is 175 USD. Continue the unresolved request.",
        finish_reason="stop",
    )

    async def invoke(req):
        adapter._build_body(req, stream=False)
        sent.append(req)
        return response

    monkeypatch.setattr(adapter, "invoke", invoke)
    args = {
        "router": router,
        "agent_type": "lead",
        "profile": "lead",
        "user_pref": None,
        "controller": Decisions(),
        "start": 0,
        "protected_from": len(old),
        "fixed_messages": current,
        "max_growth_tokens": 12500,
        "timeout": 10,
        "authorize": AsyncMock(),
        "commit": AsyncMock(),
        "started": AsyncMock(),
    }
    yield SimpleNamespace(router=router, registry=registry, request=request, current=current, sent=sent, response=response, args=args)
    await registry.close_all()


async def test_summary_keeps_tools_and_prefix_without_executing_and_commits_before_adoption(context_case):
    case = context_case
    seen = []

    async def commit(candidate):
        assert case.request.messages[0].content.startswith("evidence")
        seen.append(candidate)

    result = await ContextMaintenance().prepare(case.request, **{**case.args, "commit": commit})
    assert result.candidate is seen[0]
    assert case.args["controller"].used == 1
    assert case.args["controller"].admit()
    assert case.args["controller"].used == 2
    assert len(case.sent) == 1
    assert case.sent[0].summary_only and case.sent[0].tool_choice == "none"
    assert case.sent[0].system == _inject_runtime_context(case.request).system
    assert case.sent[0].tools == case.request.tools
    assert case.sent[0].messages[:-1] == case.request.messages
    assert result.request.messages[-3:] == case.current
    assert result.candidate.after.budget.input_estimate < result.candidate.before.budget.input_estimate


async def test_last_ordinary_decision_is_not_spent_on_preventive_compaction(context_case):
    case = context_case
    controller = Decisions(limit=1)
    result = await ContextMaintenance().prepare(case.request, **{**case.args, "controller": controller})
    assert result.request is case.request
    assert result.skipped == "insufficient_remaining_decisions"
    assert controller.used == 0 and controller.admit()
    assert case.sent == []


async def test_transient_preventive_summary_failure_keeps_the_admissible_request(context_case, monkeypatch):
    case = context_case
    monkeypatch.setattr(case.router, "invoke", AsyncMock(side_effect=TimeoutError()))
    result = await ContextMaintenance().prepare(case.request, **case.args)
    assert result.request is case.request and result.skipped == "summary_call_failed"
    assert result.attempted and case.args["controller"].used == 1
    case.args["commit"].assert_not_awaited()
    assert case.args["authorize"].await_count == 3


async def test_manual_summary_failure_is_not_reported_as_success(context_case, monkeypatch):
    case = context_case
    monkeypatch.setattr(case.router, "invoke", AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        await ContextMaintenance().prepare(case.request, **{**case.args, "trigger": "manual"})
    case.args["commit"].assert_not_awaited()


async def test_summary_transport_restriction_does_not_block_fitting_ordinary_input(context_case, monkeypatch):
    from bowhead_router.input_admission import SummaryTransportUnsupported

    case = context_case
    original = case.router.request_context_budget

    async def measure(agent_type, request, **kwargs):
        if request.summary_only:
            raise SummaryTransportUnsupported("llm_summary_tool_dispatch_forbidden")
        return await original(agent_type, request, **kwargs)

    monkeypatch.setattr(case.router, "request_context_budget", measure)
    result = await ContextMaintenance().prepare(case.request, **case.args)
    assert result.request is case.request and result.skipped == "summary_transport_unavailable"
    assert not result.attempted and case.args["controller"].used == 0
    assert case.sent == []
    case.args["commit"].assert_not_awaited()


@pytest.mark.parametrize(
    "kind,continues",
    [("request_rejected", True), ("response_invalid", True), ("context_overflow", True), ("permission", False), ("auth", False), ("unknown", False)],
)
async def test_summary_failure_does_not_expand_the_ordinary_failure_boundary(context_case, monkeypatch, kind, continues):
    from bowhead_router.errors import InvocationTargetsExhaustedError, LLMTargetFailure, ProviderFailureKind

    case = context_case
    failure = InvocationTargetsExhaustedError(
        [
            LLMTargetFailure(
                provider="provider",
                model="model",
                provider_family="openai",
                mode="invoke",
                target_index=1,
                attempt=1,
                kind=ProviderFailureKind(kind),
                retryable=False,
            )
        ]
    )
    monkeypatch.setattr(case.router, "invoke", AsyncMock(side_effect=failure))
    if continues:
        result = await ContextMaintenance().prepare(case.request, **case.args)
        assert result.request is case.request and result.skipped == "summary_call_failed"
        assert case.args["controller"].used == 1
    else:
        with pytest.raises(InvocationTargetsExhaustedError):
            await ContextMaintenance().prepare(case.request, **case.args)
    case.args["commit"].assert_not_awaited()


async def test_explicit_off_preserves_all_messages_and_still_enforces_hard_capacity(context_case):
    case = context_case
    declaration = case.registry.definition_for("provider")["models"][0]
    declaration["context_policy"]["enabled"] = False
    original = case.request.messages[:]
    result = await ContextMaintenance().prepare(case.request, **case.args)
    assert result.skipped == "automatic_compaction_disabled"
    assert case.request.messages == original and case.sent == []
    huge = replace(case.request, messages=[Message(role="user", content="evidence " * 60000)])
    with pytest.raises(ContextCapacityError):
        await ContextMaintenance().prepare(huge, **{**case.args, "protected_from": 0})
    assert case.sent == []


async def test_invalid_summary_does_not_adopt_or_repeat_unchanged_source(context_case):
    case = context_case
    case.response.tool_calls = [ToolCall(id="unsafe", name="read_source", arguments={})]
    owner = ContextMaintenance()
    first = await owner.prepare(case.request, **case.args)
    second = await owner.prepare(case.request, **case.args)
    assert first.skipped == "summary_result_invalid"
    assert second.skipped == "unchanged_source_already_attempted"
    assert len(case.sent) == 1
    case.args["commit"].assert_not_awaited()


async def test_commit_failure_and_revoked_source_never_adopt(context_case):
    case = context_case
    case.args["commit"].side_effect = RuntimeError("injected commit failure")
    with pytest.raises(RuntimeError, match="commit failure"):
        await ContextMaintenance().prepare(case.request, **case.args)
    assert case.request.messages[0].content.startswith("evidence")
    case.args["authorize"].side_effect = PermissionError("source revoked")
    with pytest.raises(PermissionError, match="source revoked"):
        await ContextMaintenance().prepare(case.request, **case.args)
    assert len(case.sent) == 1


async def test_manual_maintenance_works_with_auto_off_and_one_decision(context_case):
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    result = await ContextMaintenance().prepare(case.request, **{**case.args, "controller": Decisions(limit=1), "trigger": "manual"})
    assert result.candidate is not None
    assert len(case.sent) == 1


async def test_selected_small_fallback_is_summarized_on_fitting_admitted_primary(context_case):
    case = context_case
    case.registry.definition_for("provider")["models"].append({"id": "small", "context_window": 8000, "context_policy": {"keep_recent_tokens": 1000}})
    case.router.bind(AgentBinding(agent_type="lead", primary=("provider", "model"), fallback=[("provider", "small")]))
    case.router.set_context_windows({("provider", "model"): 32000, ("provider", "small"): 8000})
    result = await ContextMaintenance().prepare(case.request, **{**case.args, "target": ("provider", "small"), "trigger": "capacity"})
    assert result.candidate is not None
    assert case.sent[0].model == "model"
    assert result.candidate.after.model == "small"
    assert result.candidate.after.budget.fits
    assert len(case.sent) == 1


async def test_source_revoked_after_summary_cannot_be_committed(context_case):
    case = context_case
    case.args["authorize"].side_effect = [None, None, PermissionError("source revoked")]
    with pytest.raises(PermissionError, match="source revoked"):
        await ContextMaintenance().prepare(case.request, **case.args)
    assert len(case.sent) == 1
    case.args["commit"].assert_not_awaited()


async def test_cancellation_during_summary_does_not_adopt_or_continue(context_case, monkeypatch):
    import asyncio

    case = context_case
    started = asyncio.Event()

    async def wait_for_cancel(_request):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(case.registry.get("provider"), "invoke", wait_for_cancel)
    owner = ContextMaintenance()
    task = asyncio.create_task(owner.prepare(case.request, **case.args))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    case.args["commit"].assert_not_awaited()
    assert case.args["controller"].used == 1
