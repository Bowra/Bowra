"""Catalog readiness must describe what execution does with the same Agent (116 dogfood 2026-09-25).

The marketplace marked five running Agents "dependency not ready" or "safety unknown": stored tool names
the runtime renames, a tool folded into another authoring row, a lock that only stops new Agents picking a
tool, and "any Skill ⇒ safety unknown" even when the live revision pins each Skill's exact version.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from bowhead_core.agent_catalog_store import AgentCatalogSource, AgentCatalogStore, catalog_tool, tool_unavailable
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.subagent_authoring_catalog import native_authoring_tools


def _source(*, tool_refs: tuple[str, ...], skills: tuple[str, ...] = (), precise_skill_grants: bool = False) -> AgentCatalogSource:
    return AgentCatalogSource(
        id="osa_1",
        name="catalog-review-analyst",
        display_name="Catalog Analyst",
        description="Commercial analyst",
        model=None,
        visibility="team_wide",
        created_by="u1",
        status="enabled",
        lifecycle_state="active",
        definition_mode="released",
        updated_at="2026-09-25T00:00:00Z",
        row_version=1,
        tool_refs=tool_refs,
        skills=skills,
        listing={},
        listing_safety=scan_agent_listing(name="catalog-review-analyst", display_name="Catalog Analyst", description="Commercial analyst"),
        precise_skill_grants=precise_skill_grants,
    )


@pytest.fixture
def tools():
    return {tool.stable_ref: tool for tool in native_authoring_tools()}


def _compile(tmp_path, tools, source):
    return AgentCatalogStore(tmp_path / "bowhead.db").compile(source, owner_label=None, tools_by_ref=tools)


def test_tools_the_runtime_runs_are_not_reported_missing(tmp_path, tools) -> None:
    # A legacy stored name, the flat sandbox entry, and a tool unavailable for new authoring.
    projection = _compile(tmp_path, tools, _source(tool_refs=("bi_query", "canvas_add_chart", "python_exec", "skill_list")))
    assert projection.dependency_ready is True
    assert projection.dependency_reasons == ()
    assert "tool_risk_metadata_unknown" not in projection.safety_reasons
    assert catalog_tool(tools, "python_exec") is tools["python_sandbox"]
    assert not tool_unavailable(catalog_tool(tools, "skill_list"))


def test_a_tool_that_does_not_exist_is_still_missing(tmp_path, tools) -> None:
    projection = _compile(tmp_path, tools, _source(tool_refs=("bi_query", "no_such_tool")))
    assert projection.dependency_ready is False
    assert "tool_unavailable" in projection.dependency_reasons
    assert "tool_risk_metadata_unknown" in projection.safety_reasons


def test_a_runtime_lock_still_counts(tmp_path, tools) -> None:
    locked = replace(tools["web_search"], stable_ref="teammcp.src_1.search", selectable=False, locked_reason="mcp_feature_unlicensed")
    projection = _compile(tmp_path, {**tools, locked.stable_ref: locked}, _source(tool_refs=("teammcp.src_1.search",)))
    assert projection.dependency_ready is False
    assert projection.dependency_reasons == ("mcp_feature_unlicensed",)


def test_page_reader_and_web_search_have_distinct_catalog_labels(tmp_path, tools) -> None:
    reader = _compile(tmp_path, tools, _source(tool_refs=("web_fetch",)))
    research = _compile(tmp_path, tools, _source(tool_refs=("web_search", "web_fetch")))

    assert {cap.id for cap in reader.capabilities} == {"cap_web_fetch"}
    assert {cap.id for cap in research.capabilities} == {"cap_web_research", "cap_web_fetch"}


@pytest.mark.parametrize(("precise", "pending"), [(True, False), (False, True)])
def test_only_skills_without_exact_versions_are_safety_pending(tmp_path, tools, precise, pending) -> None:
    projection = _compile(tmp_path, tools, _source(tool_refs=("skill_read",), skills=("analyst-method-roe-dupont",), precise_skill_grants=precise))
    assert ("skill_scope_not_precise" in projection.safety_reasons) is pending
    assert (projection.safety_status == "pass") is not pending
