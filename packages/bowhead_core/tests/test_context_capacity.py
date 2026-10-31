"""Capacity-only projections preserve native history and protected input."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.context_capacity import capacity_message_groups
from bowhead_core.context_compaction import balanced_boundaries
from bowhead_core.context_maintenance import ContextMaintenance
from bowhead_router.output_budget import ContextCapacityError
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import Message, ToolCall
from test_context_maintenance import Decisions
from test_context_maintenance import context_case as maintenance_fixture

context_case = maintenance_fixture
pytestmark = pytest.mark.unit


def group(identity: str, content: str) -> list[Message]:
    return [
        Message(role="assistant", content="", tool_calls=[ToolCall(id=identity, name="read_source", arguments={"id": identity})]),
        Message(role="tool", name="read_source", tool_call_id=identity, content=content),
    ]


def test_capacity_groups_keep_turns_parallel_results_latest_correction_and_pending_tools():
    messages = [
        Message(role="user", content="Earlier question"),
        *group("old", "Earlier result"),
        Message(role="assistant", content="Earlier answer"),
        Message(role="user", content="Current task"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="parallel1", name="read_source", arguments={}), ToolCall(id="parallel2", name="read_source", arguments={})],
        ),
        Message(role="tool", name="read_source", tool_call_id="parallel2", content="Second result"),
        Message(role="tool", name="read_source", tool_call_id="parallel1", content="First result"),
        Message(role="user", content="Use the corrected amount"),
        *group("latest", "Unconsumed result"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="pending", name="read_source", arguments={})]),
    ]
    assert capacity_message_groups(messages, current_request=4, canonical_count=len(messages)) == ((0, 4), (5, 8))


@pytest.mark.parametrize("family", ["openai_compatible", "responses", "anthropic_compatible"])
async def test_off_uses_actual_target_rendering_without_summary_or_extra_decision(context_case, family):
    case = context_case
    model = {"id": "small", "context_window": 16000, "context_policy": {"enabled": False}}
    if family == "responses":
        model["api_surface"] = "responses"
    registry = ProviderRegistry()
    registry.load(
        [
            ProviderConfig(
                name="capacity", type="openai_compatible" if family == "responses" else family, base_url="https://example.com", models=[model]
            )
        ]
    )
    router = Router(registry)
    router.bind(AgentBinding(agent_type="lead", primary=("capacity", "small")))
    router.set_context_windows({("capacity", "small"): 16000})
    history = [Message(role="user" if i % 2 == 0 else "assistant", content=f"history-{i} " * 2500) for i in range(8)]
    messages = [*history, *deepcopy(case.current)]
    original = deepcopy(messages)
    request = replace(case.request, messages=messages, max_output_tokens=2048)
    groups = capacity_message_groups(messages, current_request=8, canonical_count=len(messages))
    try:
        result = await ContextMaintenance().prepare(
            request,
            **{**case.args, "router": router, "controller": Decisions(limit=0), "timeout": 0, "capacity_groups": groups},
        )
        assert result.trimmed_messages > 0
        assert result.budget.budget.fits
        assert result.request.messages[-3:] == original[-3:]
        assert result.request.system == request.system and result.request.tools == request.tools
        assert result.request.max_output_tokens == 2048
        assert request.messages == original
        assert not result.attempted and result.candidate is None and result.response is None
        assert not any(message.name == "context_summary" for message in result.request.messages)
        assert case.sent == []
        case.args["started"].assert_not_awaited()
        case.args["commit"].assert_not_awaited()
        # The immediately larger legal projection must not fit: preserve all
        # optional context that this physical target can actually accept.
        larger = replace(request, messages=messages[result.trimmed_messages - 2 :])
        budget = await router.request_context_budget("lead", larger, reject_over_capacity=False)
        assert not budget.budget.fits
    finally:
        await registry.close_all()


async def test_capacity_reduces_history_and_older_tool_groups_without_splitting_latest(context_case):
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    messages = [
        Message(role="user", content="Prior turn"),
        Message(role="assistant", content="history " * 10000),
        Message(role="user", content="Continue the current task"),
        *group("earlier", "large " * 50000),
        *group("latest", "The current result is 175 USD"),
        Message(role="user", content="Reply in the requested language"),
    ]
    request = replace(case.request, messages=messages)
    original = deepcopy(messages)
    result = await ContextMaintenance().prepare(
        request,
        **{**case.args, "capacity_groups": capacity_message_groups(messages, current_request=2, canonical_count=7)},
    )
    assert result.request.messages == [original[2], *original[5:]]
    assert result.trimmed_messages == 4 and result.budget.budget.fits
    assert balanced_boundaries(result.request.messages)[-1] == len(result.request.messages)
    assert messages == original


@pytest.mark.parametrize("enabled", [False, True])
async def test_current_request_and_required_output_cannot_be_evicted_to_fake_a_fit(context_case, enabled):
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = enabled
    current = Message(role="user", content="mandatory " * 60000)
    request = replace(case.request, messages=[*case.request.messages[:8], current], max_output_tokens=4096)
    with pytest.raises(ContextCapacityError):
        await ContextMaintenance().prepare(request, **{**case.args, "capacity_groups": ((0, 8),)})
    assert request.messages[-1] is current and request.max_output_tokens == 4096
    assert case.sent == []


async def test_smaller_fallback_keeps_off_and_trims_only_for_that_target(context_case):
    case = context_case
    definition = case.registry.definition_for("provider")
    definition["models"][0]["context_policy"]["enabled"] = False
    definition["models"].append({"id": "small", "context_window": 8000})
    case.router.bind(AgentBinding(agent_type="lead", primary=("provider", "model"), fallback=[("provider", "small")]))
    case.router.set_context_windows({("provider", "model"): 32000, ("provider", "small"): 8000})
    result = await ContextMaintenance().prepare(case.request, **{**case.args, "target": ("provider", "small"), "trigger": "capacity"})
    assert result.trimmed_messages > 0 and result.budget.model == "small"
    assert result.budget.budget.fits and result.budget.policy.enabled is False
    assert case.sent == []
    primary = await ContextMaintenance().prepare(case.request, **case.args)
    assert primary.request is case.request and primary.trimmed_messages == 0


async def test_upstream_recovery_uses_last_sent_projection_and_accepts_available_progress(context_case):
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    request = replace(case.request, messages=[Message(role="user", content="removable"), *case.current])
    before = await case.router.request_context_budget("lead", request)
    result = await ContextMaintenance().prepare(
        request,
        **{**case.args, "trigger": "capacity", "capacity_groups": ((0, 1),), "rejected_input_estimate": before.budget.input_estimate},
    )
    assert result.trimmed_messages == 1 and result.budget.budget.fits
    assert result.request.messages == case.current
    assert result.budget.budget.input_estimate > int(before.budget.input_estimate * 0.75)
    assert case.sent == []


async def test_capacity_projection_rechecks_authority_without_publishing_or_invoking(context_case):
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    messages = deepcopy(case.request.messages)
    for message in messages[:8]:
        message.content *= 5
    request = replace(case.request, messages=messages)
    authorize = AsyncMock(side_effect=[None, PermissionError("Source access revoked")])
    with pytest.raises(PermissionError, match="Source access revoked"):
        await ContextMaintenance().prepare(request, **{**case.args, "authorize": authorize})
    assert authorize.await_count == 2
    assert case.sent == []
    case.args["commit"].assert_not_awaited()
    case.args["started"].assert_not_awaited()


async def test_numeric_upstream_overage_keeps_more_history_than_unknown_overflow(context_case):
    from bowhead_router.errors import InvocationTargetsExhaustedError, LLMTargetFailure, ProviderFailureKind

    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    before = await case.router.request_context_budget("lead", case.request)
    maximum = before.budget.input_estimate - 100
    rejection = InvocationTargetsExhaustedError(
        [
            LLMTargetFailure(
                provider="provider",
                model="model",
                provider_family="openai",
                mode="invoke",
                target_index=1,
                attempt=1,
                kind=ProviderFailureKind.CONTEXT_OVERFLOW,
                retryable=False,
                upstream_message=f"This model's maximum context length is {maximum} tokens. However, your messages resulted in {maximum + 100} tokens.",
            ),
        ]
    )
    args = {**case.args, "trigger": "capacity", "rejected_input_estimate": before.budget.input_estimate}
    known = await ContextMaintenance().prepare(case.request, **args, rejection=rejection)
    unknown = await ContextMaintenance().prepare(case.request, **args)
    assert known.budget.budget.fits and unknown.budget.budget.fits
    assert 0 < known.trimmed_messages < unknown.trimmed_messages
    assert case.sent == []


@pytest.fixture
def pressured_context(context_case):
    case = context_case
    messages = [
        Message(role="user", content="Prior task"),
        Message(role="assistant", content="history " * 10000),
        Message(role="user", content="Continue after the latest confirmed result; do not repeat completed operations."),
        *group("completed1", "old " * 9000),
        *group("completed2", "old " * 9000),
        *group("completed3", "old " * 9000),
        *group("completed4", "old " * 9000),
        *group("latest", "Step 5 succeeded; continue with step 6."),
        Message(role="user", content="Reply in the requested language"),
    ]
    request = replace(case.request, messages=messages)
    args = {
        **case.args,
        "start": 3,
        "protected_from": 11,
        "fixed_messages": [*messages[:3], *messages[11:]],
        "capacity_groups": capacity_message_groups(messages, current_request=2, canonical_count=13),
    }
    return case, request, args


@pytest.mark.parametrize("invalid", ["truncated", "empty", "oversized", "tool_call"])
async def test_auto_on_unusable_summary_fits_by_trimming_without_adopting_or_repeating(pressured_context, invalid):
    case, request, args = pressured_context
    if invalid == "truncated":
        case.response.finish_reason = "length"
    elif invalid == "empty":
        case.response.content = ""
    elif invalid == "oversized":
        case.response.content = "summary " * 10000
    else:
        case.response.tool_calls = [ToolCall(id="forbidden", name="read_source", arguments={})]
    original = deepcopy(request.messages)
    usage = []
    before = await case.router.request_context_budget("lead", request, reject_over_capacity=False)
    assert not before.budget.fits
    owner = ContextMaintenance()
    result = await owner.prepare(request, **args, record_usage=usage.append)
    assert result.trimmed_messages > 2 and result.budget.budget.fits
    assert result.request.messages[0] == original[2]
    assert result.request.messages[-3:] == original[-3:]
    assert balanced_boundaries(result.request.messages)[-1] == len(result.request.messages)
    assert result.candidate is None and result.skipped == "summary_result_invalid"
    assert request.messages == original and usage == [case.response]
    assert len(case.sent) == 1 and case.sent[0].summary_only
    case.args["commit"].assert_not_awaited()
    again = await owner.prepare(request, **args)
    assert again.trimmed_messages > 0 and again.budget.budget.fits
    assert len(case.sent) == 1
    assert args["controller"].used == 1 and args["controller"].admit()


@pytest.mark.parametrize("trigger", ["threshold", "capacity"])
async def test_auto_on_summary_timeout_can_trim_the_actual_target(pressured_context, monkeypatch, trigger):
    case, request, args = pressured_context
    invoke = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(case.router, "invoke", invoke)
    result = await ContextMaintenance().prepare(request, **{**args, "trigger": trigger})
    assert result.budget.budget.fits and result.trimmed_messages > 2
    assert result.candidate is None and result.skipped == "summary_call_failed"
    assert result.request.messages[-3:] == request.messages[-3:]
    invoke.assert_awaited_once()
    case.args["commit"].assert_not_awaited()


async def test_auto_on_capacity_projection_rechecks_revocation_after_unusable_summary(pressured_context):
    case, request, args = pressured_context
    case.response.finish_reason = "length"
    authorize = AsyncMock(side_effect=[None, None, PermissionError("Source access revoked")])
    with pytest.raises(PermissionError, match="Source access revoked"):
        await ContextMaintenance().prepare(request, **{**args, "authorize": authorize})
    assert len(case.sent) == 1
    case.args["commit"].assert_not_awaited()


async def test_manual_unusable_summary_does_not_pretend_a_trim_is_compaction(pressured_context):
    case, request, args = pressured_context
    case.response.finish_reason = "length"
    with pytest.raises(ContextCapacityError):
        await ContextMaintenance().prepare(request, **{**args, "trigger": "manual"})
    assert len(case.sent) == 1
    case.args["commit"].assert_not_awaited()


async def test_auto_on_unusable_summary_trims_for_the_smaller_fallback_not_the_summary_model(pressured_context):
    case, request, args = pressured_context
    case.registry.definition_for("provider")["models"].append({"id": "small", "context_window": 16000})
    case.router.bind(AgentBinding(agent_type="lead", primary=("provider", "model"), fallback=[("provider", "small")]))
    case.router.set_context_windows({("provider", "model"): 32000, ("provider", "small"): 16000})
    case.response.finish_reason = "length"
    result = await ContextMaintenance().prepare(request, **{**args, "target": ("provider", "small"), "trigger": "capacity"})
    assert case.sent[0].model == "model"
    assert result.budget.model == "small" and result.budget.budget.context_window == 16000
    assert result.budget.budget.fits and result.trimmed_messages > 2
    assert result.request.messages[-3:] == request.messages[-3:]
    assert result.candidate is None
    case.args["commit"].assert_not_awaited()


async def test_auto_on_capacity_rejection_can_trim_even_when_local_estimate_fits(context_case, monkeypatch):
    from bowhead_router.input_admission import SummaryTransportUnsupported

    case = context_case
    original = case.router.request_context_budget

    async def measure(agent_type, request, **kwargs):
        if request.summary_only:
            raise SummaryTransportUnsupported("llm_summary_transport_unsupported")
        return await original(agent_type, request, **kwargs)

    monkeypatch.setattr(case.router, "request_context_budget", measure)
    before = await original("lead", case.request)
    assert before.budget.fits
    result = await ContextMaintenance().prepare(
        case.request, **{**case.args, "trigger": "capacity", "rejected_input_estimate": before.budget.input_estimate}
    )
    assert result.trimmed_messages > 0 and result.budget.budget.fits
    assert result.budget.budget.input_estimate < before.budget.input_estimate
    assert not result.attempted and case.sent == []
