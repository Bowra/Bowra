"""Resolved workspace choices are outcome data for the original native call."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.datarc_selection import DatarcConnectionBinding, current_datarc_selection
from bowhead_core.datarc_switch_continuation import DatarcSwitchResolution, capture_datarc_switch_call
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.types import ToolInvocation

pytestmark = pytest.mark.unit


@pytest.fixture
def resolved_switch():
    ctx = ToolExecutionContext(
        workspace_id="ws_one",
        user_id="usr_one",
        team_id="team_one",
        session_id="s_one",
        turn_id="t_one",
        datarc=object(),
        datarc_binding=None,
        member_uid="member_two",
        active_datarc_project="2",
        active_datarc_workspace_name="Source B",
    )
    ctx.datarc_binding = DatarcConnectionBinding("team_one", "dc_one", "revision_one", ctx.datarc)
    call = ToolInvocation(
        tool_name="tool_switch_context",
        arguments={"name": "Source", "remember": False},
        call_id="switch-original",
        result={
            "error_kind": "needs_clarification",
            "clarification": {"kind": "tool_context_select", "tool": "datarc", "options": [{"value": "1"}, {"value": "2"}]},
        },
    )
    card = SimpleNamespace(
        kind="tool_context_select",
        tool="datarc",
        status="resolved",
        request_id="card_one",
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        chosen_value="2",
        candidates=[SimpleNamespace(value="1"), SimpleNamespace(value="2")],
        resume_context={"team_id": ctx.team_id, "datarc_switch_call": capture_datarc_switch_call([call])},
    )
    source = current_datarc_selection(ctx).snapshot().model_dump(mode="json")
    return ctx, call, card, source


def test_original_call_receives_selected_source_without_repeating_a_write(resolved_switch):
    ctx, call, card, source = resolved_switch
    resolution = DatarcSwitchResolution.from_card(card, source=source)
    assistant, tool = resolution.messages(ctx)
    assert assistant.tool_calls[0].id == tool.tool_call_id == call.call_id
    assert assistant.tool_calls[0].arguments == call.arguments
    outcome = json.loads(tool.content)
    assert outcome["ok"] and outcome["switched"] == {"id": "2", "name": "Source B"}
    assert outcome["clarification_id"] == card.request_id
    assert "persistence" not in outcome
    assert resolution.messages(ctx) == [assistant, tool]


def test_choice_card_and_actual_receipt_supersede_the_original_remember_request(resolved_switch):
    ctx, _, card, source = resolved_switch
    card.resume_context["datarc_switch_call"]["arguments"]["remember"] = True
    receipt = {"status": "saved", "scope": "session", "reason": ""}
    resolution = DatarcSwitchResolution.from_card(card, source=source, persistence=receipt)
    original, result = resolution.messages(ctx)
    assert original.tool_calls[0].arguments["remember"] is True
    outcome = json.loads(result.content)
    assert outcome["selection"] == {"kind": "tool_context_select", "status": "selected", "candidate_count": 2, "selected_by": "user"}
    assert outcome["persistence"] == receipt
    assert outcome["persistence_receipt_origin"] == "original_selection"
    assert outcome["current_preference_verified"] is False
    assert outcome["preference_write_during_restore"] is False


def test_skipped_card_preserves_ambiguity_without_claiming_a_successful_switch(resolved_switch):
    ctx, _, card, source = resolved_switch
    card.chosen_value = "__bowhead_clarification_skip__"
    resolution = DatarcSwitchResolution.from_card(card, source=source)
    outcome = json.loads(resolution.messages(ctx)[1].content)
    assert outcome["ok"] is False and outcome["error_kind"] == "selection_skipped"
    assert outcome["selection"]["candidate_count"] == 2
    assert outcome["selection"]["status"] == "skipped"
    assert "switched" not in outcome and "persistence" not in outcome


@pytest.mark.parametrize("receipt", [{"status": "saved", "scope": "unknown", "reason": ""}, {"status": "saved", "scope": "session"}])
def test_incomplete_or_invalid_persistence_is_not_reported_as_a_receipt(resolved_switch, receipt):
    _, _, card, source = resolved_switch
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        DatarcSwitchResolution.from_card(card, source=source, persistence=receipt)


@pytest.mark.parametrize("field", ["team_id", "user_id", "workspace_id", "session_id", "turn_id"])
def test_resolution_cannot_be_projected_into_another_execution(resolved_switch, field):
    ctx, _, card, source = resolved_switch
    resolution = DatarcSwitchResolution.from_card(card, source=source)
    changed = replace(ctx, **{field: "other"})
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        resolution.messages(changed)


def test_resolution_rejects_a_changed_source(resolved_switch):
    ctx, _, card, source = resolved_switch
    resolution = DatarcSwitchResolution.from_card(card, source=source)
    ctx.datarc_run_state.current = replace(
        ctx.datarc_run_state.current, selection=replace(ctx.datarc_run_state.current.selection, member_uid="other")
    )
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        resolution.messages(ctx)


@pytest.mark.parametrize("change", ["pending", "wrong-choice", "wrong-tool", "wrong-contract", "empty-call", "empty-name"])
def test_only_the_original_resolved_native_choice_can_supply_an_outcome(resolved_switch, change):
    _, _, card, source = resolved_switch
    if change == "pending":
        card.status = "pending"
    elif change == "wrong-choice":
        card.chosen_value = "1"
    elif change == "wrong-tool":
        card.tool = "other"
    else:
        raw = card.resume_context["datarc_switch_call"]
        if change == "wrong-contract":
            raw["contract"] = "other"
        elif change == "empty-call":
            raw["call_id"] = ""
        else:
            raw["arguments"]["name"] = ""
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        DatarcSwitchResolution.from_card(card, source=source)


@pytest.mark.parametrize("prefix", ["tool_list_workspaces", "tool_switch_context", "subagent_spawn", "skill_invoke"])
def test_capture_admits_only_the_native_gate_after_roster_reads(resolved_switch, prefix):
    _, call, _, _ = resolved_switch
    captured = capture_datarc_switch_call([ToolInvocation(tool_name=prefix, arguments={}), call])
    assert (captured is not None) == (prefix == "tool_list_workspaces")


def test_old_cards_without_native_call_material_keep_their_original_reader(resolved_switch):
    _, _, card, source = resolved_switch
    card.resume_context.pop("datarc_switch_call")
    assert DatarcSwitchResolution.from_card(card, source=source) is None
