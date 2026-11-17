"""Repeated identical tool calls (docs/sprints/agent-repeat-guard-2026-11-16/tech-design.md §3.2–§3.5)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core.agent_coach import register_agent_coach_tools
from bowhead_core.agent_repeat_guard import RepeatChain, call_key, notice_text, refusal_text, result_digest
from bowhead_core.agent_run_limits import PROGRESS_REFERENCE_KINDS, REPEAT_NOTICE_AT, REPEAT_REFUSE_FROM
from bowhead_core.kb.management_tools import register_management_tools
from bowhead_core.tool_registry import ToolRegistry, ToolSchema, ToolSpec

_SANDBOX_RESULT = {
    "ok": True,
    "exit_code": 0,
    "stdout": "tot 163.43\n",
    "result_json": {
        "stdout": "tot 163.43\nComplete stdout: Data artifact art_cc410762a658 (sandbox-stdout.txt).",
        "duration_ms": 1576,
        "execution": {"id": "bowhead-sandbox-1073600bbb", "settled": True},
        "outputs": [{"name": "sandbox-stdout.txt", "artifact_id": "art_cc410762a658", "size": 171}],
    },
}


def _rerun(**changes: object) -> dict:
    """The same sandbox result from another run: new ids and timing, plus ``changes``."""
    result_json = {
        **_SANDBOX_RESULT["result_json"],
        "stdout": "tot 163.43\nComplete stdout: Data artifact art_5efed4b1c74d (sandbox-stdout.txt).",
        "duration_ms": 1584,
        "execution": {"id": "bowhead-sandbox-563110052b", "settled": True},
        "outputs": [{"name": "sandbox-stdout.txt", "artifact_id": "art_5efed4b1c74d", "size": 171}],
    }
    return {**_SANDBOX_RESULT, "result_json": result_json, **changes}


def _observe(chain: RepeatChain, decision: int, *, tool: str = "python_sandbox", code: str = "print(1)", result: object = None, **kwargs):
    return chain.observe(
        tool,
        {"code": code},
        result=result_digest(_SANDBOX_RESULT if result is None else result),
        failed=kwargs.pop("failed", False),
        decision=decision,
        **kwargs,
    )


def test_call_key_ignores_argument_order_but_not_content() -> None:
    assert call_key("t", {"a": 1, "b": {"y": 2, "x": 1}}) == call_key("t", {"b": {"x": 1, "y": 2}, "a": 1})
    assert call_key("t", {"a": 1}) != call_key("t", {"a": 2})
    assert call_key("t", {"a": 1}) != call_key("u", {"a": 1})
    long_text = "x" * 500
    assert call_key("bi_query", {"text": long_text + "A"}) != call_key("bi_query", {"text": long_text + "B"})


def test_result_digest_ignores_ids_and_timing_but_not_output() -> None:
    assert result_digest(_SANDBOX_RESULT) == result_digest(_rerun())
    assert result_digest(_SANDBOX_RESULT) != result_digest(_rerun(stdout="tot 163.44\n"))
    assert result_digest(_SANDBOX_RESULT) != result_digest(_rerun(exit_code=1))
    changed_size = _rerun()
    changed_size["result_json"]["outputs"] = [{"name": "sandbox-stdout.txt", "artifact_id": "art_x1", "size": 172}]
    assert result_digest(_SANDBOX_RESULT) != result_digest(changed_size)
    assert result_digest({"ok": True}) != result_digest({"ok": True}, "failed")


def test_chain_counts_decisions_and_notices_at_three_and_five() -> None:
    chain = RepeatChain()
    positions = [_observe(chain, decision) for decision in range(6)]
    assert positions == [1, 2, 3, 4, 5, 6]
    assert [notice_text("python_sandbox", p) is not None for p in positions] == [p in REPEAT_NOTICE_AT for p in positions]
    third = notice_text("python_sandbox", 3)
    fifth = notice_text("python_sandbox", 5)
    assert third is not None and "call 3 in a row of python_sandbox" in third and "not run" not in third
    assert fifth is not None and f"From the {REPEAT_REFUSE_FROM}th identical call in a row, the call is not run." in fifth


def test_copies_in_one_decision_count_once() -> None:
    chain = RepeatChain()
    assert _observe(chain, 0) == 1
    assert _observe(chain, 1) == 2
    assert _observe(chain, 1) is None
    assert _observe(chain, 2) == 3


def test_another_call_a_changed_result_or_a_failure_breaks_the_chain() -> None:
    chain = RepeatChain()
    for decision in range(4):
        _observe(chain, decision)
    assert _observe(chain, 4, code="print(2)") == 1  # another executed call starts a new chain
    assert _observe(chain, 5) == 1
    assert _observe(chain, 6) == 2
    assert _observe(chain, 7, result=_rerun(stdout="changed\n")) == 1  # same call, new result
    assert _observe(chain, 8, failed=True) is None  # failures are not part of a chain
    assert _observe(chain, 9) == 1


def test_status_polls_and_refused_calls_are_transparent() -> None:
    chain = RepeatChain()
    _observe(chain, 0)
    assert _observe(chain, 1, tool="agent_ci_get", status_poll=True) is None
    assert _observe(chain, 2) == 2
    assert chain.refusal_position("agent_ci_get", {"code": "print(1)"}, status_poll=True) is None
    for decision in range(3, 30):
        assert _observe(chain, decision, tool="agent_ci_get", code="poll", status_poll=True) is None
    assert _observe(chain, 30) == 3


def test_refused_from_the_eighth_and_positions_keep_counting() -> None:
    chain = RepeatChain()
    for decision in range(REPEAT_REFUSE_FROM - 2):
        _observe(chain, decision)
    assert chain.refusal_position("python_sandbox", {"code": "print(1)"}) is None
    _observe(chain, REPEAT_REFUSE_FROM - 2)
    assert chain.count == REPEAT_REFUSE_FROM - 1
    assert chain.refusal_position("python_sandbox", {"code": "print(1)"}) == REPEAT_REFUSE_FROM
    assert chain.refusal_position("python_sandbox", {"code": "print(2)"}) is None
    chain.note_refused(20)
    chain.note_refused(20)  # a second copy in the same decision
    assert chain.refusal_position("python_sandbox", {"code": "print(1)"}) == REPEAT_REFUSE_FROM + 1
    text = refusal_text("python_sandbox", REPEAT_REFUSE_FROM, chain.count)
    assert text.startswith("not_run: this is call 8 in a row of python_sandbox") and "the previous 7 returned the same result" in text


def test_subagent_return_is_never_refused() -> None:
    chain = RepeatChain()
    for decision in range(REPEAT_REFUSE_FROM + 2):
        chain.observe("subagent_return", {"payload": {}}, result=result_digest({"ok": True}), failed=False, decision=decision)
    assert chain.count == REPEAT_REFUSE_FROM + 2
    assert chain.refusal_position("subagent_return", {"payload": {}}) is None


def test_resumed_run_continues_the_chain_from_retained_invocations() -> None:
    invocations = [SimpleNamespace(tool_name="python_sandbox", arguments={"code": "print(1)"}, result=_rerun(), error=None) for _ in range(4)]
    chain = RepeatChain.from_invocations(invocations, is_failure=lambda inv: inv.error is not None, is_status_poll=lambda name: False)
    assert _observe(chain, 0) == 5


def test_declared_status_polls_are_exactly_the_built_in_operation_reads() -> None:
    registry = ToolRegistry()
    register_agent_coach_tools(registry)
    register_management_tools(registry)

    async def handler(_args, _ctx):  # pragma: no cover - never called
        return {}

    registry.register(ToolSpec(schema=ToolSchema(name="teammcp.job_status", description="Job status", input_schema={}), handler=handler))
    polls = {name for name in registry._specs if registry.is_status_poll(name)}
    assert polls == {"agent_ci_get", "agent_operation_get", "agent_skill_operation_get", "agent_plugin_operation_get", "kb_operation_status"}
    assert not registry.is_status_poll("teammcp.job_status")
    assert not registry.is_status_poll("not_registered")


class _Store:
    async def list_for_session(self, *args, **kwargs):
        return []


@pytest.mark.asyncio
async def test_progress_counts_only_chart_image_and_design_references() -> None:
    allowlist = RefAllowlist()
    invocations = []
    for index in range(5):
        invocations.append(SimpleNamespace(tool_name="python_sandbox", call_id=f"call_{index}", arguments={"code": "print(1)"}, result={"ok": True}))
        await allowlist.refresh_from_stores(
            canvas_store=None, artifact_store=_Store(), session_id="s_x", user_id="u_x", canvas_id=None, tool_invocations=invocations
        )
    allowlist.add("code", "sec_code_1")
    allowlist.add("data", "art_1")
    assert allowlist.reference_count() == 7
    assert allowlist.reference_count(PROGRESS_REFERENCE_KINDS) == 0
    allowlist.add("chart", "sec_chart_1")
    assert allowlist.reference_count(PROGRESS_REFERENCE_KINDS) == 1
