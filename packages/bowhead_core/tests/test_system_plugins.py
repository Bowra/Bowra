"""Built-in Plugin registry: manifest loading, validation and resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.system_plugins import builtin_plugin, builtin_plugins, load_registry, resolve_system_plugins
from pydantic import ValidationError

# The Tools every settings Plugin compiled to before the registry moved into a manifest.
CONFIGURATION_RESOURCES = [
    "configuration_read",
    "configuration_open",
    "configuration_propose",
    "configuration_apply",
    "configuration_result",
    "configuration_submit",
    "skill_read",
]

_REVISION = "sha256:" + "a" * 64


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "builtin.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _entry(name: str = "demo", version: int = 1, **overrides: str) -> str:
    fields = {
        "family": "settings",
        "selectable": "false",
        "tools": "[]",
        "parts": "[configuration_read]",
        "skill_revision": _REVISION,
        **overrides,
    }
    lines = [f"  - name: {name}", f"    version: {version}"] + [f"    {key}: {value}" for key, value in fields.items()]
    return "\n".join(lines)


def _general(name: str = "demo", listing: str | None = "full", extra: str = "", selectable: str = "true") -> str:
    lines = [f"  - name: {name}", "    version: 1", "    family: general", f"    selectable: {selectable}", "    tools: [web_search]"]
    copy = 'name: "N", description: "D", use_when: "U", example: "E"' if listing == "full" else 'name: "N", description: "D"'
    if listing:
        lines.append("    listing:")
        lines += [f"      {locale}: {{ {copy} }}" for locale in ("zh-CN", "en-US", "de-DE", "fr-FR")]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def test_settings_plugins_keep_their_compiled_resources_and_skill_grant():
    plugin = builtin_plugin("builtin:settings-space-metadata@3")
    tools, grants = resolve_system_plugins([plugin.ref], [], [])
    assert tools == CONFIGURATION_RESOURCES
    assert grants is not None and len(grants) == 1
    assert grants[0]["skill_ref"]["id"] == "builtin:settings-space-metadata"
    assert grants[0]["revision"] == plugin.skill_revision


def test_every_registered_version_resolves_and_the_configuration_agent_refs_exist():
    refs = [plugin.ref for plugin in builtin_plugins() if plugin.family == "settings"]
    assert len(refs) == len(set(refs)) == 19
    for ref in refs:
        tools, _ = resolve_system_plugins([ref], [], [])
        assert tools == CONFIGURATION_RESOURCES
    manifest = AgentRegistry.bootstrap().get("configuration-agent")
    for ref in manifest.plugins:
        assert builtin_plugin(ref).family == "settings"


def test_resolution_keeps_manifest_tools_first_and_deduplicates():
    tools, _ = resolve_system_plugins(["builtin:settings-language-preferences@1"], ["skill_read", "bi_query"], [])
    assert tools[:2] == ["skill_read", "bi_query"]
    assert tools.count("skill_read") == 1


def test_unknown_version_fails_closed():
    with pytest.raises(ValueError, match="unknown system Plugin"):
        builtin_plugin("builtin:settings-space-preferences@99")


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (f"schema_version: 1\nplugins:\n{_entry()}\n{_entry()}\n", ValueError),
        (f"schema_version: 1\nplugins:\n{_entry(tools='[configuration_read]')}\n", ValueError),
        (f"schema_version: 1\nplugins:\n{_entry(skill_revision='sha256:short')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_entry(family='other')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_entry(parts='[bi_query]')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_general()}\n{_general(name='other')}\n", ValueError),
        (f"schema_version: 1\nplugins:\n{_general(listing=None)}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_general(listing='bare')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_general(selectable='false')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_general(extra='    parts: [skill_read]')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_general(extra='    prompt_modules: [no-version]')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_general(extra='    availability: [weather]')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_entry(parts='[skill_read, skill_read]')}\n", ValidationError),
        (f"schema_version: 1\nplugins:\n{_entry(extra='1')}\n", ValidationError),
        (f"schema_version: 2\nplugins:\n{_entry()}\n", ValidationError),
    ],
    ids=[
        "duplicate-version",
        "tool-and-part",
        "bad-revision",
        "unknown-family",
        "tool-as-part",
        "tool-in-two-plugins",
        "general-without-listing",
        "general-without-use-when",
        "general-not-selectable",
        "general-with-parts",
        "bad-module-ref",
        "unknown-availability",
        "duplicate-part",
        "extra-field",
        "schema-version",
    ],
)
def test_invalid_registries_fail_to_load(tmp_path, body, error):
    with pytest.raises(error):
        load_registry(_write(tmp_path, body))


def test_valid_registry_loads(tmp_path):
    loaded = load_registry(_write(tmp_path, f"schema_version: 1\nplugins:\n{_entry()}\n{_entry(version=2)}\n"))
    assert sorted(loaded) == ["builtin:demo@1", "builtin:demo@2"]


def test_general_plugins_cover_selectable_visible_tools():
    from bowhead_core.agents._generated.tool_kinds import USER_VISIBLE_TOOLS
    from bowhead_core.system_plugins import latest_builtin_plugin

    general = [latest_builtin_plugin(name) for name in dict.fromkeys(plugin.name for plugin in builtin_plugins() if plugin.family == "general")]
    assert [plugin.name for plugin in general] == [
        "data-analysis",
        "reports-canvas",
        "designs",
        "knowledge-base",
        "knowledge-management",
        "web-research",
        "code-computation",
        "image-generation",
        "google-delivery",
        "email",
        "document-authoring",
    ]
    tools = [tool for plugin in general for tool in plugin.tools]
    assert len(tools) == len(set(tools))
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP

    # Requester identity belongs to Lead, outside selectable custom-Agent Plugins.
    assert _PARADIGM_V2_ROLE_MAP["get_current_user"] == frozenset({"lead"})
    assert set(tools) == USER_VISIBLE_TOOLS - {"skill_save_generated", "get_current_user"}
    assert "tool_switch_context" not in tools
    # Every general Plugin is selectable: choosing Email or Google delivery is the Owner's grant.
    assert all(plugin.selectable for plugin in general)
    assert all(item.use_when and item.example for plugin in general for item in (plugin.listing or {}).values())
    assert not any(plugin.selectable for plugin in builtin_plugins() if plugin.family == "settings")


def test_reports_canvas_adds_native_file_presentation_in_a_new_retained_version():
    original = builtin_plugin("builtin:reports-canvas@1")
    current = builtin_plugin("builtin:reports-canvas@2")
    assert "present_artifacts" not in original.tools
    assert current.tools == (*original.tools[:2], "present_artifacts", *original.tools[2:])


def test_general_plugin_prompt_modules_exist():
    from bowhead_core.agent_artifact_capability import default_artifact_registry

    registry = default_artifact_registry()
    assert registry is not None
    refs = {ref for plugin in builtin_plugins() for ref in plugin.prompt_modules}
    assert refs == {"data-query-datarc-protocol@1", "canvas-report-protocol@1", "sandbox-io-protocol@1"}
    for ref in refs:
        module_id, _, version = ref.rpartition("@")
        registry.get_prompt_module(module_id, int(version))


def test_general_plugin_resolves_to_its_tools_without_a_skill_grant():
    tools, grants = resolve_system_plugins(["builtin:data-analysis@1"], ["web_search"], [])
    assert tools == ["web_search", "bi_schema", "bi_query", "bi_concept_resolve", "tool_list_workspaces"]
    assert grants == []


# ─── Built-in Plugin selection for team Agents (tech design §6) ─────────


def test_builtin_selection_expands_latest_version_and_attaches_guidance():
    from bowhead_core.system_plugins import expand_builtin_plugins

    expanded = expand_builtin_plugins({"tool_refs": ["web_search"], "builtin_plugin_ids": ["builtin:data-analysis", "builtin:designs"]})
    assert expanded["tool_refs"] == sorted(
        {
            "web_search",
            "bi_schema",
            "bi_query",
            "bi_concept_resolve",
            "tool_list_workspaces",
            "canvas_create_design",
            "canvas_update_design_text",
            "canvas_flatten_design",
        }
    )
    assert expanded["resolved_builtin_plugins"] == [{"name": "data-analysis", "version": 1}, {"name": "designs", "version": 1}]
    assert expanded["capability_protocol_block"] is True
    designs = expand_builtin_plugins({"tool_refs": [], "builtin_plugin_ids": ["builtin:designs"]})
    assert "capability_protocol_block" not in designs
    assert expand_builtin_plugins({"tool_refs": ["web_search"]}) == {"tool_refs": ["web_search"]}


def test_code_computation_requests_the_python_skill_as_selecting_the_tool_does():
    from bowhead_core.system_plugins import expand_builtin_plugins

    source = {"tool_refs": [], "skill_grants": [], "builtin_plugin_ids": ["builtin:code-computation"]}
    expanded = expand_builtin_plugins(source)
    [grant] = expanded["skill_grants"]
    assert grant["skill_ref"] == {"kind": "builtin", "id": "builtin:python_sandbox", "name": "python_sandbox"}
    assert set(grant["permissions"]) == {"invoke", "read"} and grant["revision"].startswith("sha256:")
    # Derived on expansion only: the stored selection (and so its hash) is untouched.
    assert source["skill_grants"] == []
    # A read-only grant the Owner saved gains invoke; an existing invoke grant is kept as it is.
    read_only = {**grant, "permissions": ["read"]}
    assert expand_builtin_plugins({**source, "skill_grants": [read_only]})["skill_grants"] == [grant]
    assert expand_builtin_plugins({**source, "skill_grants": [grant]})["skill_grants"] == [grant]
    # Only a Plugin that brings the sandbox implies the grant; a Tool selected directly keeps its own rule.
    direct = expand_builtin_plugins({"tool_refs": ["python_sandbox"], "skill_grants": [], "builtin_plugin_ids": ["builtin:designs"]})
    assert direct["skill_grants"] == []


@pytest.mark.anyio
async def test_run_admission_resolves_the_python_skill_for_a_revision_saved_without_it():
    import json

    from bowhead_core.agent_execution_binding import _resolved_team_definition_json

    # A revision saved before the grant was derived: run admission resolves it, the revision stays as stored.
    stored = json.dumps({"tool_refs": [], "skill_grants": [], "builtin_plugin_ids": ["builtin:code-computation"]})
    resolved = await _resolved_team_definition_json(None, team_id="team_1", snapshot={"definition_json": stored})  # type: ignore[arg-type]
    assert resolved is not None
    [grant] = json.loads(resolved)["skill_grants"]
    assert grant["skill_ref"]["id"] == "builtin:python_sandbox" and "invoke" in grant["permissions"]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("builtin:designs", "builtin_plugin_ids_invalid"),
        (["designs"], "builtin_plugin_ids_invalid"),
        (["builtin:designs@1"], "builtin_plugin_ids_invalid"),
        (["builtin:designs", "builtin:designs"], "builtin_plugin_ids_invalid"),
        (["builtin:nope"], "builtin_plugin_unknown"),
        (["builtin:settings-space-preferences"], "builtin_plugin_not_selectable"),
    ],
)
def test_invalid_builtin_selection_is_refused(value, code):
    from bowhead_core.system_plugins import BuiltinPluginSelectionError, selected_builtin_plugin_ids

    with pytest.raises(BuiltinPluginSelectionError, match=code):
        selected_builtin_plugin_ids({"builtin_plugin_ids": value})


def test_run_admission_skips_plugins_that_stopped_being_selectable():
    from bowhead_core.system_plugins import expand_builtin_plugins

    expanded = expand_builtin_plugins(
        {"tool_refs": [], "builtin_plugin_ids": ["builtin:settings-space-preferences", "builtin:designs"]}, strict=False
    )
    assert expanded["resolved_builtin_plugins"] == [{"name": "designs", "version": 1}]


def test_email_plugin_is_selectable_and_brings_send_email():
    from bowhead_core.system_plugins import expand_builtin_plugins

    expanded = expand_builtin_plugins({"tool_refs": [], "builtin_plugin_ids": ["builtin:email"]}, strict=True)
    assert expanded["resolved_builtin_plugins"] == [{"name": "email", "version": 1}]
    assert "send_email" in expanded["tool_refs"]
