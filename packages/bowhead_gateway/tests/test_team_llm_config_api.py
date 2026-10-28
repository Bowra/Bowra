"""Team BYO LLM config API behavior."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import jwt as _jwt
import pytest
from bowhead_core.error_catalog import (
    default_error_catalog_dir,
    load_error_catalog,
)
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "member"])
async def test_runtime_parameter_preview_is_authorized_read_only_and_secret_free(client, store, monkeypatch, role):
    monkeypatch.delenv("FEATURE_MODEL_RUNTIME_PARAMETERS", raising=False)
    await _seed_team(store)
    await _seed_user(store, "u_preview", "preview@t.com", is_creator=role == "owner")
    before = await store.get_team_llm_config_with_version("team_llm")
    body = {
        "base_config_version": 0,
        "provider_draft": {
            "runtime_key": "draft",
            "display_name": "Draft",
            "type": "openai_compatible",
            "base_url": "https://93.184.216.34/v1",
            "api_key": "do-not-echo-this-secret",
            "models": [
                {
                    "id": "model",
                    "capabilities": ["chat", "tool"],
                    "context_window": 32768,
                    "outbound_params": {"temperature": {"mode": "omit"}},
                    "runtime_parameters": {
                        "profile": "dashscope-mixed-chat-v1",
                        "defaults": {"thinking_mode": "off", "outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}}},
                    },
                }
            ],
        },
        "model_id": "model",
        "capability": "tool",
    }
    monkeypatch.setattr("httpx2.AsyncClient", lambda **kwargs: pytest.fail("Preview must not construct an upstream client"))
    response = client.post("/api/v1/admin/llm-config/parameter-preview", json=body, cookies={"bowhead_jwt": _jwt_cookie("u_preview")})
    assert response.status_code == (200 if role == "owner" else 403), response.text
    assert await store.get_team_llm_config_with_version("team_llm") == before
    if role == "owner":
        payload = response.json()
        assert payload["provider_request_count"] == payload["configuration_writes"] == 0
        assert "do-not-echo" not in response.text
        assert next(row for row in payload["parameters"] if row["field"] == "enable_thinking")["value"] is False
        assert next(row for row in payload["parameters"] if row["field"] == "parallel_tool_calls")["value"] is False
        body["base_config_version"] = 1
        assert (
            client.post("/api/v1/admin/llm-config/parameter-preview", json=body, cookies={"bowhead_jwt": _jwt_cookie("u_preview")}).status_code == 409
        )


def test_runtime_parameter_draft_preserves_outbound_context_and_false():
    from bowhead_gateway.routes.admin_llm_config_v2 import V2ProviderDraft

    draft = V2ProviderDraft.model_validate(
        {
            "runtime_key": "draft",
            "display_name": "Draft",
            "type": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "outbound_params": {"temperature": {"mode": "omit"}},
            "context_window": 10000,
            "models": [
                {
                    "id": "model",
                    "capabilities": ["tool"],
                    "context_window": 32768,
                    "outbound_params": {"max_tokens": {"mode": "cap", "value": 2048}},
                    "runtime_parameters": {"defaults": {"outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}}}},
                }
            ],
        }
    )
    provider = draft.provider_config(api_key="key")
    assert provider["outbound_params"] == {"temperature": {"mode": "omit"}}
    assert provider["context_window"] == 10000
    model = provider["models"][0]
    assert model["context_window"] == 32768
    assert model["outbound_params"]["max_tokens"]["value"] == 2048
    assert model["runtime_parameters"]["defaults"]["outbound_params"]["parallel_tool_calls"]["value"] is False


@pytest.mark.asyncio
async def test_runtime_policy_apply_reopens_exact_values_and_closed_admission_preserves_them(client, store, monkeypatch):
    monkeypatch.delenv("FEATURE_MODEL_RUNTIME_PARAMETERS", raising=False)
    await _seed_team(store)
    await _seed_user(store, "u_policy", "policy@t.com", is_creator=True)
    config = _tier_v2_config()
    config["providers"]["main"]["base_url"] = "https://93.184.216.34/v1"
    policy = {
        "profile": "dashscope-mixed-chat-v1",
        "defaults": {"thinking_mode": "off", "outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}}},
        "capability_overrides": {"tool": {"thinking_mode": "on"}},
    }
    config["providers"]["main"]["models"][0]["runtime_parameters"] = policy
    cookies = {"bowhead_jwt": _jwt_cookie("u_policy")}
    response = _apply_config(client, "/api/v1/admin/llm-config", json={"config": config, "expected_version": 0}, cookies=cookies)
    assert response.status_code == 200, response.text
    loaded = client.get("/api/v1/admin/llm-config", cookies=cookies).json()
    assert loaded["config"]["providers"]["main"]["models"][0]["runtime_parameters"] == policy
    assert loaded["version"] == 1
    assert loaded["features"]["model_runtime_parameters"] is True
    assert loaded["parameter_profiles"]
    monkeypatch.setenv("FEATURE_MODEL_RUNTIME_PARAMETERS", "0")
    assert client.get("/api/v1/admin/llm-config", cookies=cookies).json()["features"]["model_runtime_parameters"] is False
    loaded["config"]["providers"]["main"]["display_name"] = "Renamed service"
    response = _apply_config(client, "/api/v1/admin/llm-config", json={"config": loaded["config"], "expected_version": 1}, cookies=cookies)
    assert response.status_code == 200, response.text
    saved, version = await store.get_team_llm_config_with_version("team_llm")
    assert version == 2
    assert saved["providers"]["main"]["models"][0]["runtime_parameters"] == policy
    changed = json.loads(json.dumps(saved))
    changed["providers"]["main"]["models"][0]["runtime_parameters"]["defaults"]["thinking_mode"] = "on"
    blocked = client.put("/api/v1/admin/llm-config", json={"config": changed, "expected_version": version}, cookies=cookies)
    assert blocked.status_code == 409
    assert (await store.get_team_llm_config_with_version("team_llm"))[1] == version


@pytest.mark.asyncio
async def test_capability_probe_forwards_model_protocol(monkeypatch) -> None:
    from bowhead_gateway.routes.admin_billing import (
        _verify_provider_capability_request,
    )

    import bowhead_router

    seen: dict = {}

    async def fake_verify_provider_capabilities(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "code": "ok", "steps": []}

    monkeypatch.setattr(
        bowhead_router,
        "verify_provider_capabilities",
        fake_verify_provider_capabilities,
    )

    await _verify_provider_capability_request(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-secret",
        model="qwen-plus",
        capability="web_search",
        capability_protocol="dashscope_compat",
        anthropic_version="2023-06-01",
        messages_path=None,
    )

    assert seen["models"] == [
        {
            "id": "qwen-plus",
            "capabilities": ["web_search"],
            "capability_protocol": "dashscope_compat",
        },
    ]


def test_preserve_provider_secret_accepts_auth_secret_storage_shape() -> None:
    from bowhead_gateway.routes.admin_billing import (
        _preserve_provider_secrets,
    )

    old = {
        "schema_version": 2,
        "providers": {
            "uptimize": {
                "type": "azure_openai",
                "auth": {
                    "type": "bearer",
                    "secret": "encrypted-at-rest-value",
                },
            }
        },
    }
    incoming = {
        "schema_version": 2,
        "providers": {
            "uptimize": {
                "type": "azure_openai",
                "auth": {
                    "type": "bearer",
                    "fingerprint": "sha256:redacted",
                },
            }
        },
    }

    merged = _preserve_provider_secrets(old, incoming)

    assert merged["providers"]["uptimize"]["api_key"] == ("encrypted-at-rest-value")
    assert "fingerprint" not in merged["providers"]["uptimize"]["auth"]


def test_preserve_provider_secret_clears_it_when_auth_is_disabled() -> None:
    from bowhead_gateway.routes.admin_billing import (
        _preserve_provider_secrets,
    )

    old = {
        "schema_version": 2,
        "providers": {
            "custom_gateway": {
                "type": "custom_http",
                "api_key": "stored-secret",
                "auth": {"type": "bearer"},
            }
        },
    }
    incoming = {
        "schema_version": 2,
        "providers": {
            "custom_gateway": {
                "type": "custom_http",
                "auth": {"type": "none"},
            }
        },
    }

    merged = _preserve_provider_secrets(old, incoming)

    assert "api_key" not in merged["providers"]["custom_gateway"]
    assert "secret" not in merged["providers"]["custom_gateway"]["auth"]


def test_embed_signature_tracks_custom_transport_and_azure_deployment() -> None:
    from bowhead_gateway.routes.admin_billing import (
        _embed_signature,
        _preview_embed_signature,
    )

    custom = {
        "schema_version": 2,
        "providers": {
            "gateway": {
                "type": "custom_http",
                "base_url": "",
                "models": [
                    {
                        "id": "embed-v1",
                        "deployment": "embed-prod",
                        "capability_transports": {"embed": "embed"},
                        "request_body_overrides": {
                            "embed": {"dimensions": 1024},
                        },
                    }
                ],
                "transports": {
                    "embed": {
                        "contract": "openai_embeddings",
                        "request": {
                            "method": "POST",
                            "url_template": "https://gateway.example/v1/embeddings",
                            "body_template": {
                                "input": {"$bowhead": "input"},
                                "dimensions": 1536,
                            },
                        },
                    }
                },
            }
        },
        "kb": {
            "models": {
                "active_embed": {
                    "provider": "gateway",
                    "real_id": "embed-v1",
                    "capability": "embed",
                    "dim": 1024,
                }
            },
            "active": {"embed": "active_embed"},
        },
    }
    changed_custom = json.loads(json.dumps(custom))
    changed_custom["providers"]["gateway"]["transports"]["embed"]["request"]["url_template"] = "https://gateway.example/v2/embeddings"

    assert _preview_embed_signature(custom, "active_embed") == (_embed_signature(custom, "active_embed"))
    assert _embed_signature(custom, "active_embed") != _embed_signature(
        changed_custom,
        "active_embed",
    )

    changed_deployment = json.loads(json.dumps(custom))
    changed_deployment["providers"]["gateway"]["models"][0]["deployment"] = "embed-canary"
    assert _embed_signature(custom, "active_embed") != _embed_signature(
        changed_deployment,
        "active_embed",
    )


@pytest.fixture
async def store(tmp_path):
    s = UserStore(str(tmp_path / "bowhead.db"))
    import sqlite3

    from bowhead_cli.migrate import upgrade
    from bowhead_core.migrations.baseline_schema import initial_schema

    with sqlite3.connect(s.db_path) as connection:
        connection.executescript(initial_schema("core"))

    upgrade(
        f"sqlite+aiosqlite:///{s.db_path}",
        target="core",
    )
    await s.initialize()
    return s


async def _seed_team(s: UserStore, team_id: str = "team_llm") -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'Team LLM', 10, 'ultra', 5000, 30000, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()


async def _seed_user(
    s: UserStore,
    user_id: str,
    email: str,
    *,
    team_id: str = "team_llm",
    is_creator: bool = False,
) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


def _jwt_cookie(user_id: str) -> str:
    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def app(store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    from bowhead_gateway.routes import admin_billing as admin_billing_routes
    from bowhead_gateway.routes import (
        admin_llm_config_v2 as admin_llm_config_v2_routes,
    )
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store
    a.state.deployment_mode = "saas"
    a.state.error_catalog = load_error_catalog(default_error_catalog_dir())

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()

    class _VerificationRunner:
        def __init__(self) -> None:
            self.started: list[dict[str, str]] = []
            self.cancelled: list[str] = []

        def start(self, **kwargs: str) -> None:
            self.started.append(kwargs)

        def cancel(self, batch_id: str) -> None:
            self.cancelled.append(batch_id)

    a.state.llm_verification_runner = _VerificationRunner()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(admin_billing_routes.router)
    a.include_router(admin_llm_config_v2_routes.router)
    return a


@pytest.mark.asyncio
async def test_model_discovery_v2_precheck_reserves_without_provider_request(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)

    response = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json={
            "runtime_key": "openai_main",
            "display_name": "OpenAI Main",
            "type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": "write-only-secret",
            "models": [],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["provider_id"].startswith("llmp_")
    assert body["discovery"] == {"supported": True}
    assert [item["purpose"] for item in body["request_previews"]] == [
        "chat",
        "models",
        "embed",
    ]

    repeated = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json={
            "runtime_key": "openai_main",
            "display_name": "OpenAI Main",
            "type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": "write-only-secret",
            "models": [],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["provider_id"] == body["provider_id"]


@pytest.mark.asyncio
async def test_azure_precheck_uses_declared_model_not_models_endpoint(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bowhead_core.team_llm_config as team_llm_config

    monkeypatch.setattr(team_llm_config, "validate_url", lambda *_args, **_kwargs: None)
    await _seed_team(store)
    await _seed_user(store, "u_azure", "azure@t.com", is_creator=True)

    response = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json={
            "runtime_key": "uptimize",
            "display_name": "UPTIMIZE",
            "type": "azure_openai",
            "endpoint": "https://api.openai.com",
            "api_version": "2024-10-21",
            "api_key": "write-only-secret",
            "auth": {"type": "bearer"},
            "models": [
                {
                    "id": "gpt-51",
                    "deployment": "gpt-51",
                    "capabilities": ["chat", "tool"],
                }
            ],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_azure")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["discovery"] == {"supported": False}
    assert [item["purpose"] for item in body["request_previews"]] == [
        "chat",
        "tool",
    ]
    assert all("/models" not in item["safe_url"] for item in body["request_previews"])
    assert body["request_previews"][0]["safe_url"].endswith("/openai/deployments/gpt-51/chat/completions?api-version=2024-10-21")


@pytest.mark.asyncio
async def test_custom_http_precheck_compiles_safe_declared_transport(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bowhead_core.team_llm_config as team_llm_config

    monkeypatch.setattr(team_llm_config, "validate_url", lambda *_args, **_kwargs: None)
    await _seed_team(store)
    await _seed_user(store, "u_custom", "custom@t.com", is_creator=True)

    response = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json={
            "runtime_key": "internal_gateway",
            "type": "custom_http",
            "base_url": "https://api.openai.com",
            "api_key": "write-only-secret",
            "auth": {
                "type": "secret_header",
                "header_name": "X-API-Key",
            },
            "transports": {
                "chat": {
                    "contract": "openai_chat_completions",
                    "request": {
                        "method": "POST",
                        "url_template": "{base_url}/internal/chat",
                        "body_template": {
                            "model": {"$bowhead": "model"},
                            "messages": {"$bowhead": "messages"},
                        },
                    },
                }
            },
            "models": [
                {
                    "id": "company-gpt",
                    "capabilities": ["chat"],
                    "capability_transports": {"chat": "chat"},
                }
            ],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_custom")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["discovery"] == {"supported": False}
    assert body["request_previews"] == [
        {
            "purpose": "chat",
            "method": "POST",
            "safe_url": "https://api.openai.com/internal/chat",
        }
    ]


@pytest.mark.asyncio
async def test_custom_http_precheck_accepts_full_url_without_base_url(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bowhead_core.team_llm_config as team_llm_config

    monkeypatch.setattr(team_llm_config, "validate_url", lambda *_args, **_kwargs: None)
    await _seed_team(store)
    await _seed_user(store, "u_custom_full", "custom-full@t.com", is_creator=True)

    response = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json={
            "runtime_key": "full_url_gateway",
            "type": "custom_http",
            "base_url": "",
            "auth": {"type": "none"},
            "transports": {
                "chat": {
                    "contract": "openai_chat_completions",
                    "request": {
                        "method": "POST",
                        "url_template": "https://api.openai.com/internal/chat",
                        "body_template": {
                            "messages": {"$bowhead": "messages"},
                        },
                    },
                }
            },
            "models": [
                {
                    "id": "company-gpt",
                    "capabilities": ["chat"],
                    "capability_transports": {"chat": "chat"},
                }
            ],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_custom_full")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == []
    assert response.json()["request_previews"] == [
        {
            "purpose": "chat",
            "method": "POST",
            "safe_url": "https://api.openai.com/internal/chat",
        }
    ]


@pytest.mark.asyncio
async def test_model_discovery_v2_returns_isolated_candidates(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    draft = {
        "runtime_key": "openai_main",
        "type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "write-only-secret",
        "models": [
            {
                "id": "existing-model",
                "capabilities": ["chat"],
            }
        ],
    }
    precheck = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json=draft,
        cookies={"bowhead_jwt": jwt},
    )
    assert precheck.status_code == 200, precheck.text
    provider_id = precheck.json()["provider_id"]

    async def fake_discover(**_kwargs):
        return {
            "ok": True,
            "code": "ok",
            "http_status": 200,
            "candidates": [
                {
                    "model_id": "existing-model",
                    "upstream_metadata": {"owned_by": "customer"},
                },
                {
                    "model_id": "new-model",
                    "upstream_metadata": {},
                },
            ],
            "returned_count": 2,
            "truncated": False,
            "limit": 1000,
        }

    from bowhead_gateway.routes import admin_llm_config_v2

    monkeypatch.setattr(
        admin_llm_config_v2,
        "discover_provider_model_candidates",
        fake_discover,
    )
    response = client.post(
        f"/api/v1/admin/llm-config/providers/{provider_id}/discover-models",
        json={**draft, "provider_id": provider_id},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [candidate["model_id"] for candidate in body["candidates"]] == [
        "existing-model",
        "new-model",
    ]
    assert body["candidates"][0]["already_added"] is True
    assert body["candidates"][1]["already_added"] is False
    assert {capability for candidate in body["candidates"] for capability in candidate.get("capabilities", [])} == set()


@pytest.mark.asyncio
async def test_model_verification_v2_plan_then_batch_is_confirmed_and_persisted(
    app: FastAPI,
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    jwt = _jwt_cookie("u_creator")
    draft = {
        "runtime_key": "openai_main",
        "display_name": "OpenAI Main",
        "type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "write-only-secret",
        "models": [],
    }
    precheck = client.post(
        "/api/v1/admin/llm-config/providers/precheck",
        json=draft,
        cookies={"bowhead_jwt": jwt},
    )
    provider_id = precheck.json()["provider_id"]
    version = (await store.get_team_llm_config_with_version("team_llm"))[1]

    plan = client.post(
        "/api/v1/admin/llm-config/verification-plans",
        json={
            "base_config_version": version,
            "provider_drafts": [{**draft, "provider_id": provider_id}],
            "items": [
                {
                    "provider_id": provider_id,
                    "model_id": "gpt-4.1",
                    "capability": "chat",
                },
                {
                    "provider_id": provider_id,
                    "model_id": "rerank-1",
                    "capability": "rerank",
                },
            ],
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert plan.status_code == 200, plan.text
    plan_body = plan.json()
    assert plan_body["item_count"] == 2
    assert plan_body["provider_request_count"] == 1
    assert [item["testability"] for item in plan_body["items"]] == [
        "testable",
        "not_testable",
    ]
    assert "config_signature" not in plan.text

    batch = client.post(
        "/api/v1/admin/llm-config/verification-batches",
        headers={"Idempotency-Key": "batch-attempt-1"},
        json={
            "plan_id": plan_body["plan_id"],
            "confirmation_token": plan_body["confirmation_token"],
            "provider_drafts": [{**draft, "provider_id": provider_id}],
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert batch.status_code == 202, batch.text
    batch_body = batch.json()
    assert batch_body["state"] == "queued"
    assert len(app.state.llm_verification_runner.started) == 1

    status = client.get(
        f"/api/v1/admin/llm-config/verification-batches/{batch_body['batch_id']}",
        cookies={"bowhead_jwt": jwt},
    )
    assert status.status_code == 200, status.text
    assert status.json()["counts"] == {"queued": 2}

    replay = client.post(
        "/api/v1/admin/llm-config/verification-batches",
        headers={"Idempotency-Key": "batch-attempt-1"},
        json={
            "plan_id": plan_body["plan_id"],
            "confirmation_token": plan_body["confirmation_token"],
            "provider_drafts": [{**draft, "provider_id": provider_id}],
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["batch_id"] == batch_body["batch_id"]
    assert replay.json()["replayed"] is True
    assert len(app.state.llm_verification_runner.started) == 1


@pytest.mark.asyncio
async def test_model_verification_v2_rejects_tampered_confirmation(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    response = client.post(
        "/api/v1/admin/llm-config/verification-batches",
        headers={"Idempotency-Key": "batch-attempt-1"},
        json={
            "plan_id": f"lvplan_{'0' * 32}",
            "confirmation_token": f"{'a' * 30}.{'b' * 30}",
            "provider_drafts": [
                {
                    "runtime_key": "openai_main",
                    "type": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "secret",
                }
            ],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_confirmation_token"


@pytest.fixture
def client(app):
    return TestClient(app)


def _v2_config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "openai_org": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "sk-openai-team-secret-1234567890",
                "models": ["gpt-4.1", "gpt-4o-mini"],
            },
            "anthropic_team": {
                "type": "anthropic_compatible",
                "base_url": "https://93.184.216.35",
                "api_key": "sk-ant-team-secret-1234567890",
                "models": ["claude-3-5-sonnet"],
            },
        },
        "agents": {
            "lead": {
                "primary": {"provider": "openai_org", "model": "gpt-4.1"},
                "fallback": [{"provider": "anthropic_team", "model": "claude-3-5-sonnet"}],
            },
            "modeler": {
                "primary": {"provider": "openai_org", "model": "gpt-4o-mini"},
                "fallback": [],
            },
            "analyst": {
                "default": {
                    "primary": {"provider": "anthropic_team", "model": "claude-3-5-sonnet"},
                    "fallback": [],
                },
                "profiles": {},
            },
            "narrative": {
                "primary": {"provider": "openai_org", "model": "gpt-4o-mini"},
                "fallback": [],
            },
            "echo": {
                "primary": {"provider": "openai_org", "model": "gpt-4o-mini"},
                "fallback": [],
            },
            "judge": {
                "primary": {"provider": "anthropic_team", "model": "claude-3-5-sonnet"},
                "fallback": [],
            },
        },
        "kb": {
            "models": {
                "embed_small": {
                    "provider": "openai_org",
                    "real_id": "text-embedding-3-small",
                    "capability": "embed",
                    "dim": 1536,
                }
            },
            "active": {"embed": "embed_small"},
        },
    }


def _tier_v2_config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "main": {
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-team-secret-1234567890",
                "models": [
                    {
                        "id": "gpt-main",
                        "capabilities": ["chat", "tool"],
                    }
                ],
            }
        },
        "tiers": {
            "primary": {
                "provider": "main",
                "model": "gpt-main",
            },
            "fast": {"follow": "primary"},
            "judge": {"auto": True},
        },
        "kb": {"models": {}, "active": {}},
        "media": {"models": {}, "active": {}},
        "search": {"models": {}, "active": {}},
    }


@pytest.mark.asyncio
async def test_v2_validation_reports_missing_provider_precheck(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )

    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={
            "config": _tier_v2_config(),
            "expected_version": 0,
        },
        cookies={
            "bowhead_jwt": _jwt_cookie("u_creator"),
            "NEXT_LOCALE": "en-US",
        },
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["valid"] is False
    assert preview.json()["issues"] == [
        {
            "code": "provider_identity_required",
            "path": "providers.main",
            "message": ("This model source has not completed its safe precheck. Return to the source, save it again, and retry."),
        }
    ]


@pytest.mark.asyncio
async def test_v2_readiness_migration_apply_is_atomic_and_snapshotted(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    await store.update_team_llm_config(
        "team_llm",
        _tier_v2_config(),
    )
    jwt = _jwt_cookie("u_creator")
    loaded = client.get(
        "/api/v1/admin/llm-config",
        cookies={"bowhead_jwt": jwt},
    )
    assert loaded.status_code == 200, loaded.text
    loaded_body = loaded.json()
    assert loaded_body["provider_identities"]["main"].startswith("llmp_")
    assert loaded_body["migration"]["state"] == "available"
    assert {entry["capability"] for entry in loaded_body["verification"]["entries"]} == {"chat", "tool"}

    validate = client.post(
        "/api/v1/admin/llm-config/validate",
        json={
            "config": loaded_body["config"],
            "expected_version": loaded_body["version"],
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert validate.status_code == 200, validate.text
    readiness = validate.json()["readiness"]
    assert readiness["migration_acceptance"] is not None
    assert readiness["blocking"] == []
    assert {row["status"] for row in readiness["rows"]} == {"grandfathered"}

    apply = client.put(
        "/api/v1/admin/llm-config",
        json={
            "config": loaded_body["config"],
            "expected_version": loaded_body["version"],
            "readiness": {
                "draft_semantic_hash": readiness["draft_semantic_hash"],
                "acceptance_tokens": [],
                "migration_acceptance_token": readiness["migration_acceptance"]["acceptance_token"],
            },
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert apply.status_code == 200, apply.text
    assert apply.json()["version"] == loaded_body["version"] + 1
    assert apply.json()["apply_snapshot_id"].startswith("llmas_")
    # The page replaces its state with this response, so it carries the same features as GET (B14:
    # without them the page showed "no assignments yet" right after a successful Apply).
    assert apply.json()["features"] == loaded_body["features"]

    async with aiosqlite.connect(store.db_path) as db:
        baseline = await (
            await db.execute(
                """
                SELECT applied_config_version
                FROM team_llm_v2_migration_baselines
                WHERE team_id = 'team_llm'
                """
            )
        ).fetchone()
        snapshot = await (
            await db.execute(
                """
                SELECT config_version
                FROM team_llm_apply_snapshots
                WHERE team_id = 'team_llm'
                """
            )
        ).fetchone()
        audit = await (
            await db.execute(
                """
                SELECT action
                FROM admin_audit_log
                WHERE team_id = 'team_llm'
                  AND action = 'llm_config_apply'
                """
            )
        ).fetchone()
    assert baseline == (loaded_body["version"] + 1,)
    assert snapshot == (loaded_body["version"] + 1,)
    assert audit == ("llm_config_apply",)


@pytest.mark.asyncio
async def test_full_config_write_requires_encryption_by_default(client, store, monkeypatch):
    monkeypatch.delenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", raising=False)
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)

    r = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "llm_config_encryption_required"


@pytest.mark.asyncio
async def test_legacy_add_provider_requires_encryption_by_default(client, store, monkeypatch):
    monkeypatch.delenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", raising=False)
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)

    r = client.post(
        "/api/v1/admin/llm-config/providers",
        json={
            "provider_name": "deepseek",
            "api_key": "sk-deepseek-test-secret-1234567890",
            "role": "lead",
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "llm_config_encryption_required"


@pytest.mark.asyncio
async def test_creator_can_save_full_v2_config_redacted_and_versioned(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)

    r = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    payload = json.dumps(body, sort_keys=True)
    assert body["version"] == 1
    assert body["config"]["schema_version"] == 2
    assert "sk-openai-team-secret" not in payload
    assert "api_key" not in payload
    assert body["providers"][0]["fingerprint"].startswith("sha256:")

    followup = client.get(
        "/api/v1/admin/llm-config",
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )
    assert followup.status_code == 200
    assert followup.json()["version"] == 1


@pytest.mark.asyncio
async def test_config_validation_preview_is_read_only_and_reports_impact(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    saved = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": jwt},
    )
    draft = saved.json()["config"]
    draft["agents"]["lead"]["primary"] = {
        "provider": "openai_org",
        "model": "gpt-4o-mini",
    }

    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": draft, "expected_version": 1},
        cookies={"bowhead_jwt": jwt},
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["valid"] is True
    assert body["version"] == 1
    assert body["changed_sections"] == ["routing"]
    assert body["impact"]["routing_changed"] is True
    assert body["impact"]["providers_changed"] is False
    assert "api_key" not in json.dumps(body)
    stored, version = await store.get_team_llm_config_with_version("team_llm")
    assert version == 1
    assert stored["agents"]["lead"]["primary"]["model"] == "gpt-4.1"


@pytest.mark.parametrize(
    ("language", "expected_message"),
    [
        (
            "zh-CN",
            "模型来源地址被安全策略拦截，请填写允许访问的公开地址。",
        ),
        (
            "en-US",
            "A model source address was blocked by the security policy. Enter an allowed public address.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_config_validation_preview_returns_field_issue(
    client,
    store,
    monkeypatch,
    language,
    expected_message,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    cfg = _v2_config()
    cfg["providers"]["openai_org"]["base_url"] = "http://169.254.169.254/latest/meta-data"

    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": cfg, "expected_version": 0},
        cookies={
            "bowhead_jwt": _jwt_cookie("u_creator"),
            "NEXT_LOCALE": language,
        },
    )

    assert preview.status_code == 200, preview.text
    issue = preview.json()["issues"][0]
    assert preview.json()["valid"] is False
    assert issue["code"] == "blocked_url"
    assert issue["path"] == "providers.openai_org.base_url"
    assert issue["message"] == expected_message
    assert "SSRF" not in issue["message"]


@pytest.mark.asyncio
async def test_knowledge_result_count_round_trips_without_reindex(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_count", "count@t.com", is_creator=True)
    cookies = {"bowhead_jwt": _jwt_cookie("u_count")}
    config = _tier_v2_config()
    config["kb"]["retrieval_top_k"] = 30
    saved = _apply_config(client, "/api/v1/admin/llm-config", json={"config": config, "expected_version": 0}, cookies=cookies)
    assert saved.status_code == 200, saved.text
    loaded = client.get("/api/v1/admin/llm-config", cookies=cookies).json()
    assert loaded["config"]["kb"]["retrieval_top_k"] == 30
    loaded["config"]["kb"]["retrieval_top_k"] = 50
    preview = client.post(
        "/api/v1/admin/llm-config/validate", json={"config": loaded["config"], "expected_version": loaded["version"]}, cookies=cookies
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["impact"]["embedding"]["reindex_required"] is False


@pytest.mark.asyncio
async def test_config_validation_preview_reports_embed_reindex(
    client,
    app,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    saved = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": jwt},
    )
    draft = saved.json()["config"]
    draft["kb"]["models"]["embed_next"] = {
        "provider": "openai_org",
        "real_id": "text-embedding-3-large",
        "capability": "embed",
        "dim": 3072,
    }
    draft["kb"]["active"]["embed"] = "embed_next"

    class FilesStore:
        async def list_active_for_team_all_scopes(
            self,
            team_id: str,
        ) -> list[SimpleNamespace]:
            assert team_id == "team_llm"
            return [
                SimpleNamespace(file_id="file_a"),
                SimpleNamespace(file_id="file_b"),
            ]

    app.state.kb_files_store = FilesStore()
    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": draft, "expected_version": 1},
        cookies={"bowhead_jwt": jwt},
    )

    assert preview.status_code == 200, preview.text
    embedding = preview.json()["impact"]["embedding"]
    assert embedding["changed"] is True
    assert embedding["reindex_required"] is True
    assert embedding["active_files"] == 2
    assert embedding["from"]["name"] == "embed_small"
    assert embedding["to"]["name"] == "embed_next"


@pytest.mark.asyncio
async def test_full_config_put_rejects_stale_expected_version(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    first = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config(), "expected_version": 0},
        cookies={"bowhead_jwt": jwt},
    )
    assert first.status_code == 200, first.text

    newer = _v2_config()
    newer["agents"]["lead"]["primary"]["model"] = "gpt-4o-mini"
    await store.update_team_llm_config("team_llm", newer)

    stale = _v2_config()
    stale["agents"]["narrative"]["primary"]["model"] = "gpt-4.1"
    response = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": stale, "expected_version": 1},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "config_version_conflict"
    assert detail["expected_version"] == 1
    assert detail["current_version"] == 2
    stored, version = await store.get_team_llm_config_with_version("team_llm")
    assert version == 2
    assert stored["agents"]["lead"]["primary"]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_saved_provider_verify_keeps_secret_server_side(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    saved = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": jwt},
    )
    assert saved.status_code == 200, saved.text
    calls: list[dict[str, object]] = []

    class VerifyResult:
        def as_dict(self) -> dict[str, object]:
            return {"ok": True, "code": "ok", "model_count": 2}

    async def fake_verify_provider(**kwargs):
        calls.append(kwargs)
        return VerifyResult().as_dict()

    import bowhead_router

    monkeypatch.setattr(bowhead_router, "verify_provider_capabilities", fake_verify_provider)
    response = client.post(
        "/api/v1/admin/llm-config/providers/verify-saved",
        json={"provider_name": "openai_org"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "code": "ok", "model_count": 2}
    assert calls[0]["api_key"] == "sk-openai-team-secret-1234567890"
    assert "sk-openai-team-secret" not in response.text


@pytest.mark.asyncio
async def test_draft_provider_verify_uses_saved_secret_with_candidate_fields(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    saved = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": jwt},
    )
    assert saved.status_code == 200, saved.text
    calls: list[dict[str, object]] = []

    class VerifyResult:
        def as_dict(self) -> dict[str, object]:
            return {"ok": True, "code": "ok", "latency_ms": 48}

    async def fake_verify_provider(**kwargs):
        calls.append(kwargs)
        return VerifyResult().as_dict()

    import bowhead_router

    monkeypatch.setattr(bowhead_router, "verify_provider_capabilities", fake_verify_provider)
    response = client.post(
        "/api/v1/admin/llm-config/providers/verify-draft",
        json={
            "provider_name": "openai_org",
            "provider_type": "openai_compatible",
            "base_url": "https://candidate.example.com/v1",
            "model": "gpt-4.1-mini",
        },
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "code": "ok", "latency_ms": 48}
    assert calls == [
        {
            "provider_type": "openai_compatible",
            "base_url": "https://candidate.example.com/v1",
            "api_key": "sk-openai-team-secret-1234567890",
            "models": [{"id": "gpt-4.1-mini", "capabilities": ["chat"]}],
            "anthropic_version": "2023-06-01",
            "messages_path": None,
        }
    ]
    assert "sk-openai-team-secret" not in response.text


@pytest.mark.asyncio
async def test_saved_provider_verify_missing_fields_returns_stable_code(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    cfg = _v2_config()
    del cfg["providers"]["openai_org"]["api_key"]
    await store.update_team_llm_config("team_llm", cfg)

    response = client.post(
        "/api/v1/admin/llm-config/providers/verify-saved",
        json={"provider_name": "openai_org"},
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "provider_not_testable",
        "missing_fields": ["api_key"],
    }


@pytest.mark.asyncio
async def test_saved_provider_verify_unsupported_type_returns_stable_code(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    cfg = _v2_config()
    cfg["providers"]["openai_org"]["type"] = "future_protocol"
    await store.update_team_llm_config("team_llm", cfg)

    response = client.post(
        "/api/v1/admin/llm-config/providers/verify-saved",
        json={"provider_name": "openai_org"},
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "provider_type_unsupported",
    }


@pytest.mark.asyncio
async def test_legacy_provider_add_rejects_missing_v2_provider_with_stable_code(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    await store.update_team_llm_config("team_llm", _v2_config())

    response = client.post(
        "/api/v1/admin/llm-config/providers",
        json={
            "provider_name": "new_provider",
            "api_key": "sk-new-provider-secret-1234567890",
            "role": "lead",
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "provider_not_in_v2_config",
    }


@pytest.mark.asyncio
async def test_reindex_status_not_found_returns_stable_code(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )

    response = client.get(
        "/api/v1/admin/llm-config/kb/reindex/kb_reindex_missing",
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "reindex_task_not_found",
    }


@pytest.mark.asyncio
async def test_reindex_status_hides_other_team_in_memory_task(
    client,
    app,
    store,
):
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    app.state.team_kb_reindex_tasks = {
        "kb_reindex_other": {
            "task_id": "kb_reindex_other",
            "team_id": "team_other",
            "state": "failed",
            "error_code": "reindex_failed",
        }
    }

    response = client.get(
        "/api/v1/admin/llm-config/kb/reindex/kb_reindex_other",
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "reindex_task_not_found",
    }


@pytest.mark.asyncio
async def test_reindex_failed_status_is_localized_and_hides_worker_error(
    client,
    app,
    store,
):
    await _seed_team(store)
    await _seed_user(
        store,
        "u_creator",
        "creator@t.com",
        is_creator=True,
    )
    app.state.team_kb_reindex_tasks = {
        "kb_reindex_failed": {
            "task_id": "kb_reindex_failed",
            "team_id": "team_llm",
            "state": "failed",
            "error_code": "reindex_failed",
            "error": "RAW_WORKER_EXCEPTION_WITH_PROVIDER_SECRET",
        }
    }

    response = client.get(
        "/api/v1/admin/llm-config/kb/reindex/kb_reindex_failed",
        cookies={
            "bowhead_jwt": _jwt_cookie("u_creator"),
            "NEXT_LOCALE": "en-US",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] == {
        "code": "reindex_failed",
        "message": (
            "The knowledge-base index rebuild failed, and the previous "
            "index is still serving. Check the model source, then select "
            "the vector model again and apply."
        ),
    }
    assert "team_id" not in body
    assert "RAW_WORKER_EXCEPTION" not in response.text
    assert "PROVIDER_SECRET" not in response.text


@pytest.mark.asyncio
async def test_full_config_rejects_ssrf_provider_base_url(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    cfg = _v2_config()
    cfg["providers"]["openai_org"]["base_url"] = "http://169.254.169.254/latest/meta-data"

    r = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": cfg},
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert r.status_code == 400
    assert r.json()["detail"] == {"code": "invalid_llm_config"}


@pytest.mark.asyncio
async def test_normal_user_catalog_is_secret_free(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    await _seed_user(store, "u_member", "member@t.com")
    _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    r = client.get(
        "/api/v1/llm/catalog",
        cookies={"bowhead_jwt": _jwt_cookie("u_member")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    payload = json.dumps(body, sort_keys=True)
    assert body["version"] == 1
    assert body["default_model"] == {"provider": "openai_org", "model": "gpt-4.1"}
    assert "sk-openai-team-secret" not in payload
    assert "api_key" not in payload


@pytest.mark.asyncio
async def test_normal_user_catalog_read_failure_never_exposes_deployment_catalog(
    client,
    store,
    monkeypatch,
):
    from bowhead_router.config_loader import LoadedRoutingConfig
    from bowhead_router.registry import ProviderConfig
    from bowhead_router.router import AgentBinding

    await _seed_team(store)
    await _seed_user(store, "u_member", "member@t.com")
    client.app.state.routing_config_loaded = LoadedRoutingConfig(
        providers=[ProviderConfig(name="deployment_provider", type="mock")],
        agent_bindings=[
            AgentBinding(
                agent_type="lead",
                primary=("deployment_provider", "deployment_model"),
            )
        ],
    )

    async def _raise_config_read(_team_id: str):
        raise RuntimeError("team config store unavailable")

    monkeypatch.setattr(
        store,
        "get_team_llm_config_with_version",
        _raise_config_read,
    )

    response = client.get(
        "/api/v1/llm/catalog",
        cookies={"bowhead_jwt": _jwt_cookie("u_member")},
    )

    assert response.status_code == 200
    assert response.json() == {
        "version": 0,
        "providers": [],
        "default_model": None,
        "kb": {},
    }


@pytest.mark.asyncio
async def test_audit_admin_cannot_save_full_v2_config(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    await _seed_user(store, "u_auditor", "billing@t.com")
    await store.promote_team_role(
        user_id="u_auditor",
        team_id="team_llm",
        new_role="audit_admin",
        promoted_by="u_creator",
    )

    r = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": _v2_config()},
        cookies={"bowhead_jwt": _jwt_cookie("u_auditor")},
    )

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_legacy_key_only_config_get_migrates_and_put_preserves_secret(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    jwt = _jwt_cookie("u_creator")
    secret = "sk-deepseek-legacy-secret-1234567890"
    add = client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "deepseek", "api_key": secret, "role": "lead"},
        cookies={"bowhead_jwt": jwt},
    )
    assert add.status_code == 200, add.text

    get_resp = client.get(
        "/api/v1/admin/llm-config",
        cookies={"bowhead_jwt": jwt},
    )
    assert get_resp.status_code == 200, get_resp.text
    body = get_resp.json()
    payload = json.dumps(body, sort_keys=True)
    assert body["migrated_from_legacy"] is True
    assert body["config"]["schema_version"] == 2
    assert body["config"]["providers"]["deepseek"]["type"] == "anthropic_compatible"
    assert body["config"]["agents"]["lead"]["primary"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
    }
    assert secret not in payload
    assert "api_key" not in payload

    put_resp = _apply_config(
        client,
        "/api/v1/admin/llm-config",
        json={"config": body["config"]},
        cookies={"bowhead_jwt": jwt},
    )
    assert put_resp.status_code == 200, put_resp.text
    stored = await store.get_team_llm_config_decrypted("team_llm")
    assert stored["schema_version"] == 2
    assert stored["providers"]["deepseek"]["api_key"] == secret


@pytest.mark.asyncio
async def test_normal_user_catalog_migrates_legacy_key_only_config(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    await _seed_user(store, "u_member", "member@t.com")
    client.post(
        "/api/v1/admin/llm-config/providers",
        json={
            "provider_name": "deepseek",
            "api_key": "sk-deepseek-legacy-secret-1234567890",
            "role": "lead",
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    r = client.get(
        "/api/v1/llm/catalog",
        cookies={"bowhead_jwt": _jwt_cookie("u_member")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    payload = json.dumps(body, sort_keys=True)
    assert body["default_model"] == {"provider": "deepseek", "model": "deepseek-v4-pro"}
    assert body["providers"][0]["id"] == "deepseek"
    assert "sk-deepseek-legacy" not in payload
    assert "api_key" not in payload


@pytest.mark.asyncio
async def test_resume_pending_embed_reindex_restarts_pending_task(store, monkeypatch):
    await _seed_team(store)
    cfg = _v2_config()
    cfg["kb"]["pending_embed"] = {
        "model": "embed_small",
        "target_index_version": "kbidx_next",
        "task_id": "kb_reindex_abc",
    }
    await store.update_team_llm_config("team_llm", cfg)
    from bowhead_gateway.routes import admin_billing as admin_billing_routes

    calls: list[tuple[str, str]] = []

    def fake_start(
        *,
        request: object,
        store: UserStore,
        team_id: str,
        reindex: dict[str, object],
    ) -> None:
        del request, store
        calls.append((team_id, str(reindex["task_id"])))

    monkeypatch.setattr(admin_billing_routes, "_start_embed_reindex_task", fake_start)

    class Req:
        app = type("App", (), {"state": object()})()

    count = await admin_billing_routes.resume_pending_embed_reindexes(Req(), store)

    assert count == 1
    assert calls == [("team_llm", "kb_reindex_abc")]


@pytest.mark.asyncio
async def test_persist_reindex_failure_stores_only_stable_code(store):
    await _seed_team(store)
    cfg = _v2_config()
    cfg["kb"]["pending_embed"] = {
        "model": "embed_small",
        "target_index_version": "kbidx_next",
        "task_id": "kb_reindex_failed",
        "error": "OLD_RAW_WORKER_EXCEPTION",
    }
    await store.update_team_llm_config("team_llm", cfg)
    from bowhead_gateway.routes import admin_billing as admin_billing_routes

    await admin_billing_routes._persist_reindex_failure(
        store=store,
        team_id="team_llm",
        task_id="kb_reindex_failed",
    )

    stored, _version = await store.get_team_llm_config_with_version(
        "team_llm",
    )
    pending = stored["kb"]["pending_embed"]
    assert pending["state"] == "failed"
    assert pending["error_code"] == "reindex_failed"
    assert "error" not in pending


@pytest.mark.asyncio
async def test_embed_reindex_processes_delta_files_before_activation():
    from bowhead_gateway.routes import admin_billing as admin_billing_routes

    cfg = _v2_config()
    cfg["kb"]["active"]["embed"] = "old_embed"
    cfg["kb"]["models"]["old_embed"] = {
        "provider": "openai_org",
        "real_id": "old-embed",
        "capability": "embed",
        "dim": 1536,
    }
    cfg["kb"]["pending_embed"] = {
        "model": "embed_small",
        "target_index_version": "kbidx_next",
        "task_id": "kb_reindex_delta",
    }

    class FakeStore:
        version = 4

        async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict, int]:
            assert team_id == "team_llm"
            return cfg, self.version

        async def update_team_llm_config_if_version(
            self,
            team_id: str,
            config: dict,
            *,
            expected_version: int,
        ) -> bool:
            assert team_id == "team_llm"
            if expected_version != self.version:
                return False
            replacement = json.loads(json.dumps(config))
            cfg.clear()
            cfg.update(replacement)
            self.version += 1
            return True

    class FakeWorker:
        def __init__(self) -> None:
            self.parsed: list[tuple[str, int]] = []

        async def reindex_file_chunks(self, file_id: str, version: int) -> bool:
            self.parsed.append((file_id, version))
            return True

    worker = FakeWorker()

    class FakeRuntimeRegistry:
        def build_runtime_for_config(
            self,
            target_config: dict,
            version: int,
            *,
            router: object | None = None,
        ) -> SimpleNamespace:
            assert target_config["kb"]["index_version"] == "kbidx_next"
            assert version == 4
            assert router is None
            return SimpleNamespace(worker=worker)

    class FakeFilesStore:
        def __init__(self) -> None:
            self.calls = 0

        async def list_active_for_team_all_scopes(self, team_id: str) -> list[SimpleNamespace]:
            assert team_id == "team_llm"
            self.calls += 1
            if self.calls == 1:
                return [SimpleNamespace(file_id="file_a", version=1)]
            return [
                SimpleNamespace(file_id="file_a", version=2),
                SimpleNamespace(file_id="file_b", version=1),
            ]

    class FakeGate:
        @asynccontextmanager
        async def promotion(self, team_id: str):
            assert team_id == "team_llm"
            yield

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                team_kb_runtime_registry=FakeRuntimeRegistry(),
                kb_files_store=FakeFilesStore(),
                team_kb_reindex_tasks={},
                team_kb_ingestion_gate=FakeGate(),
            ),
        ),
    )

    await admin_billing_routes._run_embed_reindex_task(
        request=request,
        store=FakeStore(),  # type: ignore[arg-type]
        team_id="team_llm",
        task_id="kb_reindex_delta",
    )

    assert worker.parsed == [
        ("file_a", 1),
        ("file_a", 2),
        ("file_b", 1),
    ]
    assert "pending_embed" not in cfg["kb"]
    assert cfg["kb"]["active"]["embed"] == "embed_small"
    assert cfg["kb"]["index_version"] == "kbidx_next"


@pytest.mark.asyncio
async def test_stage_embed_reindex_idempotent_when_target_already_pending():
    """WS5: a repeat PUT wanting the SAME embed that's already staged/in-flight is a
    no-op — no second task minted, the in-flight pending (task_id) is preserved, and
    we keep serving the OLD embed (desired/serving separation, not a premature flip)."""
    from bowhead_gateway.routes import admin_billing as ab

    base = _v2_config()
    base["kb"]["models"]["emb_a"] = {"provider": "openai_org", "real_id": "emb-a", "capability": "embed", "dim": 1024}
    base["kb"]["models"]["emb_b"] = {"provider": "openai_org", "real_id": "emb-b", "capability": "embed", "dim": 1024}
    old = json.loads(json.dumps(base))
    old["kb"]["active"]["embed"] = "emb_a"
    old["kb"]["index_version"] = "kbidx_old"
    old["kb"]["pending_embed"] = {"model": "emb_b", "target_index_version": "kbidx_new", "task_id": "kb_reindex_x"}
    new = json.loads(json.dumps(base))
    new["kb"]["active"]["embed"] = "emb_b"  # user still wants B while B is mid-reindex

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    staged, reindex = await ab._stage_embed_reindex_if_needed(
        request=req,
        team_id="team_llm",
        old_config=old,
        new_config=new,
        confirm_embed_reindex=True,
    )
    assert reindex is None  # no new task
    assert staged["kb"]["active"]["embed"] == "emb_a"  # keep serving old
    assert staged["kb"]["pending_embed"]["task_id"] == "kb_reindex_x"  # preserved, not re-minted
    assert staged["kb"]["index_version"] == "kbidx_old"


@pytest.mark.asyncio
async def test_start_embed_reindex_cancels_prior_team_task(monkeypatch):
    """WS5: starting a new reindex cancels the team's prior in-flight task and marks it
    superseded, so a stale reindex can't keep churning / race the new target."""
    import asyncio
    import contextlib

    from bowhead_gateway.routes import admin_billing as ab

    async def _noop(**_kwargs):
        return None

    monkeypatch.setattr(ab, "_run_embed_reindex_task", _noop)

    async def _sleep_forever():
        await asyncio.sleep(60)

    prior = asyncio.create_task(_sleep_forever())
    await asyncio.sleep(0)  # let it start running

    statuses = {"kb_reindex_old": {"task_id": "kb_reindex_old", "team_id": "team_llm", "state": "running", "task": prior}}
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(team_kb_reindex_tasks=statuses)))

    ab._start_embed_reindex_task(
        request=req,
        store=SimpleNamespace(),  # type: ignore[arg-type]
        team_id="team_llm",
        reindex={"task_id": "kb_reindex_new", "target_index_version": "kbidx_new", "total_files": 3},
    )
    assert statuses["kb_reindex_old"]["state"] == "superseded"
    with contextlib.suppress(asyncio.CancelledError):
        await prior
    assert prior.cancelled()
    new_task = statuses["kb_reindex_new"]["task"]
    assert new_task is not None
    with contextlib.suppress(Exception):  # drain the (no-op) new task regardless
        await new_task


