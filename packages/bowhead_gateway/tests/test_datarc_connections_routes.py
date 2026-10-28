"""ws-datarc-decouple P2b (③/R3) — datarc connection CRUD route tests (TestClient).

team-admin authz is stubbed via dependency_overrides (its logic is covered by auth tests); these
assert ENDPOINT behavior: CRUD round-trip, team-scoping / IDOR-safety (an team_a admin can't
see/touch team_b's connection — R7.1), no-secret-echo, create-time SSRF base_url guard (R7.4),
F14 (no FERNET → 503), and the /validate dry-run (SSRF reject + respx happy path).
"""

from __future__ import annotations

import asyncio

import respx
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import datarc_connections as dc
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PUB = "https://93.184.216.34"  # public IP literal → passes SSRF without a DNS lookup
_PUB_HTTP = "http://93.184.216.34"
_INTRANET_HTTP = "http://10.20.30.40:8000/"
_BASE = "/api/v1/datarc-connections"


def _run(coro):
    return asyncio.run(coro)


def _client(store: DatarcConnectionStore, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(dc.router, prefix=_BASE)
    app.state.datarc_connection_store = store
    app.dependency_overrides[dc._REQUIRE_TEAM_ADMIN] = lambda: user
    return TestClient(app)


def _store(tmp_path) -> DatarcConnectionStore:
    s = DatarcConnectionStore(str(tmp_path / "u.db"))
    _run(s.initialize())
    return s


def test_crud_roundtrip_and_team_scoping(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = _store(tmp_path)
    # seed an team_b connection directly — an team_a admin must NEVER see / touch it (IDOR / R7.1)
    other = _run(store.create_connection(team_id="team_b", name="other", base_url=_PUB, auth_key="key-B-secret-zzzz"))

    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"))

    # create
    r = client.post(
        _BASE,
        json={
            "name": "datarc-prod",
            "base_url": _PUB,
            "auth_key": "key-A-secret-wxyz",
            "account_uid": "acc_a",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    dc_id = body["id"]
    assert body["team_id"] == "team_a"
    assert body["key_hint"] == "wxyz"  # last 4 only
    assert "key-A-secret-wxyz" not in r.text  # no secret echoed
    assert "auth_ref" not in body

    # list — only team_a's connection, NOT team_b's
    ids = {i["id"] for i in client.get(_BASE).json()["items"]}
    assert ids == {dc_id} and other.id not in ids

    # get
    assert client.get(f"{_BASE}/{dc_id}").json()["name"] == "datarc-prod"

    # IDOR: team_a admin can't get / patch team_b's connection
    assert client.get(f"{_BASE}/{other.id}").status_code == 404
    assert client.patch(f"{_BASE}/{other.id}", json={"status": "archived"}).status_code == 404

    # patch: rotate key (key_hint updates) + archive
    r2 = client.patch(f"{_BASE}/{dc_id}", json={"auth_key": "new-key-1234", "status": "archived"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["key_hint"] == "1234"
    assert r2.json()["status"] == "archived"
    # archived no longer in the default list, but visible with include_archived
    assert client.get(_BASE).json()["items"] == []
    assert len(client.get(f"{_BASE}?include_archived=true").json()["items"]) == 1


def test_create_allows_http_and_intranet_but_rejects_ssrf_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    # Public HTTP is supported for on-prem / legacy Datarc services.
    r1 = client.post(_BASE, json={"name": "x", "base_url": _PUB_HTTP, "auth_key": "k"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["base_url"] == _PUB_HTTP
    # Regression: actual private-network HTTP connection URL, including a non-default port and trailing slash.
    r2 = client.post(_BASE, json={"name": "intranet", "base_url": _INTRANET_HTTP, "auth_key": "k"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["base_url"] == _INTRANET_HTTP
    # SaaS mode rejects metadata / link-local targets.
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    r3 = client.post(_BASE, json={"name": "x", "base_url": "https://169.254.169.254", "auth_key": "k"})
    assert r3.status_code == 422
    assert r3.json()["detail"]["code"] == "datarc_connection_url_rejected"
    assert r3.json()["detail"]["diagnostics"] == {
        "schema_version": 1,
        "disclosure": "admin",
        "diagnostic_code": "datarc_connection_failure",
        "safe_detail": "phase=validate_input; failure_class=policy",
        "retryable": False,
    }


def test_create_archived_connection_keeps_current_active(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    active = client.post(_BASE, json={"name": "active", "base_url": _PUB, "auth_key": "key-1"})
    assert active.status_code == 200, active.text

    archived = client.post(
        _BASE,
        json={"name": "saved-only", "base_url": _PUB, "auth_key": "key-2", "status": "archived"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert {item["id"] for item in client.get(_BASE).json()["items"]} == {active.json()["id"]}


def test_create_rejects_unknown_status(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    response = client.post(
        _BASE,
        json={"name": "invalid", "base_url": _PUB, "auth_key": "key", "status": "unknown"},
    )
    assert response.status_code == 422


def test_create_without_fernet_returns_503(tmp_path, monkeypatch):
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    r = client.post(_BASE, json={"name": "x", "base_url": _PUB, "auth_key": "k"})
    assert r.status_code == 503  # F14: refuse to persist plaintext
    assert r.json()["detail"]["code"] == "datarc_connection_secret_storage_unavailable"
    assert "BOWHEAD_FERNET_KEY" not in r.text


def test_validate_rejects_ssrf_in_saas(tmp_path, monkeypatch):
    """链路本地地址只在 saas 下被拒；local 是企业内网部署，按策略放行。

    此前本例不设部署模式，于是在默认的 local 下根本不会拒绝，真去连了 169.254.169.254
    并以 ConnectError 失败 —— 自 22014fa9e 起一直是红的。断言「会被拒绝」必须自己把
    模式钉死，否则量的是环境不是代码。
    """
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    r = client.post(f"{_BASE}/validate", json={"base_url": "https://169.254.169.254", "auth_key": "k"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "datarc_connection_url_rejected"


@respx.mock
def test_validate_happy_path_lists_workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    respx.get(f"{_PUB}/openapi/v1/workspaces/").respond(
        200,
        json={"errcode": 200, "errmsg": "ok", "data": [{"id": 7, "name": "Sales"}, {"id": 8, "name": "Ops"}]},
    )
    # embed-via-openapi: validate also probes embed proxy health (advisory).
    respx.get(f"{_PUB}/openapi/v1/embed/").respond(
        200,
        json={"errcode": 200, "errmsg": "success", "data": {"status": "healthy"}},
    )
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    r = client.post(f"{_BASE}/validate", json={"base_url": _PUB, "auth_key": "good-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["workspace_count"] == 2
    assert {w["name"] for w in body["workspaces"]} == {"Sales", "Ops"}
    assert body["embed"] == {"available": True}


@respx.mock
def test_validate_reports_embed_permission_missing(tmp_path, monkeypatch):
    """key 没开 embed 权限 → embed.available=False + 错误提示，validate 本身仍 ok（advisory）."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    respx.get(f"{_PUB}/openapi/v1/workspaces/").respond(
        200,
        json={"errcode": 200, "errmsg": "ok", "data": [{"id": 7, "name": "Sales"}]},
    )
    respx.get(f"{_PUB}/openapi/v1/embed/").respond(403, json={"detail": "无当前接口权限"})
    client = _client(_store(tmp_path), User(id="u_a", team_id="team_a", email="a@x.com"))
    r = client.post(f"{_BASE}/validate", json={"base_url": _PUB, "auth_key": "good-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True  # BI validation is independent of embed.
    assert body["embed"]["available"] is False
    assert "error" not in body["embed"]


@respx.mock
def test_validate_projects_permission_failure_without_provider_prose(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    respx.get(f"{_PUB}/openapi/v1/embed/").respond(
        403,
        json={"detail": "Authorization: Bearer upstream-secret"},
    )
    respx.get(f"{_PUB}/openapi/v1/workspaces/").respond(
        403,
        json={"detail": "Authorization: Bearer upstream-secret"},
    )
    client = _client(
        _store(tmp_path),
        User(id="u_a", team_id="team_a", email="a@x.com"),
    )

    response = client.post(
        f"{_BASE}/validate",
        json={"base_url": _PUB, "auth_key": "client-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "datarc_connection_auth_rejected"
    assert body["diagnostics"] == {
        "schema_version": 1,
        "disclosure": "admin",
        "diagnostic_code": "datarc_connection_failure",
        "safe_detail": "phase=validate; failure_class=access",
        "retryable": False,
    }
    assert "upstream-secret" not in response.text
    assert "client-secret" not in response.text
