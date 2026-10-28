"""Team Plugin versions expand into Agent resources without changing direct selections."""

from __future__ import annotations

import json
import sqlite3
from importlib import import_module

import pytest
from alembic import command
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import _resolved_team_definition_json
from bowhead_core.team_plugin_store import TeamPluginError, TeamPluginStore, expand_plugins_conn
from bowhead_core.user_store import UserStore
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0123_team_capability_bundles")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_team_plugin_migration_is_scoped_and_repeatable(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        present = db.execute("SELECT 1 FROM sqlite_master WHERE name='team_capability'").fetchone() is not None
        assert present == (target == "core")
        if target == "core":
            assert {row[1] for row in db.execute("PRAGMA table_info(turn_agent_execution)")} >= {"resolved_definition_json"}


@pytest.mark.asyncio
async def test_plugin_latest_revision_and_team_boundary(tmp_path):
    path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{path}", target="core") == ("core",)
    users = UserStore(path)
    await users.initialize()
    first_team = await users.create_team(name="First", max_seats=2)
    second_team = await users.create_team(name="Second", max_seats=2)
    owner = await users.create_user(team_id=first_team.id, email="capability@example.com")
    catalog = TeamPluginStore(str(path))
    item = await catalog.create(
        team_id=first_team.id,
        actor_user_id=owner.id,
        name="Research",
        description="Research tools",
        tool_refs=["web_search"],
        skill_grants=[],
    )
    assert await catalog.get(second_team.id, item["id"]) is None
    async with open_shared_ctx(str(path)) as db:
        with pytest.raises(TeamPluginError, match="capability_unavailable"):
            await expand_plugins_conn(db, team_id=second_team.id, definition={"capability_ids": [item["id"]]})
        with pytest.raises(TeamPluginError, match="capability_agent_resources_invalid"):
            await expand_plugins_conn(db, team_id=first_team.id, definition={"capability_ids": [item["id"]], "tool_refs": None})
    with pytest.raises(TeamPluginError, match="capability_skill_invalid"):
        await catalog.create(
            team_id=first_team.id,
            actor_user_id=owner.id,
            name="Private",
            description="Personal Skill must not be shared",
            tool_refs=[],
            skill_grants=[
                {
                    "skill_ref": {"kind": "user", "id": "skill_personal", "name": "private"},
                    "permissions": ["read", "invoke"],
                    "revision": "sha256:personal",
                    "distribution": "reference",
                }
            ],
        )
    async with open_shared_ctx(str(path)) as db:
        original = await expand_plugins_conn(
            db, team_id=first_team.id, definition={"tool_refs": ["web_fetch"], "skill_grants": [], "capability_ids": [item["id"]]}
        )
    assert original["tool_refs"] == ["web_fetch", "web_search"]
    updated = await catalog.update(
        team_id=first_team.id,
        plugin_id=item["id"],
        actor_user_id=owner.id,
        expected_row_version=item["row_version"],
        name="Research",
        description="Updated",
        tool_refs=["web_map", "teammcp.ibt_sales.datarc_query"],
        skill_grants=[],
    )
    assert updated["revision"] == 2
    async with open_shared_ctx(str(path)) as db:
        latest = await expand_plugins_conn(
            db, team_id=first_team.id, definition={"tool_refs": ["web_fetch"], "skill_grants": [], "capability_ids": [item["id"]]}
        )
    assert latest["tool_refs"] == ["teammcp.ibt_sales.datarc_query", "web_fetch", "web_map"]
    assert latest["resolved_capabilities"] == [{"id": item["id"], "revision": 2}]
    with pytest.raises(TeamPluginError, match="capability_version_conflict"):
        await catalog.update(
            team_id=first_team.id,
            plugin_id=item["id"],
            actor_user_id=owner.id,
            expected_row_version=item["row_version"],
            name="Research",
            description="Stale",
            tool_refs=["web_search"],
            skill_grants=[],
        )
    await catalog.set_enabled(team_id=first_team.id, plugin_id=item["id"], enabled=False, expected_row_version=updated["row_version"])
    async with open_shared_ctx(str(path)) as db:
        with pytest.raises(TeamPluginError, match="capability_unavailable"):
            await expand_plugins_conn(db, team_id=first_team.id, definition={"tool_refs": [], "skill_grants": [], "capability_ids": [item["id"]]})


