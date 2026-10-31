"""Exact read-only Plugin methods across admission, compilation and per-turn removal."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler, AgentDependencyContext, parse_exact_skill_grants
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.resource_disable import DisableState, team_plugin_provenance
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.subagent import SubagentProfile, _apply_resource_disable
from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogSnapshot, native_authoring_tools
from bowhead_core.system_plugins import BuiltinPlugin, BuiltinPluginSelectionError, expand_builtin_plugins, resolve_system_plugins

from bowhead_core import agent_dependency_compiler, system_plugins
from bowhead_skill import SkillCatalog


@pytest.mark.anyio
async def test_document_plugin_compiles_its_actual_readonly_package():
    from bowhead_core.system_plugins import builtin_plugin

    plugin = builtin_plugin("builtin:document-authoring@1")
    authored = {"tool_refs": [], "skill_grants": [], "builtin_plugin_ids": ["builtin:document-authoring"]}
    resolved = expand_builtin_plugins(authored)
    context = AgentDependencyContext(agent_id="osa_document", team_id="team_document", owner_user_id="usr_document", audience_mode="private")
    catalog = SubagentAuthoringCatalogSnapshot(
        team_id=context.team_id,
        actor_user_id=context.owner_user_id,
        native_tools=native_authoring_tools(),
        team_tools=(),
        fingerprint="sha256:" + "1" * 64,
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )
    compiled = await AgentDependencyCompiler().compile(resolved, context=context, tool_catalog=catalog)
    assert compiled.publishable, compiled.reason_codes
    assert compiled.skill_scope.read_names == ("pptx-authoring",)
    assert compiled.skill_scope.invoke_names == ()
    assert compiled.skills[0].grant == plugin.skill_grants[0]
    tool_refs = {item.stable_ref for item in compiled.tools}
    assert {"template_document_submit", "skill_read"} <= tool_refs
    assert "skill_invoke" not in tool_refs
    assert authored["tool_refs"] == [] and authored["skill_grants"] == []


@pytest.fixture
def method_plugin(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    source = root / "plugin-method"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: plugin-method\ndescription: Synthetic method\nkind: knowledge\n---\n\nRead the supplied evidence.\n")
    grant = {
        "skill_ref": {"kind": "builtin", "id": "builtin:plugin-method", "name": "plugin-method"},
        "permissions": ["read"],
        "revision": "sha256:" + bundle_sha256(source),
        "distribution": "reference",
    }
    registry = dict(system_plugins._registry())
    raw = registry["builtin:web-research@1"].model_dump()
    raw.update(version=2, skill_grants=[grant])
    plugin = BuiltinPlugin.model_validate(raw)
    registry[plugin.ref] = plugin
    monkeypatch.setattr(system_plugins, "_registry", lambda: registry)
    monkeypatch.setattr(agent_dependency_compiler, "builtin_catalog_dir", lambda: root)
    return SimpleNamespace(source=source, grant=grant, plugin=plugin, compiler=AgentDependencyCompiler(builtin_skills=SkillCatalog(roots=[root])))


def test_general_method_shape_round_trips_and_system_resolution_derives_only_read(method_plugin):
    plugin = method_plugin.plugin
    assert BuiltinPlugin.model_validate(plugin.model_dump()) == plugin
    tools, grants = resolve_system_plugins([plugin.ref], [], [])
    assert "skill_read" in tools and "skill_invoke" not in tools
    assert grants == [method_plugin.grant]
    # A Tool reference alone has no Plugin method authority.
    assert resolve_system_plugins([], list(plugin.tools), [])[1] == []


@pytest.mark.parametrize("mutation", ["invoke", "team", "embedded", "identity", "revision", "duplicate", "unknown_field"])
def test_general_plugin_rejects_wider_or_ambiguous_skill_authority(method_plugin, mutation):
    raw = method_plugin.plugin.model_dump()
    grant = raw["skill_grants"][0]
    if mutation == "invoke":
        grant["permissions"].append("invoke")
    elif mutation == "team":
        grant["skill_ref"]["kind"] = "team"
    elif mutation == "embedded":
        grant["distribution"] = "embedded_agent_scope"
    elif mutation == "identity":
        grant["skill_ref"]["id"] = "builtin:another-method"
    elif mutation == "revision":
        grant["revision"] = "latest"
    elif mutation == "duplicate":
        raw["skill_grants"].append(copy.deepcopy(grant))
    else:
        grant["scripts"] = ["run.py"]
    with pytest.raises(ValueError):
        BuiltinPlugin.model_validate(raw)


def test_custom_expansion_keeps_authored_content_and_independent_grant_authority(method_plugin):
    source = {"tool_refs": [], "skill_grants": [], "builtin_plugin_ids": ["builtin:web-research"]}
    original = copy.deepcopy(source)
    expanded = expand_builtin_plugins(source)
    assert source == original
    assert expanded["skill_grants"] == [method_plugin.grant]
    assert expanded["resolved_builtin_plugins"] == [{"name": "web-research", "version": 2}]
    direct = {**source, "skill_grants": [{**method_plugin.grant, "permissions": ["read", "invoke"]}]}
    assert parse_exact_skill_grants(expand_builtin_plugins(direct)["skill_grants"]) == parse_exact_skill_grants(direct["skill_grants"])
    conflict = {**source, "skill_grants": [{**method_plugin.grant, "revision": "sha256:" + "f" * 64}]}
    with pytest.raises(BuiltinPluginSelectionError, match="builtin_plugin_skill_conflict") as failure:
        expand_builtin_plugins(conflict)
    assert failure.value.blocked_items[0]["name"] == "plugin-method"


@pytest.mark.anyio
async def test_compiler_keeps_the_declared_method_pin_and_refuses_changed_bytes(method_plugin):
    definition = expand_builtin_plugins({"tool_refs": [], "skill_grants": [], "builtin_plugin_ids": ["builtin:web-research"]})
    context = AgentDependencyContext(agent_id="osa_method", team_id="team_method", owner_user_id="usr_owner", audience_mode="private")
    catalog = SubagentAuthoringCatalogSnapshot(
        team_id=context.team_id,
        actor_user_id=context.owner_user_id,
        native_tools=native_authoring_tools(),
        team_tools=(),
        fingerprint="sha256:" + "1" * 64,
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )
    compiled = await method_plugin.compiler.compile(definition, context=context, tool_catalog=catalog)
    assert compiled.publishable, compiled.reason_codes
    assert compiled.skill_scope.read_names == ("plugin-method",)
    assert compiled.skill_scope.invoke_names == ()
    assert compiled.skills[0].grant.to_canonical_dict() == method_plugin.grant
    (method_plugin.source / "SKILL.md").write_text((method_plugin.source / "SKILL.md").read_text() + "\nChanged method.\n")
    changed = await method_plugin.compiler.compile(definition, context=context, tool_catalog=catalog)
    assert not changed.publishable
    assert "builtin_plugin_skill_revision_mismatch" in changed.reason_codes
    assert changed.skills[0].grant.revision == method_plugin.grant["revision"]
    # D2 still permits an independently selected Skill to follow its approved package.
    independent = await method_plugin.compiler.compile(
        {"tool_refs": [], "skill_grants": [method_plugin.grant]}, context=context, tool_catalog=catalog
    )
    assert independent.publishable
    assert independent.skills[0].grant.revision != method_plugin.grant["revision"]


@pytest.mark.anyio
async def test_exact_run_admission_derives_methods_without_rewriting_the_revision(method_plugin):
    from bowhead_core.agent_execution_binding import _resolved_team_definition_json

    original = json.dumps({"tool_refs": [], "skill_grants": [], "builtin_plugin_ids": ["builtin:web-research"]})
    snapshot = {"definition_json": original}
    resolved = await _resolved_team_definition_json(None, team_id="team_method", snapshot=snapshot)  # type: ignore[arg-type]
    assert json.loads(resolved)["skill_grants"] == [method_plugin.grant]
    assert snapshot["definition_json"] == original


@pytest.mark.parametrize("disabled_by", ["team", "user"])
def test_system_profile_removes_plugin_methods_but_preserves_direct_reads(method_plugin, disabled_by):
    manifest = AgentRegistry.bootstrap().get("lead").model_copy(update={"plugins": [method_plugin.plugin.ref], "skill_grants": []})
    profile = manifest_to_profile(manifest, prompt_loader=lambda _: "Synthetic profile")
    state = DisableState(**{f"{disabled_by}_builtin_plugins": frozenset({"web-research"})})
    stripped = _apply_resource_disable(profile, state)
    assert stripped.skill_runtime_grants == ()
    assert stripped.skill_runtime_scope.read_names == ()
    assert "web_search" not in stripped.tool_whitelist
    direct = manifest.model_copy(update={"skill_grants": [method_plugin.grant]})
    kept = _apply_resource_disable(manifest_to_profile(direct, prompt_loader=lambda _: "Synthetic profile"), state)
    assert kept.skill_runtime_grants == parse_exact_skill_grants([method_plugin.grant])


@pytest.mark.anyio
@pytest.mark.parametrize("direct", [False, True])
async def test_custom_profile_provenance_removes_only_the_disabled_method_source(method_plugin, tmp_path, direct):
    authored = {
        "tool_refs": ["web_search"] if direct else [],
        "skill_grants": [method_plugin.grant] if direct else [],
        "builtin_plugin_ids": ["builtin:web-research"],
    }
    resolved = expand_builtin_plugins(authored)
    provenance = await team_plugin_provenance(str(tmp_path / "provenance.db"), authored, resolved)
    assert provenance is not None
    assert provenance.plugins[0].skills == ("builtin:plugin-method",)
    profile = SubagentProfile(
        name="synthetic",
        llm_agent_type="analyst",
        system_prompt="",
        tool_whitelist=("web_search", "skill_read"),
        team_tool_bundle=(),
        skill_runtime_grants=parse_exact_skill_grants(resolved["skill_grants"]),
        resource_provenance=provenance,
    )
    result = _apply_resource_disable(profile, DisableState(user_builtin_plugins=frozenset({"web-research"})))
    assert bool(result.skill_runtime_grants) is direct
    assert ("web_search" in result.tool_whitelist) is direct
    assert result.skill_runtime_scope.invoke_names == ()


def test_impact_includes_an_explicit_plugin_without_direct_tool_membership(method_plugin, monkeypatch):
    from bowhead_core import resource_disable_impact

    monkeypatch.setattr(
        resource_disable_impact,
        "_system_agents",
        lambda: (resource_disable_impact._SystemAgent("explicit", frozenset(), frozenset({"web-research"})),),
    )
    assert resource_disable_impact._affected_system_agents("builtin_plugin", "web-research") == ["explicit"]


@pytest.mark.anyio
async def test_publication_validator_compiles_the_plugin_method_and_blocks_a_stale_package(method_plugin):
    import aiosqlite
    from bowhead_core.agent_revision_store import AgentAudience, AgentPublicationBlockedError, DeterministicAgentPublicationValidator

    async def current_catalog(**kwargs):
        return SubagentAuthoringCatalogSnapshot(
            team_id=kwargs["team_id"],
            actor_user_id=kwargs["actor_user_id"],
            native_tools=native_authoring_tools(),
            team_tools=(),
            fingerprint="sha256:" + "1" * 64,
            dependency_guard=(),
            built_at=datetime.now(UTC),
        )

    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=method_plugin.compiler,
        catalog_service=SimpleNamespace(build=current_catalog),
    )
    kwargs = {
        "team_id": "team_method",
        "agent_id": "osa_method",
        "owner_user_id": "usr_owner",
        "routing_name": "Method Agent",
        "definition": {
            "system_prompt": "Read supplied evidence.",
            "tool_refs": [],
            "skill_grants": [],
            "builtin_plugin_ids": ["builtin:web-research"],
        },
        "listing": {"display_name": "Method Agent", "description": "Read supplied evidence."},
        "audience": AgentAudience("private"),
    }
    async with aiosqlite.connect(":memory:") as db:
        current = await validator.validate(db, **kwargs)
        assert current.compilation.publishable, current.compilation.reason_codes
        assert current.compilation.skill_scope.read_names == ("plugin-method",)
        assert current.compilation.skill_scope.invoke_names == ()
        conflicting = {**kwargs["definition"], "skill_grants": [{**method_plugin.grant, "revision": "sha256:" + "f" * 64}]}
        with pytest.raises(AgentPublicationBlockedError) as conflict:
            await validator.validate(db, **{**kwargs, "definition": conflicting})
        assert conflict.value.blocked_items[0]["name"] == "plugin-method"
        method_plugin.source.joinpath("SKILL.md").write_text(method_plugin.source.joinpath("SKILL.md").read_text() + "\nA changed method.\n")
        changed = await validator.validate(db, **kwargs)
        assert not changed.compilation.publishable
        assert changed.compilation.blocked_items[0]["reason_codes"] == ["builtin_plugin_skill_revision_mismatch"]


def test_catalog_reports_the_actual_method_count(method_plugin):
    from bowhead_gateway.routes.builtin_resources import _plugin_view

    view = _plugin_view(method_plugin.plugin, team_disabled=frozenset(), my_disabled=frozenset(), unconfigured=set())
    assert view["skill_count"] == 1
