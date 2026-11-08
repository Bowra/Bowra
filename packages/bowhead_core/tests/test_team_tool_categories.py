"""Administrator-confirmed team MCP tool categories (FEATURE_TEAM_TOOL_CONFIRMATION step 2)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.openapi_tool_source import mcp_entry
from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogService
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.team_tool_categories import (
    effective_admin_category,
    load_team_tool_categories,
    lowers_annotation,
    save_team_tool_categories,
)
from bowhead_core.team_tool_confirmation import resolve_team_mcp_category
from bowhead_core.team_tool_loader import load_team_tool_entries
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.user_store import TeamToolSource, UserStore

_TEAM = "team_a"
_SRC = "ots_crm000000001"

_LOOKUP = {"name": "lookup", "description": "Find a contact", "input_schema": {"type": "object"}, "destructive": False}
_NOTES = {
    "name": "notes",
    "description": "Read notes",
    "input_schema": {"type": "object"},
    "destructive": False,
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def _record(meta: dict, category: str) -> dict:
    from bowhead_core.team_tool_categories import category_digest

    return {"risk": category, "schema_digest": category_digest(meta)}


def test_a_confirmation_applies_only_to_the_definition_it_reviewed_and_never_lowers() -> None:
    assert effective_admin_category(_LOOKUP, _record(_LOOKUP, "read")) == "read"
    changed = {**_LOOKUP, "description": "Find and update a contact"}
    assert effective_admin_category(changed, _record(_LOOKUP, "read")) is None
    assert lowers_annotation("read", {**_LOOKUP, "annotations": {"readOnlyHint": False, "destructiveHint": True}})
    assert effective_admin_category(_NOTES, _record(_NOTES, "destructive")) == "destructive"
    destructive = {**_LOOKUP, "annotations": {"readOnlyHint": False, "destructiveHint": True}}
    assert effective_admin_category(destructive, _record(destructive, "read")) is None
    assert effective_admin_category(_LOOKUP, {"risk": "unknown", "schema_digest": "x"}) is None


def test_resolution_settles_unknown_and_raises_but_keeps_higher_annotations() -> None:
    assert resolve_team_mcp_category(read_only=None, destructive=None, admin_category="read") == ("read", "admin_override")
    assert resolve_team_mcp_category(read_only=True, destructive=False, admin_category="destructive") == ("destructive", "admin_override")
    assert resolve_team_mcp_category(read_only=True, destructive=False, admin_category="read") == ("read", "mcp_annotation")
    assert resolve_team_mcp_category(read_only=False, destructive=True, admin_category="read") == ("destructive", "mcp_annotation")


@pytest.fixture
async def store(tmp_path) -> UserStore:
    path = tmp_path / "bowhead.db"
    users = UserStore(str(path))
    await users.initialize()
    await TeamSettingsStore(path).init()
    await users.create_team_tool_source(
        TeamToolSource(
            id=_SRC,
            team_id=_TEAM,
            name="CRM",
            kind="mcp",
            base_url="https://93.184.216.34/mcp",
            tools_schema=[_LOOKUP, _NOTES],
            selected_op_ids=["lookup", "notes"],
            created_at="2026-11-08T00:00:00+00:00",
            updated_at="2026-11-08T00:00:00+00:00",
        )
    )
    return users


@pytest.mark.asyncio
async def test_saving_bumps_the_source_revision_and_feeds_loader_catalog_and_runtime(store, monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    tools = {"lookup": _LOOKUP, "notes": _NOTES}
    stored = await save_team_tool_categories(
        store.db_path, team_id=_TEAM, source_id=_SRC, changes={"lookup": "read"}, tools=tools, actor_user_id="usr_admin"
    )
    assert stored is not None and stored["lookup"]["risk"] == "read" and stored["lookup"]["confirmed_by"] == "usr_admin"
    assert (await load_team_tool_categories(store.db_path, _TEAM))[_SRC]["lookup"]["risk"] == "read"
    source = await store.get_team_tool_source(_TEAM, _SRC)
    assert source.updated_at != "2026-11-08T00:00:00+00:00"

    entries, _ = await load_team_tool_entries(_TEAM, store)
    by_name = {entry.dispatch_name: entry for entry in entries}
    assert by_name["lookup"].admin_category == "read"
    assert by_name["notes"].admin_category is None

    # Only the source registry: the catalog's built-in connection tables are outside this fixture.
    sources_only = SimpleNamespace(db_path=store.db_path, list_team_tool_sources=store.list_team_tool_sources)
    snapshot = await SubagentAuthoringCatalogService().build(
        store=sources_only, team_id=_TEAM, actor_user_id="usr_admin", actor_active=True, mcp_entitled=True
    )
    lookup = next(tool for tool in snapshot.team_tools if tool.stable_ref.endswith(".lookup")).for_selection()
    assert (lookup.risk, lookup.risk_source, lookup.selectable) == ("read", "admin_override", True)

    specs = {spec.schema.name.rsplit(".", 1)[-1]: spec for spec in build_team_specs(_TEAM, entries, client=None)}
    assert specs["lookup"].annotations.read_only is True and specs["lookup"].risk_level == "low"

    removed = await save_team_tool_categories(
        store.db_path, team_id=_TEAM, source_id=_SRC, changes={"lookup": None}, tools=tools, actor_user_id="usr_admin"
    )
    assert removed == {}
    assert await load_team_tool_categories(store.db_path, _TEAM) == {}


@pytest.mark.asyncio
async def test_categories_are_ignored_without_the_switch_and_for_a_missing_source(store, monkeypatch) -> None:
    monkeypatch.delenv("FEATURE_TEAM_TOOL_CONFIRMATION", raising=False)
    tools = {"lookup": _LOOKUP}
    await save_team_tool_categories(store.db_path, team_id=_TEAM, source_id=_SRC, changes={"lookup": "read"}, tools=tools, actor_user_id="u")
    entries, _ = await load_team_tool_entries(_TEAM, store)
    assert all(entry.admin_category is None for entry in entries)
    assert (
        await save_team_tool_categories(
            store.db_path, team_id=_TEAM, source_id="ots_missing00001", changes={"lookup": "read"}, tools=tools, actor_user_id="u"
        )
        is None
    )


def test_a_raised_category_is_confirmed_at_run_time(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    entry = replace(mcp_entry(_SRC, _NOTES), admin_category="destructive")
    (spec,) = build_team_specs(_TEAM, [entry], client=None)
    assert spec.annotations.destructive is True and spec.risk_level == "medium"
