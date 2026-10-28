"""Impact preview before disabling a Plugin or Tool (capability-center tech design §8.6)."""

from __future__ import annotations

import json

import aiosqlite
from bowhead_core.resource_disable_impact import disable_impact

# Only the columns the preview reads; the gateway test runs the same queries on the migrated schema.
_SCHEMA = """
CREATE TABLE team_subagent (id TEXT, team_id TEXT, name TEXT, display_name TEXT, status TEXT, live_revision_id TEXT);
CREATE TABLE team_subagent_revision (id TEXT, team_id TEXT, definition_json TEXT);
CREATE TABLE team_capability (id TEXT, team_id TEXT, current_revision INTEGER);
CREATE TABLE team_capability_resource (capability_id TEXT, revision INTEGER, kind TEXT, resource_ref TEXT);
CREATE TABLE workspaces (id TEXT, team_id TEXT);
CREATE TABLE scheduled_jobs (id TEXT, name TEXT, workspace_id TEXT, status TEXT);
CREATE TABLE scheduled_job_agent_binding (job_id TEXT, agent_id TEXT, state TEXT);
"""


async def _db(tmp_path) -> str:
    path = str(tmp_path / "impact.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SCHEMA)
        agents = [
            ("ts_direct", "direct", {"tool_refs": ["web_search"]}),
            ("ts_plugin", "plugin", {"tool_refs": [], "capability_ids": ["tcap_pack"]}),
            ("ts_other", "other", {"tool_refs": ["bi_query"]}),
            ("ts_builtin", "builtin", {"tool_refs": [], "builtin_plugin_ids": ["builtin:web-research"]}),
        ]
        for agent_id, name, definition in agents:
            await db.execute("INSERT INTO team_subagent VALUES (?,'t1',?,?,'enabled',?)", (agent_id, name, name.title(), f"arv_{agent_id}"))
            await db.execute("INSERT INTO team_subagent_revision VALUES (?,'t1',?)", (f"arv_{agent_id}", json.dumps(definition)))
        await db.execute("INSERT INTO team_subagent VALUES ('ts_off','t1','off','Off','disabled','arv_off')")
        await db.execute("INSERT INTO team_subagent_revision VALUES ('arv_off','t1',?)", (json.dumps({"tool_refs": ["web_search"]}),))
        await db.execute("INSERT INTO team_capability VALUES ('tcap_pack','t1',2)")
        await db.executemany("INSERT INTO team_capability_resource VALUES ('tcap_pack',?, 'tool', ?)", [(1, "web_map"), (2, "web_search")])
        await db.execute("INSERT INTO workspaces VALUES ('ws1','t1')")
        await db.executemany(
            "INSERT INTO scheduled_jobs VALUES (?,?, 'ws1', ?)",
            [("job_1", "Weekly", "active"), ("job_2", "Paused", "paused"), ("job_3", "Lead digest", "active")],
        )
        await db.executemany(
            "INSERT INTO scheduled_job_agent_binding VALUES (?,?, 'ready')",
            [("job_1", "ts_plugin"), ("job_2", "ts_direct"), ("job_3", "system:lead")],
        )
        await db.commit()
    return path


async def test_tool_impact_covers_direct_plugin_and_system_agents(tmp_path):
    impact = await disable_impact(await _db(tmp_path), "t1", "builtin_tool", "web_search")
    assert {item["name"] for item in impact["system_agents"]} >= {"lead", "compute"}
    assert [item["id"] for item in impact["team_agents"]] == ["ts_builtin", "ts_direct", "ts_plugin"]
    # job_2 is paused, so only the active schedules of affected Agents are listed.
    assert [item["id"] for item in impact["scheduled_tasks"]] == ["job_3", "job_1"]


async def test_team_plugin_impact_lists_agents_that_select_it(tmp_path):
    impact = await disable_impact(await _db(tmp_path), "t1", "team_plugin", "tcap_pack")
    assert impact["system_agents"] == []
    assert [item["id"] for item in impact["team_agents"]] == ["ts_plugin"]
    assert [item["id"] for item in impact["scheduled_tasks"]] == ["job_1"]


async def test_builtin_plugin_impact_follows_tool_membership_of_system_agents(tmp_path):
    path = await _db(tmp_path)
    impact = await disable_impact(path, "t1", "builtin_plugin", "web-research")
    assert {item["name"] for item in impact["system_agents"]} == {"compute", "lead", "marketing-analyst"}
    # Team Agents that selected the built-in Plugin are affected; others are not.
    assert [item["id"] for item in impact["team_agents"]] == ["ts_builtin"]
    settings = await disable_impact(path, "t1", "builtin_plugin", "settings-space-preferences")
    assert [item["name"] for item in settings["system_agents"]] == ["configuration-agent"]


async def test_unknown_resources_return_none(tmp_path):
    path = await _db(tmp_path)
    assert await disable_impact(path, "t1", "builtin_plugin", "nope") is None
    assert await disable_impact(path, "t1", "builtin_tool", "skill_read") is None
    assert await disable_impact(path, "t1", "team_plugin", "tcap_missing") is None


async def test_lead_is_not_listed_for_tools_its_run_denies(tmp_path):
    impact = await disable_impact(await _db(tmp_path), "t1", "builtin_tool", "bi_query")
    assert "lead" not in {item["name"] for item in impact["system_agents"]}


def test_sandbox_impact_follows_runtime_names():
    from bowhead_core.resource_disable_impact import _affected_system_agents

    # System manifests list python_exec; the user-facing Tool and Plugin name python_sandbox.
    assert "compute" in _affected_system_agents("builtin_tool", "python_sandbox")
    assert "compute" in _affected_system_agents("builtin_plugin", "code-computation")
