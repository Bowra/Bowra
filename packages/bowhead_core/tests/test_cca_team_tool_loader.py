"""cca LOAD POINT — team_tool_loader (entries + source-resolver) unit tests."""

from __future__ import annotations

import json

import pytest
from bowhead_core.team_tool_loader import load_team_tool_entries, make_team_source_resolver
from bowhead_core.user_store import TeamToolSource


def _spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "datarc", "version": "1"},
        "paths": {
            "/metric/{mid}": {
                "get": {
                    "operationId": "getMetric",
                    "parameters": [
                        {"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "tenant", "in": "query", "schema": {"type": "string"}},
                    ],
                }
            },
            "/list": {"get": {"operationId": "listAll", "parameters": []}},
        },
    }


def _source(
    sid: str = "src_1", *, status: str = "enabled", kind: str = "openapi", selected: list[str] | None = None, base_url: str = "https://api.datarc.cn"
) -> TeamToolSource:
    return TeamToolSource(
        id=sid,
        team_id="team_a",
        name="datarc",
        base_url=base_url,
        kind=kind,
        openapi_spec_json=json.dumps(_spec()),
        selected_op_ids=selected if selected is not None else [],
        auth_type="bearer",
        status=status,
    )


class _Store:
    def __init__(self, sources: list[TeamToolSource]) -> None:
        self._s = sources

    async def list_team_tool_sources(self, team_id: str, *, only_enabled: bool = False) -> list[TeamToolSource]:
        return [s for s in self._s if s.team_id == team_id and (not only_enabled or s.status == "enabled")]


class _Auth:
    async def get_headers(self, team_id: str, source_id: str) -> dict[str, str]:
        return {"Authorization": "Bearer tok"}


@pytest.mark.asyncio
async def test_load_entries_parses_all_ops() -> None:
    entries, by_id = await load_team_tool_entries("team_a", _Store([_source()]))
    assert {e.op.operation_id for e in entries if e.op} == {"getMetric", "listAll"}
    assert all(e.source_id == "src_1" for e in entries)
    assert set(by_id) == {"src_1"}


@pytest.mark.asyncio
async def test_selected_op_ids_filters() -> None:
    entries, _ = await load_team_tool_entries("team_a", _Store([_source(selected=["getMetric"])]))
    assert {e.op.operation_id for e in entries if e.op} == {"getMetric"}


@pytest.mark.asyncio
async def test_fail_closed_falsy_team_or_no_store() -> None:
    assert await load_team_tool_entries("", _Store([_source()])) == ([], {})
    assert await load_team_tool_entries("team_a", None) == ([], {})


@pytest.mark.asyncio
async def test_disabled_and_malformed_skipped() -> None:
    bad = _source(sid="bad")
    bad.openapi_spec_json = "{not valid json"  # parse → skip, must not break the turn
    store = _Store([_source(sid="ok"), _source(sid="off", status="disabled"), bad])
    _entries, by_id = await load_team_tool_entries("team_a", store)
    assert set(by_id) == {"ok"}  # 'off' only_enabled-filtered; 'bad' parse-skipped


@pytest.mark.asyncio
async def test_resolver_team_binding_and_missing_source() -> None:
    resolver = make_team_source_resolver("team_a", {"src_1": _source()}, _Auth())
    base, headers = await resolver("team_a", "src_1")
    assert base == "https://api.datarc.cn"
    assert headers["Authorization"] == "Bearer tok"
    assert await resolver("team_b", "src_1") == ("", {})  # cross-team → fail-closed
    assert await resolver("team_a", "nope") == ("", {})  # unknown source → fail-closed
