"""Release-time Plugin versions and the update notice (capability-center tech design §13)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import requires_admin_review
from bowhead_core.release_plugin_versions import plugin_updates_conn, plugin_versions_conn
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.user_store import UserStore


@pytest.mark.asyncio
async def test_release_records_exact_versions_and_reports_later_updates(tmp_path):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Team", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    catalog = TeamPluginStore(str(path))
    item = await catalog.create(team_id=team.id, actor_user_id=owner.id, name="Store pack", description="", tool_refs=["web_search"], skill_grants=[])
    definition = {"tool_refs": [], "capability_ids": [item["id"]], "builtin_plugin_ids": ["builtin:designs"]}
    async with open_shared_ctx(str(path)) as db:
        recorded = await plugin_versions_conn(db, team_id=team.id, definition=definition)
        assert json.loads(recorded) == [
            {"id": "builtin:designs", "kind": "builtin", "version": 1},
            {"id": item["id"], "kind": "team", "version": 1},
        ]
        assert await plugin_updates_conn(db, team_id=team.id, recorded_json=recorded) == []
        assert await plugin_versions_conn(db, team_id=team.id, definition={"tool_refs": ["web_search"]}) is None
    await catalog.update(
        team_id=team.id,
        plugin_id=item["id"],
        actor_user_id=owner.id,
        expected_row_version=item["row_version"],
        name="Store pack",
        description="More",
        tool_refs=["web_search", "web_fetch"],
        skill_grants=[],
    )
    older_builtin = json.dumps([{"kind": "builtin", "id": "builtin:settings-space-preferences", "version": 4}])
    async with open_shared_ctx(str(path)) as db:
        updates = await plugin_updates_conn(db, team_id=team.id, recorded_json=recorded)
        builtin = await plugin_updates_conn(db, team_id=team.id, recorded_json=older_builtin)
    assert [(entry["id"], entry["name"], entry["release_version"], entry["current_version"]) for entry in updates] == [
        (item["id"], "Store pack", 1, 2)
    ]
    assert updates[0]["changed_at"]
    # A built-in Plugin newer than the recorded version is reported the same way.
    assert builtin == [
        {
            "kind": "builtin",
            "id": "builtin:settings-space-preferences",
            "name": "settings-space-preferences",
            "release_version": 4,
            "current_version": 5,
            "changed_at": None,
        }
    ]


def _validation(*, level: str, approval_required: bool, reasons: tuple[str, ...] = ()):
    risk = SimpleNamespace(level=level, approval_required=approval_required, reasons=reasons)
    return SimpleNamespace(safety_status="pass", compilation=SimpleNamespace(status="ready", risk=risk))


def test_shared_agents_with_risky_tools_need_release_approval_and_private_ones_do_not():
    audience = SimpleNamespace(mode="custom")
    external_tool = _validation(level="high", approval_required=True)
    assert requires_admin_review(external_tool, audience=audience, eval_gate_installed=False) is True
    vendored_skill = _validation(level="high", approval_required=True, reasons=("skill_vendored_dependency",))
    assert requires_admin_review(vendored_skill, audience=audience, eval_gate_installed=False) is True
    # A danger keyword in SKILL.md is a notice only; it no longer forces review on its own.
    keyword_skill = _validation(level="medium", approval_required=False, reasons=("skill_static_risk_signal",))
    assert requires_admin_review(keyword_skill, audience=audience, eval_gate_installed=False) is False
    assert requires_admin_review(external_tool, audience=SimpleNamespace(mode="private"), eval_gate_installed=False) is False
