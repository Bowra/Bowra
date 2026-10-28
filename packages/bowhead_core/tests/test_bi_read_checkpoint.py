"""Native checkpoint structure, independent of durable task authorization."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from bowhead_core.bi_workspace_continuation import BiWorkspaceReadContinuation, capture_bi_workspace_checkpoint
from bowhead_core.types import ToolInvocation
from bowhead_router.types import Message, ToolCall

pytestmark = pytest.mark.unit


@pytest.fixture
def capture_args():
    context = SimpleNamespace(
        turn_id="t",
        session_id="s",
        team_id="team",
        user_id="u",
        workspace_id="w",
        datarc=object(),
        active_datarc_project=None,
        accessible_datarc_workspaces=("a", "b"),
        reference_skill_required=True,
        reference_skill_access=object(),
    )
    binding = SimpleNamespace(execution_id="e", policy_fingerprint="p", capability_fingerprint="c")
    read = {"body": "ORIGINAL METHOD"}
    question = {
        "error_kind": "needs_clarification",
        "clarification": {"kind": "tool_context_select", "tool": "datarc", "options": [{"value": "a"}, {"value": "b"}]},
    }
    messages = [
        Message(role="user", content="Original task"),
        Message(role="assistant", content="Read method", tool_calls=[ToolCall(id="read", name="skill_read", arguments={"name": "method"})]),
        Message(role="tool", name="skill_read", tool_call_id="read", content=json.dumps(read)),
        Message(role="assistant", content="Select workspace", tool_calls=[ToolCall(id="select", name="bi_query", arguments={"query": "revenue"})]),
        Message(role="tool", name="bi_query", tool_call_id="select", content=json.dumps(question)),
    ]
    return {
        "messages": messages,
        "invocations": [
            ToolInvocation(tool_name="skill_read", arguments={"name": "method"}, result=read),
            ToolInvocation(tool_name="bi_query", arguments={"query": "revenue"}, result=question),
        ],
        "iteration": 1,
        "binding": binding,
        "ctx": context,
        "task": "Original task",
        "subagent_id": "sub",
        "profile": "analyst",
        "tool_deny": frozenset(),
        "effective_tools": frozenset({"skill_read", "bi_query"}),
        "max_iterations": 10,
    }


def test_v2_retains_read_messages_and_round_number(capture_args):
    checkpoint = capture_bi_workspace_checkpoint(**capture_args)
    assert type(checkpoint) is BiWorkspaceReadContinuation
    restored = BiWorkspaceReadContinuation.model_validate_json(checkpoint.model_dump_json())
    args = {key: value for key, value in capture_args.items() if key not in {"messages", "invocations", "iteration"}}
    messages = restored.resume_messages(**args, answer="Use B")
    assert [asdict(message) for message in messages[:-1]] == [asdict(message) for message in capture_args["messages"]]
    assert messages[-1].content == "Use B" and restored.next_iteration == 2
    capture_args["messages"][2].content = "CHANGED"
    assert restored.read_messages()[1].content == '{"body": "ORIGINAL METHOD"}'


@pytest.mark.parametrize(
    "change", ["invoke", "remote", "failed", "error_result", "unbound", "parallel", "call_id", "images", "round_count", "budget"]
)
def test_checkpoint_does_not_absorb_other_effects_or_invalid_transcripts(capture_args, change):
    assert capture_bi_workspace_checkpoint(**capture_args) is not None
    read = capture_args["invocations"][0]
    if change == "invoke":
        read.tool_name = "skill_invoke"
    elif change == "remote":
        read.tool_name = "bi_query"
    elif change == "failed":
        read.error = "failed"
    elif change == "error_result":
        read.result = {"ok": False}
    elif change == "unbound":
        capture_args["ctx"].reference_skill_required = False
    elif change == "parallel":
        capture_args["messages"][1].tool_calls.append(ToolCall(id="other", name="skill_read", arguments={}))
    elif change == "call_id":
        capture_args["messages"][2].tool_call_id = "other"
    elif change == "images":
        capture_args["messages"][1].images.append("unexpected")
    elif change == "round_count":
        capture_args["iteration"] = 2
    elif change == "budget":
        capture_args["max_iterations"] = 1
    assert capture_bi_workspace_checkpoint(**capture_args) is None


@pytest.mark.parametrize("change", ["tool", "unknown", "duplicate_call", "round_count", "oversize"])
def test_v2_reader_rejects_changed_or_unbounded_history(capture_args, change):
    checkpoint = capture_bi_workspace_checkpoint(**capture_args)
    data = checkpoint.model_dump()
    history = json.loads(data["read_history_json"])
    if change == "tool":
        history[0]["tool_calls"][0]["name"] = "skill_invoke"
    elif change == "unknown":
        history[0]["extra"] = "future"
    elif change == "duplicate_call":
        history[0]["tool_calls"][0]["id"] = "select"
    elif change == "round_count":
        data["completed_read_rounds"] = 2
    elif change == "oversize":
        history[1]["content"] = "x" * 524289
    data["read_history_json"] = json.dumps(history)
    with pytest.raises(ValueError):
        BiWorkspaceReadContinuation.model_validate(data)
