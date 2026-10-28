from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core.user_store import TeamBuiltinTool
from bowhead_gateway.routes.team_builtin_tools import _audit_builtin_tool, _fetch_datarc_scope, _normalize_datarc_endpoint, _public
from fastapi import HTTPException


def test_datarc_endpoint_uses_shared_deployment_mode_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    assert _normalize_datarc_endpoint(" http://datarc.internal:8080/ ") == "http://datarc.internal:8080"

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    with pytest.raises(HTTPException) as exc_info:
        _normalize_datarc_endpoint("http://127.0.0.1:8080")
    assert exc_info.value.detail == "datarc_endpoint_rejected"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@openapi.datarc.cn",
        "https://openapi.datarc.cn/openapi/v1",
        "https://openapi.datarc.cn?token=secret",
    ],
)
def test_datarc_endpoint_must_be_a_credential_free_service_root(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    with pytest.raises(HTTPException) as exc_info:
        _normalize_datarc_endpoint(endpoint)
    assert exc_info.value.detail in {"datarc_endpoint_rejected", "datarc_endpoint_must_be_service_root"}


def test_datarc_public_projection_prefixes_name_and_never_returns_ciphertext() -> None:
    projected = _public(
        TeamBuiltinTool(
            id="ibt_1",
            team_id="team_1",
            provider="datarc",
            display_name="财务数据",
            endpoint="https://openapi.datarc.cn",
            status="enabled",
            auth_ref="encrypted-secret",
            key_hint="1234",
            scope_state="verified",
            scope={"permissions": ["account", "workspace"], "workspaces": [{"id": "1", "name": "财务"}]},
            scope_checked_at="2026-09-23T00:00:00+00:00",
        )
    )

    assert projected["display_name"] == "Datarc - 财务数据"
    assert projected["name"] == "财务数据"
    assert projected["key_hint"] == "1234"
    assert projected["supported_tools"] == ["datarc_schema", "datarc_workspaces"]
    assert projected["scope"]["workspaces"] == [{"id": "1", "name": "财务"}]
    assert "auth_ref" not in projected


@pytest.mark.asyncio
async def test_builtin_tool_audit_failure_never_turns_a_committed_create_into_an_error() -> None:
    class Store:
        calls = 0

        async def write_admin_audit_log(self, **kwargs) -> None:
            self.calls += 1
            assert kwargs["scope"] == "team"
            raise RuntimeError("audit unavailable")

    store = Store()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=store)))
    user = SimpleNamespace(id="user_1", team_id="team_1")
    tool = TeamBuiltinTool(
        id="ibt_1",
        team_id="team_1",
        provider="datarc",
        display_name="财务数据",
        endpoint="https://openapi.datarc.cn",
        status="enabled",
        auth_ref="encrypted-secret",
    )

    await _audit_builtin_tool(request, user, "team_tool_source_created", tool)

    assert store.calls == 1


_DATARC = "https://demo.datarc.cn"
# demo.datarc.cn answers every unknown OpenAPI route (before auth) with the same HTML 404.
_ROUTE_MISSING = {"status_code": 404, "html": "<html><body>北极九章 404</body></html>"}


