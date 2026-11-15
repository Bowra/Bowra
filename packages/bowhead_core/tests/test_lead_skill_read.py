"""The system Lead reads the body of a Skill its own index can list, and no other."""

import pytest
from bowhead_core.agents._lead_context import reset_system_lead_run, set_system_lead_run
from bowhead_core.skill_access import lead_can_read_skill, render_lead_skill_index
from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
from bowhead_core.system_plugins import general_plugin_skill_names
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

from bowhead_skill import SkillCatalog, builtin_catalog_dir


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(user_id="u1", member_uid="u1", workspace_id="ws_test", turn_id="t_skill", session_id="s_skill", datarc=None)


async def _read(name: str, *, lead: bool, file: str | None = None, ctx: ToolExecutionContext | None = None):
    registry = ToolRegistry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    token = set_system_lead_run(lead)
    try:
        return await registry.get("skill_read").handler({"name": name, **({"file": file} if file else {})}, ctx or _context())
    finally:
        reset_system_lead_run(token)


@pytest.mark.unit
async def test_lead_reads_an_indexed_skill_and_its_method_file():
    body = await _read("pptx", lead=True)
    assert "references/edit-existing-deck.md" in body["body"]
    method = await _read("pptx", lead=True, file="references/edit-existing-deck.md")
    assert "Each sandbox call gets a new working directory" in method["body"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["python_sandbox", "pptx-authoring", "docx-authoring"])
async def test_lead_is_refused_a_skill_outside_its_index(name):
    refused = await _read(name, lead=True)
    assert refused["ok"] is False and refused["reason"] == "not_granted" and refused["skill"] == name
    assert "subagent_spawn" in refused["error"]
    # The Agent that does the work still reads it.
    assert "body" in await _read(name, lead=False)


@pytest.mark.unit
async def test_a_body_cached_by_a_peer_is_not_returned_to_the_lead():
    ctx = _context()
    assert "body" in await _read("python_sandbox", lead=False, ctx=ctx)
    assert (await _read("python_sandbox", lead=True, ctx=ctx))["ok"] is False


@pytest.mark.unit
def test_lead_index_lists_exactly_the_skills_it_can_read():
    catalog = SkillCatalog(roots=[builtin_catalog_dir()])
    catalog.refresh()
    index = render_lead_skill_index(catalog)
    assert {"pptx-authoring", "docx-authoring"} <= general_plugin_skill_names()
    for spec in catalog.list_specs():
        assert (f"**{spec.name}**" in index) == lead_can_read_skill(spec), spec.name
    assert "**pptx**" in index and "**pptx-authoring**" not in index and "**python_sandbox**" not in index