async def test_pending_risk_acceptance_does_not_make_the_config_invalid(
    client,
    store,
    monkeypatch,
):
    """「等着用户确认风险」不是「配置不合法」。

    116 2026-08-15 实测的死锁：`validate` 把 `valid` 与 `may_apply` 相与，而
    `may_apply = not blocking and not required_acceptances`。于是只要存在一条待认领，
    `valid` 就是 False —— 前端应用按钮要求 `valid` 为真，**勾满所有确认框也永远点不亮**，
    返回体还是 `valid: false` + `issues: []`（说不合法却零条理由）。连一字未改的原配置也这样。

    为什么全套测试没逮到：表达式里还 OR 了 `migration_eligible`，而夹具通常正处在
    「刚进 v2、尚无迁移基线」的窗口，那个 OR 把问题兜住了；116 上基线早已存在才露出来。
    所以本条**显式跳过不在目标形态的夹具**，而不是假装覆盖到了。
    """
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    await store.update_team_llm_config("team_llm", _tier_v2_config())
    jwt = _jwt_cookie("u_creator")

    loaded = client.get("/api/v1/admin/llm-config", cookies={"bowhead_jwt": jwt})
    assert loaded.status_code == 200, loaded.text
    draft = loaded.json()["config"]
    # Add a declared model without any verification evidence and assign judge, taking draft outside migration grandfather
    # window; assignment becomes `capability_unknown` = **awaiting claim** (not a hard block).
    # Exactly the 116 shape, the sole shape under test.
    draft["providers"]["main"]["models"].append({"id": "gpt-judge", "capabilities": ["chat", "tool"]})
    draft["tiers"]["judge"] = {"provider": "main", "model": "gpt-judge"}

    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": draft, "expected_version": loaded.json()["version"]},
        cookies={"bowhead_jwt": jwt},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    readiness = body.get("readiness")
    assert readiness is not None, "没跑到 readiness 分支，夹具不对"

    if not readiness["required_acceptances"] or readiness["blocking"]:
        pytest.skip(
            f"夹具不在『有待认领、无阻断』形态: acceptances={len(readiness['required_acceptances'])} blocking={len(readiness['blocking'])}",
        )

    assert body["valid"] is True, "待认领被当成了『配置不合法』——前端应用按钮要求 valid，用户勾满确认框也点不亮，且 issues 为空，连原因都不给。"
    assert body["issues"] == []


