"""cca LOAD POINT integration — real UserStore + ToolAuthRegistry + engine + mock upstream.

End-to-end cross-team isolation (tech-design §11 "1-op 真环 + 并发隔离 → 零串台"): two teams
with a SAME-named source "datarc" at DIFFERENT base_urls + DIFFERENT bearer tokens. Each team's
per-turn view sees ONLY its own tool; a call hits ONLY its own base_url with its own auth; a
cross-team ctx is denied; the token never echoes back through the (scrubbed) success body.

This wires the LOAD POINT path end-to-end through the REAL store + auth registry (only the
upstream HTTP + DNS are mocked) — the security-critical isolation proof for the hot-path change.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from bowhead_core.openapi_tool_source import (
    build_team_mcp_server,
    connect_team_mcp,
    make_http_executor,
    op_tool_name,
)
from bowhead_core.team_tool_loader import load_team_tool_entries, make_team_source_resolver
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.tool_auth_registry import ToolAuthRegistry
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import TeamToolSource, UserStore, _id, _now
from cryptography.fernet import Fernet

_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "datarc", "version": "1"},
    "paths": {"/ping": {"get": {"operationId": "ping", "summary": "ping", "responses": {"200": {"description": "ok"}}}}},
}
_PUBLIC = ["93.184.216.34"]


def _ctx(team_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        team_id=team_id,
        role=None,
    )


async def _seed(store: UserStore, team_id: str, base_url: str, token: str) -> str:
    src = TeamToolSource(
        id=_id("ots"),
        team_id=team_id,
        name="datarc",
        base_url=base_url,
        openapi_spec_json=json.dumps(_SPEC),
        selected_op_ids=["ping"],
        auth_type="bearer",
        created_at=_now(),
        updated_at=_now(),
    )
    await store.create_team_tool_source(src)
    await store.update_team_tool_source_auth(team_id, src.id, auth_type="bearer", auth={"token": token})
    return src.id


def _capture_transport(received: list[dict]) -> httpx2.MockTransport:
    def handler(req: httpx2.Request) -> httpx2.Response:
        received.append({"host": req.headers.get("host"), "auth": req.headers.get("authorization")})
        return httpx2.Response(200, json={"pong": True})

    return httpx2.MockTransport(handler)


async def _run_team(store: UserStore, registry: ToolAuthRegistry, team_id: str):
    entries, by_id = await load_team_tool_entries(team_id, store)
    recv: list[dict] = []
    execu = make_http_executor(
        team_id,
        make_team_source_resolver(team_id, by_id, registry),
        dns_resolver=lambda h: _PUBLIC,
        transport=_capture_transport(recv),
    )
    server = build_team_mcp_server(entries, execu)
    async with connect_team_mcp(server) as session:
        specs = build_team_specs(team_id, entries, session, audit_store=store)
        names = {s.schema.name for s in specs}
        res = await next(iter(specs)).handler({}, _ctx(team_id))
    return names, res, recv


@pytest.mark.asyncio
async def test_two_teams_same_source_name_zero_crosstalk(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    await store.initialize()
    src_a = await _seed(store, "team_a", "https://api-a.example", "tok-A")
    src_b = await _seed(store, "team_b", "https://api-b.example", "tok-B")
    registry = ToolAuthRegistry(store)

    names_a, res_a, recv_a = await _run_team(store, registry, "team_a")
    names_b, res_b, recv_b = await _run_team(store, registry, "team_b")

    # each team's view holds ONLY its own teammcp.* tool (keyed by its source_id) — zero overlap
    assert names_a == {op_tool_name(src_a, "ping")}
    assert names_b == {op_tool_name(src_b, "ping")}
    assert names_a.isdisjoint(names_b)

    # the call hit the RIGHT per-team base_url WITH the right per-team auth (Fernet-decrypted)
    assert res_a["ok"] is True and res_b["ok"] is True
    assert recv_a[0] == {"host": "api-a.example", "auth": "Bearer tok-A"}
    assert recv_b[0] == {"host": "api-b.example", "auth": "Bearer tok-B"}
    # the upstream token never echoes back through the scrubbed success body
    assert "tok-A" not in json.dumps(res_a)
    assert "tok-B" not in json.dumps(res_b)


@pytest.mark.asyncio
async def test_cross_team_ctx_is_denied_at_dispatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    await store.initialize()
    await _seed(store, "team_a", "https://api-a.example", "tok-A")
    registry = ToolAuthRegistry(store)

    entries, by_id = await load_team_tool_entries("team_a", store)
    execu = make_http_executor(
        "team_a", make_team_source_resolver("team_a", by_id, registry), dns_resolver=lambda h: _PUBLIC, transport=_capture_transport([])
    )
    server = build_team_mcp_server(entries, execu)
    async with connect_team_mcp(server) as session:
        spec = next(iter(build_team_specs("team_a", entries, session)))
        # team_a's tool handler invoked with an team_b ctx → §3.5 dispatch-time team-bind denies
        denied = await spec.handler({}, _ctx("team_b"))
        empty = await spec.handler({}, _ctx(""))
    assert denied == {"ok": False, "error_kind": "team_denied", "error": "tool not available for this team"}
    assert empty["error_kind"] == "team_denied"


@pytest.mark.asyncio
async def test_falsy_team_loads_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    await store.initialize()
    await _seed(store, "team_a", "https://api-a.example", "tok-A")
    # fail-closed: a falsy team_id loads no entries (native-only turn) even with sources present
    entries, by_id = await load_team_tool_entries("", store)
    assert entries == [] and by_id == {}


async def _seed_apikey(store, team_id, base_url, key, overrides=None):
    src = TeamToolSource(
        id=_id("ots"),
        team_id=team_id,
        name="datarc",
        base_url=base_url,
        openapi_spec_json=json.dumps(_SPEC),
        selected_op_ids=["ping"],
        tool_overrides=overrides or {},
        auth_type="api_key",
        created_at=_now(),
        updated_at=_now(),
    )
    await store.create_team_tool_source(src)
    await store.update_team_tool_source_auth(team_id, src.id, auth_type="api_key", auth={"value": key})
    return src.id


@pytest.mark.asyncio
async def test_api_key_auth_and_description_override_end_to_end(tmp_path, monkeypatch) -> None:
    """r4 e2e: the pre-merge P0 auth-contract fix (the card sends {value} for api_key → backend
    must produce an X-API-Key header) + the per-op description override (slice-1) flow all the way
    through create → Fernet store → LOAD POINT → ToolAuthRegistry → dispatch."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    await store.initialize()
    await _seed_apikey(store, "team_a", "https://api-a.example", "secret-key-123", overrides={"ping": {"description": "查询某基准货币的实时汇率"}})
    registry = ToolAuthRegistry(store)

    entries, by_id = await load_team_tool_entries("team_a", store)
    recv: list[dict] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        recv.append({"host": req.headers.get("host"), "x_api_key": req.headers.get("x-api-key"), "auth": req.headers.get("authorization")})
        return httpx2.Response(200, json={"pong": True})

    execu = make_http_executor(
        "team_a", make_team_source_resolver("team_a", by_id, registry), dns_resolver=lambda h: _PUBLIC, transport=httpx2.MockTransport(handler)
    )
    server = build_team_mcp_server(entries, execu)
    async with connect_team_mcp(server) as session:
        specs = list(build_team_specs("team_a", entries, session, audit_store=store))
        spec = specs[0]
        res = await spec.handler({}, _ctx("team_a"))

    assert res["ok"] is True
    # P0 fix: api_key creds {value} → X-API-Key header actually sent (not Bearer, not missing)
    assert recv[0]["x_api_key"] == "secret-key-123"
    assert recv[0]["auth"] is None
    # slice-1: the per-op description override is the advertised tool description
    assert "查询某基准货币的实时汇率" in (spec.schema.description or "")
    # the key never echoes back through the scrubbed result body
    assert "secret-key-123" not in json.dumps(res)


@pytest.mark.asyncio
async def test_load_point_stamps_source_labels_on_ctx(tmp_path, monkeypatch) -> None:
    """2026-07-22 按源分组: resolver 在 LOAD POINT 把 {advertised_name → 源显示名}
    盖到 ctx.team_tool_source_labels——Lead/planner 的 <team_tools> 组头用它。
    挂 ctx 而非 view(view 会被后续 clone 替换)。"""
    import contextlib as _ctxlib

    from bowhead_core.capability_resolver import CapabilityPrincipal, CapabilityResolver
    from bowhead_core.tool_registry import ToolRegistry

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    await store.initialize()
    src_id = await _seed(store, "team_a", "https://api-a.example", "tok-A")
    registry = ToolAuthRegistry(store)

    resolver = CapabilityResolver(base_registry=ToolRegistry(), user_store=store, tool_auth_registry=registry)
    ctx = _ctx("team_a")
    async with _ctxlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(user_id="u", team_id="team_a", workspace_id="w", role="lead"),
            ctx,
        )
        assert resolved.tool_view is not None
        labels = ctx.team_tool_source_labels
        assert labels, "load point 应盖上标签映射"
        assert labels[op_tool_name(src_id, "ping")] == "datarc"  # TeamToolSource.name from _seed.
