"""Per-turn removal of disabled Plugins and Tools (capability-center tech design §8)."""

from __future__ import annotations

from bowhead_core.resource_disable import (
    DisableState,
    PluginContribution,
    RemovedResource,
    ResourceProvenance,
    attribute_system_tools,
    compute_turn_resources,
    default_disabled_tools,
    team_disabled_tools,
)

WEB = PluginContribution("builtin", "web-research", ("web_search", "web_fetch"))
PACK = PluginContribution("team", "tcap_pack", ("bi_query",), ("skill_store_kpi",))


def test_disabling_a_plugin_removes_only_its_contribution():
    provenance = ResourceProvenance(direct_tools=("web_search",), plugins=(WEB,))
    result = compute_turn_resources(provenance, DisableState(team_builtin_plugins=frozenset({"web-research"})))
    assert result.tools == {"web_search"}
    assert result.removed == (RemovedResource("plugin", "web-research", "team"),)


def test_disabling_a_tool_removes_it_from_every_source():
    provenance = ResourceProvenance(direct_tools=("web_search",), plugins=(WEB,))
    result = compute_turn_resources(provenance, DisableState(team_tools=frozenset({"web_search"})))
    assert result.tools == {"web_fetch"}
    assert result.removed == (RemovedResource("tool", "web_search", "team"),)


def test_team_plugins_follow_status_and_personal_settings():
    provenance = ResourceProvenance(direct_tools=("read_report",), plugins=(PACK,))
    inactive = compute_turn_resources(provenance, DisableState(inactive_team_plugins=frozenset({"tcap_pack"})))
    assert inactive.tools == {"read_report"} and inactive.skills == set()
    assert inactive.removed == (RemovedResource("plugin", "tcap_pack", "team"),)
    mine = compute_turn_resources(provenance, DisableState(user_team_plugins=frozenset({"tcap_pack"})))
    assert mine.removed == (RemovedResource("plugin", "tcap_pack", "user"),)
    kept = compute_turn_resources(provenance, DisableState())
    assert kept.tools == {"read_report", "bi_query"} and kept.skills == {"skill_store_kpi"} and kept.removed == ()


def test_team_disable_wins_over_personal_for_builtin_plugins():
    state = DisableState(team_builtin_plugins=frozenset({"web-research"}), user_builtin_plugins=frozenset({"web-research"}))
    result = compute_turn_resources(ResourceProvenance(plugins=(WEB,)), state)
    assert result.removed == (RemovedResource("plugin", "web-research", "team"),)


def test_high_risk_tools_stay_enabled_until_confirmation_ships(monkeypatch):
    from bowhead_core import resource_disable

    assert default_disabled_tools() == set()
    monkeypatch.setattr(resource_disable, "HIGH_RISK_DEFAULT_OFF", True)
    assert default_disabled_tools() == {"send_email", "google_doc_write", "google_sheet_write", "google_slides_write"}
    assert team_disabled_tools({"send_email": {"enabled": True}}) == {"google_doc_write", "google_sheet_write", "google_slides_write"}


def test_explicit_tool_states_override_the_defaults():
    states = {"send_email": {"enabled": True}, "web_search": {"enabled": False}, "not_a_tool": {"enabled": False}, "bi_query": "bad"}
    assert team_disabled_tools(states) == {"web_search"}


def test_system_agent_tools_are_attributed_to_their_plugin():
    provenance = attribute_system_tools(["bi_query", "bi_schema", "skill_read", "web_search", "subagent_return"])
    assert provenance.direct_tools == ("skill_read", "subagent_return")
    assert {(plugin.id, plugin.tools) for plugin in provenance.plugins} == {
        ("data-analysis", ("bi_query", "bi_schema")),
        ("web-research", ("web_search",)),
    }
    result = compute_turn_resources(provenance, DisableState(team_builtin_plugins=frozenset({"data-analysis"})))
    assert result.tools == {"skill_read", "web_search", "subagent_return"}


# ─── Settings read and run-time application ──────────────────────────────


async def _seed(db_path, *, team=None, user=None, plugins=()):
    import aiosqlite
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_settings_store import UserSettingsStore

    await TeamSettingsStore(db_path).init()
    await UserSettingsStore(db_path).init()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE team_capability (id TEXT, team_id TEXT, status TEXT)")
        await db.executemany("INSERT INTO team_capability VALUES (?, 't1', ?)", plugins)
        for key, value in (team or {}).items():
            await db.execute("INSERT INTO team_settings (team_id, key, value, updated_at) VALUES ('t1', ?, ?, '')", (key, value))
        for key, value in (user or {}).items():
            await db.execute("INSERT INTO user_settings (user_id, key, value, updated_at) VALUES ('u1', ?, ?, '')", (key, value))
        await db.commit()


