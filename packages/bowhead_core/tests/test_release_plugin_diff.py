"""Release comparison lists Plugin selection changes (Capability Center tech design §13)."""

from __future__ import annotations

from bowhead_core.agent_release_read_store import _plugin_changes, _plugin_selection


def test_plugin_changes_cover_team_and_builtin_plugins():
    base = {"capability_ids": ["tcap_a"], "builtin_plugin_ids": ["builtin:designs"]}
    after = {"capability_ids": ["tcap_b"], "builtin_plugin_ids": ["builtin:designs", "builtin:data-analysis"]}
    assert _plugin_changes(base, after) == {"added": ["builtin:data-analysis", "tcap_b"], "removed": ["tcap_a"]}
    assert _plugin_selection({}) == []
    assert _plugin_changes({}, {}) == {"added": [], "removed": []}
