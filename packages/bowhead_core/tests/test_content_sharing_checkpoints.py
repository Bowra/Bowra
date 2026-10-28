"""Runtime sharing checkpoints use current access and personal assignments.

Agent fixtures run the real migration/cutover; the Skill resolver retains its
supported prefetch-set contract through the separate store port.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agents.lead_orchestrator import _gather_lead_peers
from bowhead_core.subagent import SubagentRunner
from bowhead_core.user_store import UserStore
from bowhead_skill.skill_resolver import resolve_skill
from test_agent_legacy_deployment_dispatch import _publish_release
from test_team_subagent_access import _setup_fixture


class _Catalog:
    def get(self, name):  # builtin miss → fall through to team branch
        return None


class _TeamSkillRec:
    def __init__(self, *, rid, visibility, created_by, source_path="/nonexistent"):
        self.id = rid
        self.visibility = visibility
        self.created_by = created_by
        self.source_path = source_path


class _TeamSkillStore:
    def __init__(self, rec):
        self._rec = rec

    async def get_by_name(self, team_id, name, only_enabled=True):
        return self._rec


@pytest.fixture
async def agents(tmp_path):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    fixture = await _setup_fixture(str(path))
    await AgentAssignmentStore(path).backfill_existing_access(apply=True, source_build="test-sharing")
    # Member Agents run only from a live release since 8e6f85c9a.
    owner = SimpleNamespace(db_path=fixture.db_path, team_id=fixture.team_id, owner_id=fixture.owner.id)
    for agent_id, name in (("osa_private", "private-agent"), ("osa_custom_user", "custom-user-agent")):
        await _publish_release(owner, agent_id=agent_id, name=name, display_name=name)
    return fixture


async def revoke_shared_agent(agents):
    async with aiosqlite.connect(agents.db_path) as db:
        await db.execute(
            "UPDATE resource_grants SET revoked_at='2026-09-14T00:00:00+00:00' WHERE resource_id='osa_custom_user' AND principal_id=?",
            (agents.member.id,),
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_shared_agent_rechecks_current_access(agents):
    store = UserStore(agents.db_path)
    runner = SubagentRunner(router=None, tool_registry=None, team_subagent_store=store)
    assert await runner._resolve_profile("private-agent", agents.member.id, agents.team_id) is None
    assert await runner._resolve_profile("private-agent", agents.owner.id, agents.team_id) is not None
    profile = await runner._resolve_profile("custom-user-agent", agents.member.id, agents.team_id)
    assert profile is not None and profile.name == "custom-user-agent"
    await revoke_shared_agent(agents)
    assert await runner._resolve_profile("custom-user-agent", agents.member.id, agents.team_id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lead_peers_advertise_currently_shared_agent(agents):
    async def gather():
        peers = await _gather_lead_peers(
            registry=None,
            workspace_id=None,
            custom_store=None,
            user_id=agents.member.id,
            team_id=agents.team_id,
            team_subagent_store=UserStore(agents.db_path),
        )
        return {peer.name for peer in peers}

    names = await gather()
    assert "private-agent" not in names
    assert "custom-user-agent" in names
    await revoke_shared_agent(agents)
    assert "custom-user-agent" not in await gather()


# ── skill_resolver team_skill branch ───────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_resolver_granted_overlay(monkeypatch):
    rec = _TeamSkillRec(rid="oskill_1", visibility="creator_only", created_by="u_alice")
    loaded = []
    monkeypatch.setattr(
        "bowhead_skill.skill_resolver._safe_load",
        lambda path: loaded.append(path) or "SPEC",
    )
    common = {"catalog": _Catalog(), "team_id": "team_a", "team_skill_store": _TeamSkillStore(rec)}

    # No set + not creator → None (current behavior).
    assert await resolve_skill("s", user_id="u_bob", **common) is None
    # Creator always passes.
    assert await resolve_skill("s", user_id="u_alice", **common) == "SPEC"
    # Set match → sharing recipient resolves successfully.
    assert (
        await resolve_skill(
            "s",
            user_id="u_bob",
            granted_team_skill_ids=frozenset({"oskill_1"}),
            **common,
        )
        == "SPEC"
    )
    # No set match → None.
    assert (
        await resolve_skill(
            "s",
            user_id="u_bob",
            granted_team_skill_ids=frozenset({"oskill_x"}),
            **common,
        )
        is None
    )