@pytest.fixture
def unguarded_datarc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real DatarcClient over respx; only the DNS-pinning SSRF transport is switched off."""

    import bowhead_bi_bridge

    real = bowhead_bi_bridge.DatarcClient
    monkeypatch.setattr(bowhead_bi_bridge, "DatarcClient", lambda **kwargs: real(**{**kwargs, "ssrf_guard": False}))


def _ok(data: object) -> dict[str, object]:
    return {"errcode": 200, "errmsg": "success", "data": data}


@pytest.mark.asyncio
@respx.mock
async def test_datarc_without_scope_route_verifies_key_via_workspaces(unguarded_datarc: None) -> None:
    respx.get(f"{_DATARC}/openapi/v1/scopes/").respond(**_ROUTE_MISSING)
    workspaces = respx.get(f"{_DATARC}/openapi/v1/workspaces/").respond(
        200, json=_ok([{"id": 1, "name": "零售"}, {"id": 2, "name": "财务", "desc": "月报"}])
    )

    scope, fingerprint, _ = await _fetch_datarc_scope(_DATARC, "good-key")

    assert workspaces.calls[0].request.headers["Authorization"] == "Bearer good-key"
    assert scope["source"] == "workspaces"
    assert scope["permissions"] == []
    assert scope["workspaces"] == [{"id": "1", "name": "零售", "desc": ""}, {"id": "2", "name": "财务", "desc": "月报"}]
    assert fingerprint.startswith("sha256:")
    tool = TeamBuiltinTool(
        id="ibt_1",
        team_id="team_1",
        provider="datarc",
        display_name="演示",
        endpoint=_DATARC,
        status="enabled",
        auth_ref="encrypted-secret",
        scope_state="verified",
        scope=scope,
    )
    assert _public(tool)["supported_tools"] == ["datarc_query", "datarc_schema", "datarc_workspaces"]


@pytest.mark.asyncio
@respx.mock
async def test_datarc_declared_scope_is_used_when_the_instance_serves_it(unguarded_datarc: None) -> None:
    respx.get(f"{_DATARC}/openapi/v1/scopes/").respond(
        200, json=_ok({"id": "key-1", "permissions": ["account", "workspace"], "workspaces": [{"id": 7, "name": "销售"}]})
    )
    workspaces = respx.get(f"{_DATARC}/openapi/v1/workspaces/").respond(200, json=_ok([]))

    scope, _, _ = await _fetch_datarc_scope(_DATARC, "good-key")

    assert "source" not in scope
    assert scope["permissions"] == ["account", "workspace"]
    assert scope["workspaces"] == [{"id": "7", "name": "销售", "desc": ""}]
    assert not workspaces.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_response", "workspaces_response"),
    [
        (_ROUTE_MISSING, {"status_code": 401, "json": {"detail": "Authentication credentials were not provided."}}),
        (_ROUTE_MISSING, {"status_code": 200, "json": {"errcode": 401, "errmsg": "invalid token", "data": None}}),
        ({"status_code": 403, "json": {"detail": "无当前接口权限"}}, None),
    ],
)
@respx.mock
async def test_datarc_rejected_key_is_a_422_with_a_localizable_code(
    unguarded_datarc: None,
    scope_response: dict[str, object],
    workspaces_response: dict[str, object] | None,
) -> None:
    respx.get(f"{_DATARC}/openapi/v1/scopes/").respond(**scope_response)
    if workspaces_response is not None:
        respx.get(f"{_DATARC}/openapi/v1/workspaces/").respond(**workspaces_response)

    with pytest.raises(HTTPException) as exc_info:
        await _fetch_datarc_scope(_DATARC, "bad-key")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {"code": "datarc_connection_key_invalid"}


@pytest.mark.asyncio
@respx.mock
async def test_datarc_endpoint_without_any_openapi_route_is_a_422(unguarded_datarc: None) -> None:
    respx.get(f"{_DATARC}/openapi/v1/scopes/").respond(**_ROUTE_MISSING)
    respx.get(f"{_DATARC}/openapi/v1/workspaces/").respond(**_ROUTE_MISSING)

    with pytest.raises(HTTPException) as exc_info:
        await _fetch_datarc_scope(_DATARC, "any-key")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {"code": "datarc_endpoint_unsupported"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_side_effect", "code"),
    [
        (httpx2.ConnectError("refused"), "datarc_verification_unreachable"),
        (httpx2.Response(503, text="bad gateway"), "datarc_verification_unreachable"),
        (httpx2.Response(200, json=_ok({"permissions": [], "workspaces": []})), "datarc_verification_failed"),
    ],
)
@respx.mock
async def test_datarc_upstream_failure_stays_a_502(unguarded_datarc: None, scope_side_effect: object, code: str) -> None:
    route = respx.get(f"{_DATARC}/openapi/v1/scopes/")
    if isinstance(scope_side_effect, httpx2.Response):
        route.mock(return_value=scope_side_effect)
    else:
        route.mock(side_effect=scope_side_effect)
    workspaces = respx.get(f"{_DATARC}/openapi/v1/workspaces/").respond(200, json=_ok([]))

    with pytest.raises(HTTPException) as exc_info:
        await _fetch_datarc_scope(_DATARC, "good-key")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {"code": code}
    assert not workspaces.called


def test_datarc_verification_codes_are_in_the_runtime_catalog() -> None:
    from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog

    catalog = load_error_catalog(default_error_catalog_dir())
    for code in (
        "datarc_connection_key_invalid",
        "datarc_endpoint_unsupported",
        "datarc_verification_unreachable",
        "datarc_verification_failed",
    ):
        for language in ("zh-CN", "en-US"):
            assert catalog.has(code, language), (code, language)