async def test_disable_state_reads_team_user_and_plugin_status(tmp_path):
    from bowhead_core.resource_disable import load_disable_state

    db_path = str(tmp_path / "bowhead.db")
    await _seed(
        db_path,
        team={"builtin_plugins_disabled": '["designs"]', "builtin_tool_states": '{"send_email": {"enabled": true}}'},
        user={"builtin_plugins_disabled": '["web-research"]', "team_plugins_disabled": '["tcap_a"]'},
        plugins=[("tcap_a", "active"), ("tcap_b", "disabled")],
    )
    state = await load_disable_state(db_path, "t1", "u1")
    assert state.team_builtin_plugins == {"designs"}
    assert state.user_builtin_plugins == {"web-research"}
    assert state.user_team_plugins == {"tcap_a"}
    assert state.inactive_team_plugins == {"tcap_b"}
    assert state.team_tools == set()


async def test_bad_settings_fall_back_to_defaults(tmp_path):
    from bowhead_core.resource_disable import load_disable_state

    db_path = str(tmp_path / "bowhead.db")
    await _seed(db_path, team={"builtin_plugins_disabled": "{not json", "builtin_tool_states": "[]"})
    state = await load_disable_state(db_path, "t1", "u1")
    assert state.team_builtin_plugins == set()
    assert state.team_tools == default_disabled_tools()
    missing = await load_disable_state(str(tmp_path / "missing" / "x.db"), "t1", "u1")
    assert missing == DisableState(team_tools=default_disabled_tools())


def _profile(tools, bundle=None):
    from bowhead_core.subagent import SubagentProfile

    return SubagentProfile(name="p", llm_agent_type="analyst", tool_whitelist=tuple(tools), system_prompt="", team_tool_bundle=bundle)


def test_runner_removes_plugin_tools_from_system_agents_only():
    from bowhead_core.subagent import _apply_resource_disable

    state = DisableState(team_builtin_plugins=frozenset({"data-analysis"}), team_tools=frozenset({"send_email"}))
    system = _apply_resource_disable(_profile(["bi_query", "skill_read", "send_email"]), state)
    assert system.tool_whitelist == ("skill_read",)
    custom = _apply_resource_disable(_profile(["bi_query", "skill_read", "send_email"], bundle=()), state)
    assert custom.tool_whitelist == ("bi_query", "skill_read")


def test_runner_is_unchanged_without_state():
    from bowhead_core.subagent import _apply_resource_disable

    profile = _profile(["bi_query", "send_email"])
    assert _apply_resource_disable(profile, None) is profile
    assert _apply_resource_disable(profile, DisableState()) is profile


def _grant(skill_id):
    from bowhead_core.agent_dependency_compiler import ExactSkillGrant

    return ExactSkillGrant("team", skill_id, skill_id, ("read",), "sha256:" + "a" * 64, "reference")


def test_team_agent_loses_only_the_disabled_plugin_contribution():
    from dataclasses import replace

    from bowhead_core.subagent import _apply_resource_disable

    provenance = ResourceProvenance(
        direct_tools=("web_search",),
        direct_skills=("skill_mine",),
        plugins=(PluginContribution("team", "tcap_pack", ("web_map", "teammcp.src.read"), ("skill_pack",)),),
    )
    profile = replace(
        _profile(["web_search", "web_map", "skill_read"], bundle=("teammcp.src.read", "teammcp.src.other")),
        resource_provenance=provenance,
        skill_runtime_grants=(_grant("skill_mine"), _grant("skill_pack")),
    )
    result = _apply_resource_disable(profile, DisableState(inactive_team_plugins=frozenset({"tcap_pack"})))
    # skill_read and teammcp.src.other are not in the provenance, so they stay.
    assert result.tool_whitelist == ("web_search", "skill_read")
    assert result.team_tool_bundle == ("teammcp.src.other",)
    assert [grant.id for grant in result.skill_runtime_grants] == ["skill_mine"]
    assert result.skill_runtime_scope.read_names == ("skill_mine",)


def test_team_agent_without_provenance_keeps_its_bundle():
    from bowhead_core.subagent import _apply_resource_disable

    profile = _profile(["web_search", "send_email"], bundle=("teammcp.src.read",))
    result = _apply_resource_disable(profile, DisableState(team_tools=frozenset({"send_email"})))
    assert result.tool_whitelist == ("web_search",)
    assert result.team_tool_bundle == ("teammcp.src.read",)


def test_removals_are_logged_with_turn_and_team(caplog):
    import logging

    from bowhead_core.subagent import _apply_resource_disable

    with caplog.at_level(logging.INFO, logger="bowhead_core.subagent"):
        _apply_resource_disable(
            _profile(["bi_query"]), DisableState(team_builtin_plugins=frozenset({"data-analysis"})), turn_id="t_1", team_id="team_a"
        )
    assert "turn=t_1 team=team_a profile=p removed=[('plugin', 'data-analysis', 'team')]" in caplog.text


def test_plugin_inactive_at_admission_is_removed_without_turn_state():
    from dataclasses import replace

    from bowhead_core.subagent import _apply_resource_disable

    provenance = ResourceProvenance(
        direct_tools=("web_search",), plugins=(PluginContribution("team", "tcap_pack", ("teammcp.src.read",), inactive=True),)
    )
    profile = replace(_profile(["web_search"], bundle=("teammcp.src.read",)), resource_provenance=provenance)
    result = _apply_resource_disable(profile, None)
    assert result.tool_whitelist == ("web_search",)
    assert result.team_tool_bundle == ()


