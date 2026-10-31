"""Lookup scope and closeout rules must reach the actual model prompt paths.

These tests verify prompt delivery and precedence, not real-model compliance.
"""

import pytest
from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import PeerEntry
from bowhead_core.agents.prompts.simple_lookup_locale import render_lookup_template
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.subagent import PROFILES, _build_compute_system_prompt


@pytest.mark.parametrize("custom_peer", [False, True], ids=["builtin", "team-data-agent"])
def test_lookup_scope_reaches_lead_for_each_data_roster(custom_peer: bool) -> None:
    peers = [PeerEntry(name="team-data", description="Data lookup", source="team", tool_names=frozenset({"bi_query"}))] if custom_peer else None
    prompt = build_lead_system_prompt("ws_lookup", facts=[], peers=peers)
    assert "keep `task` and `data_hint` limited to the requested metrics, period and comparisons" in prompt
    assert "Prior artifacts are available evidence, not additional requirements" in prompt
    assert "do not add brand/store breakdowns" in prompt
    assert "return once the requested facts are verified" in prompt
    assert "In the final answer, do not add a derived comparison that has not been verified" in prompt
    assert "evidence that identifies the compared value, baseline and calculation" in prompt


@pytest.mark.parametrize("path", ["boot", "fallback"])
def test_lookup_closeout_and_explicit_artifacts_reach_compute(path: str) -> None:
    if path == "boot":
        register_all_system_manifests(AgentRegistry.bootstrap())
        prompt = PROFILES["compute"].system_prompt
    else:
        prompt = _build_compute_system_prompt()
    prompt = render_lookup_template(prompt)

    assert "a sourced `exec_summary` with all requested values is sufficient" in prompt
    assert "an existing summary already containing the requested values and rates does not need another calculation or CSV export" in prompt
    assert "Simple lookup delivery takes precedence over the general artifact rule" in prompt
    assert "the next action is `subagent_return` (after an explicitly requested artifact, if any)" in prompt
    assert "Existing breakdown files and Lead suggestions do not expand the user's original request" in prompt
    assert "concrete evidence that a requested value, scope or unit is unresolved" in prompt
    assert "deliver explicit table/chart/report requests and multi-entity or multi-period lists through `add_chart" in prompt
