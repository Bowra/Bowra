"""Both Agent sources share exact output authority without starting new work."""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore
from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride, BuiltinSubagentOverrideStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.subagent import PROFILES, SubagentRunner
from test_agent_execution_binding import _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("profile", ["compute", "kb-analyst", "configuration-agent"])
@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("boundary", ["read", "disabled", "override", "definition", "grants", "scope", "outcome", "dispatch", "legacy"])
async def test_exact_system_output_uses_current_authority(tmp_path, monkeypatch, profile, status, boundary):
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    monkeypatch.setitem(PROFILES, profile, manifest_to_profile(AgentRegistry.bootstrap().get(profile)))
    fixture = await _setup(tmp_path)
    scope = ReferenceRequestScope(team_id=fixture.team_id, user_id=fixture.member_id, workspace_id=fixture.workspace_id, session_id="s_m3b")
    runner = SubagentRunner(router=None, tool_registry=None, team_subagent_store=fixture.users)
    binding = await runner.admit_execution_binding(
        profile_name=profile,
        parent_turn_id=fixture.turn_id,
        parent_ctx=SimpleNamespace(**scope.model_dump()),
        dispatch_key="call:original",
    )
    assert binding is not None
    store = AgentExecutionBindingStore(fixture.db_path)
    await store.mark_running(binding.execution_id)
    failure_code = "peer_empty_result" if status == "failed" else None
    await store.mark_terminal(binding.execution_id, status=status, failure_code=failure_code)
    if boundary in {"disabled", "override"}:
        await BuiltinSubagentOverrideStore(str(fixture.db_path)).upsert(
            BuiltinSubagentOverride(team_id=fixture.team_id, name=profile, enabled=boundary != "disabled", prompt_append="Changed policy"),
            expected_version=None,
            actor_user_id=fixture.owner_id,
            audit_action="update",
        )
    elif boundary in {"definition", "grants"}:
        changes = {"system_prompt": "Changed definition"} if boundary == "definition" else {"skill_invoke_allowlist": ("changed-grant",)}
        monkeypatch.setitem(PROFILES, profile, replace(PROFILES[profile], **changes))
    kwargs = {
        "scope": scope.model_copy(update={"user_id": fixture.owner_id}) if boundary == "scope" else scope,
        "turn_id": fixture.turn_id,
        "execution_id": binding.execution_id,
        "dispatch_key": "call:other" if boundary == "dispatch" else "call:original",
    }
    reader = store.read_settled_helper_in_connection
    if boundary == "legacy":
        reader = store.read_completed_helper_in_connection
    else:
        kwargs.update(
            status="running" if boundary == "outcome" else status, failure_code=failure_code, system_profile_resolver=runner._resolve_system_profile
        )
    with sqlite3.connect(fixture.db_path) as db:
        original = db.execute("SELECT * FROM turn_agent_execution").fetchall()
    async with open_shared_ctx(fixture.db_path) as db:
        await db.execute("BEGIN")
        await db.execute("PRAGMA query_only=1")
        factory, changes_before = db.row_factory, db.total_changes
        if boundary == "read":
            assert await reader(db, **kwargs) == replace(binding, status=status)
        else:
            with pytest.raises(AgentExecutionBindingError):
                await reader(db, **kwargs)
        assert db.in_transaction and db.row_factory is factory and db.total_changes == changes_before
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT * FROM turn_agent_execution").fetchall() == original
        assert db.execute("SELECT COUNT(*) FROM turn_agent_binding").fetchone() == (0,)
