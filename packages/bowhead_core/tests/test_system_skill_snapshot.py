"""Actual immutable system admission/restoration preserves Skill restrictions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.agent_execution_binding import AgentExecutionBindingError
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner, _materialize_system_profile_snapshot, _system_profile_snapshot
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from test_agent_execution_binding import _setup
from test_skill_operation_scope import _router

from bowhead_router import LLMResponse, ToolCall


def profile(*, empty=False, scope_only=False):
    grants = tuple(
        ExactSkillGrant(kind="builtin", id=name, name=name, permissions=permissions, revision="sha256:" + "a" * 64, distribution="reference")
        for name, permissions in (("method", ("read",)), ("runner", ("invoke", "read")))
    )
    return SubagentProfile(
        name="compute",
        llm_agent_type="analyst",
        tool_whitelist=("skill_read", "skill_invoke"),
        system_prompt="Use declared methods.",
        skill_runtime_scope=ExactSkillRuntimeScope(read_names=() if empty else ("method", "runner"), invoke_names=() if empty else ("runner",)),
        skill_runtime_grants=None if scope_only else (() if empty else grants),
    )


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["legacy", "empty", "scope", "exact"])
def test_snapshot_roundtrip_keeps_independent_operation_and_source_limits(kind):
    original = profile(empty=kind == "empty", scope_only=kind == "scope")
    if kind == "legacy":
        original = replace(original, skill_runtime_scope=None, skill_runtime_grants=None)
    snapshot = json.loads(json.dumps(_system_profile_snapshot(original)))
    assert snapshot["schema_version"] == 3
    assert snapshot["max_output_tokens"] is None
    restored = _materialize_system_profile_snapshot(snapshot, expected_name="compute")
    assert restored.skill_runtime_scope == original.skill_runtime_scope
    assert restored.skill_runtime_grants == original.skill_runtime_grants
    if kind != "legacy":
        assert restored.skill_runtime_scope.error_for_call(tool_name="skill_invoke", arguments={"name": "method"}) == "skill_scope_denied"


@pytest.mark.unit
@pytest.mark.parametrize("change", ["downgrade", "missing", "both_null", "bad_scope", "bad_name", "bad_grant", "duplicate", "invoke_without_read"])
def test_malformed_snapshot_cannot_fall_back_to_generic_access(change):
    value = _system_profile_snapshot(profile())
    if change == "downgrade":
        value["schema_version"] = 1
    elif change == "missing":
        del value["skill_runtime_scope"]
    elif change == "both_null":
        value.update(skill_runtime_scope=None, skill_grants=None)
    elif change == "bad_scope":
        value["skill_runtime_scope"]["schema_version"] = True
    elif change == "bad_name":
        value["skill_runtime_scope"]["read_names"] = [{"name": "method"}]
    elif change == "bad_grant":
        value["skill_grants"][0]["revision"] = "latest"
    elif change == "duplicate":
        value["skill_grants"].append(value["skill_grants"][0])
    else:
        value["skill_grants"][0]["permissions"] = ["invoke"]
    with pytest.raises(ValueError):
        _materialize_system_profile_snapshot(value, expected_name="compute")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_stored_system_revision_keeps_original_scope_at_native_dispatch(tmp_path, monkeypatch, empty):
    fixture = await _setup(tmp_path)
    configured = profile(empty=empty)
    monkeypatch.setitem(PROFILES, "compute", configured)
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "0")
    registry = ToolRegistry()
    seen = []

    async def handler(args, ctx):
        seen.append((args["name"], ctx.skill_runtime_scope, ctx.skill_runtime_grants))
        return {"ok": True}

    for name in ("skill_read", "skill_invoke"):
        registry.register(ToolSpec(schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}), handler=handler))
    runner = SubagentRunner(
        router=_router(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="allowed", name="skill_read", arguments={"name": "method"}),
                    ToolCall(id="denied", name="skill_invoke", arguments={"name": "method"}),
                    ToolCall(id="foreign", name="skill_read", arguments={"name": "other"}),
                ],
            ),
            LLMResponse(content="Finished."),
        ),
        tool_registry=registry,
        team_subagent_store=fixture.users,
    )
    ctx = ToolExecutionContext(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        member_uid=fixture.member_id,
        workspace_id=fixture.workspace_id,
        session_id="s_m3b",
        turn_id=fixture.turn_id,
        datarc=None,
    )
    binding = await runner.admit_execution_binding(
        profile_name="compute", parent_turn_id=fixture.turn_id, parent_ctx=ctx, dispatch_key="system-precise"
    )
    assert binding.definition["schema_version"] == 3
    # A changed live profile cannot widen the saved execution.
    monkeypatch.setitem(PROFILES, "compute", replace(configured, skill_runtime_scope=None, skill_runtime_grants=None))
    result = await runner.run(
        profile_name="compute",
        task="Use the original permissions.",
        parent_turn_id=fixture.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
        exact_binding=binding,
    )
    assert result.ok
    assert [name for name, _, _ in seen] == ([] if empty else ["method"])
    assert all(scope == configured.skill_runtime_scope and grants == configured.skill_runtime_grants for _, scope, grants in seen)
    assert ctx.skill_runtime_scope is None and ctx.skill_runtime_grants is None
    with sqlite3.connect(fixture.db_path) as db:
        stored = json.loads(
            db.execute("SELECT definition_json FROM system_agent_effective_revision WHERE id=?", (binding.revision_ref,)).fetchone()[0]
        )
    assert stored == binding.definition


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_migration_rejects_system_admission_before_revision_or_execution(tmp_path, monkeypatch):
    fixture = await _setup(tmp_path)
    monkeypatch.setitem(PROFILES, "compute", profile())
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("DROP TRIGGER trg_system_precise_skill_snapshot")
    runner = SubagentRunner(router=None, tool_registry=None, team_subagent_store=fixture.users)
    ctx = ToolExecutionContext(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        member_uid=fixture.member_id,
        workspace_id=fixture.workspace_id,
        turn_id=fixture.turn_id,
        datarc=None,
    )
    with pytest.raises(AgentExecutionBindingError, match="agent_system_skill_snapshot_migration_required"):
        await runner.admit_execution_binding(profile_name="compute", parent_turn_id=fixture.turn_id, parent_ctx=ctx, dispatch_key="system-precise")
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM system_agent_effective_revision").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (0,)