def test_opened_tools_pass_the_load_and_call_role_gates(monkeypatch):
    from bowhead_core.subagent import _strip_non_analyst_natives
    from bowhead_core.tool_registry import _opened_to_role

    # Email and Google writes are opened too: selecting them into a custom Agent is the Owner's grant.
    assert _strip_non_analyst_natives(("tool_list_workspaces", "bi_query", "send_email", "subagent_spawn")) == (
        "tool_list_workspaces",
        "bi_query",
        "send_email",
    )
    assert _opened_to_role("tool_list_workspaces", "analyst")
    assert _opened_to_role("send_email", "analyst") and _opened_to_role("google_sheet_write", "analyst")
    assert not _opened_to_role("subagent_spawn", "analyst")


# ─── Review fixes: sandbox entries, renamed Tools, prelude, fail-closed provenance ─────


def test_disabling_the_sandbox_removes_both_runtime_entries():
    from dataclasses import replace

    from bowhead_core.subagent import _apply_resource_disable, _skill_operation_scope_error

    system = _apply_resource_disable(
        _profile(["bi_query", "python_exec", "skill_invoke", "skill_read"]), DisableState(team_builtin_plugins=frozenset({"code-computation"}))
    )
    assert system.tool_whitelist == ("bi_query", "skill_invoke", "skill_read")
    assert _skill_operation_scope_error(system, "skill_invoke", {"name": "python_sandbox"}) is not None
    assert _skill_operation_scope_error(system, "skill_invoke", {"name": "kpi-method"}) is None

    team = replace(
        _profile(["python_exec", "skill_invoke"], bundle=()),
        skill_invoke_allowlist=("python_sandbox",),
        resource_provenance=ResourceProvenance(direct_tools=("python_sandbox",)),
    )
    result = _apply_resource_disable(team, DisableState(team_tools=frozenset({"python_sandbox"})))
    assert result.tool_whitelist == ()


def test_renamed_tools_follow_their_current_name():
    from dataclasses import replace

    from bowhead_core.subagent import _apply_resource_disable

    provenance = ResourceProvenance(
        direct_tools=("canvas_add_chart",),
        plugins=(PluginContribution("builtin", "reports-canvas", ("add_chart", "read_report")),),
    )
    profile = replace(_profile(["add_chart", "read_report"], bundle=()), resource_provenance=provenance)
    # The directly selected chart Tool (old name) survives disabling the Plugin.
    assert _apply_resource_disable(profile, DisableState(team_builtin_plugins=frozenset({"reports-canvas"}))).tool_whitelist == ("add_chart",)
    # Disabling the Tool by its current name removes the old-name selection too.
    assert _apply_resource_disable(profile, DisableState(team_tools=frozenset({"add_chart"}))).tool_whitelist == ("read_report",)


def test_removed_skills_leave_the_auto_read_prelude():
    from bowhead_core.subagent import _prune_auto_read_skills

    prompt = 'Intro\n\n## Auto-read skills\n第一步**必须**先 `skill_read` 读这些 SOP，再开工: "a", "b"\n\nRest'
    assert _prune_auto_read_skills(prompt, {"a"}) == 'Intro\n\n## Auto-read skills\n第一步**必须**先 `skill_read` 读这些 SOP，再开工: "a"\n\nRest'
    assert _prune_auto_read_skills(prompt, set()) == "Intro\n\nRest"
    assert _prune_auto_read_skills(prompt, {"a", "b"}) == prompt


async def test_unreadable_team_plugin_provenance_refuses_the_run(monkeypatch):
    from types import SimpleNamespace

    import pytest
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError
    from bowhead_core.subagent import SubagentRunner

    from bowhead_core import resource_disable

    async def unreadable(*_args):
        return None

    monkeypatch.setattr(resource_disable, "team_plugin_provenance", unreadable)
    runner = SimpleNamespace(team_subagent_store=SimpleNamespace(db_path="unused.db"))
    binding = SimpleNamespace(authored_definition={"tool_refs": []}, definition={"resolved_capabilities": [{"id": "tcap_a", "revision": 1}]})
    with pytest.raises(AgentExecutionBindingError, match="capability_unavailable"):
        await SubagentRunner._with_team_plugin_provenance(runner, _profile([]), binding)
    runner.team_subagent_store = None
    with pytest.raises(AgentExecutionBindingError, match="capability_unavailable"):
        await SubagentRunner._with_team_plugin_provenance(runner, _profile([]), binding)


def test_a_stored_always_confirm_value_is_ignored_and_dropped_on_the_next_write():
    import json

    from bowhead_core.resource_disable import tool_states, with_tool_state

    raw = '{"send_email": {"enabled": true, "always_confirm": true}}'
    assert tool_states(raw)["send_email"] == {"enabled": True}
    assert json.loads(with_tool_state(raw, "send_email", enabled=False)) == {"send_email": {"enabled": False}}