async def test_apply_honors_stored_acceptances_no_409_death_loop(
    client,
    store,
    monkeypatch,
):
    """存量认领必须在 apply 重算时依然生效——否则确定性 409 死循环。

    116 2026-08-26 实测：check 面（validate/readiness）把历史已认领的行算
    `risk_accepted`，apply 面重算成 `risk_acceptance_required` ⇒ 语义哈希必不等 ⇒
    每次「载入最新配置并重新检查」都回到同一个 409。根因 = apply 服务内联拼的
    认领键是 6 元组、`acceptance_key_for` 是 7 元组（含 risk_code）——第三份实现
    没跟上 91d512b8f 把 risk_code 补进匹配键的那次改动。这也是 08-24 客户报
    「加模型后应用 409」的第二根因（4fd56ea80 只修了 evidence_id 那一半）。
    """
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    await store.update_team_llm_config("team_llm", _tier_v2_config())
    jwt = _jwt_cookie("u_creator")

    loaded = client.get("/api/v1/admin/llm-config", cookies={"bowhead_jwt": jwt})
    assert loaded.status_code == 200, loaded.text
    draft = loaded.json()["config"]
    version = loaded.json()["version"]
    # Create an awaiting-claim row: declared model with zero evidence assigned judge (same as 116).
    draft["providers"]["main"]["models"].append({"id": "gpt-judge", "capabilities": ["chat", "tool"]})
    draft["tiers"]["judge"] = {"provider": "main", "model": "gpt-judge"}

    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": draft, "expected_version": version},
        cookies={"bowhead_jwt": jwt},
    )
    assert preview.status_code == 200, preview.text
    readiness = preview.json()["readiness"]
    if not readiness["required_acceptances"] or readiness["blocking"]:
        pytest.skip(
            f"夹具不在『有待认领、无阻断』形态: acceptances={len(readiness['required_acceptances'])} blocking={len(readiness['blocking'])}",
        )

    tokens = [row["acceptance_token"] for row in readiness["required_acceptances"]]
    migration = readiness.get("migration_acceptance")
    first_apply = client.put(
        "/api/v1/admin/llm-config",
        json={
            "config": draft,
            "expected_version": version,
            "readiness": {
                "draft_semantic_hash": readiness["draft_semantic_hash"],
                "acceptance_tokens": tokens,
                **({"migration_acceptance_token": migration["acceptance_token"]} if migration else {}),
            },
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert first_apply.status_code == 200, first_apply.text
    version2 = first_apply.json()["version"]

    # ── Second round: edit configuration again (add an unassigned model, exactly the 116 dogfood shape).
    # check counts claimed judge row as risk_accepted; apply recomputation must agree (this case's criterion).
    loaded2 = client.get("/api/v1/admin/llm-config", cookies={"bowhead_jwt": jwt})
    draft2 = loaded2.json()["config"]
    draft2["providers"]["main"]["models"].append({"id": "gpt-extra", "capabilities": ["chat"]})

    preview2 = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": draft2, "expected_version": version2},
        cookies={"bowhead_jwt": jwt},
    )
    assert preview2.status_code == 200, preview2.text
    readiness2 = preview2.json()["readiness"]
    accepted_rows = [row for row in readiness2["rows"] if row["status"] == "risk_accepted"]
    if not accepted_rows:
        pytest.skip("夹具没有产生 risk_accepted 行，测不到存量认领对齐")

    second_apply = client.put(
        "/api/v1/admin/llm-config",
        json={
            "config": draft2,
            "expected_version": version2,
            "readiness": {
                "draft_semantic_hash": readiness2["draft_semantic_hash"],
                "acceptance_tokens": [row["acceptance_token"] for row in readiness2["required_acceptances"]],
            },
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert second_apply.status_code == 200, f"存量认领在 apply 重算时隐形 ⇒ 409 死循环（readiness_review_stale）: {second_apply.text}"


async def test_real_blocking_still_makes_the_config_invalid(
    client,
    store,
    monkeypatch,
):
    """负对照：真正的硬阻断仍须让 valid 为假——本次只放开「待认领」那一档。"""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    await store.update_team_llm_config("team_llm", _tier_v2_config())
    jwt = _jwt_cookie("u_creator")

    saved = client.get("/api/v1/admin/llm-config", cookies={"bowhead_jwt": jwt})
    draft = saved.json()["config"]
    first_provider = next(iter(draft["providers"]))
    draft.setdefault("tiers", {})["primary"] = {
        "provider": first_provider,
        "model": "model-that-is-not-declared-anywhere",
    }

    preview = client.post(
        "/api/v1/admin/llm-config/validate",
        json={"config": draft, "expected_version": saved.json()["version"]},
        cookies={"bowhead_jwt": jwt},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    readiness = body.get("readiness")
    if readiness is None or not readiness["blocking"]:
        pytest.skip("夹具未产生硬阻断，无法做负对照")

    assert body["valid"] is False


# ── Outbound parameters: runtime automatic-adaptation read interface ────────
#
# Settings' '已自动适配' state reads this. **Observation**, not configuration; page explains it and offers '固化为配置',
# without writing configuration for users. Design: docs/sprints/llm-outbound-params-2026-08/tech-design.md §3.5.


async def _seed_config_with_provider(store: UserStore, config: dict) -> None:
    await _seed_team(store)
    await _seed_user(store, "u_admin_out", "outbound@t.com", is_creator=True)
    await store.update_team_llm_config("team_llm", config)


def _sample_config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "sample-genai": {
                "type": "openai_compatible",
                "base_url": "https://provider.example.com/v1",
                "api_key": "k",
                "models": [{"id": "openai_gpt56_luna", "capabilities": ["chat", "tool"]}],
            }
        },
        "tiers": {"primary": {"provider": "sample-genai", "model": "openai_gpt56_luna"}},
    }


@pytest.mark.asyncio
async def test_outbound_adaptations_reports_what_the_runtime_learned(
    client: TestClient,
    store: UserStore,
) -> None:
    from bowhead_router.outbound import learn_constraint, reset_learned_for_test

    reset_learned_for_test()
    await _seed_config_with_provider(store, _sample_config())
    learn_constraint(
        adapter_type="openai_compatible",
        base_url="https://provider.example.com/v1",
        model="openai_gpt56_luna",
        param="temperature",
    )
    try:
        response = client.get(
            "/api/v1/admin/llm-config/outbound-adaptations",
            cookies={"bowhead_jwt": _jwt_cookie("u_admin_out")},
        )
    finally:
        reset_learned_for_test()

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["provider"] == "sample-genai"
    assert items[0]["model_id"] == "openai_gpt56_luna"
    assert items[0]["param"] == "temperature"
    assert items[0]["since"].endswith("+00:00")
    # Parameter name/time only; no addresses, credentials or raw provider error text.
    body = response.text
    assert "provider.example.com" not in body
    assert "api_key" not in body and '"k"' not in body


@pytest.mark.asyncio
async def test_outbound_adaptations_is_empty_when_nothing_was_learned(
    client: TestClient,
    store: UserStore,
) -> None:
    """★ 常态是空 —— 页面因此默认安静（不能一上来就长得像出了事）。"""
    from bowhead_router.outbound import reset_learned_for_test

    reset_learned_for_test()
    await _seed_config_with_provider(store, _sample_config())

    response = client.get(
        "/api/v1/admin/llm-config/outbound-adaptations",
        cookies={"bowhead_jwt": _jwt_cookie("u_admin_out")},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_outbound_adaptations_never_shows_another_gateway_with_the_same_name(
    client: TestClient,
    store: UserStore,
) -> None:
    """Keep learned constraints isolated by transport identity across teams.

    Identically named providers can point to different gateways. This team's
    interface must not expose constraints learned for another gateway.
    """
    from bowhead_router.outbound import learn_constraint, reset_learned_for_test

    reset_learned_for_test()
    await _seed_config_with_provider(store, _sample_config())
    # Same name/model, but constraint learned from **another gateway**.
    learn_constraint(
        adapter_type="openai_compatible",
        base_url="https://some-other-gateway.example.com/v1",
        model="openai_gpt56_luna",
        param="temperature",
    )
    try:
        response = client.get(
            "/api/v1/admin/llm-config/outbound-adaptations",
            cookies={"bowhead_jwt": _jwt_cookie("u_admin_out")},
        )
    finally:
        reset_learned_for_test()

    assert response.json()["items"] == []


def test_outbound_adaptations_requires_team_admin(client: TestClient) -> None:
    assert client.get("/api/v1/admin/llm-config/outbound-adaptations").status_code == 401


@pytest.mark.parametrize("mode", ["local", "saas"])
@pytest.mark.asyncio
async def test_config_read_reports_deployment_mode(client, store, monkeypatch, mode):
    """地址提示语要跟着部署模式走，所以模式必须随配置一起下发。

    ★ 前端拿不到它时**只能闭嘴**（见 models.tsx 的地址 hint）——写「内网也可以」在 saas
    上是假话，写「只收公网」在私有化部署上也是假话。所以这个字段缺失不是小事：
    它一缺，那句提示就永远不出现，用户永远不知道这台部署到底收什么地址。
    """

    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    client.app.state.deployment_mode = mode

    resp = client.get(
        "/api/v1/admin/llm-config",
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["features"]["deployment_mode"] == mode


@pytest.mark.asyncio
async def test_config_read_defaults_to_saas_when_unset(client, store, monkeypatch):
    """负对照：模式没设时按 `saas` 报（fail-closed，别默认宣称宽松）。"""

    monkeypatch.setenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    if hasattr(client.app.state, "deployment_mode"):
        delattr(client.app.state, "deployment_mode")

    resp = client.get(
        "/api/v1/admin/llm-config",
        cookies={"bowhead_jwt": _jwt_cookie("u_creator")},
    )

    assert resp.json()["features"]["deployment_mode"] == "saas"


@pytest.mark.asyncio
async def test_sections_save_connection_without_production_assignment(client, store, monkeypatch):
    """The first connection survives save/read; strict runtime has no primary."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@t.com", is_creator=True)
    config = {
        "schema_version": 2,
        "providers": {
            "corp": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "section-test-key-123456789",
                "models": [{"id": "unused", "capabilities": ["chat"]}],
            }
        },
        "tiers": {},
        "kb": {"models": {}, "active": {}},
    }
    # Read enrolls the saved identities, exactly as entering the overview does.
    await store.update_team_llm_config("team_llm", config)
    cookies = {"bowhead_jwt": _jwt_cookie("u_creator")}
    read = client.get("/api/v1/admin/llm-config", cookies=cookies)
    assert read.status_code == 200, read.text
    entries = read.json()["verification"]["entries"]
    assert [(row["model_id"], row["capability"], row["fact_state"]) for row in entries] == [("unused", "chat", "unknown")]
    config["providers"]["corp"]["base_url"] = "https://93.184.216.35/v1"
    version = read.json()["version"]
    review = client.post("/api/v1/admin/llm-config/validate", json={"config": config, "expected_version": version}, cookies=cookies)
    assert review.status_code == 200, review.text
    assert review.json()["valid"] is True
    assert review.json()["readiness"]["rows"] == []
    saved = client.put(
        "/api/v1/admin/llm-config",
        json={
            "config": config,
            "expected_version": version,
            "readiness": {"draft_semantic_hash": review.json()["readiness"]["draft_semantic_hash"], "acceptance_tokens": []},
        },
        cookies=cookies,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["config"]["tiers"] == {}
    assert "section-test-key-123456789" not in json.dumps(saved.json())


@pytest.mark.asyncio
async def test_failed_index_can_retry_same_model_after_confirmation():
    """A failed rebuild may target the currently selected ID after a transport edit.

    Mutation check: restoring the old equal-signature early return rejects this retry.
    """
    from bowhead_gateway.routes import admin_billing as ab

    config = _v2_config()
    model = config["kb"]["active"]["embed"]
    config["kb"]["index_version"] = "kbidx_serving"
    config["kb"]["pending_embed"] = {"model": model, "task_id": "kb_reindex_failed", "state": "failed"}

    class Files:
        async def list_active_for_team_all_scopes(self, team_id):
            return [{"id": "file_1"}]

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(kb_files_store=Files())))
    unchanged, task = await ab._stage_embed_reindex_if_needed(
        request=request, team_id="team_llm", old_config=config, new_config=config, confirm_embed_reindex=False
    )
    assert task is None
    assert unchanged["kb"]["pending_embed"]["task_id"] == "kb_reindex_failed"
    staged, task = await ab._stage_embed_reindex_if_needed(
        request=request, team_id="team_llm", old_config=config, new_config=config, confirm_embed_reindex=True
    )
    assert task is not None
    assert task["total_files"] == 1
    assert task["task_id"] != "kb_reindex_failed"
    assert staged["kb"]["index_version"] == "kbidx_serving"
    assert staged["kb"]["active"]["embed"] == model


def _apply_config(client, url, *, json, cookies):
    """Exercise supported validate → confirm readiness → apply for business tests."""
    if json.get("expected_version") is not None:
        loaded = client.get(url, cookies=cookies)
        if loaded.status_code == 200 and loaded.json()["version"] != json["expected_version"]:
            return client.put(url, json=json, cookies=cookies)
    import copy

    json = copy.deepcopy(json)
    for runtime_key, provider in json["config"].get("providers", {}).items():
        if not isinstance(provider, dict) or provider.get("provider_id"):
            continue
        declarations = [model if isinstance(model, dict) else {"id": model, "capabilities": ["chat", "tool"]} for model in provider.get("models", [])]
        for embed in json["config"].get("kb", {}).get("models", {}).values():
            if (
                isinstance(embed, dict)
                and embed.get("provider") == runtime_key
                and not any(model["id"] == embed.get("model", embed.get("real_id")) for model in declarations)
            ):
                declarations.append({"id": embed.get("model", embed.get("real_id")), "capabilities": ["embed"]})
        provider["models"] = declarations
        precheck = client.post(
            url + "/providers/precheck",
            json={
                **provider,
                "runtime_key": runtime_key,
                "display_name": runtime_key,
            },
            cookies=cookies,
        )
        if precheck.status_code == 200:
            provider["provider_id"] = precheck.json()["provider_id"]
    preview = client.post(url + "/validate", json={"config": json["config"]}, cookies=cookies)
    body = preview.json() if preview.status_code == 200 else {}
    readiness = body.get("readiness")
    if not readiness:
        return client.put(url, json=json, cookies=cookies)
    migration = readiness.get("migration_acceptance")
    request = {
        **json,
        "expected_version": json.get("expected_version", body["version"]),
        "readiness": {
            "draft_semantic_hash": readiness["draft_semantic_hash"],
            "acceptance_tokens": [row["acceptance_token"] for row in readiness["required_acceptances"]],
            **({"migration_acceptance_token": migration["acceptance_token"]} if migration else {}),
        },
    }
    return client.put(url, json=request, cookies=cookies)


@pytest.mark.asyncio
async def test_retry_setting_saves_without_review_and_survives_full_save(client, store):
    from bowhead_core.team_llm_config import build_router_from_v2_config

    await _seed_team(store)
    await _seed_user(store, "u_retry", "retry@t.com", is_creator=True)
    cookies = {"bowhead_jwt": _jwt_cookie("u_retry")}
    config = _tier_v2_config()
    config["providers"]["main"]["base_url"] = "https://93.184.216.34/v1"
    assert _apply_config(client, "/api/v1/admin/llm-config", json={"config": config, "expected_version": 0}, cookies=cookies).status_code == 200

    stale = client.put("/api/v1/admin/llm-config/retry", json={"max_retries": 1, "expected_version": 0}, cookies=cookies)
    assert stale.status_code == 409
    invalid = client.put("/api/v1/admin/llm-config/retry", json={"max_retries": 6, "expected_version": 1}, cookies=cookies)
    assert invalid.status_code == 400 and invalid.json()["detail"]["code"] == "invalid_llm_retry"
    saved = client.put("/api/v1/admin/llm-config/retry", json={"max_retries": 1, "expected_version": 1}, cookies=cookies)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"ok": True, "version": 2, "retry": {"max_retries": 1}}
    loaded = client.get("/api/v1/admin/llm-config", cookies=cookies).json()
    assert loaded["config"]["retry"] == {"max_retries": 1}

    # A full model save from a page that does not send the setting keeps it.
    without_retry = {key: value for key, value in loaded["config"].items() if key != "retry"}
    without_retry["providers"]["main"]["display_name"] = "Renamed service"
    response = _apply_config(client, "/api/v1/admin/llm-config", json={"config": without_retry, "expected_version": 2}, cookies=cookies)
    assert response.status_code == 200, response.text
    stored = await store.get_team_llm_config_decrypted("team_llm")
    assert stored["retry"] == {"max_retries": 1}
    assert build_router_from_v2_config(stored).max_retries == 1


@pytest.mark.asyncio
async def test_context_window_catalog_returns_reviewed_windows_and_the_default(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_config_with_provider(store, _sample_config())

    response = client.get(
        "/api/v1/admin/llm-config/context-window-catalog",
        cookies={"bowhead_jwt": _jwt_cookie("u_admin_out")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == 131_072
    assert body["models"]["deepseek-flash"] == 1_000_000
    assert body["models"]["qwen3.7-plus-us"] == 991_808
