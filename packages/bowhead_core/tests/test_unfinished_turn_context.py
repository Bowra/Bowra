"""Repairing an unfinished transcript and recording what a running helper ran into."""

from types import SimpleNamespace

from bowhead_core.context_assembly import TOOL_FAILURES_MAX_BYTES
from bowhead_core.unfinished_turn_context import (
    PENDING_RESULT,
    OpenDelegations,
    open_delegations,
    repair_pending_tool_calls,
    returned_error_messages,
)
from bowhead_router.types import Message, ToolCall


def test_only_calls_without_a_result_are_repaired_in_call_order() -> None:
    messages = [
        Message(role="user", content="Analyse the data."),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="bi_schema", arguments={})]),
        Message(role="tool", name="bi_schema", tool_call_id="c1", content="schema"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="c2", name="subagent_spawn", arguments={}),
                ToolCall(id="c3", name="kb_search", arguments={}),
                ToolCall(id="c4", name="web_search", arguments={}),
                ToolCall(id="c5", name="write_report", arguments={}),
            ],
        ),
        # A precheck answered one call of the batch before the others ran.
        Message(role="tool", name="kb_search", tool_call_id="c3", content="retrieval limit reached"),
    ]
    known = {"c2": "failed｜helper did not return", "c4": '{"results": 8}'}
    repaired = repair_pending_tool_calls(messages, lambda call: known.get(call.id))
    assert repaired[: len(messages)] == messages
    assert [(message.role, message.name, message.tool_call_id, message.content) for message in repaired[len(messages) :]] == [
        ("tool", "subagent_spawn", "c2", "failed｜helper did not return"),
        ("tool", "web_search", "c4", '{"results": 8}'),
        # Nothing is known about the write. The text does not claim it did not run.
        ("tool", "write_report", "c5", PENDING_RESULT),
    ]
    assert "may have run" in PENDING_RESULT
    # A transcript whose calls all have results is returned as it is.
    assert repair_pending_tool_calls(messages[:3], lambda call: None) == messages[:3]


def test_a_running_helper_reports_its_tool_count_and_failures_until_it_returns() -> None:
    bus = SimpleNamespace()
    record = open_delegations(bus)
    assert open_delegations(bus) is record
    record.open("call_a", profile="compute", subagent_id="sub_a")
    record.open("call_b", profile="kb-analyst", subagent_id="sub_b")
    record.observe("sub_a", "bi_schema", None)
    record.observe("sub_a", "bi_query", ["download failed"])
    record.observe("sub_a", "bi_query", ["download failed"])
    record.observe("sub_b", "kb_search", None)
    # The Lead's own calls and unknown helpers are not counted.
    record.observe("sub_lead", "web_search", ["search failed"])
    assert record.result("call_a", "failed") == (
        "failed｜子工具 3 次｜没有回执（被中止前是否已写入画布未知，以画布现状为准）｜Helper tool failures: bi_query ×2: download failed"
    )
    assert record.result("call_b", "cancelled") == "cancelled｜子工具 1 次｜没有回执（被中止前是否已写入画布未知，以画布现状为准）"
    # A helper that returned is no longer open: its real result speaks for it.
    record.close("call_a")
    assert record.result("call_a", "failed") is None and record.result("call_unknown", "failed") is None
    # A call without an ID or a helper ID is not recorded.
    empty = OpenDelegations()
    empty.open("", profile="compute", subagent_id="sub_x")
    empty.open("call_x", profile="compute", subagent_id="")
    assert empty.result("", "failed") is None and empty.result("call_x", "failed") is None


def test_one_failure_text_is_bounded_in_memory() -> None:
    record = OpenDelegations()
    record.open("call_a", profile="compute", subagent_id="sub_a")
    record.observe("sub_a", "python_sandbox", ["错" * 5000])
    (failure,) = record._by_call["call_a"].failures
    assert len(failure[1].encode()) <= TOOL_FAILURES_MAX_BYTES
    failure[1].encode().decode()
    assert len(record.result("call_a", "failed").split("｜")[-1].encode()) <= TOOL_FAILURES_MAX_BYTES


def test_returned_error_messages_prefer_each_failed_item() -> None:
    batch = {
        "ok": False,
        "error_message": "All queries failed.",
        "results": [{"ok": False, "error_message": "download failed"}, {"ok": True}, {"ok": False, "error_message": "download failed"}],
    }
    assert returned_error_messages(batch) == ["download failed"]
    assert returned_error_messages({"ok": False, "error_message": "workspace unavailable"}) == ["workspace unavailable"]
    assert returned_error_messages({"ok": False}) == [] and returned_error_messages(None) == [] and returned_error_messages("text") == []


def test_a_bus_that_cannot_hold_the_record_never_breaks_a_tool_call() -> None:
    detached = open_delegations(None)
    detached.observe("sub_a", "bi_query", ["boom"])
    assert detached.result("call_a", "failed") is None and open_delegations(None) is not detached
