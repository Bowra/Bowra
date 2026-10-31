"""Compaction preserves protected original messages and complete Tool groups."""

from __future__ import annotations

from copy import deepcopy

import pytest
from bowhead_core.context_compaction import (
    CompactionPreparationError,
    balanced_boundaries,
    select_compaction_span,
    summarized_messages,
    summary_request,
)
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.parameter_preview import compile_request_body
from bowhead_router.types import LLMRequest, LLMResponse, Message, ToolCall, ToolSchema

pytestmark = pytest.mark.unit


def transcript():
    return [
        Message(role="user", content="Use source_12, EUR and 2025 values. Do not publish."),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="call_a", name="read", arguments={"source": "source_12"}),
                ToolCall(id="call_b", name="read", arguments={"source": "source_13"}),
            ],
        ),
        Message(role="tool", name="read", tool_call_id="call_b", content="Optional old source data. " * 100),
        Message(role="tool", name="read", tool_call_id="call_a", content="Revenue was EUR 1200. " * 100),
        Message(role="assistant", content="Both reads completed; values differ."),
        Message(role="user", content="Correction: use 2024 values; leave publication cancelled."),
    ]


def test_balanced_selection_preserves_active_request_and_complete_parallel_tools():
    messages = transcript()
    assert balanced_boundaries(messages) == (0, 1, 4, 5, 6)
    span = select_compaction_span(messages, start=0, protected_from=5, keep_recent_tokens=0)
    assert span is not None and span.end == 5
    before = deepcopy(messages)
    result = summarized_messages(
        messages,
        span,
        LLMResponse(content="Read source_12 and source_13. Revenue EUR 1200; values differ. No publication occurred."),
        max_summary_tokens=200,
    )
    assert messages == before
    assert result[-1] is messages[-1]
    assert result[0].role == "user"
    assert not result[0].tool_calls and not result[0].tool_call_id
    # Protection inside a transaction moves the cut before its call, never between results.
    protected = select_compaction_span(messages, start=0, protected_from=3, keep_recent_tokens=0)
    assert protected is not None and protected.end == 1


def test_current_request_can_remain_pinned_while_old_completed_work_is_summarized():
    messages = transcript()
    span = select_compaction_span(messages, start=1, protected_from=5, keep_recent_tokens=0)
    assert span is not None
    result = summarized_messages(messages, span, LLMResponse(content="Two reads completed; no publication."), max_summary_tokens=100)
    assert result[0] is messages[0]
    assert result[-1] is messages[-1]
    assert len(result) == 3
    assert select_compaction_span(messages, start=1, protected_from=5, keep_recent_tokens=100_000) is None


def test_completed_transactions_can_reuse_provider_local_call_ids():
    transaction = [
        Message(role="assistant", content="", tool_calls=[ToolCall(id="local-1", name="read", arguments={})]),
        Message(role="tool", content="Observed result", name="read", tool_call_id="local-1"),
    ]
    assert balanced_boundaries([*transaction, *deepcopy(transaction)]) == (0, 2, 4)


def test_protected_suffix_does_not_consume_recent_source_retention():
    messages = [Message(role="user", content="Earlier goal."), Message(role="assistant", content="Recent fact.")]
    small = [*messages, Message(role="user", content="Current request.")]
    large = [*messages, Message(role="user", content="Required material " * 5000)]
    first = select_compaction_span(small, start=0, protected_from=2, keep_recent_tokens=1)
    second = select_compaction_span(large, start=0, protected_from=2, keep_recent_tokens=1)
    assert first is not None and second is not None
    assert first.end == second.end == 1


@pytest.mark.parametrize("family", ["chat", "responses", "anthropic"])
def test_summary_retains_prefix_and_definitions_but_disables_tool_dispatch(family):
    messages = transcript()
    span = select_compaction_span(messages, start=0, protected_from=5, keep_recent_tokens=0)
    request = LLMRequest(
        model="model",
        messages=messages,
        system="Preserve evidence.",
        tools=[ToolSchema(name="read", description="Read a source.", input_schema={"type": "object"})],
    )
    before = deepcopy(request)
    prepared = summary_request(request, span, output_tokens=1000)
    definition = {
        "type": "anthropic_compatible" if family == "anthropic" else "openai_compatible",
        "models": [{"id": "model", "context_window": 128000, **({"api_surface": "responses"} if family == "responses" else {})}],
    }
    body = compile_request_body(definition, prepared)
    assert request == before
    assert prepared.messages[:-1] == request.messages
    assert prepared.system == request.system and prepared.tools == request.tools
    assert body["tools"]
    assert body["tool_choice"] == ({"type": "none"} if family == "anthropic" else "none")
    assert prepared.summary_only
    assert "[0, 5)" in prepared.messages[-1].content


@pytest.mark.parametrize("changes", ["append", "source", "protected"])
def test_delayed_summary_rejects_any_changed_source_boundary(changes):
    messages = transcript()
    span = select_compaction_span(messages, start=0, protected_from=5, keep_recent_tokens=0)
    if changes == "append":
        messages.append(Message(role="user", content="Stop."))
    else:
        messages[3 if changes == "source" else -1].content = "Changed"
    with pytest.raises(CompactionPreparationError, match="context_source_changed"):
        summarized_messages(messages, span, LLMResponse(content="Stale summary"), max_summary_tokens=100)


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(content=""),
        LLMResponse(content="Partial", finish_reason="length"),
        LLMResponse(content="Call", tool_calls=[ToolCall(id="write", name="publish", arguments={})]),
    ],
)
def test_rejected_summary_never_replaces_source(response):
    messages = transcript()
    before = deepcopy(messages)
    span = select_compaction_span(messages, start=0, protected_from=5, keep_recent_tokens=0)
    with pytest.raises(CompactionPreparationError, match="summary_incomplete"):
        summarized_messages(messages, span, response, max_summary_tokens=100)
    assert messages == before


def test_pending_tool_transaction_is_not_summary_input():
    messages = transcript()[:3]
    span = select_compaction_span(messages, start=0, protected_from=3, keep_recent_tokens=0)
    assert span is not None and span.end == 1
    with pytest.raises(CompactionPreparationError, match="pending_tool_call"):
        summary_request(LLMRequest(model="model", messages=messages), span, output_tokens=1000)


def test_unknown_or_unpaired_receipts_are_refused():
    messages = transcript()
    messages[2].tool_call_id = "unrelated"
    with pytest.raises(CompactionPreparationError, match="unpaired"):
        select_compaction_span(messages, start=0, protected_from=5, keep_recent_tokens=0)


def test_summary_transport_rejects_provider_tools_and_override_reenabled_dispatch():
    from bowhead_router.input_admission import validate_summary_transport

    for body in (
        {"tools": [{"type": "web_search"}], "tool_choice": "none"},
        {"tools": [{"type": "function"}], "tool_choice": "auto"},
        {"tools": [], "mcp_servers": []},
    ):
        with pytest.raises(LLMInputAdmissionError, match="llm_summary_"):
            validate_summary_transport(body, family="openai_responses")
    with pytest.raises(LLMInputAdmissionError, match="unsupported"):
        validate_summary_transport({}, family="unsupported_custom_http")
