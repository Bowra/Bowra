"""A selected helper gate cannot replay arbitrary prior work or cross owners."""

import json
from dataclasses import replace

import pytest
from bowhead_core.datarc_helper_switch_continuation import DatarcHelperSwitchResolution, capture_datarc_helper_switch_call
from bowhead_core.datarc_switch_continuation import DatarcSwitchResolution
from bowhead_core.types import ToolInvocation
from test_datarc_switch_continuation import resolved_switch as resolved_switch

pytestmark = pytest.mark.unit


@pytest.fixture
def delegated_switch(resolved_switch):
    ctx, switch, card, source = resolved_switch
    invocation = ToolInvocation(
        tool_name="subagent_spawn",
        call_id="delegated-original",
        arguments={"profile": "compute", "task": "Switch then query sales."},
        result={**switch.result, "profile": "compute"},
        nested=[switch],
    )
    card.resume_context = {"team_id": ctx.team_id, "datarc_helper_switch_call": capture_datarc_helper_switch_call([invocation])}
    return ctx, invocation, card, source


def test_only_the_retried_delegation_receives_its_original_switch_outcome(delegated_switch):
    ctx, invocation, card, source = delegated_switch
    resolution = DatarcHelperSwitchResolution.from_card(card, source=source)
    dispatch = resolution.dispatch(ctx)
    assert dispatch.name == "subagent_spawn" and dispatch.arguments == invocation.arguments
    assert dispatch.id != invocation.call_id and dispatch == resolution.dispatch(ctx)
    outcome = resolution.helper_outcome(ctx, call_id=dispatch.id, profile="compute", task=invocation.arguments["task"])
    assert outcome.messages(ctx)[0].tool_calls[0].id == invocation.nested[0].call_id
    assert json.loads(outcome.messages(ctx)[1].content)["switched"]["id"] == "2"
    assert DatarcSwitchResolution.from_card(card, source=source) is None
    assert resolution.helper_outcome(ctx, call_id="unrelated-call", profile="compute", task=invocation.arguments["task"]) is None


@pytest.mark.parametrize("changed", ["owner", "profile", "task"])
def test_retained_delegation_and_source_cannot_be_reassigned(delegated_switch, changed):
    ctx, invocation, card, source = delegated_switch
    resolution = DatarcHelperSwitchResolution.from_card(card, source=source)
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        resolution.helper_outcome(
            replace(ctx, session_id="s_other") if changed == "owner" else ctx,
            call_id=resolution.dispatch_id,
            profile="researcher" if changed == "profile" else "compute",
            task="A different assignment" if changed == "task" else invocation.arguments["task"],
        )


@pytest.mark.parametrize("prefix", ["bi_query", "tool_switch_context", "canvas_compose"])
def test_prior_business_work_cannot_be_replayed_as_a_first_switch_gate(delegated_switch, prefix):
    _, invocation, _, _ = delegated_switch
    invocation.nested.insert(0, ToolInvocation(tool_name=prefix, arguments={}, result={"ok": True}))
    assert capture_datarc_helper_switch_call([invocation]) is None
    invocation.nested.pop(0)
    assert capture_datarc_helper_switch_call([ToolInvocation(tool_name=prefix, arguments={}), invocation]) is None


def test_skipping_the_helper_card_does_not_dispatch_or_claim_a_query(delegated_switch):
    ctx, _, card, source = delegated_switch
    card.chosen_value = "__bowhead_clarification_skip__"
    resolution = DatarcHelperSwitchResolution.from_card(card, source=source)
    assert resolution.dispatch(ctx) is None
    outcome = json.loads(resolution.skipped_messages(ctx)[1].content)
    assert outcome["helper_status"] == "blocked" and outcome["query_executed"] is False
    assert outcome["workspace_selection"]["selection"]["candidate_count"] == 2
