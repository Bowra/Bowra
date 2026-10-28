"""Sprint chat-polish-r3 Phase 2 (B7) — _build_bi_query_sub_steps tests.

Locks the contract that backend step_done payload for batch bi_query
carries a sub_steps array shaped per events.SubStepResult. FE
turn-store-dispatch.mapSubSteps reads this; BatchRail renders per-query
state. Single-query bi_query / non-bi_query tools skip the branch.

Integration tests at the bottom exercise `_execute_tool` end-to-end
against a stub bi_query handler returning batch results — these lock
the call-site logic (parent state="partial" when sub-queries mix, etc.)
on top of the pure helper unit tests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import SubagentRunner, _build_bi_query_sub_steps
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSchema,
    ToolSpec,
)

from bowhead_router import ToolCall


def _tc(name: str, arguments: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, arguments=arguments or {})


def _inv(result: dict | list | None) -> SimpleNamespace:
    return SimpleNamespace(result=result)


class TestBiQuerySubStepsHappy:
    def test_three_query_batch_all_ok(self) -> None:
        tc = _tc(
            "bi_query",
            {
                "queries": [
                    "2025 营业额按月",
                    "2024 营业额按月",
                    "同比增长率",
                ]
            },
        )
        inv = _inv(
            {
                "results": [
                    {"ok": True, "sample_rows": [{}, {}, {}], "row_count": 12},
                    {"ok": True, "sample_rows": [{}, {}, {}], "row_count": 12},
                    {"ok": True, "sample_rows": [{}], "row_count": 1},
                ],
            }
        )
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert len(sub) == 3
        assert sub[0] == {
            "label": "2025 营业额按月",
            "state": "success",
            "output_preview": "3 行",
            "error_kind": None,
        }
        assert sub[2]["label"] == "同比增长率"
        assert sub[2]["output_preview"] == "1 行"

    def test_mixed_success_failure(self) -> None:
        tc = _tc("bi_query", {"queries": ["a", "b"]})
        inv = _inv(
            {
                "results": [
                    {"ok": True, "sample_rows": [{}, {}]},
                    {"ok": False, "error_kind": "datarc_no_data", "error": "no rows"},
                ],
            }
        )
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["state"] == "success"
        assert sub[1]["state"] == "failure"
        assert sub[1]["error_kind"] == "datarc_no_data"


class TestBiQuerySubStepsLabelEdgeCases:
    def test_missing_query_text_falls_back_to_index_label(self) -> None:
        tc = _tc("bi_query", {"queries": ["a"]})  # only 1 query but 2 results
        inv = _inv(
            {
                "results": [
                    {"ok": True, "sample_rows": []},
                    {"ok": True, "sample_rows": []},
                ],
            }
        )
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["label"] == "a"
        assert sub[1]["label"] == "query 2"

    def test_non_string_query_falls_back_to_index_label(self) -> None:
        tc = _tc("bi_query", {"queries": [None, 123]})
        inv = _inv(
            {
                "results": [
                    {"ok": True, "sample_rows": []},
                    {"ok": True, "sample_rows": []},
                ],
            }
        )
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["label"] == "query 1"
        assert sub[1]["label"] == "query 2"

    def test_long_query_text_truncated_to_120(self) -> None:
        long_q = "a" * 200
        tc = _tc("bi_query", {"queries": [long_q]})
        inv = _inv({"results": [{"ok": True, "sample_rows": []}]})
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert len(sub[0]["label"]) == 120


class TestBiQuerySubStepsOutputPreview:
    def test_total_rows_priority(self) -> None:
        # 0.40.1 Bug 2 — when authoritative `total_rows` is present (preserved
        # through tool_result_budget which caps sample_rows to 5), prefer it
        # over len(sample_rows). Before the fix, the chip read "5 行" for a 250-row
        # batches because budget had truncated sample_rows before FE saw it.
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv(
            {
                "results": [
                    {
                        "ok": True,
                        "sample_rows": [{}, {}, {}, {}, {}],  # capped to 5 by budget
                        "total_rows": 250,  # authoritative
                        "row_count": 999,  # ignored
                    }
                ]
            }
        )
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["output_preview"] == "250 行"

    def test_sample_rows_count_priority(self) -> None:
        # total_rows absent → fall to sample. row_count still ignored.
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv(
            {
                "results": [
                    {
                        "ok": True,
                        "sample_rows": [{}, {}, {}, {}, {}],
                        "row_count": 999,
                    }
                ]
            }
        )
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["output_preview"] == "5 行"

    def test_row_count_fallback(self) -> None:
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv({"results": [{"ok": True, "row_count": 42}]})
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["output_preview"] == "42 行"

    def test_no_row_data_yields_none_preview(self) -> None:
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv({"results": [{"ok": True}]})
        sub = _build_bi_query_sub_steps(tc, inv)
        assert sub is not None
        assert sub[0]["output_preview"] is None


class TestBiQuerySubStepsSkipCases:
    def test_non_bi_query_returns_none(self) -> None:
        tc = _tc("python_sandbox", {"script": "print('hi')"})
        inv = _inv({"results": [{"ok": True}]})
        assert _build_bi_query_sub_steps(tc, inv) is None

    def test_single_query_shape_returns_none(self) -> None:
        """bi_query without `results` key (single-query response). FE
        BatchRail gates on subSteps.length > 0; backend just omits the
        field so FE falls through to the parent step's prettify preview."""
        tc = _tc("bi_query", {"query": "single shot"})
        inv = _inv({"columns": ["a"], "rows": [{"a": 1}], "row_count": 1})
        assert _build_bi_query_sub_steps(tc, inv) is None

    def test_empty_results_list_returns_none(self) -> None:
        tc = _tc("bi_query", {"queries": []})
        inv = _inv({"results": []})
        assert _build_bi_query_sub_steps(tc, inv) is None

    def test_non_dict_result_returns_none(self) -> None:
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv(["string", "not dict"])
        assert _build_bi_query_sub_steps(tc, inv) is None

    def test_none_result_returns_none(self) -> None:
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv(None)
        assert _build_bi_query_sub_steps(tc, inv) is None

    def test_results_key_not_a_list_returns_none(self) -> None:
        tc = _tc("bi_query", {"queries": ["a"]})
        inv = _inv({"results": "string-not-list"})
        assert _build_bi_query_sub_steps(tc, inv) is None