@pytest.mark.asyncio
async def test_delete_plugin_cleans_drafts_and_resources_but_keeps_published_history(tmp_path):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="First", max_seats=2)
    other_team = await users.create_team(name="Second", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    catalog = TeamPluginStore(str(path))
    item = await catalog.create(team_id=team.id, actor_user_id=owner.id, name="Research", description="", tool_refs=["web_search"], skill_grants=[])
    updated = await catalog.update(
        team_id=team.id,
        plugin_id=item["id"],
        actor_user_id=owner.id,
        expected_row_version=1,
        name="Research",
        description="New",
        tool_refs=["web_fetch"],
        skill_grants=[],
    )
    definition = {"capability_ids": [item["id"]], "tool_refs": [], "skill_grants": []}
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """INSERT INTO team_subagent(id,team_id,name,display_name,description,system_prompt,tool_refs_json,
               skills_json,visibility,created_by,status,created_at,updated_at)
               VALUES (?,?,?,?,?,'','[]','[]','private',?,'enabled','now','now')""",
            ("osa_1", team.id, "agent", "Agent", "", owner.id),
        )
        db.execute(
            """INSERT INTO team_subagent_revision(id,team_id,subagent_id,sequence,revision_kind,definition_json,
               listing_json,definition_hash,dependency_fingerprint,capability_fingerprint,risk_compiler_version,
               risk_json,created_by,created_at) VALUES (?,?,?,1,'authored',?,?,'sha256:original','sha256:dependency',
               'sha256:capability','test','{}',?,'now')""",
            ("arv_1", team.id, "osa_1", json.dumps(definition), "{}", owner.id),
        )
        db.execute("UPDATE team_subagent SET live_revision_id='arv_1' WHERE id='osa_1'")
        db.execute(
            "INSERT INTO team_subagent_capability_binding(team_id,subagent_id,revision_id,capability_id) VALUES (?,?,?,?)",
            (team.id, "osa_1", "arv_1", item["id"]),
        )
        db.execute(
            """INSERT INTO team_subagent_working_copy(id,team_id,subagent_id,definition_json,listing_json,
               audience_json,validation_json,updated_by,created_at,updated_at)
               VALUES ('awc_1',?,?,?,'{}','{}','{}',?,'now','now')""",
            (team.id, "osa_1", json.dumps(definition), owner.id),
        )
        db.commit()

    assert (await catalog.get(team.id, item["id"]))["agent_count"] == 1
    with pytest.raises(TeamPluginError, match="capability_not_found"):
        await catalog.delete(team_id=other_team.id, plugin_id=item["id"], expected_row_version=updated["row_version"])
    with pytest.raises(TeamPluginError, match="capability_version_conflict"):
        await catalog.delete(team_id=team.id, plugin_id=item["id"], expected_row_version=1)
    await catalog.delete(team_id=team.id, plugin_id=item["id"], expected_row_version=updated["row_version"])
    assert await catalog.get(team.id, item["id"]) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_capability WHERE id=?", (item["id"],)).fetchone()[0] == 0
        for table in ("team_capability_revision", "team_capability_resource", "team_subagent_capability_binding"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE capability_id=?", (item["id"],)).fetchone()[0] == 0
        assert json.loads(db.execute("SELECT definition_json FROM team_subagent_revision WHERE id='arv_1'").fetchone()[0]) == definition
        working = db.execute("SELECT definition_json,row_version,validation_json FROM team_subagent_working_copy WHERE id='awc_1'").fetchone()
        assert json.loads(working[0])["capability_ids"] == []
        assert working[1] == 2
        assert json.loads(working[2])["status"] == "not_validated"
    async with open_shared_ctx(str(path)) as db:
        resolved_json = await _resolved_team_definition_json(db, team_id=team.id, snapshot={"definition_json": json.dumps(definition)})
        with pytest.raises(TeamPluginError, match="capability_unavailable"):
            await expand_plugins_conn(db, team_id=team.id, definition=definition)
    assert resolved_json is not None
    resolved = json.loads(resolved_json)
    assert resolved["capability_ids"] == []
    assert resolved["tool_refs"] == []


@pytest.mark.asyncio
async def test_disabled_plugin_expands_for_run_admission_and_records_its_frozen_contribution(tmp_path, monkeypatch):
    from bowhead_core.resource_disable import team_plugin_provenance

    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Team", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    catalog = TeamPluginStore(str(path))
    item = await catalog.create(
        team_id=team.id, actor_user_id=owner.id, name="Research", description="Research tools", tool_refs=["web_map"], skill_grants=[]
    )
    await catalog.set_enabled(team_id=team.id, plugin_id=item["id"], enabled=False, expected_row_version=item["row_version"])
    authored = {"tool_refs": ["web_search"], "skill_grants": [], "capability_ids": [item["id"]]}
    async with open_shared_ctx(str(path)) as db:
        with pytest.raises(TeamPluginError, match="capability_unavailable"):
            await expand_plugins_conn(db, team_id=team.id, definition=authored)
        resolved = await expand_plugins_conn(db, team_id=team.id, definition=authored, allow_inactive=True)
        snapshot = {"definition_json": json.dumps(authored)}
        assert json.loads(await _resolved_team_definition_json(db, team_id=team.id, snapshot=snapshot)) == resolved
    assert resolved["tool_refs"] == ["web_map", "web_search"]
    provenance = await team_plugin_provenance(str(path), authored, resolved)
    assert provenance is not None
    assert provenance.direct_tools == ("web_search",)
    assert [(plugin.id, plugin.tools) for plugin in provenance.plugins] == [(item["id"], ("web_map",))]


@pytest.mark.asyncio
async def test_builtin_plugins_expand_at_run_admission(tmp_path):
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError, _expand_builtin_plugins_for_publication
    from bowhead_core.resource_disable import DisableState, compute_turn_resources, team_plugin_provenance

    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    authored = {"tool_refs": ["web_search", "bi_query"], "skill_grants": [], "builtin_plugin_ids": ["builtin:data-analysis"]}
    snapshot = {"definition_json": json.dumps(authored)}
    async with open_shared_ctx(str(path)) as db:
        resolved = json.loads(await _resolved_team_definition_json(db, team_id="team_a", snapshot=snapshot))
    assert resolved == _expand_builtin_plugins_for_publication(authored)
    assert resolved["resolved_builtin_plugins"] == [{"name": "data-analysis", "version": 1}]
    provenance = await team_plugin_provenance(str(path), authored, resolved)
    assert [(plugin.kind, plugin.id) for plugin in provenance.plugins] == [("builtin", "data-analysis")]
    # Disabling the Plugin removes what it brought; bi_query was also selected directly, so it stays.
    turn = compute_turn_resources(provenance, DisableState(team_builtin_plugins=frozenset({"data-analysis"})))
    assert turn.tools == {"web_search", "bi_query"}
    with pytest.raises(AgentPublicationBlockedError, match="builtin_plugin_not_selectable"):
        _expand_builtin_plugins_for_publication({**authored, "builtin_plugin_ids": ["builtin:email"]})


def _team_skill_grant(revision: str, permissions: list[str] | None = None) -> dict:
    return {
        "skill_ref": {"kind": "team", "id": "tsk_sales", "name": "sales-report"},
        "permissions": permissions or ["read"],
        "revision": revision,
        "distribution": "reference",
    }


@pytest.mark.asyncio
async def test_skill_revision_differences_do_not_conflict_on_expansion_or_run_admission(tmp_path):
    """A frozen Agent keeps its direct grant's revision; the Plugin was saved with a later one."""
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Team", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    released, current = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    catalog = TeamPluginStore(str(path))
    newer = await catalog.create(
        team_id=team.id, actor_user_id=owner.id, name="Sales", description="", tool_refs=[], skill_grants=[_team_skill_grant(current)]
    )
    older = await catalog.create(
        team_id=team.id, actor_user_id=owner.id, name="Reports", description="", tool_refs=[], skill_grants=[_team_skill_grant(released)]
    )
    authored = {"tool_refs": [], "skill_grants": [_team_skill_grant(released)], "capability_ids": [newer["id"], older["id"]]}
    async with open_shared_ctx(str(path)) as db:
        expanded = await expand_plugins_conn(db, team_id=team.id, definition=authored)
        admitted = await _resolved_team_definition_json(db, team_id=team.id, snapshot={"definition_json": json.dumps(authored)})
        # Plugins alone agree on the Skill too.
        plugins_only = await expand_plugins_conn(
            db, team_id=team.id, definition={"tool_refs": [], "skill_grants": [], "capability_ids": [older["id"], newer["id"]]}
        )
        conflicting = {**authored, "skill_grants": [_team_skill_grant(released, ["invoke", "read"])]}
        with pytest.raises(TeamPluginError, match="capability_skill_conflict"):
            await expand_plugins_conn(db, team_id=team.id, definition=conflicting)
    # The direct grant wins; the compiler rebases it to the latest approved revision.
    assert expanded["skill_grants"] == [_team_skill_grant(released)]
    assert admitted is not None and json.loads(admitted)["skill_grants"] == [_team_skill_grant(released)]
    assert plugins_only["skill_grants"] == [_team_skill_grant(released)]
