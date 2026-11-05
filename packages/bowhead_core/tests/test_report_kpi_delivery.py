"""Persisted report KPI scope, Tool admission and shared final-answer delivery."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents._lead_context import set_current_subagent_id
from bowhead_core.agents.report_tools import _write_report
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.report_writer_loop import _emit_kpi_band
from bowhead_core.subagent_return_tool import consume_returned_payload, register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

pytestmark = pytest.mark.integration


class Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
async def report(tmp_path: Path) -> tuple[CanvasStore, Any, Bus]:
    store = CanvasStore(tmp_path / "report.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="s_kpi", workspace_id="ws_kpi", user_id="u_kpi")
    return store, canvas, Bus()


@pytest.mark.parametrize("excluded", [True, False])
async def test_finisher_respects_explicit_exclusion_and_optional_cards(report: tuple, excluded: bool) -> None:
    store, canvas, bus = report
    request = SimpleNamespace(text="不要HTML/PDF/图片/KPI。" if excluded else "Show the verified KPI with the chart.", turn_id="t_kpi")
    written = await _emit_kpi_band(store, bus, canvas.id, request, {"kpis": [{"label": "CSV bytes", "value": "119"}]})
    sections = await store.list_sections(canvas.id)
    assert [s.kind for s in sections] == ([] if excluded else ["kpi_row"])
    assert written == ([] if excluded else ["kpi_row"])
    assert [e.event_type for e in bus.events] == ([] if excluded else ["canvas.section_added"])
    if not excluded:
        assert bus.events[0].payload["section"]["cells"] == sections[0].payload["cells"]


async def test_confirmed_authored_report_returns_without_extra_kpis_or_insights(report: tuple) -> None:
    store, canvas, bus = report
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    ctx = ToolExecutionContext(workspace_id="ws_kpi", member_uid="", user_id="u_kpi", turn_id="t_kpi", session_id="s_kpi", datarc=None)
    ctx.canvas_store = store
    ctx.parent_bus = bus
    ctx._psi_turn_user_text = "Write only the supplied chart, without KPI cards."
    token = set_current_subagent_id("sub_kpi")
    try:
        written = await _write_report({"kind": "narrative", "content": "The supplied synthetic chart is retained."}, ctx)
        assert written["ok"] is True
        refused = await registry.execute(
            "subagent_return",
            {"payload": {"delivery_surface": "report", "exec_summary": "The report is ready.", "kpis": [{"label": "CSV bytes", "value": "119"}]}},
            ctx,
        )
        assert refused.result["error_kind"] == "report_kpis_excluded"
        assert consume_returned_payload(ctx) is None
        accepted = await registry.execute(
            "subagent_return", {"payload": {"delivery_surface": "report", "exec_summary": "The supplied chart report is ready."}}, ctx
        )
        assert accepted.result["ok"] is True
        assert accepted.result["summary"]["kpis_count"] == 0
        assert accepted.result["summary"]["insights_count"] == 0
        sections = await store.list_sections(canvas.id)
        assert [s.id for s in sections] == [written["section_id"]]
    finally:
        token.var.reset(token)


@pytest.mark.parametrize("force_canvas", [False, True], ids=["ordinary", "plan"])
async def test_shared_closeout_reconciles_actual_saved_cards(report: tuple, monkeypatch: pytest.MonkeyPatch, force_canvas: bool) -> None:
    from bowhead_core.agents import lead_orchestrator

    store, canvas, bus = report
    await store.append_section(canvas_id=canvas.id, kind="kpi_row", payload={"cells": [{"lbl": "CSV bytes", "val": "119"}]}, by_turn_id="t_kpi")
    ctx = SimpleNamespace(
        canvas_store=store, language_context=SimpleNamespace(output_language="en-US"), _lead_narrative_text="The report has no KPI cards."
    )
    runtime = SimpleNamespace(parent_ctx=ctx, parent_bus=bus)

    async def composed(**kwargs: Any) -> tuple[list[str], str]:
        assert kwargs["force_canvas"] is force_canvas
        return ["kpi_row"], "CSV: 119 bytes. The report has no KPI cards."

    monkeypatch.setattr(lead_orchestrator, "_compose_turn_output_unchecked", composed)
    kinds, text = await lead_orchestrator._compose_turn_output(
        runtime=runtime,
        sctx=SimpleNamespace(session_id="s_kpi", turn_id="t_kpi"),
        result=SimpleNamespace(chat_direct_delivered=False),
        aggregated_payload={"kpis": []},
        user_text="Review the report.",
        peer_payloads=[],
        force_canvas=force_canvas,
    )
    assert kinds == ["kpi_row"]
    assert text == "CSV: 119 bytes. The report contains 1 KPI card."
    assert ctx._lead_narrative_superseded is True


async def test_payload_composition_excludes_only_new_kpis(report: tuple) -> None:
    from bowhead_core.canvas_composer import compose_canvas_from_payload

    store, canvas, bus = report
    old = await store.append_section(canvas_id=canvas.id, kind="kpi_row", payload={"cells": [{"lbl": "Existing", "val": "7"}]}, by_turn_id="t_prior")
    payload = {"title": "Synthetic chart review", "exec_summary": "The chart is ready.", "kpis": [{"label": "CSV bytes", "value": "119"}]}
    await compose_canvas_from_payload(
        canvas_store=store,
        bus=bus,
        request=SimpleNamespace(session_id="s_kpi", workspace_id="ws_kpi", user_id="u_kpi", turn_id="t_kpi", text="Do not add KPIs."),
        payload=payload,
    )
    cards = [s for s in await store.list_sections(canvas.id) if s.kind == "kpi_row"]
    assert [s.id for s in cards] == [old.id]
    assert payload["kpis"] == [{"label": "CSV bytes", "value": "119"}]


@pytest.mark.parametrize("authored", [False, True])
async def test_final_publication_uses_confirmed_authored_body(report: tuple, authored: bool) -> None:
    from bowhead_core.agents.lead_orchestrator import _validate_final_report_payload

    store, canvas, bus = report
    if authored:
        await store.append_section(
            canvas_id=canvas.id,
            kind="narrative",
            payload={"markdown": "[figure:sec_chart]", "style": "executive"},
            owner="sub_kpi",
            by_turn_id="t_kpi",
        )
    payload = {"delivery_surface": "report", "title": "Synthetic report", "exec_summary": "The requested chart is ready."}
    valid, errors = await _validate_final_report_payload(SimpleNamespace(canvas_store=store), bus, "t_kpi", payload)
    assert valid is authored
    assert bool(errors) is not authored
    if authored:
        malformed = {**payload, "kpis": [{"label": "CSV bytes", "value": "119", "sentiment": "invalid"}]}
        valid, errors = await _validate_final_report_payload(SimpleNamespace(canvas_store=store), bus, "t_kpi", malformed)
        assert not valid
        assert any("sentiment" in error for error in errors)
