"""Presentation-only follow-ups edit the existing chart in place.

116 1.7.18 `t_d4d4d3a87752`: "make the bar chart above green, keep everything else" was
delegated as "redraw the bar chart and produce a new chart", without naming the earlier
chat-delivered chart. The helper drew a duplicate and recolored the duplicate; the
original chart stayed unchanged. In `t_7ab16f11ce9a` Lead wrote its own green styling idea
into the task as a requirement and the helper then told the user it was the user's request.

These tests pin that each place routing a chart follow-up carries the in-place route
(`read_chart` + `edit_chart` on the named chart) while data or scope changes keep the
redraw path, and that delegated suggestions stay distinguishable from user requirements.
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_tool_use_guidance
from bowhead_core.canvas_revise import canvas_mode_context_line, prior_charts_reminder
from bowhead_core.subagent import _build_compute_system_prompt, register_subagent_tool
from bowhead_core.tool_registry import build_default_registry

pytestmark = pytest.mark.unit


def _spawn_properties(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    return registry.get("subagent_spawn").schema.input_schema["properties"]


def test_spawn_task_contract_names_the_target_chart_for_in_place_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _spawn_properties(monkeypatch)["task"]["description"]
    assert "read_chart" in task and "edit_chart" in task
    assert "section ID" in task, "the helper needs the real chart ID, not only a description of the chart"


def test_spawn_task_contract_keeps_lead_suggestions_out_of_user_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _spawn_properties(monkeypatch)["task"]["description"]
    assert "optional suggestion" in task, "Lead's own ideas must be labeled so the helper does not attribute them to the user"


def test_canvas_mode_sends_report_chart_presentation_edits_to_revise(monkeypatch: pytest.MonkeyPatch) -> None:
    description = _spawn_properties(monkeypatch)["canvas_mode"]["description"]
    clause = next(part for part in description.split("；") if "edit_chart" in part)
    assert "'revise'" in clause, "a presentation edit of a report chart is a revision of that report, not a new report"


def test_lead_routes_presentation_edits_of_earlier_charts_in_place() -> None:
    guidance = build_tool_use_guidance(planning_auto=False, data_peer="compute", compute_peer="compute")
    assert "edit_chart" in guidance
    assert "[figure:sec_" in guidance, "Lead must know where an earlier chat answer exposes the chart ID"
    # 116 1.7.21: a native chart offered no type change, yet Lead promised one in place.
    assert "ask before drawing a new chart" in guidance
    without_compute = build_tool_use_guidance(planning_auto=False, data_peer="compute", compute_peer=None)
    assert "edit_chart" not in without_compute, "chart routing is rendered only when a computation Agent can carry it out"


def test_compute_prompt_edits_existing_charts_instead_of_drawing_copies() -> None:
    prompt = _build_compute_system_prompt()
    start = prompt.index("**Changing an existing chart:**")
    block = prompt[start : prompt.index("\n\n", start)]
    assert "read_chart" in block and "edit_chart" in block
    # 116 1.7.21: the helper retried a refused type change three times.
    assert "allowed_changes" in block and "supported_kinds" in block and "do not retry" in block
    assert "read_report" in block, "a task that names only a title needs a reader that lists chat-delivered charts"
    assert "add_chart" in block, "data or scope changes keep the new-chart path"


def test_compute_prompt_does_not_attribute_task_suggestions_to_the_user() -> None:
    prompt = _build_compute_system_prompt()
    start = prompt.index("**User-visible fields speak the user's terms**")
    bullet = prompt[start : prompt.index("\n", start)]
    assert "user_verbatim" in bullet and "suggestion" in bullet


def test_revision_instructions_split_presentation_edits_from_redraws() -> None:
    line = canvas_mode_context_line("revise", "报告")
    assert "edit_chart" in line, "a revision that only restyles a chart must not redraw and fold it"
    assert "remove_chart" in line, "a chart whose data or scope changed is still replaced"


def test_prior_chart_reminder_points_presentation_changes_to_in_place_edits() -> None:
    note = prior_charts_reminder([{"section_id": "sec_trend", "title": "月度趋势"}])
    assert "sec_trend" in note and "remove_chart" in note
    assert "edit_chart" in note, "folding the original is wrong when only its presentation changed"


def test_remove_chart_is_not_the_route_for_presentation_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_tools import register_canvas_tools

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_canvas_tools(registry)
    assert "edit_chart" in registry.get("remove_chart").schema.description
