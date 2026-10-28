"""M3-12: one actor-scoped roster drives every Agent name in Lead/Planner prose."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents.capability_roles import agent_capabilities
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import (
    PeerEntry,
    TeamToolEntry,
    build_prompt_roster,
    default_prompt_peers,
    render_planner_team_tools_block,
)
from bowhead_core.plan_authoring import SYSTEM_PROMPT as PLANNER_SYSTEM_PROMPT


@pytest.mark.parametrize("removed_name", [peer.name for peer in default_prompt_peers()])
def test_absent_agent_name_has_zero_prompt_references(removed_name: str) -> None:
    peers = [peer for peer in default_prompt_peers() if peer.name != removed_name]

    prompt = build_lead_system_prompt(
        "ws_test",
        facts=[],
        peers=peers,
    )

    assert removed_name not in prompt


@pytest.mark.parametrize("agent_name", [peer.name for peer in default_prompt_peers()])
def test_planner_static_rules_do_not_name_default_agents(agent_name: str) -> None:
    """Planner names must come from its actor-scoped roster, never static prose."""

    assert agent_name not in PLANNER_SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("role", "removed_name", "configuration_copy"),
    [
        ("data", "compute", "data-query post"),
        ("compute", "compute", "run-code and data-chart post"),
        ("deck", "consultant", "PPT slide-deck post"),
        ("knowledge", "kb-analyst", "KB document research post"),
        ("image_gen", "marketing-analyst", "image-generation post"),
    ],
)
def test_missing_role_is_explicit_configuration_state(
    role: str,
    removed_name: str,
    configuration_copy: str,
) -> None:
    peers = [peer for peer in default_prompt_peers() if peer.name != removed_name]
    roster = build_prompt_roster(peers)
    prompt = build_lead_system_prompt(
        "ws_test",
        facts=[],
        peers=peers,
    )

    assert roster.holder(role) is None
    assert configuration_copy in prompt
    assert "configuration state" in prompt


def test_power_bi_only_team_agent_can_hold_data_role() -> None:
    capabilities = agent_capabilities(("teammcp.ibt_powerbi.pbi_execute_dax",))
    peer = PeerEntry(
        name="powerbi-analyst",
        description="Queries the team's Power BI semantic model.",
        source="team",
        agent_id="osa_powerbi",
        tool_names=frozenset({"teammcp.ibt_powerbi.pbi_execute_dax"}),
    )

    assert "cap_data_query" in capabilities
    roster = build_prompt_roster([peer])
    assert roster.holder("data") == "powerbi-analyst"
    assert roster.holder("compute") is None

    prompt = build_lead_system_prompt(
        "ws_test",
        facts=[],
        peers=[peer],
    )
    assert "MUST spawn powerbi-analyst" in prompt
    assert "The run-code and data-chart post currently has no holder" in prompt
    assert "Pure calculation / charts / running code MUST spawn powerbi-analyst" not in prompt


def test_manifest_default_and_explicit_default_are_byte_identical() -> None:
    implicit = build_lead_system_prompt(
        "ws_test",
        facts=[],
    )
    explicit = build_lead_system_prompt(
        "ws_test",
        facts=[],
        peers=list(default_prompt_peers()),
    )

    assert implicit == explicit


def test_planner_team_tool_footer_uses_same_compute_role_holder() -> None:
    peer = PeerEntry(
        name="automation-runner",
        description="Runs approved tools and automations",
        source="team",
        agent_id="osa_automation",
        tool_names=frozenset({"python_exec"}),
        primary_capability="cap_compute",
    )
    block = render_planner_team_tools_block(
        [TeamToolEntry("teammcp.powerbi.refresh", "Refresh a dataset")],
        data_peer=build_prompt_roster([peer]).holder("compute"),
    )

    assert "该步派 automation-runner" in block
    assert "compute" not in block


def test_roster_entries_carry_stable_agent_ids() -> None:
    peers = default_prompt_peers()

    assert peers
    assert all(peer.agent_id == f"system:{peer.name}" for peer in peers)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_team_disable_refreshes_next_turn_roster_and_blocks_cached_dispatch(tmp_path):
    from bowhead_core.agent_assignment_store import AgentAssignmentStore
    from bowhead_core.agents.lead_orchestrator import _gather_actor_scoped_prompt_peers
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry
    from test_agent_execution_binding import _create_team_agent, _setup

    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    # Member Agents run only after the assignment cutover since 8e6f85c9a.
    await AgentAssignmentStore(fixture.db_path).backfill_existing_access(apply=True, source_build="test-roster")
    runner = SubagentRunner(router=None, tool_registry=ToolRegistry(), team_subagent_store=fixture.users)
    scope = SimpleNamespace(workspace_id=fixture.workspace_id, team_id=fixture.team_id, user_id=fixture.member_id)

    def new_turn():
        return SimpleNamespace(
            parent_ctx=SimpleNamespace(team_id=fixture.team_id, user_id=fixture.member_id),
            parent_bus=SimpleNamespace(),
            team_subagent_store=fixture.users,
            custom_subagent_store=None,
            subagent_runner=runner,
        )

    first = new_turn()
    peers = await _gather_actor_scoped_prompt_peers(scope, first, registry=None)
    assert [peer.name for peer in peers] == ["exact-agent"]
    assert await runner._resolve_profile("exact-agent", fixture.member_id, fixture.team_id) is not None
    await fixture.users.update_team_subagent_meta(fixture.team_id, "osa_exact", status="disabled")
    # Existing Turn prose keeps its snapshot, but that snapshot grants no dispatch.
    assert await _gather_actor_scoped_prompt_peers(scope, first, registry=None) == peers
    assert await runner._resolve_profile("exact-agent", fixture.member_id, fixture.team_id) is None
    next_peers = await _gather_actor_scoped_prompt_peers(scope, new_turn(), registry=None)
    assert next_peers == []
    prompt = build_lead_system_prompt(fixture.workspace_id, facts=[], peers=next_peers)
    assert "exact-agent" not in prompt and "Exact Agent" not in prompt


def test_named_agent_fallback_uses_roster_identity_and_a_real_question():
    prompt = build_lead_system_prompt(
        "ws_test", facts=[], peers=[PeerEntry("method-reader", "Read the method", "team", display_name="Method Reader")]
    )
    assert "A similar description is not an alias" in prompt
    assert "obtain the user's choice through clarification_ask before substituting" in prompt
    assert "does not establish why the Agent is absent" in prompt
    assert "actual dispatched Agent's roster display name" in prompt


@pytest.mark.asyncio
async def test_planner_preflight_and_lead_share_one_turn_roster_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 Turn 多个消费者只能触发一次 actor-scoped roster 读取。"""
    from bowhead_core.agents import lead_orchestrator

    calls = 0
    expected = [PeerEntry("team-agent", "Team Agent", "team", agent_id="osa_team")]

    async def fake_gather(**_kwargs):
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(lead_orchestrator, "_gather_lead_peers", fake_gather)
    sctx = SimpleNamespace(
        workspace_id="ws_test",
        user_id="usr_test",
        team_id="team_test",
    )
    runtime = SimpleNamespace(
        parent_ctx=SimpleNamespace(),
        custom_subagent_store=None,
        team_subagent_store=None,
    )

    first = await lead_orchestrator._gather_actor_scoped_prompt_peers(
        sctx,
        runtime,
        registry=None,
    )
    first.clear()
    second = await lead_orchestrator._gather_actor_scoped_prompt_peers(
        sctx,
        runtime,
        registry=None,
    )

    assert calls == 1
    assert [peer.name for peer in second] == ["team-agent"]


def test_configuration_routing_is_rendered_from_the_roster() -> None:
    peers = list(default_prompt_peers())
    present = build_lead_system_prompt("ws_test", facts=[], peers=peers)
    assert "MUST delegate to `configuration-agent`" in present
    assert "<configuration_routing>" in present

    absent = build_lead_system_prompt("ws_test", facts=[], peers=[peer for peer in peers if peer.name != "configuration-agent"])
    assert "<configuration_routing>" not in absent
    assert "no Agent that can change saved settings" in absent
