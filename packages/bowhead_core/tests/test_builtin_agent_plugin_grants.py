"""A built-in Agent holds a Plugin's Tools only by recorded decision.

Built-in Agents list their Tools by hand (``tools.allow``) and are deliberately
narrower than the built-in Plugins whose Tools they hold, while Team Agents
built from a Plugin float to its latest version. When a newer Plugin version
adds a Tool, nothing else asks whether the built-in Agents holding the rest of
that Plugin should get it. This test fails until the Tool is granted in the
manifest or listed below with the reason
(docs/sprints/builtin-agent-tool-drift-2026-11-23/spike.md, G2).
"""

from __future__ import annotations

from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.product_identity import product_name
from bowhead_core.system_plugins import builtin_plugins, latest_builtin_plugin

# The sandbox Tool of the code-computation Plugin runs through these runtime entries.
_SANDBOX_ENTRIES = frozenset({"python_exec", "skill_invoke"})

_ANALYST_ROLE = "bound to the analyst role; Lead delegates this work"
_WORKSPACE = "switching the Datarc workspace is left to Lead and compute; specialists work in the selected workspace"
_CANVAS_BLOCKS = (
    "built-in Agents write report bodies with write_report and charts with add_chart; these Canvas section Tools reach Team Agents through the Plugin"
)
_CHART_EDITS = "Lead sends in-place chart edits and folding of replaced charts to compute"
_PRESENT = "presenting retained files is Lead's step"
_IMAGES = "raster images come from compute and marketing-analyst"
_NO_REPORT = "the Consultant returns a deck, not a report body"
_CANVAS_OMISSIONS = dict.fromkeys(
    ("canvas_add_external_iframe", "canvas_add_html_artifact", "canvas_add_insight", "canvas_add_spec_block"), _CANVAS_BLOCKS
)
_SPECIALIST_OMISSIONS = {
    "tool_list_workspaces": _WORKSPACE,
    "add_image": _IMAGES,
    "edit_chart": _CHART_EDITS,
    "remove_chart": _CHART_EDITS,
    "present_artifacts": _PRESENT,
    **_CANVAS_OMISSIONS,
}

OMITTED_PLUGIN_TOOLS: dict[str, dict[str, str]] = {
    "business-analyst": _SPECIALIST_OMISSIONS,
    "financial-analyst": _SPECIALIST_OMISSIONS,
    "compliance-analyst": _SPECIALIST_OMISSIONS,
    "kb-analyst": {
        **_SPECIALIST_OMISSIONS,
        "attachment_read": "not granted to built-in helpers yet; on-demand Office reading for kb-analyst is an open decision",
    },
    "consultant": {**_SPECIALIST_OMISSIONS, "read_report": _NO_REPORT, "write_report": _NO_REPORT, "remove_report_section": _NO_REPORT},
    "marketing-analyst": {
        **{tool: reason for tool, reason in _SPECIALIST_OMISSIONS.items() if tool != "add_image"},
        "web_fetch": "it reads pages with web_extract (tavily-tools-2026-09-24 §3.3)",
    },
    "compute": {
        **_CANVAS_OMISSIONS,
        "present_artifacts": _PRESENT,
        "web_deep_research": "deep research is granted to marketing-analyst only (tavily-tools-2026-09-24 §3.3)",
    },
    "lead": {
        **dict.fromkeys(
            (
                "add_chart",
                "add_image",
                "attachment_read",
                "edit_chart",
                "read_chart",
                "remove_chart",
                "web_crawl",
                "web_deep_research",
                "web_map",
                "write_report",
                "remove_report_section",
                *_CANVAS_OMISSIONS,
            ),
            _ANALYST_ROLE,
        ),
    },
}
if product_name() == "Bowra":
    # The Community consultant is a reviewed overlay composition with its own, narrower Tool set.
    OMITTED_PLUGIN_TOOLS.pop("consultant")


def _held(tool: str, allow: frozenset[str]) -> bool:
    return tool in allow or (tool == "python_sandbox" and bool(_SANDBOX_ENTRIES & allow))


def _omitted_plugin_tools(allow: frozenset[str]) -> set[str]:
    """Tools of the latest general Plugins this Agent holds partly, which it does not hold."""
    omitted: set[str] = set()
    for name in sorted({plugin.name for plugin in builtin_plugins() if plugin.family == "general"}):
        plugin = latest_builtin_plugin(name)
        assert plugin is not None
        held = {tool for tool in plugin.tools if _held(tool, allow)}
        if held:
            omitted |= set(plugin.tools) - held
    return omitted


def test_every_omitted_plugin_tool_is_a_recorded_decision() -> None:
    registry = AgentRegistry.bootstrap()
    names = set(registry.list_all())
    assert {"compute", "business-analyst", "lead"} <= names, "positive control: the system registry did not load"
    for name in sorted(names):
        if product_name() == "Bowra" and name == "consultant":
            continue
        actual = _omitted_plugin_tools(frozenset(registry.get(name).tools.allow))
        recorded = set(OMITTED_PLUGIN_TOOLS.get(name, {}))
        assert not actual - recorded, (
            f"{name} holds part of a built-in Plugin but not {sorted(actual - recorded)}. "
            "Grant the Tool in its manifest, or record the omission and its reason in OMITTED_PLUGIN_TOOLS."
        )
        assert not recorded - actual, (
            f"{name}: {sorted(recorded - actual)} are granted or no longer in a Plugin it holds; remove them from OMITTED_PLUGIN_TOOLS"
        )


def test_an_unrecorded_omission_is_reported() -> None:
    """Control: a Plugin Tool the Agent does not hold and nobody recorded shows up as unrecorded."""
    allow = frozenset(AgentRegistry.bootstrap().get("business-analyst").tools.allow)
    recorded = set(OMITTED_PLUGIN_TOOLS["business-analyst"])
    assert _omitted_plugin_tools(allow) == recorded
    assert _omitted_plugin_tools(allow - {"read_chart"}) - recorded == {"read_chart"}
    # The sandbox Tool counts as held through either runtime entry.
    assert "python_sandbox" not in _omitted_plugin_tools(allow - {"python_exec"})
