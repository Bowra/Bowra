"""Bowra authorization imports credentials without applying production configuration."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest
from bowhead_core.bowra_connect import BowraConnectStore
from bowhead_gateway.routes import bowra_connect
from cryptography.fernet import Fernet
from test_team_llm_config_api import _jwt_cookie, _seed_team, _seed_user, app, client, store

__all__ = ["app", "client", "store"]
pytestmark = pytest.mark.asyncio
ROOT = "/api/v1/admin/llm-config/bowra"
RESULT = {
    "base_url": "https://api.example.test/custom",
    "api_key": "returned-private-key",
    "models": ["model-one"],
    "name": "Bowhead",
    "expires_at": None,
    "quota_limit": None,
}


@pytest.fixture
async def setup(app, store, monkeypatch):
    await _seed_team(store)
    await _seed_user(store, "bowra_owner", "bowra@team.test", is_creator=True)
    monkeypatch.setenv("BOWRA_URL", "https://auth.example.test/prefix")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(bowra_connect, "product_name", lambda: "Bowhead")
    monkeypatch.setattr(
        bowra_connect,
        "current_request_public_access_context",
        lambda: SimpleNamespace(base_url="https://app.example.test/bowhead", prefix_known=True),
    )
    app.include_router(bowra_connect.router)
    calls = []

    class Exchange:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200, headers={"content-type": "application/json"}, json=lambda: dict(RESULT))

    monkeypatch.setattr(bowra_connect, "build_guarded_async_client", lambda **kwargs: Exchange())
    return SimpleNamespace(cookies={"bowhead_jwt": _jwt_cookie("bowra_owner")}, calls=calls)


def start(client, setup):
    response = client.post(ROOT + "/start", cookies=setup.cookies)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return parse_qs(urlsplit(response.json()["authorize_url"]).query)


async def test_first_connection_import_is_one_use_encrypted_and_does_not_apply(client, store, setup):
    query = start(client, setup)
    assert set(query) == {"app_name", "redirect_uri", "state", "code_challenge", "code_challenge_method"}
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]
    assert query["redirect_uri"] == ["https://app.example.test/bowhead" + bowra_connect.CALLBACK_PATH]
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT token_hash,payload_cipher FROM bowra_connect_pending")).fetchone()
        assert row[0] != state and row[1].startswith("FERNET:")
        assert "verifier" not in row[1]
    before = await store.get_team_llm_config_with_version("team_llm")
    response = client.get(ROOT + "/callback", params={"state": state, "code": "authorization-code"}, cookies=setup.cookies, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"].startswith("/bowhead/team/settings/models/?")
    token = parse_qs(urlsplit(response.headers["location"]).query)["bowra_import"][0]
    url, kwargs = setup.calls[0]
    assert url == "https://auth.example.test/prefix/connect/exchange"
    assert set(kwargs["data"]) == {"code", "code_verifier", "redirect_uri"}
    assert kwargs["data"]["redirect_uri"] == query["redirect_uri"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(kwargs["data"]["code_verifier"].encode()).digest()).decode().rstrip("=")
    assert challenge == query["code_challenge"][0]
    response = client.post(ROOT + "/imports/" + token, cookies=setup.cookies)
    assert response.status_code == 200
    assert response.json() == {"base_url": "https://api.example.test/custom/v1", "api_key": RESULT["api_key"], "models": RESULT["models"]}
    assert response.headers["cache-control"] == "no-store"
    assert await store.get_team_llm_config_with_version("team_llm") == before
    assert client.post(ROOT + "/imports/" + token, cookies=setup.cookies).status_code == 410
    response = client.get(ROOT + "/callback", params={"state": state, "code": "authorization-code"}, cookies=setup.cookies, follow_redirects=False)
    assert "invalid_callback" in response.headers["location"]
    assert len(setup.calls) == 1


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"error": "access_denied"}, "cancelled"),
        ({"code": "one", "error": "access_denied"}, "invalid_callback"),
        ({"code": ""}, "invalid_callback"),
        ({}, "invalid_callback"),
        ({"error": "unknown"}, "invalid_callback"),
    ],
)
async def test_denial_and_malformed_results_never_exchange(client, setup, params, reason):
    query = start(client, setup)
    response = client.get(ROOT + "/callback", params={"state": query["state"][0], **params}, cookies=setup.cookies, follow_redirects=False)
    assert reason in response.headers["location"]
    assert setup.calls == []


async def test_duplicate_parameters_and_expiry_never_exchange(client, store, setup):
    state = start(client, setup)["state"][0]
    response = client.get(ROOT + "/callback", params=[("state", state), ("code", "a"), ("code", "b")], cookies=setup.cookies, follow_redirects=False)
    assert "invalid_callback" in response.headers["location"]
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE bowra_connect_pending SET expires_at=0")
        await db.commit()
    response = client.get(ROOT + "/callback", params={"state": state, "code": "a"}, cookies=setup.cookies, follow_redirects=False)
    assert "invalid_callback" in response.headers["location"]
    assert not setup.calls


async def test_admin_permissions_and_current_team_binding(client, store, setup):
    await _seed_user(store, "bowra_member", "member@team.test", is_creator=False)
    member = {"bowhead_jwt": _jwt_cookie("bowra_member")}
    assert client.post(ROOT + "/start", cookies=member).status_code == 403
    state = start(client, setup)["state"][0]
    response = client.get(ROOT + "/callback", params={"state": state, "code": "a"}, cookies=member, follow_redirects=False)
    assert "permission_changed" in response.headers["location"]
    assert not setup.calls
    pending = BowraConnectStore(store.db_path)
    assert await pending.take(state, user_id="bowra_owner", team_id="other-team", kind="authorization") is None
    assert await pending.take(state, user_id="other-user", team_id="team_llm", kind="authorization") is None
    assert await pending.take(state, user_id="bowra_owner", team_id="team_llm", kind="authorization") is not None


@pytest.mark.parametrize(
    "address", ["http://app.example.test", "https://localhost", "https://127.0.0.1", "https://app.example.test:444", "https://app.localhost"]
)
async def test_unsupported_callback_rejected_before_authorization(client, setup, monkeypatch, address):
    monkeypatch.setattr(bowra_connect, "current_request_public_access_context", lambda: SimpleNamespace(base_url=address, prefix_known=True))
    response = client.post(ROOT + "/start", cookies=setup.cookies)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "bowra_https_callback_required"


async def test_unconfigured_service_and_missing_encryption(client, setup, monkeypatch):
    monkeypatch.delenv("BOWRA_URL")
    assert client.post(ROOT + "/start", cookies=setup.cookies).status_code == 404
    monkeypatch.setenv("BOWRA_URL", "https://auth.example.test")
    monkeypatch.delenv("BOWHEAD_FERNET_KEY")
    assert client.post(ROOT + "/start", cookies=setup.cookies).status_code == 503


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://api.example.test", "https://api.example.test/v1"),
        ("https://api.example.test/", "https://api.example.test/v1"),
        ("https://api.example.test/custom", "https://api.example.test/custom/v1"),
        ("https://api.example.test/custom/", "https://api.example.test/custom/v1"),
        ("https://api.example.test/nested/custom/", "https://api.example.test/nested/custom/v1"),
        ("https://api.example.test/v1", "https://api.example.test/v1"),
        ("https://api.example.test/v1/", "https://api.example.test/v1"),
        ("http://localhost:8080/custom/v1/", "http://localhost:8080/custom/v1"),
    ],
)
async def test_exchange_endpoint_adds_v1_once(endpoint, expected, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    payload = {**RESULT, "base_url": endpoint}
    result = bowra_connect._validated_result(payload, "Bowhead")
    assert result == {"base_url": expected, "api_key": RESULT["api_key"], "models": RESULT["models"]}
    assert payload["base_url"] == endpoint


@pytest.mark.parametrize(
    "patch",
    [
        {"api_key": ""},
        {"base_url": ""},
        {"base_url": "file:///tmp/model"},
        {"base_url": "https://api.example.test/" + "a" * 476},
        {"models": [1]},
        {"models": ["a", "a"]},
        {"name": "another-app"},
        {"quota_limit": 1},
        {"base_url": "https://user:pass@api.test"},
    ],
)
async def test_exchange_response_validation(patch, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    with pytest.raises(ValueError):
        bowra_connect._validated_result({**RESULT, **patch}, "Bowhead")


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/nested/application"])
async def test_browser_callback_uses_its_exact_prefix(client, setup, monkeypatch, prefix):
    monkeypatch.setattr(
        bowra_connect,
        "current_request_public_access_context",
        lambda: SimpleNamespace(base_url="https://browser.example.test" + prefix, prefix_known=True),
    )
    query = start(client, setup)
    assert query["redirect_uri"] == ["https://browser.example.test" + prefix + bowra_connect.CALLBACK_PATH]
    # A different Team default is not consulted to create this callback.
    assert "app.example.test" not in query["redirect_uri"][0]


async def test_missing_browser_evidence_fails_before_authorization(client, setup, monkeypatch):
    monkeypatch.setattr(bowra_connect, "current_request_public_access_context", lambda: None)
    assert client.post(ROOT + "/start", cookies=setup.cookies).status_code == 422


@pytest.mark.parametrize(
    "status,error,models,reason",
    [
        (400, "invalid_grant", [], "exchange_rejected"),
        (429, "rate_limited", [], "rate_limited"),
        (503, "service_unavailable", [], "service_unavailable"),
        (200, "", [], "no_models"),
        (200, "", ["model"], "exchange_uncertain"),
    ],
)
async def test_exchange_failure_does_not_apply_or_automatically_retry(client, setup, store, monkeypatch, status, error, models, reason):
    calls = []

    class FailedExchange:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            calls.append(1)
            payload = {"error": error} if status != 200 else {**RESULT, "models": models}
            return SimpleNamespace(
                status_code=status, headers={"content-type": "application/json" if not models else "text/html"}, json=lambda: payload
            )

    monkeypatch.setattr(bowra_connect, "build_guarded_async_client", lambda **kwargs: FailedExchange())
    before = await store.get_team_llm_config_with_version("team_llm")
    state = start(client, setup)["state"][0]
    response = client.get(ROOT + "/callback", params={"state": state, "code": "a"}, cookies=setup.cookies, follow_redirects=False)
    assert reason in response.headers["location"]
    assert len(calls) == 1
    assert await store.get_team_llm_config_with_version("team_llm") == before


async def test_import_keeps_original_review_apply_and_encrypted_runtime_credentials(client, setup, store):
    from test_team_llm_config_api import _apply_config

    state = start(client, setup)["state"][0]
    response = client.get(ROOT + "/callback", params={"state": state, "code": "a"}, cookies=setup.cookies, follow_redirects=False)
    token = parse_qs(urlsplit(response.headers["location"]).query)["bowra_import"][0]
    result = client.post(ROOT + "/imports/" + token, cookies=setup.cookies).json()
    config = {
        "schema_version": 2,
        "providers": {
            "bowra": {
                "type": "openai_compatible",
                "base_url": result["base_url"],
                "api_key": result["api_key"],
                "models": [{"id": model, "capabilities": ["chat", "tool"]} for model in result["models"]],
            }
        },
        "tiers": {"primary": {"provider": "bowra", "model": result["models"][0]}},
        "kb": {"models": {}, "active": {}},
        "media": {"models": {}, "active": {}},
        "search": {"models": {}, "active": {}},
    }
    url = "/api/v1/admin/llm-config"
    body = {"config": config, "expected_version": 0}
    assert client.put(url, json=body, cookies=setup.cookies).status_code == 409
    applied = _apply_config(client, url, json=body, cookies=setup.cookies)
    assert applied.status_code == 200, applied.text
    saved, version = await store.get_team_llm_config_with_version("team_llm")
    assert saved["providers"]["bowra"]["base_url"] == "https://api.example.test/custom/v1"
    assert saved["providers"]["bowra"]["api_key"] == RESULT["api_key"]
    assert saved["tiers"]["primary"] == config["tiers"]["primary"]
    assert version > 0
    assert RESULT["api_key"] not in applied.text
    assert client.put(url, json=body, cookies=setup.cookies).status_code == 409