# ─── Integration tests — _execute_tool emit path ──────────────────────


def _make_runner(tool_handlers: dict[str, Any]) -> SubagentRunner:
    reg = ToolRegistry()
    for name, handler in tool_handlers.items():
        spec = ToolSpec(
            schema=ToolSchema(name=name, description="", input_schema={}),
            handler=handler,
        )
        reg.register(spec)

    class _StubRouter:
        async def invoke(self, *a, **kw):
            raise NotImplementedError

        def stream(self, *a, **kw):
            raise NotImplementedError

        async def close(self):
            pass

    return SubagentRunner(router=_StubRouter(), tool_registry=reg)  # type: ignore[arg-type]


def _make_ctx(turn_id: str = "t_b7") -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,
        # ws-datarc-decouple P3: non-None sentinel so the execute() datarc-capability gate
        # passes (a real bi_query turn always has a client bound). The stub handlers below
        # Never touch it. Batch 1/c3: explicitly seed the Datarc workspace (workspace_id no longer falls back).
        datarc=object(),  # type: ignore[arg-type]
        active_datarc_project="w",
        role="lead",
    )


@pytest.mark.asyncio
async def test_execute_tool_bi_query_batch_all_success_fills_sub_steps() -> None:
    """3-query batch all ok → step_done.sub_steps has 3 entries, parent state=success."""

    async def _bi_query_handler(args: dict, ctx) -> Any:
        # Mimic bi_query batch envelope shape.
        return {
            "results": [{"ok": True, "sample_rows": [{}, {}], "query_text": q} for q in args.get("queries", [])],
        }

    runner = _make_runner({"bi_query": _bi_query_handler})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(
        id="call_b7",
        name="bi_query",
        arguments={"queries": ["q1", "q2", "q3"]},
    )
    await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_b7",
        subagent_id="lead_root",
        running_profile="lead",
    )

    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_b7", payload={}))
    await consumer

    done = [e for e in captured if e.event_type == "step_done"]
    assert len(done) == 1
    payload = done[0].payload
    assert payload["state"] == "success"
    assert payload["ok"] is True
    assert "sub_steps" in payload
    assert len(payload["sub_steps"]) == 3
    assert [s["label"] for s in payload["sub_steps"]] == ["q1", "q2", "q3"]
    assert all(s["state"] == "success" for s in payload["sub_steps"])
    assert all(s["output_preview"] == "2 行" for s in payload["sub_steps"])


@pytest.mark.asyncio
async def test_execute_tool_bi_query_mixed_state_emits_partial() -> None:
    """Some sub-queries succeed, others fail → parent state="partial" (CTO Phase 2 P1)."""

    async def _bi_query_handler(args: dict, ctx) -> Any:
        results = []
        for i, _q in enumerate(args.get("queries", [])):
            if i == 1:
                results.append({"ok": False, "error_kind": "datarc_no_data", "error": "no rows"})
            else:
                results.append({"ok": True, "sample_rows": [{}]})
        return {"results": results}

    runner = _make_runner({"bi_query": _bi_query_handler})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(
        id="call_partial",
        name="bi_query",
        arguments={"queries": ["q1", "q2", "q3"]},
    )
    await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_b7",
        subagent_id="lead_root",
        running_profile="lead",
    )

    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_b7", payload={}))
    await consumer

    done = [e for e in captured if e.event_type == "step_done"]
    assert len(done) == 1
    payload = done[0].payload
    # Parent state must be "partial" when sub-states mix (CTO P1 fix).
    assert payload["state"] == "partial"
    assert payload["ok"] is False
    sub = payload["sub_steps"]
    assert len(sub) == 3
    assert sub[0]["state"] == "success"
    assert sub[1]["state"] == "failure"
    assert sub[1]["error_kind"] == "datarc_no_data"
    assert sub[2]["state"] == "success"


@pytest.mark.asyncio
async def test_execute_tool_bi_query_single_query_omits_sub_steps() -> None:
    """Single-query shape (no `results` key) → sub_steps field NOT present."""

    async def _bi_query_handler(args: dict, ctx) -> Any:
        # Single-query shape (different from batch envelope).
        return {"columns": ["a"], "rows": [{"a": 1}], "row_count": 1}

    runner = _make_runner({"bi_query": _bi_query_handler})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(id="call_single", name="bi_query", arguments={"query": "single"})
    await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_b7",
        subagent_id="lead_root",
        running_profile="lead",
    )

    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_b7", payload={}))
    await consumer

    done = [e for e in captured if e.event_type == "step_done"]
    assert len(done) == 1
    # sub_steps may be absent OR present-but-None; FE BatchRail gates on
    # `subSteps?.length > 0` either way.
    assert "sub_steps" not in done[0].payload or done[0].payload["sub_steps"] in (None, [])
