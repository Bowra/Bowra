"""Plugin section of the Agent authoring catalog (capability-center tech design §6)."""

from __future__ import annotations

from bowhead_core.authoring_plugin_catalog import builtin_plugin_entries, team_plugin_entries


def _builtin(**kwargs):
    defaults = {"unconfigured": frozenset(), "team_disabled": frozenset(), "my_disabled": frozenset()}
    return {item["name"]: item for item in builtin_plugin_entries(**{**defaults, **kwargs})}


def test_builtin_plugins_report_why_they_cannot_be_selected():
    entries = _builtin(team_disabled=frozenset({"designs"}), my_disabled=frozenset({"knowledge-base"}))
    assert entries["designs"]["locked_reason"] == "disabled_by_team"
    assert entries["knowledge-base"]["locked_reason"] == "disabled_by_me"
    assert entries["email"]["selectable"] is True and entries["google-delivery"]["selectable"] is True
    assert entries["settings-space-metadata"]["locked_reason"] == "settings_assistant_only"
    assert entries["web-research"]["selectable"] is True


def test_readiness_needs_at_least_one_available_tool():
    entries = _builtin(unconfigured=frozenset({"image_generate", "web_search", "web_crawl"}))
    assert entries["image-generation"]["ready"] is False
    assert entries["image-generation"]["locked_reason"] == "not_ready"
    # Web research still works with the remaining Tools, so it stays ready and lists what is missing.
    assert entries["web-research"]["ready"] is True
    assert entries["web-research"]["unavailable_tools"] == ["web_crawl", "web_search"]


def test_team_plugins_follow_status_and_personal_state():
    plugins = [
        {"id": "tcap_a", "name": "A", "description": "", "status": "active", "revision": 3, "tool_refs": ["web_map"], "skill_grants": [{}]},
        {"id": "tcap_b", "name": "B", "description": "", "status": "disabled", "revision": 1, "tool_refs": ["web_map"], "skill_grants": []},
    ]
    entries = {item["id"]: item for item in team_plugin_entries(plugins, unconfigured=frozenset(), my_disabled=frozenset({"tcap_a"}))}
    assert entries["tcap_a"]["version"] == 3 and entries["tcap_a"]["skill_count"] == 1
    assert entries["tcap_a"]["locked_reason"] == "disabled_by_me"
    assert entries["tcap_b"]["locked_reason"] == "disabled_by_team"
