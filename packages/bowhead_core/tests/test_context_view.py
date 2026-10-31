"""Semantic model views never replace the original continuation evidence."""

from copy import deepcopy

import pytest
from bowhead_core.context_compaction import CompactionPreparationError, select_compaction_span, summarized_messages
from bowhead_core.context_view import ContextReplacement, ContextView, eligible_context_range
from bowhead_router.types import LLMResponse, Message, ToolCall

pytestmark = pytest.mark.unit


def group(identity, content):
    return [
        Message(role="assistant", content="", tool_calls=[ToolCall(id=identity, name="read_source", arguments={})]),
        Message(role="tool", content=content, tool_call_id=identity, name="read_source"),
    ]


def replace_span(view, source, start, end):
    span = select_compaction_span(source, start=start, protected_from=end, keep_recent_tokens=0)
    candidate = summarized_messages(
        source, span, LLMResponse(content="Observed source results; work is still pending.", finish_reason="stop"), max_summary_tokens=100
    )
    view.adopt(ContextReplacement.committed(source, span, candidate))
    return candidate


def test_repeated_compaction_and_append_preserve_native_tool_receipts():
    native = [Message(role="user", content="Current task"), *group("one", "Exact result one"), *group("two", "Exact result two")]
    snapshot = deepcopy(native)
    view = ContextView()
    first = replace_span(view, native, 1, 3)
    assert native == snapshot
    native.extend(group("three", "New result"))
    projected = view.project(native)
    assert projected == [*first, *native[-2:]]
    second = replace_span(view, projected, 1, 4)
    assert view.project(native) == second
    assert native[: len(snapshot)] == snapshot
    assert view.position(0) == 0
    assert view.position(len(native)) == len(second)


def test_restored_projection_rejects_changed_original_evidence():
    source = [
        Message(role="user", content="Earlier request"),
        Message(role="assistant", content="Earlier answer"),
        Message(role="user", content="Current request"),
    ]
    view = ContextView()
    replace_span(view, source, 0, 2)
    assert view.position(2) == 1
    with pytest.raises(CompactionPreparationError, match="protected_message_replaced"):
        view.position(0)
    changed = deepcopy(source)
    changed[0].content = "Different task"
    with pytest.raises(CompactionPreparationError, match="projection_source_changed"):
        view.project(changed)


def test_eligibility_keeps_current_request_unconsumed_parallel_results_and_corrections():
    source = [
        Message(role="user", content="Task"),
        *group("one", "Old result"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="a", name="read_source", arguments={}), ToolCall(id="b", name="read_source", arguments={})],
        ),
        Message(role="tool", name="read_source", tool_call_id="a", content="New A"),
        Message(role="tool", name="read_source", tool_call_id="b", content="New B"),
    ]
    volatile = Message(role="user", content="Current reference inventory")
    assert eligible_context_range([*source, volatile], current_request=0, canonical_count=len(source)) == (1, 3)
    correction = Message(role="user", content="Correct the target to refunds only")
    source.insert(3, correction)
    assert eligible_context_range([*source, volatile], current_request=0, canonical_count=len(source)) == (1, 3)


def test_history_is_eligible_before_current_execution_transactions():
    source = [
        Message(role="user", content="Previous question"),
        Message(role="assistant", content="Previous answer"),
        Message(role="user", content="Current task"),
        *group("one", "Result"),
    ]
    assert eligible_context_range(source, current_request=2, canonical_count=len(source)) == (0, 2)
    view = ContextView()
    summary = replace_span(view, source, 0, 2)
    assert eligible_context_range(summary, current_request=view.position(2), canonical_count=len(summary)) == (2, 2)


@pytest.mark.parametrize("summarized_history", [False, True])
def test_small_retained_history_does_not_block_a_long_current_execution(summarized_history):
    history = [
        Message(role="user", content="Earlier context", name="context_summary" if summarized_history else None),
        Message(role="assistant", content="Recent answer"),
    ]
    source = [
        *history,
        Message(role="user", content="Current task"),
        *group("old", "Large completed result " * 4000),
        *group("new", "Consume this result first"),
    ]
    start, end = eligible_context_range(source, current_request=2, canonical_count=len(source))
    assert (start, end) == (3, 5)
    view = ContextView()
    candidate = replace_span(view, source, start, end)
    assert candidate[:3] == source[:3]
    assert candidate[-2:] == source[-2:]
