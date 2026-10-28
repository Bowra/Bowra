"""Release change items and market-detail resource summaries (Capability Center brief §4.11–4.12)."""

from __future__ import annotations

import aiosqlite
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_resource_summary import agent_resources, plugin_change_items, tool_and_skill_changes
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.user_store import UserStore


def _grant(name: str) -> dict:
    return {"skill_ref": {"kind": "team", "id": f"skl_{name}", "name": name}, "permissions": ["read"], "revision": "sha256:" + "a" * 64}


def test_tool_and_skill_changes_list_added_and_removed_items():
    base = {"tool_refs": ["web_search", "bi_query"], "skill_grants": [_grant("old")]}
    after = {"tool_refs": ["bi_query", "send_email"], "skill_grants": [_grant("new")]}
    assert tool_and_skill_changes(base, after) == {
        "tool_changes": {"added": ["send_email"], "removed": ["web_search"]},
        "skill_changes": {"added": ["new"], "removed": ["old"]},
    }
    assert tool_and_skill_changes({}, {}) == {"tool_changes": {"added": [], "removed": []}, "skill_changes": {"added": [], "removed": []}}


async def _team(tmp_path):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Team", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    plugin = await TeamPluginStore(str(path)).create(
        team_id=team.id, actor_user_id=owner.id, name="Store pack", description="KPIs", tool_refs=["web_map"], skill_grants=[]
    )
    return str(path), team.id, owner.id, plugin


async def test_plugin_change_items_describe_builtin_and_team_plugins(tmp_path):
    path, team_id, _owner, plugin = await _team(tmp_path)
    async with aiosqlite.connect(path) as db:
        items = await plugin_change_items(db, team_id, {"added": ["builtin:data-analysis", plugin["id"]], "removed": ["tcap_gone"]})
    by_id = {item["id"]: item for item in items}
    assert by_id["builtin:data-analysis"]["listing"]["en-US"]["name"] == "Data analysis"
    assert by_id["builtin:data-analysis"]["version"] == 1 and "bi_query" in by_id["builtin:data-analysis"]["tools"]
    assert by_id[plugin["id"]] == {**by_id[plugin["id"]], "name": "Store pack", "version": 1, "tools": ["web_map"], "change": "added"}
    assert by_id["tcap_gone"]["change"] == "removed" and by_id["tcap_gone"]["status"] == "deleted"


async def test_agent_resources_explain_what_the_viewer_cannot_use(tmp_path):
    path, team_id, owner_id, plugin = await _team(tmp_path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO team_settings (team_id, key, value, updated_at, updated_by) VALUES (?, 'builtin_tool_states', ?, '', ?)",
            (team_id, '{"web_search": {"enabled": false}}', owner_id),
        )
        await db.execute(
            "INSERT INTO user_settings (user_id, key, value, updated_at) VALUES (?, 'builtin_plugins_disabled', '[\"designs\"]', '')",
            (owner_id,),
        )
        await db.commit()
        resources = await agent_resources(
            db,
            team_id=team_id,
            user_id=owner_id,
            definition={"tool_refs": ["web_search", "bi_query"], "builtin_plugin_ids": ["builtin:designs"], "capability_ids": [plugin["id"]]},
        )
    plugins = {item["id"]: item for item in resources["plugins"]}
    assert plugins["builtin:designs"]["unavailable_reason"] == "disabled_by_me"
    assert plugins[plugin["id"]]["unavailable_reason"] is None
    assert resources["tools"] == [
        {"ref": "web_search", "unavailable_reason": "disabled_by_team"},
        {"ref": "bi_query", "unavailable_reason": None},
    ]


def test_system_agent_resources_follow_tool_attribution():
    from bowhead_core.agent_resource_summary import system_agent_definition

    compute = system_agent_definition("compute")
    assert compute is not None
    # Tools owned by a built-in Plugin count as that Plugin; python_exec counts as the sandbox Tool (option C).
    assert {"builtin:data-analysis", "builtin:reports-canvas", "builtin:web-research", "builtin:code-computation"} <= set(
        compute["builtin_plugin_ids"]
    )
    # Internal parts never appear; user-visible Tools in no Plugin stay direct.
    assert compute["tool_refs"] == ["skill_save_generated"]
    assert system_agent_definition("no-such-agent") is None
