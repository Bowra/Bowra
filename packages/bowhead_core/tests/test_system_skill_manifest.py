"""Real system manifest loading preserves exact Skill access through dispatch."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
import respx
import yaml
from bowhead_core.agents import AgentManifest, AgentRegistry, ManifestSecurityError, ManifestValidationError
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile, register_all_system_manifests
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import AgentResourceRef, ReferenceNode
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import PROFILES, _materialize_system_profile_snapshot, _system_profile_snapshot
from bowhead_core.tool_registry import ToolExecutionContext
from cryptography.fernet import Fernet
from test_reference_start_preparation import activate, raw_document
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case
from test_skill_reference_invocation import tools_for
from test_skill_reference_runtime import rows
from test_system_reference_baseline import prepare
from test_system_skill_snapshot import profile

receiver_case, start_case = _receiver_case, _start_case
pytestmark = pytest.mark.integration


def manifest_data(grants):
    return {
        "name": "compute",
        "model": "analyst",
        "trust_class": "read-mostly",
        "max_iterations": 10,
        "max_output_tokens": 1000,
        "description": "System financial methods",
        "system": None,
        "tools": {"default_enabled": False, "allow": ["skill_read", "skill_invoke", "bi_query"]},
        "skill_grants": grants,
    }


def load_registry(tmp_path, monkeypatch, grants, *, register=True):
    system_dir = tmp_path / "system_manifests"
    system_dir.mkdir()
    (system_dir / "compute.yaml").write_text(yaml.safe_dump(manifest_data(grants)))
    registry = AgentRegistry.bootstrap(system_dir)
    if register:
        monkeypatch.setitem(PROFILES, "compute", PROFILES["compute"])
        assert register_all_system_manifests(registry) == 1
    return registry


def load_workspace(registry, tmp_path, workspace_id, *, grant_override=False):
    root = tmp_path / "user_manifests"
    directory = root / workspace_id
    directory.mkdir(parents=True)
    data = manifest_data(None)
    data.pop("trust_class")
    if not grant_override:
        data.pop("skill_grants")
    data.update(description="Workspace wording", max_iterations=3)
    data["tools"]["allow"].append("skill_list")
    (directory / "compute.yaml").write_text(yaml.safe_dump(data))
    registry.load_user_workspace(workspace_id, plan_tier="enterprise", user_dir=root, prompt_dir=tmp_path / "prompts")


@pytest.mark.parametrize("kind", ["legacy", "empty", "exact"])
def test_system_manifest_and_workspace_override_retain_exact_permissions(tmp_path, monkeypatch, kind):
    grants = None if kind == "legacy" else ([] if kind == "empty" else _system_profile_snapshot(profile())["skill_grants"])
    registry = load_registry(tmp_path, monkeypatch, grants)
    load_workspace(registry, tmp_path, "ws_exact")
    original = manifest_to_profile(registry.get("compute"))
    assert (None if original.skill_runtime_grants is None else [grant.to_canonical_dict() for grant in original.skill_runtime_grants]) == grants
    effective = manifest_to_profile(registry.get("compute", "ws_exact"))
    assert effective.max_iterations == 3 and original.max_iterations == 10
    assert effective.skill_runtime_scope == original.skill_runtime_scope
    assert effective.skill_runtime_grants == original.skill_runtime_grants
    snapshot = _system_profile_snapshot(effective)
    restored = _materialize_system_profile_snapshot(snapshot, expected_name="compute")
    assert restored.skill_runtime_grants == original.skill_runtime_grants
    if kind != "legacy":
        assert restored.skill_runtime_scope is not None
        assert restored.skill_runtime_scope.error_for_call(tool_name="skill_invoke", arguments={"name": "method"}) == "skill_scope_denied"
        assert restored.skill_runtime_scope.error_for_call(tool_name="skill_read", arguments={"name": "other"}) == "skill_scope_denied"
        # Returned workspace objects must not expose a mutable handle to source grants.
        overridden = registry.get("compute", "ws_exact")
        overridden.skill_grants.append(_system_profile_snapshot(profile())["skill_grants"][0])
        assert registry.get("compute").skill_grants == grants
        assert registry.get("compute", "ws_exact").skill_grants == grants


def test_workspace_cannot_clear_or_supply_system_grants(tmp_path, monkeypatch):
    registry = load_registry(tmp_path, monkeypatch, _system_profile_snapshot(profile())["skill_grants"])
    with pytest.raises(ManifestSecurityError, match="skill_grants"):
        load_workspace(registry, tmp_path, "ws_exact", grant_override=True)
    assert not registry.has_user_manifest("ws_exact", "compute")


@pytest.mark.parametrize("change", ["floating", "duplicate", "invoke_only", "non_list"])
def test_manifest_load_rejects_incomplete_exact_authority(tmp_path, monkeypatch, change):
    grants = _system_profile_snapshot(profile())["skill_grants"]
    if change == "floating":
        grants[0]["revision"] = "latest"
    elif change == "duplicate":
        grants.append(grants[0])
    elif change == "invoke_only":
        grants[0]["permissions"] = ["invoke"]
    else:
        grants = {"method": grants[0]}
    with pytest.raises(ManifestValidationError, match="exact_skill_grants_unavailable"):
        load_registry(tmp_path, monkeypatch, grants, register=False)


@pytest.mark.parametrize("kind", ["team", "user", "embedded"])
def test_global_system_manifest_cannot_bind_tenant_or_embedded_sources(tmp_path, monkeypatch, kind):
    grants = _system_profile_snapshot(profile())["skill_grants"]
    if kind == "embedded":
        grants[0]["distribution"] = "embedded_agent_scope"
    else:
        grants[0]["skill_ref"]["kind"] = kind
    with pytest.raises(ManifestValidationError, match="system_skill_grants_require_builtin_reference"):
        load_registry(tmp_path, monkeypatch, grants, register=False)


@pytest.mark.parametrize("change", ["malformed", "tenant", "embedded"])
def test_in_memory_manifest_copy_is_revalidated_before_profile(change):
    manifest = AgentManifest.model_validate(manifest_data([]))
    grants = [{"name": "method"}] if change == "malformed" else _system_profile_snapshot(profile())["skill_grants"]
    if change == "tenant":
        grants[0]["skill_ref"]["kind"] = "team"
    elif change == "embedded":
        grants[0]["distribution"] = "embedded_agent_scope"
    unsafe = manifest.model_copy(update={"skill_grants": grants})
    with pytest.raises(ValueError, match="exact_skill_grants_unavailable|system_skill_grants_require_builtin_reference"):
        manifest_to_profile(unsafe)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_file_registry_reaches_reference_admission_and_original_model_context(receiver_case, tmp_path, monkeypatch):
    fixture = receiver_case
    activate(fixture)
    path = fixture.case.fixture.db_path
    with sqlite3.connect(path) as db:
        definition = json.loads(
            db.execute(
                "SELECT definition_json FROM team_subagent_revision WHERE id=?", (fixture.case.args["reference_start"].expected.revision_ref,)
            ).fetchone()[0]
        )
    registry = load_registry(tmp_path, monkeypatch, definition["skill_grants"])
    load_workspace(registry, tmp_path, fixture.scope.workspace_id)
    runner = fixture.case.runner
    runner._registry, runner._shadow_workspace_id = registry, fixture.scope.workspace_id
    monkeypatch.setenv("BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED", "1")
    projected = await runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=fixture.case.ctx)
    document = raw_document(fixture, skills=True)
    document = document.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={"resource": AgentResourceRef(kind="agent", source_kind="system", id="system:compute", version=projected.revision_ref)}
                )
                if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)
                else node
                for node in document.nodes
            ]
        }
    )
    admission = await prepare(fixture, document)
    assert admission.system_revision.definition["max_iterations"] == 3
    fixture.case.args.update(reference_start=admission, user_text=document.display_text())
    await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    scope = ReferenceScope(**fixture.scope.model_dump(exclude={"session_id"}), session_id="s_reference", policy_epoch=admission.policy_epoch)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    claim = await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="system-manifest-worker",
    )
    assert claim.outcome == "claimed"
    runtime = ReferencePrimaryRuntime(
        scope=scope,
        turn_id="t_reference",
        owner_id="system-manifest-worker",
        bindings=fixture.case.bindings,
        cipher=cipher,
        skill_sources=fixture.sources,
    )
    adapter = adapter_for("openai")
    runner.router = router_for(adapter)
    runner.tool_registry, _, _ = tools_for(fixture.root, monkeypatch)
    ctx = ToolExecutionContext(**scope.identity(), turn_id="t_reference", member_uid=scope.user_id, datarc=None, session_store=fixture.case.store)
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        return success("openai", "stream" if body.get("stream") else "invoke")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
            assert result.ok and result.final_answer == "OK" and route.call_count == 1
        assert "BODY_method" in json.dumps(bodies[0])
        execution = rows(SimpleNamespace(path=path), "turn_agent_execution")[0]
        assert execution["agent_source_kind"] == "system" and execution["status"] == "completed"
    finally:
        await adapter.close()
