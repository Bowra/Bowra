from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lark_oapi.ws.client as lark_ws_client
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
from bowhead_core.events import OrchestratorEvent, get_event_registry
from bowhead_core.public_entries import PublicEntryStore, normalize_public_base_url
from bowhead_core.session_lifecycle import SessionLifecycleError
from bowhead_core.types import IntentType, TurnRequest, TurnResult, TurnStatus
from bowhead_core.user_store import UserStore
from bowhead_gateway.feishu import renderer
from bowhead_gateway.feishu.bridge import FeishuTurnBridge, extract_text, parse_message_event
from bowhead_gateway.feishu.config import (
    DbBackedFeishuAppConfigResolver,
    EnvFeishuAppConfigResolver,
)
from bowhead_gateway.feishu.long_connection import (
    FeishuCardAwareWSClient,
    FeishuLongConnectionWorker,
    _install_sdk_thread_event_loop,
    _restore_sdk_event_loop,
)
from bowhead_gateway.feishu.qr import QrPayloadTooLongError, render_setup_qr_svg
from bowhead_gateway.feishu.security import (
    FeishuSecurityConfig,
    FeishuSecurityError,
    parse_callback_body,
)
from bowhead_gateway.feishu.store import FeishuEventStore
from bowhead_gateway.main import _load_dotenv_into_process
from bowhead_gateway.member_resolver import MemberUidResolver
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.routes import feishu as feishu_routes
from bowhead_gateway.settings import Settings
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lark_oapi.ws.const import (
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.enum import FrameType, MessageType
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from bowhead_core import OrderedTurnLogSink, SessionStore, TurnLogStore
from bowhead_router import AgentBinding, ProviderConfig

REAL_FEISHU_WEBHOOK_FIXTURE = Path(__file__).parent / "fixtures" / "feishu" / "encrypted_webhook_real.json"


@pytest.fixture(autouse=True)
def business_copy_identity(monkeypatch):
    """Pin historical display copy without changing the installed adapters."""
    from bowhead_gateway import product_identity

    monkeypatch.setattr(product_identity, "read_build_edition", lambda: "business")


class _LegacySurfaceProtocolStore:
    def __init__(self, *, version: int | None = None, error=None) -> None:
        self.version = version
        self.error = error

    async def read_persisted_turn_protocol_binding(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return "feishu:tenant_1:oc_chat:ou_user", self.version


class _NoopSessionLifecycleCoordinator:
    async def revive(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(changed=False)


def test_feishu_security_verifies_challenge_token() -> None:
    body = {"type": "url_verification", "token": "tok", "challenge": "abc"}
    parsed = parse_callback_body(
        raw_body=json.dumps(body).encode(),
        headers={},
        config=FeishuSecurityConfig(verification_token="tok"),
    )
    assert parsed["challenge"] == "abc"

    with pytest.raises(FeishuSecurityError):
        parse_callback_body(
            raw_body=json.dumps({**body, "token": "bad"}).encode(),
            headers={},
            config=FeishuSecurityConfig(verification_token="tok"),
        )


def test_feishu_security_decrypts_encrypted_callback_with_signature() -> None:
    encrypt_key = "encrypt_key_for_test"
    payload = {
        "schema": "2.0",
        "header": {
            "token": "tok",
            "event_id": "evt_encrypted",
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant_1",
        },
        "event": {
            "message": {
                "message_id": "om_encrypted",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    raw_body, headers = _encrypted_feishu_body(payload, encrypt_key)

    parsed = parse_callback_body(
        raw_body=raw_body,
        headers=headers,
        config=FeishuSecurityConfig(verification_token="tok", encrypt_key=encrypt_key),
    )

    assert parsed["header"]["event_id"] == "evt_encrypted"
    assert parsed["event"]["message"]["message_id"] == "om_encrypted"

    with pytest.raises(FeishuSecurityError):
        parse_callback_body(
            raw_body=raw_body,
            headers={**headers, "x-lark-signature": "bad"},
            config=FeishuSecurityConfig(verification_token="tok", encrypt_key=encrypt_key),
        )

    with pytest.raises(FeishuSecurityError):
        parse_callback_body(
            raw_body=raw_body,
            headers={**headers, "x-lark-signature": "坏签名"},
            config=FeishuSecurityConfig(verification_token="tok", encrypt_key=encrypt_key),
        )


def test_feishu_security_non_ascii_token_returns_security_error() -> None:
    with pytest.raises(FeishuSecurityError):
        parse_callback_body(
            raw_body=json.dumps(
                {
                    "type": "url_verification",
                    "token": "坏token",
                    "challenge": "abc",
                },
                ensure_ascii=False,
            ).encode(),
            headers={},
            config=FeishuSecurityConfig(verification_token="tok"),
        )


def test_feishu_security_real_encrypted_webhook_fixture_if_available() -> None:
    fixture_path = Path(
        os.environ.get("BOWHEAD_REAL_FEISHU_WEBHOOK_FIXTURE", str(REAL_FEISHU_WEBHOOK_FIXTURE)),
    )
    require_real = os.environ.get("BOWHEAD_REQUIRE_REAL_FEISHU_WEBHOOK_FIXTURE") == "1"
    if not fixture_path.exists():
        if require_real:
            pytest.fail(f"missing real Feishu encrypted webhook fixture: {fixture_path}")
        pytest.skip("real Feishu encrypted webhook fixture not captured yet")

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if require_real and fixture.get("derived_from_real_feishu_callback") is not True:
        pytest.fail("fixture must be marked derived_from_real_feishu_callback=true")
    raw_body = _fixture_raw_body(fixture)
    headers = {str(k).lower(): str(v) for k, v in fixture.get("headers", {}).items()}
    config = FeishuSecurityConfig(
        verification_token=str(fixture.get("verification_token") or ""),
        encrypt_key=str(fixture.get("encrypt_key") or ""),
    )

    parsed = parse_callback_body(raw_body=raw_body, headers=headers, config=config)
    expected = fixture.get("expected") or {}
    if expected.get("event_type"):
        assert parsed["header"]["event_type"] == expected["event_type"]
    if expected.get("message_type"):
        assert parsed["event"]["message"]["message_type"] == expected["message_type"]
    if expected.get("challenge"):
        assert parsed["challenge"] == expected["challenge"]

    with pytest.raises(FeishuSecurityError):
        parse_callback_body(
            raw_body=raw_body,
            headers={**headers, "x-lark-signature": "bad"},
            config=config,
        )


def test_feishu_webhook_encrypted_challenge_route_returns_challenge() -> None:
    encrypt_key = "encrypt_key_for_test"
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_verification_token="tok",
            feishu_encrypt_key=encrypt_key,
            feishu_event_ingress="webhook",
        )
    )
    client = TestClient(app)
    raw_body, headers = _encrypted_feishu_body(
        {"type": "url_verification", "token": "tok", "challenge": "abc"},
        encrypt_key,
    )

    resp = client.post(
        "/api/v1/integrations/feishu/events",
        content=raw_body,
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc"}


def test_feishu_webhook_encrypted_event_rejects_missing_signature() -> None:
    encrypt_key = "encrypt_key_for_test"
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_verification_token="tok",
            feishu_encrypt_key=encrypt_key,
            feishu_event_ingress="webhook",
        )
    )
    client = TestClient(app)
    raw_body, headers = _encrypted_feishu_body(_message_event(), encrypt_key)

    resp = client.post(
        "/api/v1/integrations/feishu/events",
        content=raw_body,
        headers={key: value for key, value in headers.items() if key != "x-lark-signature"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "feishu_security_error"
    assert "missing Feishu signature headers" in resp.json()["detail"]["error"]["message"]


def test_feishu_extract_text_keeps_words_matching_mention_display_name() -> None:
    text = extract_text(
        {
            "content": json.dumps(
                {"text": '<at id="bot">data</at> 看下 data 域收入'},
                ensure_ascii=False,
            ),
            "mentions": [{"key": '<at id="bot">data</at>', "name": "data"}],
        }
    )

    assert text == "看下 data 域收入"


def test_feishu_settings_and_app_config_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FEISHU_GROUP_PUBLIC_ANSWERS_ENABLED", "true")
    settings = Settings(
        feishu_enabled=True,
        feishu_app_id="cli_app",
        feishu_app_secret="cli_secret",
    )
    resolver = EnvFeishuAppConfigResolver(settings)
    config = resolver.resolve("tenant_1")

    assert settings.feishu_group_public_answers_enabled is True
    assert config.enabled is True
    assert config.app_type == "internal"
    assert config.group_public_answers_enabled is True
    assert config.token_cache_key("tenant_1") == ("cli_app", "tenant_1", "internal")


def test_gateway_loads_dotenv_for_non_settings_provider_env(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Log:
        def info(self, *args: Any, **kwargs: Any) -> None:
            pass

        def warning(self, *args: Any, **kwargs: Any) -> None:
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8")

    _load_dotenv_into_process(_Log())  # type: ignore[arg-type]

    assert os.environ["DEEPSEEK_API_KEY"] == "from-dotenv"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "preexisting")
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8")

    _load_dotenv_into_process(_Log())  # type: ignore[arg-type]

    assert os.environ["DEEPSEEK_API_KEY"] == "preexisting"


def test_feishu_public_health_is_minimal_when_disabled() -> None:
    app = _feishu_route_app(Settings(feishu_enabled=False))
    client = TestClient(app)

    resp = client.get("/api/v1/integrations/feishu/health")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "enabled": False, "degraded": False}


def test_feishu_public_health_does_not_require_public_entry_context() -> None:
    app = FastAPI()
    app.state.settings = Settings(feishu_enabled=False)
    app.include_router(feishu_routes.router, prefix="/api/v1/integrations/feishu")
    client = TestClient(app)

    resp = client.get("/api/v1/integrations/feishu/health")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "enabled": False, "degraded": False}


def test_feishu_disabled_diagnostics_still_reports_missing_credentials() -> None:
    app = _feishu_route_app(Settings(feishu_enabled=False))
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    resp = client.get("/api/v1/integrations/feishu/admin/diagnostics")

    assert resp.status_code == 200
    diagnostics = resp.json()
    assert diagnostics["ok"] is True
    assert diagnostics["enabled"] is False
    assert diagnostics["degraded"] is False
    assert diagnostics["missing_config"] == []
    assert diagnostics["missing_credentials"] == [
        "app_id",
        "app_secret",
        "verification_token",
        "bot_open_id",
    ]


def test_feishu_admin_config_rejects_disabled_event_ingress() -> None:
    app = _feishu_route_app(Settings(feishu_enabled=False))
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/integrations/feishu/admin/config",
        json={"event_ingress": "disabled"},
    )

    assert resp.status_code == 422


def test_feishu_disabled_health_degrades_when_stale_long_connection_restart_is_pending() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=False,
            feishu_event_ingress="disabled",
        )
    )
    app.state.feishu_event_store = object()
    app.state.feishu_long_connection_runtime = {
        "worker_signature": "1|long_connection|cli_app|deadbeefcafe",
        "restart_required": True,
        "message": "Long-connection settings changed. Restart the gateway/worker before claiming production readiness.",
        "changed_fields": ["enabled"],
    }
    client = TestClient(app)

    resp = client.get("/api/v1/integrations/feishu/health")

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "enabled": False, "degraded": True}


def test_feishu_event_callback_fails_closed_when_disabled_or_unconfigured() -> None:
    disabled = TestClient(_feishu_route_app(Settings(feishu_enabled=False)))
    resp = disabled.post("/api/v1/integrations/feishu/events", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"]["code"] == "feishu_disabled_or_unconfigured"

    unconfigured = TestClient(
        _feishu_route_app(
            Settings(
                feishu_enabled=True,
                feishu_app_id="cli_app",
                feishu_app_secret="cli_secret",
                feishu_event_ingress="webhook",
            )
        )
    )
    resp = unconfigured.post("/api/v1/integrations/feishu/events", json={})
    assert resp.status_code == 503
    assert "verification token" in resp.json()["detail"]["error"]["message"]


def test_feishu_long_connection_ingress_does_not_require_webhook_token() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_bot_open_id="ou_bot",
            feishu_event_ingress="long_connection",
        )
    )
    app.state.feishu_event_store = object()
    app.state.feishu_long_connection_worker = _FakeRunningLongConnectionWorker()
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    diag = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()
    assert diag["degraded"] is False
    assert diag["event_ingress"] == "long_connection"
    assert diag["auth_mode"] == "dev"
    assert diag["prod_auth_mode"] is False
    assert "verification_token" not in diag["missing_config"]
    checklist = {item["key"]: item for item in diag["setup_checklist"]}
    assert checklist["prod_auth_mode"]["status"] == "degraded"

    resp = client.post("/api/v1/integrations/feishu/events", json={})
    assert resp.status_code == 503
    assert "current ingress is long_connection" in resp.json()["detail"]["error"]["message"]


def test_feishu_long_connection_health_degrades_without_worker() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_bot_open_id="ou_bot",
            feishu_event_ingress="long_connection",
        )
    )
    app.state.feishu_event_store = object()
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    diag = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()
    public = client.get("/api/v1/integrations/feishu/health").json()

    assert diag["degraded"] is True
    assert "long_connection_worker" in diag["missing_config"]
    assert public == {"ok": False, "enabled": True, "degraded": True}


def test_feishu_admin_diagnostics_reports_missing_llm_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_LLM_KEY", raising=False)
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_bot_open_id="ou_bot",
            feishu_event_ingress="long_connection",
        )
    )
    app.state.feishu_event_store = object()
    app.state.feishu_long_connection_worker = _FakeRunningLongConnectionWorker()
    app.state.routing_config_loaded = SimpleNamespace(
        providers=[
            ProviderConfig(
                name="missing_provider",
                type="anthropic_compatible",
                api_key_ref="env://MISSING_LLM_KEY",
            ),
        ],
        agent_bindings=[
            AgentBinding(
                agent_type="lead",
                primary=("missing_provider", "model"),
            ),
        ],
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    diag = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()

    assert diag["degraded"] is True
    assert "llm_provider_secret:missing_provider" in diag["missing_config"]
    assert diag["missing_llm_provider_secrets"] == ["missing_provider"]
    assert diag["llm_provider_secrets"] == [
        {
            "provider": "missing_provider",
            "type": "anthropic_compatible",
            "secret_ref": "env://MISSING_LLM_KEY",
            "configured": False,
        }
    ]


def test_feishu_long_connection_starts_after_runtime_dependencies() -> None:
    source = (Path(__file__).resolve().parents[1] / "src/bowhead_gateway/main.py").read_text(encoding="utf-8")
    start_idx = source.index("feishu_long_connection_worker.start()")

    for marker in (
        "app.state.user_store = user_store",
        # Entitlements are installed by the edition composition step.
        "await initialize_entitlement_services(app, user_store, _deployment_mode)",
        "app.state.team_router_registry = _team_router_registry",
        "app.state.tool_auth_registry = tool_auth_registry",
        "app.state.member_resolver = MemberUidResolver()",
    ):
        assert source.index(marker) < start_idx


def test_feishu_sdk_loop_is_installed_in_worker_thread() -> None:
    result: dict[str, bool] = {}

    def _run() -> None:
        previous = lark_ws_client.loop
        sdk_loop, restored_previous = _install_sdk_thread_event_loop()
        try:
            result["installed"] = sdk_loop is not None and lark_ws_client.loop is sdk_loop
            result["previous_returned"] = restored_previous is previous
            result["thread_loop"] = sdk_loop is asyncio.get_event_loop()
        finally:
            _restore_sdk_event_loop(sdk_loop, restored_previous)
        result["restored"] = lark_ws_client.loop is previous
        result["closed"] = bool(sdk_loop and sdk_loop.is_closed())

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=2)

    assert result == {
        "installed": True,
        "previous_returned": True,
        "thread_loop": True,
        "restored": True,
        "closed": True,
    }


def test_feishu_admin_diagnostics_exposes_setup_details() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_event_ingress="webhook",
        )
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    resp = client.get("/api/v1/integrations/feishu/admin/diagnostics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["degraded"] is True
    assert "verification_token" in data["missing_config"]
    assert data["event_subscription_url"].endswith("/api/v1/integrations/feishu/events")
    assert "im.message.receive_v1" in data["required_event_subscriptions"][0]
    assert any("im:message:send_as_bot" in item for item in data["required_permissions"])
    checklist = {item["key"]: item for item in data["setup_checklist"]}
    assert checklist["app_credentials"]["status"] == "ok"
    assert checklist["prod_auth_mode"]["status"] == "degraded"
    assert checklist["webhook_security"]["status"] == "missing"
    assert checklist["card_callback_routing"]["status"] == "manual"
    assert checklist["contact_email_permission"]["status"] == "manual"
    launch_record = data["launch_record_template"]
    assert "Bowhead x Feishu Launch Record" in launch_record
    assert "cli_***" in launch_record
    assert "cli_secret" not in launch_record
    assert "Gateway auth mode" in launch_record
    assert "Smoke Tests" in launch_record


def test_feishu_diagnostics_callback_urls_do_not_trust_request_host() -> None:
    # A bare request Host is not a public URL authority.
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_event_ingress="webhook",
        )
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app, base_url="http://bowhead.acme.test")

    data = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()

    assert data["event_subscription_url"] == ("https://bowhead.example.com/api/v1/integrations/feishu/events")
    assert data["card_callback_url"] == ("https://bowhead.example.com/api/v1/integrations/feishu/card-callback")
    assert "bowhead.acme.test" not in data["event_subscription_url"]


def test_feishu_diagnostics_callback_urls_use_resolved_public_entry() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_event_ingress="webhook",
        )
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app, base_url="http://internal-host")

    data = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()

    assert data["event_subscription_url"] == ("https://bowhead.example.com/api/v1/integrations/feishu/events")


def test_feishu_diagnostics_callback_urls_use_resolved_entry_prefix() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_event_ingress="webhook",
        ),
        public_base_url="https://dev.example.com/bowhead",
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app, base_url="http://internal-host")

    data = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()

    assert data["event_subscription_url"] == ("https://dev.example.com/bowhead/api/v1/integrations/feishu/events")
    assert data["card_callback_url"] == ("https://dev.example.com/bowhead/api/v1/integrations/feishu/card-callback")


def test_feishu_diagnostics_callback_urls_ignore_forged_x_forwarded_host() -> None:
    # B2 security: there is no reverse proxy in front (direct docker port-map), so a
    # client-supplied X-Forwarded-Host must NOT be trusted — otherwise a caller could inject
    # an arbitrary origin into the Feishu callback / OAuth-redirect URLs (host-header injection).
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_event_ingress="webhook",
        )
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app, base_url="http://real.host")

    data = client.get(
        "/api/v1/integrations/feishu/admin/diagnostics",
        headers={"X-Forwarded-Host": "attacker.evil", "X-Forwarded-Proto": "https"},
    ).json()

    # Neither bare Host nor untrusted forwarding headers become URL authority.
    assert data["event_subscription_url"] == "https://bowhead.example.com/api/v1/integrations/feishu/events"
    assert "real.host" not in data["event_subscription_url"]
    assert "attacker.evil" not in data["event_subscription_url"]
    assert "attacker.evil" not in data["card_callback_url"]


def test_feishu_admin_diagnostics_accepts_production_equivalent_auth_mode() -> None:
    app = _feishu_route_app(
        Settings(
            auth_mode="jwt",
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_bot_open_id="ou_bot",
            feishu_event_ingress="long_connection",
        )
    )
    app.state.feishu_event_store = object()
    app.state.feishu_long_connection_worker = _FakeRunningLongConnectionWorker()
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    diag = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()

    assert diag["degraded"] is False
    assert diag["auth_mode"] == "jwt"
    assert diag["prod_auth_mode"] is True
    checklist = {item["key"]: item for item in diag["setup_checklist"]}
    assert checklist["prod_auth_mode"]["status"] == "ok"


def test_feishu_admin_create_setup_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
        )
    )
    app.state.feishu_event_store = store
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    client = TestClient(app)

    resp = client.post(
        "/api/v1/integrations/feishu/admin/setup-sessions",
        json={"workspace_id": "ws_1", "tenant_key": "tenant_1"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"].startswith("fss_")
    assert data["workspace_id"] == "ws_1"
    assert data["tenant_key"] == "tenant_1"
    assert data["app_type"] == "internal_guided"
    assert data["setup_url"] == (f"https://bowhead.example.com/team/settings/integrations/feishu/?stage=02&setup={data['id']}")
    assert data["qr_payload"] == data["setup_url"]
    assert data["qr_svg_url"].endswith(f"/admin/setup-sessions/{data['id']}/qr.svg")
    assert data["test_phrase"].endswith(data["id"][-8:])
    serialized = json.dumps(data, ensure_ascii=False).lower()
    assert "secret" not in serialized
    assert "nonce" not in serialized

    qr_resp = client.get(data["qr_svg_url"])
    assert qr_resp.status_code == 200
    assert qr_resp.headers["content-type"].startswith("image/svg+xml")
    assert qr_resp.headers["cache-control"] == "no-store"
    assert "<svg" in qr_resp.text
    assert 'viewBox="0 0 65 65"' in qr_resp.text
    assert "<path" in qr_resp.text

    stored = asyncio.run(store.get_setup_session(data["id"]))
    assert stored is not None
    assert stored.team_id == "team_1"
    assert stored.created_by_user_id == "usr_admin"


def test_feishu_setup_qr_svg_renderer_rejects_oversized_payload() -> None:
    svg = render_setup_qr_svg("https://bowhead.example.com/settings?feishuSetup=fss_test")

    assert svg.startswith("<svg")
    assert 'role="img"' in svg
    assert 'shape-rendering="crispEdges"' in svg
    assert "<path" in svg

    with pytest.raises(QrPayloadTooLongError):
        render_setup_qr_svg("x" * 272)


def test_feishu_setup_qr_is_scoped_to_admin_team(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(Settings(feishu_enabled=True))
    app.state.feishu_event_store = store
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    client = TestClient(app)
    created = client.post("/api/v1/integrations/feishu/admin/setup-sessions").json()

    app.dependency_overrides[feishu_routes.require_team_admin] = _test_other_team_admin
    denied = client.get(created["qr_svg_url"])

    assert denied.status_code == 404


def test_feishu_admin_config_rejects_secret_without_fernet(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(Settings(feishu_enabled=True))
    app.state.feishu_event_store = store
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/integrations/feishu/admin/config",
        json={"app_secret": "secret_should_not_persist"},
    )

    assert resp.status_code == 409
    assert asyncio.run(store.get_app_config()) is None


def test_feishu_admin_config_patch_is_write_only_and_refreshes_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(
        Settings(
            feishu_enabled=False,
            feishu_app_id="cli_env",
            feishu_app_secret="env_secret",
            feishu_verification_token="env_token",
            feishu_encrypt_key="env_encrypt",
        )
    )
    app.state.feishu_event_store = store
    clearable = _ClearableFeishuClient()
    app.state.feishu_client = clearable
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    client = TestClient(app)

    before = client.get("/api/v1/integrations/feishu/admin/config").json()
    assert before["source"] == "env"
    assert before["app_id"] == "cli_env"
    assert before["app_secret_configured"] is True
    assert before["secret_sources"]["app_secret"] == "env"

    resp = client.patch(
        "/api/v1/integrations/feishu/admin/config",
        json={
            "enabled": True,
            "event_ingress": "webhook",
            "app_id": "cli_db",
            "app_secret": "db_secret",
            "verification_token": "db_token",
            "encrypt_key": "db_encrypt",
            "bot_open_id": "ou_bot_db",
            "group_public_answers_enabled": True,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    serialized = json.dumps(data, ensure_ascii=False)
    assert "db_secret" not in serialized
    assert "db_token" not in serialized
    assert "db_encrypt" not in serialized
    assert data["source"] == "db"
    assert data["enabled"] is True
    assert data["app_id"] == "cli_db"
    assert data["bot_open_id"] == "ou_bot_db"
    assert data["secret_sources"]["app_secret"] == "db"
    assert clearable.cleared == 1

    resolver = app.state.feishu_app_config_resolver
    assert isinstance(resolver, DbBackedFeishuAppConfigResolver)
    resolved = resolver.resolve("")
    assert resolved.app_id == "cli_db"
    assert resolved.app_secret == "db_secret"
    assert resolved.verification_token == "db_token"
    assert resolved.encrypt_key == "db_encrypt"
    assert resolved.group_public_answers_enabled is True
    assert app.state.feishu_security_config.bot_open_id == "ou_bot_db"
    assert app.state.feishu_security_config.verification_token == "db_token"


def test_feishu_admin_config_patch_marks_long_connection_restart_required(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_app",
            feishu_app_secret="cli_secret",
            feishu_bot_open_id="ou_bot",
            feishu_event_ingress="long_connection",
        )
    )
    app.state.feishu_event_store = store
    app.state.feishu_client = _ClearableFeishuClient()
    app.state.feishu_long_connection_worker = _FakeRunningLongConnectionWorker()
    app.state.feishu_long_connection_runtime = {
        "worker_signature": ("1|long_connection|cli_app|" + hashlib.sha256(b"cli_secret").hexdigest()[:12]),
        "restart_required": False,
        "message": None,
        "changed_fields": [],
    }
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/integrations/feishu/admin/config",
        json={"app_secret": "rotated_secret"},
    )

    assert resp.status_code == 200
    assert resp.json()["long_connection_restart_required"] is True

    diag = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()
    assert diag["degraded"] is True
    assert diag["long_connection_restart_required"] is True
    assert diag["long_connection_restart_changed_fields"] == ["app_secret"]
    assert "long_connection_restart_required" in diag["missing_config"]
    checklist = {item["key"]: item for item in diag["setup_checklist"]}
    assert checklist["long_connection_restart"]["status"] == "degraded"


def test_feishu_restart_flag_clears_when_not_on_long_connection_and_no_worker_signature() -> None:
    app = _feishu_route_app(
        Settings(
            feishu_enabled=False,
            feishu_event_ingress="disabled",
        )
    )
    app.state.feishu_event_store = object()
    app.state.feishu_long_connection_runtime = {
        "worker_signature": None,
        "restart_required": True,
        "message": "stale restart requirement",
        "changed_fields": ["event_ingress"],
    }
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin
    client = TestClient(app)

    diag = client.get("/api/v1/integrations/feishu/admin/diagnostics").json()

    assert diag["degraded"] is False
    assert diag["long_connection_restart_required"] is False
    assert "long_connection_restart_required" not in diag["missing_config"]
    checklist = {item["key"]: item for item in diag["setup_checklist"]}
    assert checklist["long_connection_restart"]["status"] == "ok"


def test_feishu_db_config_resolver_preserves_env_secret_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_env",
            feishu_app_secret="env_secret",
        )
    )
    app.state.feishu_event_store = store
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/integrations/feishu/admin/config",
        json={"app_id": "cli_db"},
    )

    assert resp.status_code == 200
    resolver = app.state.feishu_app_config_resolver
    assert resolver.resolve("").app_id == "cli_db"
    assert resolver.resolve("").app_secret == "env_secret"
    assert resp.json()["secret_sources"]["app_secret"] == "env"


def test_feishu_admin_config_write_invalidates_cached_db_resolver(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    app = _feishu_route_app(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_env",
            feishu_app_secret="env_secret",
        )
    )
    app.state.feishu_event_store = store
    app.state.feishu_app_config_resolver = DbBackedFeishuAppConfigResolver(
        app.state.settings,
        str(store.db_path),
    )
    app.dependency_overrides[feishu_routes.require_team_admin] = _test_team_admin_with_identity
    resolver = app.state.feishu_app_config_resolver
    assert resolver.resolve("").app_id == "cli_env"
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/integrations/feishu/admin/config",
        json={"app_id": "cli_new"},
    )

    assert resp.status_code == 200
    assert app.state.feishu_app_config_resolver is resolver
    assert resolver.resolve("").app_id == "cli_new"


def test_feishu_binding_session_complete_binds_current_user(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    session = asyncio.run(
        store.create_binding_session(
            tenant_key="tenant_1",
            open_id="ou_user",
            union_id="on_union",
            email="feishu-alias@example.com",
            chat_id="oc_chat",
            source_message_id="om_1",
        )
    )
    app = _feishu_route_app(Settings(feishu_enabled=True))
    app.state.feishu_event_store = store
    app.dependency_overrides[feishu_routes._current_user] = _test_current_user
    client = TestClient(app)

    detail = client.get(f"/api/v1/integrations/feishu/bindings/{session.id}")
    assert detail.status_code == 200
    assert detail.json()["can_complete"] is True

    resp = client.post(f"/api/v1/integrations/feishu/bindings/{session.id}/complete")

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "bound"
    assert data["already_bound_to_current_user"] is True
    binding = asyncio.run(store.get_binding(tenant_key="tenant_1", open_id="ou_user"))
    assert binding is not None
    assert binding.bowhead_user_id == "usr_web"
    assert binding.email == "web@example.com"
    assert binding.binding_method == "manual_web"


def test_feishu_binding_session_complete_rejects_existing_other_user(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    session = asyncio.run(
        store.create_binding_session(
            tenant_key="tenant_1",
            open_id="ou_user",
            union_id=None,
            email=None,
            chat_id="oc_chat",
            source_message_id="om_1",
        )
    )
    asyncio.run(
        store.upsert_binding(
            tenant_key="tenant_1",
            open_id="ou_user",
            union_id=None,
            email="other@example.com",
            bowhead_user_id="usr_other",
            binding_method="manual_web",
        )
    )
    app = _feishu_route_app(Settings(feishu_enabled=True))
    app.state.feishu_event_store = store
    app.dependency_overrides[feishu_routes._current_user] = _test_current_user
    client = TestClient(app)

    resp = client.post(f"/api/v1/integrations/feishu/bindings/{session.id}/complete")

    assert resp.status_code == 409


def _test_team_admin() -> object:
    return SimpleNamespace(id="usr_admin", team_id="team_1")


def _test_team_admin_with_identity() -> object:
    return SimpleNamespace(id="usr_admin", team_id="team_1")


def _test_other_team_admin() -> object:
    return SimpleNamespace(id="usr_admin_2", team_id="team_2")


def _test_current_user() -> object:
    return SimpleNamespace(id="usr_web", team_id="team_1", email="web@example.com")


class _ClearableFeishuClient:
    def __init__(self) -> None:
        self.cleared = 0

    def clear_tenant_token_cache(self) -> None:
        self.cleared += 1

    def tenant_token_cache_keys(self) -> list[tuple[str, str, str]]:
        return []


class _FakeLongConnectionBridge:
    def __init__(self) -> None:
        self.config = FeishuSecurityConfig(bot_open_id="ou_bot")
        self.handled = 0

    async def handle_message_event(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        self.handled += 1
        return {"status": "started"}

    async def handle_card_callback(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        return {"toast": {"type": "success", "content": "ok"}}


class _FakeRunningLongConnectionWorker:
    @property
    def status(self) -> dict[str, Any]:
        return {"running": True, "thread_alive": True, "start_error": None}


class _FakeWSDispatcher:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def _do_without_validation(self, payload: bytes) -> dict[str, Any]:
        parsed = json.loads(payload.decode())
        self.payloads.append(parsed)
        return {"toast": {"type": "success", "content": "ok"}}


class _CapturingCardAwareWSClient(FeishuCardAwareWSClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.written: list[bytes] = []

    async def _write_message(self, data: bytes) -> None:
        self.written.append(data)


def _add_frame_header(frame: Frame, key: str, value: str) -> None:
    header = frame.headers.add()
    header.key = key
    header.value = value


@pytest.mark.asyncio
async def test_feishu_event_store_claims_message_once(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()

    assert await store.claim_message(event_id="evt_1", message_id="msg_1") is True
    assert await store.claim_message(event_id="evt_1", message_id="msg_1") is False
    assert await store.claim_message(event_id="evt_2", message_id="msg_1") is False
    await store.mark_message_status(message_id="msg_1", status="done")
    assert await store.claim_message(event_id="evt_3", message_id="msg_1") is False
    await store.mark_message_status(message_id="msg_1", status="failed")
    assert await store.claim_message(event_id="evt_4", message_id="msg_1") is True
    await store.upsert_turn_link(
        turn_id="turn_msg_1",
        session_id="s_1",
        tenant_key="tenant_1",
        chat_id="oc_chat",
        sender_open_id="ou_user",
        source_message_id="msg_1",
    )
    await store.mark_message_status(message_id="msg_1", status="failed")
    assert await store.claim_message(event_id="evt_5", message_id="msg_1") is False


@pytest.mark.asyncio
async def test_feishu_long_connection_worker_claims_and_dispatches_message(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()
    bridge = _FakeLongConnectionBridge()
    worker = FeishuLongConnectionWorker(
        bridge=bridge,
        store=store,
        app_config_resolver=EnvFeishuAppConfigResolver(Settings()),
    )
    body = _message_event()

    await worker.handle_message_payload(body)
    await worker.handle_message_payload(body)

    assert bridge.handled == 1
    assert await store.get_message_status("om_msg") == "started"


def test_feishu_long_connection_card_action_returns_bridge_toast(tmp_path) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        worker = FeishuLongConnectionWorker(
            bridge=_FakeLongConnectionBridge(),  # type: ignore[arg-type]
            store=object(),  # type: ignore[arg-type]
            app_config_resolver=EnvFeishuAppConfigResolver(Settings()),
            loop=loop,
        )

        resp = worker._on_card_action(
            {
                "schema": "2.0",
                "header": {"event_type": "card.action.trigger", "tenant_key": "tenant_1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {"value": {"bowhead_action": "live_callback_probe"}},
                },
            }
        )

        assert resp == {"toast": {"type": "success", "content": "ok"}}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


@pytest.mark.asyncio
async def test_feishu_card_aware_ws_client_dispatches_card_frames() -> None:
    handler = _FakeWSDispatcher()
    client = _CapturingCardAwareWSClient("cli_app", "secret", event_handler=handler)
    frame = Frame()
    frame.method = FrameType.DATA.value
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    _add_frame_header(frame, HEADER_MESSAGE_ID, "card_msg_1")
    _add_frame_header(frame, HEADER_TRACE_ID, "trace_1")
    _add_frame_header(frame, HEADER_SUM, "1")
    _add_frame_header(frame, HEADER_SEQ, "0")
    _add_frame_header(frame, HEADER_TYPE, MessageType.CARD.value)
    frame.payload = json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_type": "card.action.trigger",
                "tenant_key": "tenant_1",
            },
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"probe_id": "callback_1"}},
            },
        }
    ).encode()

    await client._handle_data_frame(frame)

    assert handler.payloads[0]["header"]["event_type"] == "card.action.trigger"
    assert client.written
    ack = Frame()
    ack.ParseFromString(client.written[-1])
    assert json.loads(ack.payload.decode())["code"] == 200


@pytest.mark.asyncio
async def test_feishu_card_aware_ws_client_ignores_unknown_event_frames() -> None:
    handler = _FakeWSDispatcher()
    client = _CapturingCardAwareWSClient("cli_app", "secret", event_handler=handler)
    frame = Frame()
    frame.method = FrameType.DATA.value
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    _add_frame_header(frame, HEADER_MESSAGE_ID, "read_msg_1")
    _add_frame_header(frame, HEADER_TRACE_ID, "trace_1")
    _add_frame_header(frame, HEADER_SUM, "1")
    _add_frame_header(frame, HEADER_SEQ, "0")
    _add_frame_header(frame, HEADER_TYPE, MessageType.EVENT.value)
    frame.payload = json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_type": "im.message.message_read_v1",
                "tenant_key": "tenant_1",
            },
            "event": {},
        }
    ).encode()

    await client._handle_data_frame(frame)

    assert handler.payloads == []
    assert client.written
    ack = Frame()
    ack.ParseFromString(client.written[-1])
    assert json.loads(ack.payload.decode())["code"] == 200


@pytest.mark.asyncio
async def test_feishu_event_store_workspace_binding(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()

    assert (
        await store.get_workspace_binding(
            tenant_key="tenant_1",
            chat_id="oc_chat",
            open_id="ou_user",
        )
        is None
    )
    await store.upsert_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
        bowhead_user_id="u_1",
        workspace_id="ws_1",
    )
    await store.upsert_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
        bowhead_user_id="u_1",
        workspace_id="ws_2",
    )

    binding = await store.get_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
    )
    assert binding is not None
    assert binding.workspace_id == "ws_2"


@pytest.mark.asyncio
async def test_feishu_event_store_binding_session_lifecycle(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()

    session = await store.create_binding_session(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_union",
        email=None,
        chat_id="oc_chat",
        source_message_id="om_1",
    )

    assert session.id.startswith("fbs_")
    assert session.state == "pending"
    assert session.bound_user_id is None
    fetched = await store.get_binding_session(session.id)
    assert fetched is not None
    assert fetched.open_id == "ou_user"
    assert fetched.email is None

    await store.complete_binding_session(session_id=session.id, bowhead_user_id="usr_1")
    completed = await store.get_binding_session(session.id)
    assert completed is not None
    assert completed.state == "bound"
    assert completed.bound_user_id == "usr_1"
    assert completed.completed_at is not None


@pytest.mark.asyncio
async def test_feishu_event_store_setup_session_is_unguessable_and_expiring(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()

    session = await store.create_setup_session(
        team_id="team_1",
        workspace_id="ws_1",
        created_by_user_id="usr_admin",
        tenant_key="tenant_1",
        app_type="internal_guided",
        expires_in_seconds=900,
    )
    loaded = await store.get_setup_session(session.id)

    assert loaded is not None
    assert loaded.id.startswith("fss_")
    assert len(loaded.id) > 30
    assert len(loaded.nonce) > 16
    assert loaded.expires_at > loaded.created_at
    assert loaded.state == "created"
    assert loaded.app_type == "internal_guided"
    assert loaded.tenant_key == "tenant_1"


def test_parse_group_mention_event_cleans_bot_mention() -> None:
    body = _message_event(
        chat_type="group",
        content={"text": '<at id="bot">Bowhead</at> 帮我看 GMV'},
        mentions=[{"id": {"open_id": "ou_bot"}, "key": '<at id="bot">Bowhead</at>'}],
    )
    inbound = parse_message_event(body, bot_open_id="ou_bot")

    assert inbound is not None
    assert inbound.mentions_bot is True
    assert inbound.text == "帮我看 GMV"
    assert inbound.event_id == "evt_1"

    no_mention = _message_event(chat_type="group", mentions=[])
    inbound = parse_message_event(no_mention, bot_open_id="ou_bot")
    assert inbound is not None
    assert inbound.mentions_bot is False


def test_workspace_picker_card_has_owner_scoped_action() -> None:
    card = renderer.workspace_picker_card(
        [{"workspace_id": "ws_1", "name": "Demo", "role": "admin"}],
        allowed_open_id="ou_user",
        tenant_key="tenant_1",
        chat_id="oc_chat",
    )

    action = card["elements"][1]["actions"][0]
    assert action["value"]["bowhead_action"] == "workspace_select"
    assert action["value"]["allowed_open_id"] == "ou_user"
    assert action["value"]["workspace_id"] == "ws_1"


def test_completed_status_card_hides_narrative_when_final_answer_is_separate() -> None:
    state = renderer.FeishuTurnCardState(
        status="completed",
        narrative="最终答案单独回复。",
        final_answer="最终答案单独回复。",
        steps={
            "s1": renderer.FeishuStepView(
                step_id="s1",
                title="查询指标",
                state="success",
                output="ok",
            ),
        },
        locale="zh-CN",
    )

    card = renderer.turn_status_card(state)
    dumped = json.dumps(card, ensure_ascii=False)

    assert "处理过程" not in dumped
    assert "最终答案已发送" in dumped
    assert "查询指标" in dumped


def test_completed_status_card_keeps_process_summary_without_duplicate_answer() -> None:
    state = renderer.FeishuTurnCardState(
        status="completed",
        narrative="正在查询 BI schema。已拉取 2026 年 1-5 月营业额, 准备对比去年同期。",
        final_answer="2026 年 1-5 月营业额约 78.62 亿元, 同比基本持平。",
        locale="zh-CN",
    )

    card = renderer.turn_status_card(state)
    dumped = json.dumps(card, ensure_ascii=False)

    assert "处理过程" in dumped
    assert "正在查询 BI schema" in dumped
    assert "2026 年 1-5 月营业额约 78.62 亿元" not in dumped
    assert "未记录到可展示的工具步骤" in dumped


def test_status_card_strips_response_kind_and_headline_markers_from_narrative() -> None:
    """Old persisted narrative deltas may still carry the internal Lead markers."""
    state = renderer.FeishuTurnCardState(status="running", locale="zh-CN")
    for delta in ["<response_kind>chat</response_kind>\n\n<headline>营收", "持平</headline>\n\n正在查询 BI schema。"]:
        renderer.apply_event_to_state(state, "narrative.delta", {"delta": delta})

    dumped = json.dumps(renderer.turn_status_card(state), ensure_ascii=False)

    assert "正在查询 BI schema" in dumped
    assert "response_kind" not in dumped
    assert "headline" not in dumped
    assert "营收持平" not in dumped


def test_answer_card_converts_markdown_tables_to_feishu_table_component() -> None:
    card = renderer.answer_card(
        "**经营概览**\n\n| 指标 | 数值 |\n|---|---|\n| 营业额 | **51.80亿** |\n| 利润 | 8.94亿 |\n\n后续说明。", locale="zh-CN"
    )

    table = next(element for element in card["elements"] if element["tag"] == "table")
    dumped = json.dumps(card, ensure_ascii=False)

    assert table["columns"][0]["display_name"] == "指标"
    assert table["rows"][0]["col_0"] == "营业额"
    assert table["rows"][0]["col_1"] == "51.80亿"
    assert "| 指标 | 数值 |" not in dumped
    assert "后续说明" in dumped


def test_answer_card_converts_inline_chart_artifacts_to_feishu_chart_component() -> None:
    card = renderer.answer_card(
        "营业额趋势如下。",
        artifacts=[_sample_inline_chart_artifact()],
        locale="zh-CN",
    )

    chart = next(element for element in card["elements"] if element["tag"] == "chart")

    assert chart["aspect_ratio"] == "16:9"
    assert chart["chart_spec"]["type"] == "line"
    assert chart["chart_spec"]["xField"] == "月份"
    assert chart["chart_spec"]["yField"] == "营业额"
    assert chart["chart_spec"]["data"]["values"][0]["月份"] == "1月"


def test_answer_card_never_draws_a_silently_partial_chart() -> None:
    """A chart that exceeds the card row limit links to the complete web view."""
    too_many = renderer.MAX_CHART_ROWS + 1
    artifact = _sample_inline_chart_artifact()
    artifact["spec"]["rows"] = [{"月份": f"D{i}", "营业额": float(i)} for i in range(too_many)]
    artifact["spec"]["row_count"] = too_many

    card = renderer.answer_card("营业额趋势如下。", artifacts=[artifact], locale="zh-CN")
    dumped = json.dumps(card, ensure_ascii=False)

    assert not [element for element in card["elements"] if element["tag"] == "chart"]
    assert f"共 {too_many} 行数据" in dumped
    assert "查看完整图表" in dumped


def _long_chart(title: str, n_rows: int, n_series: int = 1) -> dict[str, Any]:
    artifact = _sample_inline_chart_artifact()
    spec = artifact["spec"]
    spec["title"] = title
    spec["columns"] = [
        {"key": "月份", "header": "月份", "type": "categorical"},
        *[{"key": f"营业额{k}", "header": f"营业额{k}", "type": "numeric"} for k in range(n_series)],
    ]
    spec["rows"] = [{"月份": f"2026-01-{i:03d}", **{f"营业额{k}": 123456789.123 + i + k for k in range(n_series)}} for i in range(n_rows)]
    spec["row_count"] = n_rows
    return artifact


def test_answer_card_request_stays_under_the_feishu_limit() -> None:
    """飞书卡片请求体上限 30KB，超了整张答案卡发不出去（230025）——用户什么都收不到。

    review 实测：3 张 120 行的图 34.5KB（改动前同形状 15.5KB，因为后端只带 50 行）。
    超了从最后一张图起换成提示，直到请求体放得下。
    """
    charts = [_long_chart(f"图{n}", renderer.MAX_CHART_ROWS, n_series=2) for n in range(3)]
    unshrunk = renderer._card(title="t", template="green", elements=renderer._answer_elements("营业额趋势如下。", artifacts=charts))
    assert renderer.card_request_bytes(unshrunk) > renderer.MAX_CARD_BODY_BYTES, "正对照：不收缩时确实超限"

    card = renderer.answer_card("营业额趋势如下。", artifacts=charts, locale="zh-CN")
    dumped = json.dumps(card, ensure_ascii=False)

    assert renderer.card_request_bytes(card) <= renderer.MAX_CARD_BODY_BYTES
    assert [e for e in card["elements"] if e["tag"] == "chart"], "放得下的图照画"
    assert "「图2」" in dumped and "卡片里放不下" in dumped, "换掉的是最后一张，且要说出来"


def test_answer_card_keeps_charts_that_fit() -> None:
    """放得下就照画：不能因为图「看起来大」就换成提示（review：4 指标 × 50 行的图约 15.7KB）。"""
    card = renderer.answer_card("营业额趋势如下。", artifacts=[_long_chart("四指标", 50, n_series=4)], locale="zh-CN")
    assert [e for e in card["elements"] if e["tag"] == "chart"]
    assert "卡片里放不下" not in json.dumps(card, ensure_ascii=False)


def test_long_answer_text_pushes_charts_out_before_the_card_breaks() -> None:
    """正文很长时，图 + 正文会一起顶破上限——先让图让位（此前正文长 + 图也会发不出去）。"""
    text = "营业额分析。" * 1300  # ≈7800 characters, near `MAX_CARD_MARKDOWN_CHARS`.
    charts = [_long_chart(f"图{n}", 100) for n in range(2)]
    card = renderer.answer_card(text, artifacts=charts, locale="zh-CN")
    assert renderer.card_request_bytes(card) <= renderer.MAX_CARD_BODY_BYTES
    assert "卡片里放不下" in json.dumps(card, ensure_ascii=False)


def test_answer_card_discloses_backend_top_n_under_the_chart() -> None:
    """后端取了 top-N（spec 只带部分行）时照画，但图下写明——与网页图下那句同一口径。"""
    artifact = _sample_inline_chart_artifact()
    artifact["spec"]["row_count"] = 98

    card = renderer.answer_card("营业额趋势如下。", artifacts=[artifact], locale="zh-CN")
    tags = [element["tag"] for element in card["elements"]]
    dumped = json.dumps(card, ensure_ascii=False)

    assert "chart" in tags
    assert tags[tags.index("chart") + 1] == "note"
    assert "仅显示前 2 项 / 共 98 项" in dumped


def test_answer_card_uses_the_series_wording_when_series_were_capped() -> None:
    """★ 系列被后端截过时要按**系列**的量词说 —— 少掉的行就是被截掉的系列，按行数说是假话。

    59 个省取前 15 时行数 708→180：卡片若沿用「仅显示前 180 项 / 共 708 项」，
    就把系列数说成了行数。网页图下同一口径（`ChartSubcaptions`），两处不能各说各的。
    """
    artifact = _sample_inline_chart_artifact()
    artifact["spec"]["row_count"] = 708
    artifact["spec"]["series_shown"] = 15
    artifact["spec"]["series_total"] = 59

    card = renderer.answer_card("各省走势如下。", artifacts=[artifact], locale="zh-CN")
    dumped = json.dumps(card, ensure_ascii=False)

    assert "共 59 条系列 / 按合计显示前 15 条" in dumped
    assert "仅显示前" not in dumped, "同一件事不能再按错误的量词说第二遍"


def test_answer_card_says_how_many_charts_did_not_fit() -> None:
    """Disclose every chart omitted because the card has reached its chart limit."""
    charts = []
    for i in range(6):
        a = _sample_inline_chart_artifact()
        a["id"] = f"sec_{i}"
        a["spec"] = {**a["spec"], "title": f"图{i}"}
        charts.append(a)

    card = renderer.answer_card("六张图如下。", artifacts=charts, locale="zh-CN")
    dumped = json.dumps(card, ensure_ascii=False)

    assert dumped.count('"tag": "chart"') == renderer.MAX_CHARTS_PER_CARD, "放得下的就画 3 张"
    assert "还有 3 张图" in dumped, f"没放下的必须说出来：{dumped[-400:]}"
    assert "「图3」" in dumped and "「图5」" in dumped, "要点名是哪几张"
    assert "网页端" in dumped, "要给出口"


def test_answer_card_reports_a_single_overflow_chart() -> None:
    """★ 正好 4 张图（只溢出 1 张）是最常见的形态，必须单独钉 —— 复审突变
    `if len(overflow) > 1` 在整套用例里存活过（4 张时又静默丢弃）。"""
    charts = []
    for i in range(4):
        a = _sample_inline_chart_artifact()
        a["id"] = f"sec_{i}"
        a["spec"] = {**a["spec"], "title": f"图{i}"}
        charts.append(a)

    dumped = json.dumps(renderer.answer_card("四张图。", artifacts=charts, locale="zh-CN"), ensure_ascii=False)
    assert dumped.count('"tag": "chart"') == 3
    assert "还有 1 张图" in dumped and "「图3」" in dumped, dumped[-300:]


def test_overflow_note_keeps_the_exit_even_with_very_long_titles() -> None:
    """★ 出口那半句在句尾，而 `_note` 会硬切在 300 字 —— 长标题必须先截短。"""
    charts = []
    for i in range(6):
        a = _sample_inline_chart_artifact()
        a["id"] = f"sec_{i}"
        a["spec"] = {**a["spec"], "title": "这是一个非常非常长的图表标题" * 30}
        charts.append(a)

    dumped = json.dumps(renderer.answer_card("t", artifacts=charts, locale="zh-CN"), ensure_ascii=False)
    assert "网页端" in dumped, "出口被 300 字硬切吃掉了"
    assert "还有 3 张图" in dumped


def test_overflow_note_does_not_say_the_count_twice() -> None:
    """★ 张数在句首已经说过一次，点名后面不必再说「等 N 张」。"""
    charts = []
    for i in range(7):
        a = _sample_inline_chart_artifact()
        a["id"] = f"sec_{i}"
        a["spec"] = {**a["spec"], "title": f"图{i}"}
        charts.append(a)

    dumped = json.dumps(renderer.answer_card("t", artifacts=charts, locale="zh-CN"), ensure_ascii=False)
    assert "还有 4 张图" in dumped
    assert "等 4 张" not in dumped, "同一个数说了两遍"
    assert "」 等）" in dumped or "」等）" in dumped, dumped[-260:]


def test_answer_card_says_nothing_extra_when_every_chart_fits() -> None:
    """负对照：3 张及以内一个字都不多说。"""
    for n in (1, 3):
        charts = []
        for i in range(n):
            a = _sample_inline_chart_artifact()
            a["id"] = f"sec_{i}"
            a["spec"] = {**a["spec"], "title": f"图{i}"}
            charts.append(a)
        dumped = json.dumps(renderer.answer_card("t", artifacts=charts, locale="zh-CN"), ensure_ascii=False)
        assert "一张卡片放不下" not in dumped, f"{n} 张图不该冒出溢出提示"


def test_undrawable_chart_kinds_do_not_count_toward_the_card_limit() -> None:
    """★ 表格 / KPI 本来就不画进卡片，不占图位，也不能被算进「没放下」里吓唬用户。"""
    charts = []
    for kind in ("table", "kpi", "table"):
        a = _sample_inline_chart_artifact()
        a["spec"] = {**a["spec"], "kind": kind, "title": f"{kind}段"}
        charts.append(a)
    for i in range(2):
        a = _sample_inline_chart_artifact()
        a["id"] = f"sec_line_{i}"
        a["spec"] = {**a["spec"], "title": f"折线{i}"}
        charts.append(a)

    dumped = json.dumps(renderer.answer_card("t", artifacts=charts, locale="zh-CN"), ensure_ascii=False)
    assert dumped.count('"tag": "chart"') == 2
    assert "一张卡片放不下" not in dumped, "只有 2 张画得出的图，不该说有图没放下"


def test_answer_card_reports_downsampling_with_the_time_point_wording() -> None:
    """★ 降采样按**时间点**说，且不再叠加按行数那句 —— 整段时间还在，只是点稀疏了。"""
    artifact = _sample_inline_chart_artifact()
    artifact["spec"]["row_count"] = 4500
    artifact["spec"]["points_shown"] = 805
    artifact["spec"]["points_total"] = 900

    dumped = json.dumps(renderer.answer_card("t", artifacts=[artifact], locale="zh-CN"), ensure_ascii=False)
    assert "共 900 个时间点 / 已按整体走势降采样至 805 个（首尾时刻保留）" in dumped
    assert "仅显示前" not in dumped, "按行数说会把「点稀疏」说成「后面那截没画」"


def test_answer_card_keeps_both_notes_when_rows_were_also_cut() -> None:
    """★ 行另有一刀（`top_n` 有值）时两句都要出 —— 让位只在行损失全来自系列截断时成立。"""
    artifact = _sample_inline_chart_artifact()
    artifact["spec"]["row_count"] = 708
    artifact["spec"]["top_n"] = 400
    artifact["spec"]["series_shown"] = 15
    artifact["spec"]["series_total"] = 59

    dumped = json.dumps(renderer.answer_card("t", artifacts=[artifact], locale="zh-CN"), ensure_ascii=False)
    assert "共 59 条系列 / 按合计显示前 15 条" in dumped
    assert "仅显示前 2 项 / 共 708 项" in dumped, "另一刀造成的行损失不能被吞掉"


def test_answer_card_keeps_row_wording_when_series_were_not_capped() -> None:
    """负对照：没截系列（字段缺省 / 相等）时仍按行数说，一个字都不改。"""
    for extra in ({}, {"series_shown": 15, "series_total": 15}):
        artifact = _sample_inline_chart_artifact()
        artifact["spec"]["row_count"] = 98
        artifact["spec"].update(extra)
        dumped = json.dumps(renderer.answer_card("t", artifacts=[artifact], locale="zh-CN"), ensure_ascii=False)
        assert "仅显示前 2 项 / 共 98 项" in dumped, f"extra={extra}"
        assert "条系列" not in dumped, f"extra={extra}：没截就别说"


def _sample_inline_chart_artifact() -> dict[str, Any]:
    return {
        "kind": "chart",
        "id": "sec_sales_line",
        "spec": {
            "kind": "line",
            "title": "营业额趋势",
            "rows": [
                {"月份": "1月", "营业额": 100.0},
                {"月份": "2月", "营业额": 120.0},
            ],
            "columns": [
                {"key": "月份", "header": "月份", "type": "categorical", "role": "x"},
                {"key": "营业额", "header": "营业额", "type": "numeric", "role": "y"},
            ],
            "row_count": 2,
        },
    }


def test_composed_children_follow_the_existing_card_chart_budget() -> None:
    artifact = _sample_inline_chart_artifact()
    children = []
    for index in range(4):
        child = copy.deepcopy(artifact["spec"])
        child["title"] = f"Panel {index}"
        children.append({"id": f"panel_{index}", "kind": "chart", "spec": child})
    children.append({"id": "details", "kind": "chart", "spec": {**artifact["spec"], "kind": "table"}})
    artifact["spec"] = {**artifact["spec"], "views": children}
    card = json.dumps(renderer.answer_card("Panels", artifacts=[artifact], locale="en-US"))
    assert card.count('"tag": "chart"') == 3
    assert "Panel 3" in card


@pytest.mark.asyncio
async def test_bridge_auto_binds_email_and_mirrors_turn_events(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    user_store = UserStore(db_path)
    await user_store.initialize()
    session_store = SessionStore(db_path)
    await session_store.initialize()
    turn_log_store = TurnLogStore(str(db_path))
    await turn_log_store.initialize()
    feishu_store = FeishuEventStore(db_path)
    await feishu_store.initialize()

    team = await user_store.create_team(name="Acme", domain="example.com", max_seats=5)
    await user_store.update_team_llm_config(team.id, _valid_team_llm_config())
    user = await user_store.create_user(
        team_id=team.id,
        email="alice@example.com",
        display_name="Alice",
        email_verified=True,
    )
    workspace_id = await user_store.sync_workspace_from_datarc(
        team_id=team.id,
        datarc_workspace_id="datarc_ws_1",
        name="Demo Workspace",
    )
    await user_store.add_member(workspace_id=workspace_id, user_id=user.id, role="member")

    fake_client = FakeFeishuClient(email="alice@example.com")
    orchestrator = FakeOrchestrator(
        session_store,
        inline_artifacts=[_sample_inline_chart_artifact()],
    )
    registry = get_event_registry()
    registry.set_log_sink(OrderedTurnLogSink(turn_log_store))
    orchestrator.event_registry = registry
    app = FastAPI()
    app.state.settings = Settings()
    app.state.user_store = user_store
    app.state.session_store = session_store
    app.state.turn_log_store = turn_log_store
    app.state.orchestrator = orchestrator
    app.state.legacy_session_lifecycle_allowed = True
    app.state.session_lifecycle_coordinator = _NoopSessionLifecycleCoordinator()
    app.state.member_resolver = StaticMemberResolver("member_1")
    app.state.datarc_connection_store = FakeDatarcConnectionStore(auth_key="team-key")
    app.state.datarc_client_factory = FakeDatarcClientFactory(object())
    app.state.entitlements = None
    app.state.team_router_registry = _StubTeamRouterRegistry()

    async def fast_run_turn_and_cleanup(
        orchestrator: FakeOrchestrator,
        req: TurnRequest,
        user_store: Any = None,
        team_id: str | None = None,
        metrics_log: Any = None,
        entitlements: Any = None,
        admitted_legacy_bus: Any = None,
        cleanup_grace_s: float = 5.0,
    ) -> None:
        assert admitted_legacy_bus is not None
        assert cleanup_grace_s == 5.0
        await orchestrator.run_turn(req)

    monkeypatch.setattr(chat_routes, "_run_turn_and_cleanup", fast_run_turn_and_cleanup)

    bridge = FeishuTurnBridge(
        app=app,
        client=fake_client,  # type: ignore[arg-type]
        store=feishu_store,
        config=FeishuSecurityConfig(bot_open_id="ou_bot"),
    )
    result = await bridge.handle_message_event(_message_event(content={"text": "看一下 GMV"}))

    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fake_client)) == 1)

    binding = await feishu_store.get_binding(tenant_key="tenant_1", open_id="ou_user")
    assert binding is not None
    assert binding.bowhead_user_id == user.id
    assert orchestrator.requests[0].session_id == "feishu:tenant_1:oc_chat:ou_user"
    assert orchestrator.requests[0].workspace_id == workspace_id
    assert orchestrator.requests[0].active_datarc_project == "datarc_ws_1"
    assert orchestrator.requests[0].member_uid == "member_1"
    assert fake_client.card_replies
    assert fake_client.patches
    assert fake_client.text_replies == []
    answer_card = _answer_cards(fake_client)[-1]
    assert answer_card["msg_type"] == "interactive"
    # No saved language preference: the Chinese question sets the turn and card language.
    assert orchestrator.requests[0].language_context.output_language == "zh-CN"
    assert answer_card["content"]["header"]["title"]["content"] == "Bowhead 最终答案"
    assert answer_card["content"]["elements"][0]["tag"] == "markdown"
    assert any(element["tag"] == "chart" for element in answer_card["content"]["elements"])
    assert "这是最终答案" in json.dumps(answer_card["content"], ensure_ascii=False)
    assert "这是最终答案" not in json.dumps(
        fake_client.patches[-1]["card"],
        ensure_ascii=False,
    )

    link = None
    for _ in range(100):
        link = await feishu_store.get_turn_link(orchestrator.requests[0].turn_id)
        if link is not None and link.answer_message_id is not None:
            break
        await asyncio.sleep(0.01)
    assert link is not None
    assert link.meta["workspace_id"] == workspace_id
    assert link.meta["datarc_workspace_id"] == "datarc_ws_1"
    assert link.answer_message_id is not None
    await _wait_for_dispatch_settlement_and_close(
        orchestrator,
        orchestrator.requests[0].turn_id,
    )


@pytest.mark.asyncio
async def test_bridge_threads_team_datarc_client_into_turn_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    team_datarc_client = object()
    factory = FakeDatarcClientFactory(team_datarc_client)
    fixture.bridge.app.state.datarc_connection_store = FakeDatarcConnectionStore(
        auth_key="team-key",
    )
    fixture.bridge.app.state.datarc_client_factory = factory

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    req = fixture.orchestrator.requests[0]
    assert req.datarc_client is team_datarc_client
    assert req.datarc_unavailable is False
    assert req.vector_client is None
    assert factory.calls == [("https://org.datarc.example", "team-key")] * 2
    await _wait_for_dispatch_settlement_and_close(fixture.orchestrator, req.turn_id)


@pytest.mark.asyncio
async def test_bridge_fails_closed_when_team_datarc_connection_is_broken(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    fixture.bridge.app.state.datarc_connection_store = FakeDatarcConnectionStore(auth_key=None)
    fixture.bridge.app.state.datarc_client_factory = FakeDatarcClientFactory(object())

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "denied"
    assert fixture.orchestrator.requests == []


@pytest.mark.asyncio
async def test_bridge_email_miss_creates_manual_binding_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
    )
    fixture.fake_client.email = "missing@example.com"
    public_entries = PublicEntryStore(fixture.feishu_store.db_path)
    await public_entries.initialize()
    assert await public_entries.migrate_legacy("https://bowhead.example.com/bowhead") == 1
    fixture.bridge.app.state.public_entry_store = public_entries

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "needs_binding"
    assert fixture.orchestrator.requests == []
    assert not fixture.fake_client.patches
    assert await fixture.feishu_store.get_binding(tenant_key="tenant_1", open_id="ou_user") is None
    assert fixture.fake_client.card_replies
    card = fixture.fake_client.card_replies[-1]["content"]
    action = card["elements"][1]["actions"][0]
    assert action["url"].startswith(
        "https://bowhead.example.com/bowhead/me/integrations/feishu/bind/?session=fbs_",
    )
    session_id = action["url"].split("session=", 1)[1]
    session = await fixture.feishu_store.get_binding_session(session_id)
    assert session is not None
    assert session.email == "missing@example.com"
    assert session.source_message_id == "om_msg"


@pytest.mark.asyncio
async def test_bridge_binding_link_fails_closed_without_public_entry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
    )
    fixture.fake_client.email = "missing@example.com"
    public_entries = PublicEntryStore(fixture.feishu_store.db_path)
    await public_entries.initialize()
    fixture.bridge.app.state.public_entry_store = public_entries

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "needs_binding"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    card_text = json.dumps(fixture.fake_client.card_replies[-1]["content"], ensure_ascii=False)
    # No Bowhead account is known yet, so the copy follows the message language.
    assert "暂时无法生成绑定链接" in card_text
    assert "公开入口" in card_text
    assert "/me/integrations/feishu/bind/" not in card_text


@pytest.mark.asyncio
async def test_long_connection_message_status_moves_to_done_after_turn_completion(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    worker = FeishuLongConnectionWorker(
        bridge=fixture.bridge,
        store=fixture.feishu_store,
        app_config_resolver=EnvFeishuAppConfigResolver(Settings()),
    )

    await worker.handle_message_payload(_message_event(content={"text": "看 GMV"}))
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    await _wait_for_message_status(fixture.feishu_store, "om_msg", "done")

    assert await fixture.feishu_store.get_message_status("om_msg") == "done"
    await _wait_for_dispatch_settlement_and_close(
        fixture.orchestrator,
        fixture.orchestrator.requests[0].turn_id,
    )


@pytest.mark.asyncio
async def test_bridge_throttles_progress_card_patches_and_flushes_final(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
        orchestrator_cls=BurstProgressOrchestrator,
    )

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "started"
    await _wait_for(
        lambda: len(_answer_cards(fixture.fake_client)) == 1 and len(fixture.fake_client.patches) == 2,
    )
    assert len(fixture.fake_client.patches) == 2
    assert fixture.fake_client.patches[-1]["card"]["header"]["title"]["content"] == "Bowhead 已完成 · 1 步"
    assert "burst final" in json.dumps(_answer_cards(fixture.fake_client)[-1]["content"])
    assert fixture.fake_client.text_replies == []
    await _wait_for_dispatch_settlement_and_close(
        fixture.orchestrator,
        fixture.orchestrator.requests[0].turn_id,
    )


@pytest.mark.asyncio
async def test_group_public_policy_disabled_blocks_group_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
        group_public=False,
    )

    result = await fixture.bridge.handle_message_event(_group_message_event())

    assert result["status"] == "denied"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.ephemeral_cards


@pytest.mark.asyncio
async def test_bridge_blocks_unverified_user_when_email_verification_required(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
        email_verified=False,
    )

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "account_unavailable"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    assert "邮箱验证" in fixture.fake_client.card_replies[-1]["content"]["elements"][0]["text"]["content"]
    assert (
        await fixture.feishu_store.get_binding(
            tenant_key="tenant_1",
            open_id="ou_user",
        )
        is None
    )


@pytest.mark.asyncio
async def test_bridge_fails_closed_when_member_uid_missing_in_prod_mode(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    fixture.bridge.app.state.member_resolver = None
    fixture.bridge.app.state.settings = Settings(auth_mode="jwt")

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "denied"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    denied_card = fixture.fake_client.card_replies[-1]["content"]
    assert denied_card["header"]["title"]["content"] == "无法确认数据权限"
    assert "账号关联" in json.dumps(denied_card, ensure_ascii=False)
    assert fixture.fake_client.patches == []
    assert _answer_cards(fixture.fake_client) == []


@pytest.mark.asyncio
async def test_bridge_resolves_member_uid_with_datarc_workspace_and_user_email(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    fake_datarc = FakeDatarcMembers(member_uid="member_from_datarc")
    fixture.bridge.app.state.member_resolver = MemberUidResolver(fake_datarc)  # type: ignore[arg-type]
    fixture.bridge.app.state.datarc_client_factory = FakeDatarcClientFactory(fake_datarc)
    fixture.bridge.app.state.settings = Settings(auth_mode="jwt")

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    assert fake_datarc.calls == [("datarc_ws_0", "alice@example.com")]
    assert fixture.orchestrator.requests[0].workspace_id == fixture.workspace_ids[0]
    assert fixture.orchestrator.requests[0].active_datarc_project == "datarc_ws_0"
    assert fixture.orchestrator.requests[0].member_uid == "member_from_datarc"
    await _wait_for_dispatch_settlement_and_close(
        fixture.orchestrator,
        fixture.orchestrator.requests[0].turn_id,
    )


@pytest.mark.asyncio
async def test_bridge_member_uid_transient_error_sends_retryable_card(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    fixture.bridge.app.state.member_resolver = BoomMemberResolver()
    fixture.bridge.app.state.settings = Settings(auth_mode="jwt")

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "failed"
    assert fixture.orchestrator.requests == []
    card = fixture.fake_client.card_replies[-1]["content"]
    assert card["header"]["title"]["content"] == "数据权限服务暂时不可用"


@pytest.mark.asyncio
async def test_bridge_credit_deny_does_not_start_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    async with open_shared_ctx(fixture.feishu_store.db_path) as db:
        await db.execute(
            "UPDATE users SET credit_balance=0 WHERE id=?",
            (fixture.user.id,),
        )
        await db.commit()

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "denied"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    deny_card = fixture.fake_client.card_replies[-1]["content"]
    assert deny_card["header"]["title"]["content"] == "无法开始对话"
    assert "额度" in json.dumps(deny_card, ensure_ascii=False)
    assert fixture.fake_client.patches == []
    assert _answer_cards(fixture.fake_client) == []


@pytest.mark.asyncio
async def test_group_entitlement_deny_is_ephemeral_and_does_not_start_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
        group_public=True,
    )
    fixture.bridge.app.state.entitlements = DenyingEntitlements("license suspended")

    result = await fixture.bridge.handle_message_event(_group_message_event())

    assert result["status"] == "denied"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies == []
    assert fixture.fake_client.ephemeral_cards
    deny_card = fixture.fake_client.ephemeral_cards[-1]["card"]
    assert deny_card["header"]["title"]["content"] == "无法开始对话"
    assert "权限" in json.dumps(deny_card, ensure_ascii=False)
    assert fixture.fake_client.patches == []
    assert _answer_cards(fixture.fake_client) == []


@pytest.mark.asyncio
async def test_group_public_policy_enabled_allows_group_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
        group_public=True,
    )

    result = await fixture.bridge.handle_message_event(_group_message_event())

    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    assert fixture.orchestrator.requests[0].workspace_id == fixture.workspace_ids[0]
    assert fixture.orchestrator.requests[0].active_datarc_project == fixture.datarc_workspace_ids[0]
    assert _answer_cards(fixture.fake_client)[-1]["message_id"] == "om_msg"
    await _wait_for_dispatch_settlement_and_close(
        fixture.orchestrator,
        fixture.orchestrator.requests[0].turn_id,
    )


@pytest.mark.asyncio
async def test_multi_workspace_requires_picker_then_uses_selected_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=2)

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "needs_workspace"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    action = fixture.fake_client.card_replies[-1]["content"]["elements"][1]["actions"][1]
    selected_workspace_id = action["value"]["workspace_id"]

    wrong_click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_other"}},
                "action": {"value": action["value"]},
            },
        }
    )
    assert wrong_click["toast"]["type"] == "warning"
    assert (
        await fixture.feishu_store.get_workspace_binding(
            tenant_key="tenant_1",
            chat_id="oc_chat",
            open_id="ou_user",
        )
        is None
    )

    ok_click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {"value": action["value"]},
            },
        }
    )
    assert ok_click["toast"]["type"] == "success"
    selected = await fixture.feishu_store.get_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
    )
    assert selected is not None
    assert selected.workspace_id == selected_workspace_id

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "再看 GMV"}))
    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    selected_index = fixture.workspace_ids.index(selected_workspace_id)
    assert fixture.orchestrator.requests[0].workspace_id == fixture.workspace_ids[selected_index]
    assert fixture.orchestrator.requests[0].active_datarc_project == fixture.datarc_workspace_ids[selected_index]
    await _wait_for_dispatch_settlement_and_close(
        fixture.orchestrator,
        fixture.orchestrator.requests[0].turn_id,
    )


@pytest.mark.asyncio
async def test_switch_workspace_command_prompts_picker_without_starting_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=2)
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email_match",
    )
    await fixture.feishu_store.upsert_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
        bowhead_user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
    )

    result = await fixture.bridge.handle_message_event(
        _message_event(content={"text": "/switch workspace"}),
    )

    assert result["status"] == "needs_workspace"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    picker = fixture.fake_client.card_replies[-1]["content"]
    # Without a saved preference the picker follows the English command, not users.locale.
    assert picker["header"]["title"]["content"] == "Choose a space"
    actions = picker["elements"][1]["actions"]
    assert {action["value"]["workspace_id"] for action in actions} == set(fixture.workspace_ids)
    assert {action["value"]["locale"] for action in actions} == {"en-US"}

    switch_click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {"value": actions[1]["value"]},
            },
        }
    )
    selected = await fixture.feishu_store.get_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
    )
    assert switch_click["toast"] == {"type": "success", "content": "Space switched. Send your question again."}
    assert selected is not None
    assert selected.workspace_id == actions[1]["value"]["workspace_id"]


@pytest.mark.asyncio
async def test_switch_workspace_picker_follows_message_language_not_account_column(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=2, locale="en-US")
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email_match",
    )

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "切换工作区"}))

    assert result["status"] == "needs_workspace"
    picker = fixture.fake_client.card_replies[-1]["content"]
    assert picker["header"]["title"]["content"] == "选择空间"
    actions = picker["elements"][1]["actions"]
    assert {action["value"]["locale"] for action in actions} == {"zh-CN"}
    switch_click = await fixture.bridge.handle_card_callback(
        {"tenant_key": "tenant_1", "event": {"operator": {"operator_id": {"open_id": "ou_user"}}, "action": {"value": actions[0]["value"]}}}
    )
    assert switch_click["toast"] == {"type": "success", "content": "已切换空间，请重新发送问题。"}


@pytest.mark.asyncio
async def test_mirror_fallback_locale_uses_saved_preference_not_account_column(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.user_settings_store import UserSettingsStore

    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, locale="zh-CN")
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email_match",
    )
    settings = UserSettingsStore(fixture.feishu_store.db_path)
    await settings.init()
    fixture.bridge.app.state.user_settings_store = settings

    # The account column says zh-CN, but without a saved preference the fallback is English.
    assert await fixture.bridge._presentation_locale(tenant_key="tenant_1", open_id="ou_user") == "en-US"
    await settings.set(fixture.user.id, "ui_language", "fr-FR")
    assert await fixture.bridge._presentation_locale(tenant_key="tenant_1", open_id="ou_user") == "fr-FR"
    assert await fixture.bridge._presentation_locale(tenant_key="tenant_1", open_id="ou_unbound") == "en-US"


@pytest.mark.asyncio
async def test_group_disabled_notice_follows_message_language_not_account_column(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, group_public=False, locale="en-US")
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email_match",
    )

    result = await fixture.bridge.handle_message_event(_group_message_event())

    assert result["status"] == "denied"
    notice = json.dumps(fixture.fake_client.ephemeral_cards[-1]["card"], ensure_ascii=False)
    assert "群聊公开答案未开启" in notice


@pytest.mark.asyncio
async def test_invalid_workspace_binding_prompts_picker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=2)
    await fixture.feishu_store.upsert_workspace_binding(
        tenant_key="tenant_1",
        chat_id="oc_chat",
        open_id="ou_user",
        bowhead_user_id=fixture.user.id,
        workspace_id="ws_missing",
    )

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "needs_workspace"
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies


@pytest.mark.asyncio
async def test_workspace_select_rejects_workspace_without_datarc_mapping(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=2)
    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "needs_workspace"
    action = fixture.fake_client.card_replies[-1]["content"]["elements"][1]["actions"][0]
    selected_workspace_id = action["value"]["workspace_id"]
    async with open_shared_ctx(fixture.feishu_store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET datarc_workspace_id=NULL WHERE id=?",
            (selected_workspace_id,),
        )
        await db.commit()

    click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {"value": action["value"]},
            },
        }
    )

    assert click["toast"]["type"] == "warning"
    assert "北极九章" in click["toast"]["content"]
    assert (
        await fixture.feishu_store.get_workspace_binding(
            tenant_key="tenant_1",
            chat_id="oc_chat",
            open_id="ou_user",
        )
        is None
    )


@pytest.mark.asyncio
async def test_clarification_callback_requires_clicker_and_resolves_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    clarification_store = ClarificationStore(fixture.feishu_store.db_path)
    await clarification_store.initialize()
    fixture.bridge.app.state.clarification_store = clarification_store
    turn_log_store = TurnLogStore(str(fixture.feishu_store.db_path))
    await turn_log_store.initialize()
    fixture.bridge.app.state.turn_log_store = turn_log_store
    registry = get_event_registry()
    await registry.close("turn_hitl")
    monkeypatch.setattr(
        registry,
        "_default_log_sink",
        OrderedTurnLogSink(turn_log_store),
    )
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email",
    )
    clarification = await clarification_store.put(
        turn_id="turn_hitl",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="经营指标",
        rationale="请选择要分析的指标。",
        candidates=[
            ClarificationCandidate(value="营业额", label="营业额"),
            ClarificationCandidate(value="利润", label="利润"),
        ],
        kind="options",
    )
    action_value = {
        "bowhead_action": "clarification_resolve",
        "request_id": clarification.request_id,
        "chosen_value": "营业额",
        "allowed_open_id": "ou_user",
    }

    wrong_click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_other"}},
                "action": {"value": action_value},
            },
        }
    )
    still_pending = await clarification_store.get(clarification.request_id)
    assert wrong_click["toast"]["type"] == "warning"
    assert still_pending is not None
    assert still_pending.status == "pending"

    ok_click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {"value": action_value},
            },
        }
    )
    resolved = await clarification_store.get(clarification.request_id)

    assert ok_click["toast"]["type"] == "success"
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.chosen_value == "营业额"
    assert resolved.chosen_values == ["营业额"]
    events = await turn_log_store.list_events("turn_hitl")
    assert [event.event_type for event in events] == ["clarification.resolved"]
    assert events[0].payload["chosen_value"] == "营业额"

    # A delayed second card click cannot overwrite the first answer or append
    # another durable resolution event.
    second_value = {**action_value, "chosen_value": "利润"}
    second_click = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {"value": second_value},
            },
        }
    )
    replayed = await clarification_store.get(clarification.request_id)
    replayed_events = await turn_log_store.list_events("turn_hitl")
    assert second_click["toast"]["type"] == "success"
    assert replayed is not None
    assert replayed.chosen_value == "营业额"
    assert [event.event_type for event in replayed_events] == ["clarification.resolved"]
    assert replayed_events[0].payload["chosen_value"] == "营业额"
    await registry.close("turn_hitl")


@pytest.mark.asyncio
async def test_clarification_readiness_failure_keeps_feishu_choice_pending(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway import turn_service

    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    clarification_store = ClarificationStore(fixture.feishu_store.db_path)
    await clarification_store.initialize()
    fixture.bridge.app.state.clarification_store = clarification_store
    readiness = {"ready": False}

    async def _resume_ready(_turn_id: str) -> bool:
        return readiness["ready"]

    monkeypatch.setattr(
        fixture.orchestrator.session_store,
        "is_turn_resume_ready",
        _resume_ready,
    )
    emitted: list[str] = []

    async def _emit_once(_app_state, *, turn_id: str, **_kwargs) -> bool:
        emitted.append(turn_id)
        return True

    monkeypatch.setattr(
        turn_service,
        "emit_clarification_resolution_once",
        _emit_once,
    )
    clarification = await clarification_store.put(
        turn_id="turn_feishu_resume_fence",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="",
        rationale="请选择",
        candidates=[ClarificationCandidate(value="营业额", label="营业额")],
        kind="options",
        resume_context={
            "session_id": "feishu:tenant_1:oc_chat:ou_user",
            "workspace_id": fixture.workspace_ids[0],
            "user_id": fixture.user.id,
            "user_text": "继续分析营业额",
        },
    )

    with pytest.raises(RuntimeError, match="clarification_resume_not_ready"):
        await fixture.bridge._resolve_clarification(
            request_id=clarification.request_id,
            user_id=fixture.user.id,
            chosen_value="营业额",
        )

    pending = await clarification_store.get(clarification.request_id)
    assert pending is not None and pending.status == "pending"
    assert emitted == []

    readiness["ready"] = True
    result = await fixture.bridge._resolve_clarification(
        request_id=clarification.request_id,
        user_id=fixture.user.id,
        chosen_value="营业额",
    )
    resolved = await clarification_store.get(clarification.request_id)
    assert result["action"] == "turn_resume"
    assert resolved is not None and resolved.status == "resolved"
    assert emitted == [clarification.turn_id]


@pytest.mark.asyncio
async def test_clarification_model_unavailable_leaves_feishu_card_pending(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway import turn_service

    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    clarification_store = ClarificationStore(fixture.feishu_store.db_path)
    await clarification_store.initialize()
    fixture.bridge.app.state.clarification_store = clarification_store
    clarification = await clarification_store.put(
        turn_id="turn_model_unavailable",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="",
        rationale="请选择",
        candidates=[ClarificationCandidate(value="营业额", label="营业额")],
        kind="options",
        resume_context={
            "session_id": "session_model_unavailable",
            "user_text": "继续分析",
            "team_id": "team_unavailable",
        },
    )

    async def _unavailable(*_args, **_kwargs):
        return turn_service.TeamModelRuntimeResolution(
            result="resolver_error",
            reason="router_build_failed",
        )

    monkeypatch.setattr(
        turn_service,
        "resolve_team_model_runtime",
        _unavailable,
    )

    with pytest.raises(RuntimeError, match="team_model_runtime_unavailable"):
        await fixture.bridge._resolve_clarification(
            request_id=clarification.request_id,
            user_id=fixture.user.id,
            chosen_value="营业额",
        )

    stored = await clarification_store.get(clarification.request_id)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.chosen_value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol_store",
    [
        None,
        _LegacySurfaceProtocolStore(version=1),
        _LegacySurfaceProtocolStore(error=RuntimeError("binding unavailable")),
    ],
)
async def test_clarification_terminal_surface_guard_blocks_source_write(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    protocol_store,
) -> None:
    fixture = await _build_bridge_fixture(
        tmp_path,
        monkeypatch,
        workspace_count=1,
    )
    clarification_store = ClarificationStore(fixture.feishu_store.db_path)
    await clarification_store.initialize()
    fixture.bridge.app.state.clarification_store = clarification_store
    fixture.bridge.app.state.terminal_protocol_store = protocol_store
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email",
    )
    clarification = await clarification_store.put(
        turn_id="turn_managed",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="经营指标",
        rationale="请选择要分析的指标。",
        candidates=[ClarificationCandidate(value="营业额", label="营业额")],
        kind="options",
        resume_context={
            "session_id": "feishu:tenant_1:oc_chat:ou_user",
        },
    )

    response = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {
                    "value": {
                        "bowhead_action": "clarification_resolve",
                        "request_id": clarification.request_id,
                        "chosen_value": "营业额",
                        "allowed_open_id": "ou_user",
                        "locale": "zh-CN",
                    }
                },
            },
        }
    )

    pending = await clarification_store.get(clarification.request_id)
    assert response["toast"]["type"] == "warning"
    assert "Web" in response["toast"]["content"]
    assert pending is not None and pending.status == "pending"


@pytest.mark.asyncio
async def test_lifecycle_rejection_sends_finite_error_card_without_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)

    class RejectingLifecycle:
        async def revive(self, **_kwargs):
            raise SessionLifecycleError("terminal_protocol_future_version")

    fixture.bridge.app.state.session_lifecycle_coordinator = RejectingLifecycle()

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "继续分析"}))

    assert result == {
        "status": "rejected",
        "code": "terminal_protocol_future_version",
    }
    assert fixture.orchestrator.requests == []
    assert fixture.fake_client.card_replies
    card_text = json.dumps(
        fixture.fake_client.card_replies[-1]["content"],
        ensure_ascii=False,
    )
    assert "该会话由更新版本的服务写入" in card_text
    assert "secret" not in card_text


@pytest.mark.asyncio
async def test_clarification_resume_rejection_returns_warning_not_false_success(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    clarification_store = ClarificationStore(fixture.feishu_store.db_path)
    await clarification_store.initialize()
    fixture.bridge.app.state.clarification_store = clarification_store
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email",
    )

    class RejectingLifecycle:
        async def revive(self, **_kwargs):
            raise SessionLifecycleError("session_revive_blocked")

    fixture.bridge.app.state.session_lifecycle_coordinator = RejectingLifecycle()

    async def resume_ready(_turn_id: str) -> bool:
        return True

    monkeypatch.setattr(
        fixture.orchestrator.session_store,
        "is_turn_resume_ready",
        resume_ready,
    )
    turn_log_store = TurnLogStore(str(fixture.feishu_store.db_path))
    await turn_log_store.initialize()
    fixture.bridge.app.state.turn_log_store = turn_log_store
    registry = get_event_registry()
    await registry.close("turn_resume_rejected")
    monkeypatch.setattr(
        registry,
        "_default_log_sink",
        OrderedTurnLogSink(turn_log_store),
    )
    clarification = await clarification_store.put(
        turn_id="turn_resume_rejected",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="经营指标",
        rationale="请选择要分析的指标。",
        candidates=[ClarificationCandidate(value="营业额", label="营业额")],
        kind="options",
        resume_context={
            "session_id": "feishu:tenant_1:oc_chat:ou_user",
            "workspace_id": fixture.workspace_ids[0],
            "user_id": fixture.user.id,
            "member_uid": "member_1",
            "user_text": "继续分析营业额",
            "team_id": fixture.user.team_id,
        },
    )

    response = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {
                    "value": {
                        "bowhead_action": "clarification_resolve",
                        "request_id": clarification.request_id,
                        "chosen_value": "营业额",
                        "allowed_open_id": "ou_user",
                        "locale": "zh-CN",
                    }
                },
            },
        }
    )

    resolved = await clarification_store.get(clarification.request_id)
    assert response["toast"]["type"] == "warning"
    assert "选择已保存" in response["toast"]["content"]
    assert "恢复先前任务" in response["toast"]["content"]
    assert resolved is not None and resolved.status == "resolved"
    assert fixture.orchestrator.requests == []


@pytest.mark.asyncio
async def test_clarification_resume_credit_exhausted_keeps_card_pending(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """卡片续跑与新消息同一 credit 准入：拦在答案被消费之前——卡片保持 pending、如实告知额度用完，
    充值后再点同一张卡就继续（web 侧同形，见 test_clarifications_routes）。"""
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    clarification_store = ClarificationStore(fixture.feishu_store.db_path)
    await clarification_store.initialize()
    fixture.bridge.app.state.clarification_store = clarification_store
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email",
    )

    async def resume_ready(_turn_id: str) -> bool:
        return True

    monkeypatch.setattr(
        fixture.orchestrator.session_store,
        "is_turn_resume_ready",
        resume_ready,
    )
    turn_log_store = TurnLogStore(str(fixture.feishu_store.db_path))
    await turn_log_store.initialize()
    fixture.bridge.app.state.turn_log_store = turn_log_store
    registry = get_event_registry()
    await registry.close("turn_resume_credit")
    monkeypatch.setattr(
        registry,
        "_default_log_sink",
        OrderedTurnLogSink(turn_log_store),
    )
    clarification = await clarification_store.put(
        turn_id="turn_resume_credit",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="经营指标",
        rationale="请选择要分析的指标。",
        candidates=[ClarificationCandidate(value="营业额", label="营业额")],
        kind="options",
        resume_context={
            "session_id": "feishu:tenant_1:oc_chat:ou_user",
            "workspace_id": fixture.workspace_ids[0],
            "user_id": fixture.user.id,
            "member_uid": "member_1",
            "user_text": "继续分析营业额",
            "team_id": fixture.user.team_id,
        },
    )
    async with open_shared_ctx(fixture.feishu_store.db_path) as db:
        await db.execute(
            "UPDATE users SET credit_balance=-30 WHERE id=?",
            (fixture.user.id,),
        )
        await db.commit()

    response = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {
                    "value": {
                        "bowhead_action": "clarification_resolve",
                        "request_id": clarification.request_id,
                        "chosen_value": "营业额",
                        "allowed_open_id": "ou_user",
                        "locale": "zh-CN",
                    }
                },
            },
        }
    )

    blocked_row = await clarification_store.get(clarification.request_id)
    assert response["toast"]["type"] == "warning"
    assert "额度已用完" in response["toast"]["content"]
    assert blocked_row is not None
    assert blocked_row.status == "pending", "答案不该被消费——消费了卡片就没了，用户再也点不回来"
    assert blocked_row.chosen_value is None
    assert fixture.orchestrator.requests == []

    # After top-up, click the same card again: continue with saved choices (help instructs users to click again accordingly).
    # This fixture does not put the turn into a resumable paused state; intercept resume dispatch and verify only that dispatch was reached.
    from bowhead_gateway import turn_service

    spawned: list[str] = []

    async def _record_spawn(_orchestrator, turn_req, **_kwargs):
        spawned.append(turn_req.turn_id)
        return SimpleNamespace(execution_generation=2)

    monkeypatch.setattr(turn_service, "spawn_resume_turn", _record_spawn)
    async with open_shared_ctx(fixture.feishu_store.db_path) as db:
        await db.execute(
            "UPDATE users SET credit_balance=500 WHERE id=?",
            (fixture.user.id,),
        )
        await db.commit()
    retried = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {
                    "value": {
                        "bowhead_action": "clarification_resolve",
                        "request_id": clarification.request_id,
                        "chosen_value": "营业额",
                        "allowed_open_id": "ou_user",
                        "locale": "zh-CN",
                    }
                },
            },
        }
    )
    assert retried["toast"]["type"] == "success"
    assert spawned == ["turn_resume_credit"]
    settled = await clarification_store.get(clarification.request_id)
    assert settled is not None and settled.status == "resolved"
    assert settled.chosen_value == "营业额"


@pytest.mark.asyncio
async def test_retry_skip_callbacks_return_honest_ack_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    turn_log_store = TurnLogStore(str(fixture.feishu_store.db_path))
    await turn_log_store.initialize()
    fixture.bridge.app.state.turn_log_store = turn_log_store
    await turn_log_store.record(
        turn_id="turn_retry_skip",
        event_type="step_started",
        payload={"step_id": "step_1", "header_zh": "查询数据"},
    )

    retry = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {
                    "value": {
                        "bowhead_action": "retry_step",
                        "turn_id": "turn_retry_skip",
                        "step_id": "step_1",
                    },
                },
            },
        }
    )
    skip = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "action": {
                    "value": {
                        "bowhead_action": "skip_step",
                        "turn_id": "turn_retry_skip",
                        "step_id": "step_1",
                    },
                },
            },
        }
    )

    assert retry["toast"]["type"] == "success"
    assert retry["toast"]["content"] == "Your request was received. This acknowledgment does not mean the step has run again."
    assert skip["toast"]["type"] == "success"
    assert skip["toast"]["content"] == "Your request was received. This acknowledgment does not mean the step has run again."
    await get_event_registry().close("turn_retry_skip")


@pytest.mark.asyncio
async def test_member_uid_dev_fallback_is_dev_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    fixture.bridge.app.state.member_resolver = None
    fixture.bridge.app.state.settings = Settings(auth_mode="jwt")

    member_uid = await fixture.bridge._resolve_member_uid(
        identity="alice@example.com",
        datarc_workspace_id="datarc_ws_0",
    )

    assert member_uid is None


@dataclass
class BridgeFixture:
    bridge: FeishuTurnBridge
    feishu_store: FeishuEventStore
    fake_client: FakeFeishuClient
    orchestrator: FakeOrchestrator
    user: Any
    workspace_ids: list[str]
    datarc_workspace_ids: list[str]


async def _build_bridge_fixture(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_count: int,
    group_public: bool = False,
    email_verified: bool = True,
    locale: str | None = "zh-CN",
    orchestrator_cls: type[Any] | None = None,
) -> BridgeFixture:
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    user_store = UserStore(db_path)
    await user_store.initialize()
    session_store = SessionStore(db_path)
    await session_store.initialize()
    turn_log_store = TurnLogStore(str(db_path))
    await turn_log_store.initialize()
    feishu_store = FeishuEventStore(db_path)
    await feishu_store.initialize()

    team = await user_store.create_team(name="Acme", domain="example.com", max_seats=5)
    await user_store.update_team_llm_config(team.id, _valid_team_llm_config())
    user = await user_store.create_user(
        team_id=team.id,
        email="alice@example.com",
        display_name="Alice",
        email_verified=email_verified,
    )
    if locale is not None:
        async with open_shared_ctx(db_path) as db:
            await db.execute("UPDATE users SET locale=? WHERE id=?", (locale, user.id))
            await db.commit()
        user = await user_store.get_user(user.id)
        assert user is not None
    workspace_ids: list[str] = []
    datarc_workspace_ids: list[str] = []
    for idx in range(workspace_count):
        datarc_workspace_id = f"datarc_ws_{idx}"
        workspace_id = await user_store.sync_workspace_from_datarc(
            team_id=team.id,
            datarc_workspace_id=datarc_workspace_id,
            name=f"Workspace {idx}",
        )
        await user_store.add_member(workspace_id=workspace_id, user_id=user.id, role="member")
        workspace_ids.append(workspace_id)
        datarc_workspace_ids.append(datarc_workspace_id)

    fake_client = FakeFeishuClient(email="alice@example.com")
    orchestrator = (orchestrator_cls or FakeOrchestrator)(session_store)
    registry = get_event_registry()
    registry.set_log_sink(OrderedTurnLogSink(turn_log_store))
    orchestrator.event_registry = registry
    app = FastAPI()
    app.state.settings = Settings(feishu_group_public_answers_enabled=group_public)
    app.state.user_store = user_store
    app.state.session_store = session_store
    app.state.turn_log_store = turn_log_store
    app.state.orchestrator = orchestrator
    app.state.legacy_session_lifecycle_allowed = True
    app.state.session_lifecycle_coordinator = _NoopSessionLifecycleCoordinator()
    app.state.member_resolver = StaticMemberResolver("member_1")
    app.state.datarc_connection_store = FakeDatarcConnectionStore(auth_key="team-key")
    app.state.datarc_client_factory = FakeDatarcClientFactory(object())
    app.state.entitlements = None
    app.state.team_router_registry = _StubTeamRouterRegistry()
    app.state.terminal_protocol_store = _LegacySurfaceProtocolStore()

    async def fast_run_turn_and_cleanup(
        orchestrator: FakeOrchestrator,
        req: TurnRequest,
        user_store: Any = None,
        team_id: str | None = None,
        metrics_log: Any = None,
        entitlements: Any = None,
        admitted_legacy_bus: Any = None,
        cleanup_grace_s: float = 5.0,
    ) -> None:
        assert admitted_legacy_bus is not None
        assert cleanup_grace_s == 5.0
        await orchestrator.run_turn(req)

    monkeypatch.setattr(chat_routes, "_run_turn_and_cleanup", fast_run_turn_and_cleanup)

    return BridgeFixture(
        bridge=FeishuTurnBridge(
            app=app,
            client=fake_client,  # type: ignore[arg-type]
            store=feishu_store,
            config=FeishuSecurityConfig(
                bot_open_id="ou_bot",
                group_public_answers_enabled=group_public,
            ),
        ),
        feishu_store=feishu_store,
        fake_client=fake_client,
        orchestrator=orchestrator,
        user=user,
        workspace_ids=workspace_ids,
        datarc_workspace_ids=datarc_workspace_ids,
    )


def _message_event(
    *,
    chat_type: str = "p2p",
    content: dict[str, Any] | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant_1",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user", "union_id": "on_user"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_msg",
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps(content or {"text": "hello"}, ensure_ascii=False),
                "mentions": mentions or [],
            },
        },
    }


def _group_message_event() -> dict[str, Any]:
    return _message_event(
        chat_type="group",
        content={"text": '<at id="bot">Bowhead</at> 看 GMV'},
        mentions=[{"id": {"open_id": "ou_bot"}, "key": '<at id="bot">Bowhead</at>'}],
    )


def _feishu_route_app(
    settings: Settings,
    *,
    public_base_url: str = "https://bowhead.example.com",
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    normalized = normalize_public_base_url(public_base_url)

    @app.middleware("http")
    async def public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        token = set_public_access_context(
            PublicAccessContext(
                base_url=normalized.base_url,
                origin=normalized.origin,
                base_path=normalized.base_path,
                app_root=normalized.app_root,
                match_source="forwarded_url",
            ),
        )
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    app.include_router(feishu_routes.router, prefix="/api/v1/integrations/feishu")
    return app


class FakeFeishuClient:
    def __init__(self, *, email: str) -> None:
        self.email = email
        self.card_replies: list[dict[str, Any]] = []
        self.text_replies: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.ephemeral_cards: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    async def reply_message(
        self,
        *,
        message_id: str,
        msg_type: str,
        content: dict[str, Any],
        tenant_key: str | None = None,
        uuid: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "message_id": message_id,
            "msg_type": msg_type,
            "content": content,
            "tenant_key": tenant_key,
            "uuid": uuid,
        }
        if msg_type == "interactive":
            self.card_replies.append(record)
            return {"message_id": f"card_{len(self.card_replies)}"}
        self.text_replies.append(record)
        return {"message_id": f"text_{len(self.text_replies)}"}

    async def patch_card(
        self,
        *,
        message_id: str,
        card: dict[str, Any],
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        self.patches.append({"message_id": message_id, "card": card, "tenant_key": tenant_key})
        return {}

    async def send_ephemeral_card(
        self,
        *,
        chat_id: str,
        open_id: str,
        card: dict[str, Any],
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        self.ephemeral_cards.append(
            {
                "chat_id": chat_id,
                "open_id": open_id,
                "card": card,
                "tenant_key": tenant_key,
            }
        )
        return {}

    async def add_reaction(
        self,
        *,
        message_id: str,
        emoji_type: str,
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        self.reactions.append(
            {
                "message_id": message_id,
                "emoji_type": emoji_type,
                "tenant_key": tenant_key,
            }
        )
        return {}

    async def get_user_email(
        self,
        *,
        open_id: str,
        tenant_key: str | None = None,
    ) -> str | None:
        del tenant_key
        return self.email


class _StubTeamRouterRegistry:
    """credential-readiness 批2：bridge 现要求有团队的 turn 解析出团队 router，
    未配置即渠道内提示不建 turn。本文件用例测的是 turn 语义（配置视为就绪），
    stub 恒返一个占位 router；「未配置 → 提示」用例显式置 None 注册表。"""

    async def get(self, team_id: str, cfg: object, version: int = 0) -> object:
        return object()


def _valid_team_llm_config() -> dict[str, Any]:
    provider = "team"
    model = "team-model"
    binding = {
        "primary": {"provider": provider, "model": model},
        "fallback": [],
    }
    return {
        "schema_version": 2,
        "providers": {
            provider: {
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "test-team-secret",
                "models": [model],
            },
        },
        "agents": {
            "lead": binding,
            "modeler": binding,
            "analyst": {"default": binding, "profiles": {}},
            "narrative": binding,
            "echo": binding,
            "judge": binding,
        },
    }


class StaticMemberResolver:
    def __init__(self, member_uid: str) -> None:
        self.member_uid = member_uid

    async def resolve(self, identity: str, workspace_id: str) -> str:
        return self.member_uid


class FakeDatarcMembers:
    def __init__(self, member_uid: str) -> None:
        self.member_uid = member_uid
        self.calls: list[tuple[str, str]] = []

    async def fetch_workspace_member(self, *, datarc_workspace_id: str, username: str) -> dict[str, str]:
        self.calls.append((datarc_workspace_id, username))
        return {"uid": self.member_uid}


class BoomMemberResolver:
    async def resolve(self, identity: str, workspace_id: str) -> str:
        del identity, workspace_id
        raise RuntimeError("datarc timeout")


class FakeDatarcConnectionStore:
    def __init__(self, *, auth_key: str | None) -> None:
        self.auth_key = auth_key
        self.listed_team_ids: list[str] = []

    async def list_connections(self, team_id: str) -> list[Any]:
        self.listed_team_ids.append(team_id)
        return [SimpleNamespace(id="dc_1", base_url="https://org.datarc.example")]

    async def get_connection_auth(self, dc_id: str, team_id: str) -> str | None:
        del dc_id, team_id
        return self.auth_key


class FakeDatarcClientFactory:
    def __init__(self, client: object) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def get_client(self, base_url: str, auth_key: str) -> object:
        self.calls.append((base_url, auth_key))
        return self.client


class DenyingEntitlements:
    def __init__(self, message: str) -> None:
        self.message = message
        self.team_ids: list[str | None] = []

    async def assert_can_run_turn(self, team_id: str | None) -> None:
        self.team_ids.append(team_id)
        raise RuntimeError(self.message)


@dataclass
class FakeOrchestrator:
    session_store: SessionStore
    requests: list[TurnRequest] | None = None
    inline_artifacts: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.requests is None:
            self.requests = []
        self.event_registry = get_event_registry()
        self.dispatch_owner_id = "test_feishu"
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def run_turn(self, req: TurnRequest) -> TurnResult:
        assert self.requests is not None
        self.requests.append(req)
        await asyncio.sleep(0.01)
        bus, _floor = await self.event_registry.projection_checkpoint(
            req.turn_id,
        )
        assert bus is not None
        await bus.emit(
            OrchestratorEvent(
                event_type="step_list_card_created",
                turn_id=req.turn_id,
                payload={},
            )
        )
        await bus.emit(
            OrchestratorEvent(
                event_type="step_started",
                turn_id=req.turn_id,
                payload={"step_id": "s1", "header_zh": "查询指标"},
            )
        )
        await bus.emit(
            OrchestratorEvent(
                event_type="step_done",
                turn_id=req.turn_id,
                payload={"step_id": "s1", "state": "success", "output_preview": "ok"},
            )
        )
        final_payload: dict[str, Any] = {"text": "这是最终答案"}
        if self.inline_artifacts:
            final_payload["inline_artifacts"] = self.inline_artifacts
        await bus.emit(
            OrchestratorEvent(
                event_type="final_answer",
                turn_id=req.turn_id,
                payload=final_payload,
            )
        )
        assert await self.session_store.mark_completed(
            turn_id=req.turn_id,
            intent=IntentType.LOOKUP.value,
            final_answer="这是最终答案",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
        )
        await bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=req.turn_id,
                payload={"status": "completed"},
            )
        )
        return TurnResult(
            turn_id=req.turn_id,
            status=TurnStatus.COMPLETED,
            intent=IntentType.LOOKUP,
            final_answer="这是最终答案",
        )


class BurstProgressOrchestrator(FakeOrchestrator):
    async def run_turn(self, req: TurnRequest) -> TurnResult:
        assert self.requests is not None
        self.requests.append(req)
        await asyncio.sleep(0.01)
        bus, _floor = await self.event_registry.projection_checkpoint(
            req.turn_id,
        )
        assert bus is not None
        await bus.emit(
            OrchestratorEvent(
                event_type="step_list_card_created",
                turn_id=req.turn_id,
                payload={},
            )
        )
        await bus.emit(
            OrchestratorEvent(
                event_type="step_started",
                turn_id=req.turn_id,
                payload={"step_id": "s1", "header_zh": "查询指标"},
            )
        )
        for idx in range(8):
            await bus.emit(
                OrchestratorEvent(
                    event_type="step_progress",
                    turn_id=req.turn_id,
                    payload={"step_id": "s1", "content_delta": f"p{idx} ", "completed": idx, "total": 8},
                )
            )
            await bus.emit(
                OrchestratorEvent(
                    event_type="narrative.delta",
                    turn_id=req.turn_id,
                    payload={"delta": f"n{idx} "},
                )
            )
        await bus.emit(
            OrchestratorEvent(
                event_type="final_answer",
                turn_id=req.turn_id,
                payload={"text": "burst final"},
            )
        )
        assert await self.session_store.mark_completed(
            turn_id=req.turn_id,
            intent=IntentType.LOOKUP.value,
            final_answer="burst final",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
        )
        await bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=req.turn_id,
                payload={"status": "completed"},
            )
        )
        return TurnResult(
            turn_id=req.turn_id,
            status=TurnStatus.COMPLETED,
            intent=IntentType.LOOKUP,
            final_answer="burst final",
        )


async def _wait_for(predicate: Any, *, timeout_s: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


async def _wait_for_message_status(
    store: FeishuEventStore,
    message_id: str,
    status: str,
    *,
    timeout_s: float = 1.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await store.get_message_status(message_id) == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"message {message_id} did not reach status {status}")


async def _wait_for_dispatch_settlement_and_close(
    orchestrator: FakeOrchestrator,
    turn_id: str,
) -> None:
    async def _wait_for_settlement() -> None:
        while (
            await orchestrator.session_store.get_chat_spawn_dispatch_projection_state(
                turn_id=turn_id,
            )
            != "settled"
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_for_settlement(), timeout=1)
    await orchestrator.event_registry.close(turn_id)


def _encrypted_feishu_body(
    payload: dict[str, Any],
    encrypt_key: str,
) -> tuple[bytes, dict[str, str]]:
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = b"1234567890abcdef"
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    pad_len = 16 - (len(plain) % 16)
    padded = plain + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    body = {"encrypt": base64.b64encode(iv + encrypted).decode("ascii")}
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    timestamp = "1234567890"
    nonce = "nonce_for_test"
    signature = hashlib.sha256(
        f"{timestamp}{nonce}{encrypt_key}".encode() + raw_body,
    ).hexdigest()
    return raw_body, {
        "x-lark-request-timestamp": timestamp,
        "x-lark-request-nonce": nonce,
        "x-lark-signature": signature,
    }


def _fixture_raw_body(fixture: dict[str, Any]) -> bytes:
    raw_body = fixture.get("raw_body", fixture.get("body"))
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    if isinstance(raw_body, dict):
        return json.dumps(raw_body, separators=(",", ":"), ensure_ascii=False).encode()
    raise AssertionError("fixture must include raw_body string or body object")


def _answer_cards(client: FakeFeishuClient) -> list[dict[str, Any]]:
    return [reply for reply in client.card_replies if str(reply.get("uuid") or "").startswith("feishu_answer_")]


async def test_bridge_replies_unconfigured_card_and_skips_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """credential-readiness 批2：有团队但解析不出团队 router → 渠道内 error_card
    （文案走 IM 双语目录单源）+ 不建 turn；此前不带 router 继续跑，prod 下 turn 内
    打到 RefusalAdapter loud 失败但无渠道级归因。"""
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    fixture.bridge.app.state.team_router_registry = None  # Unconfigured state.

    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))

    assert result["status"] == "team_model_unconfigured"
    assert fixture.orchestrator.requests == []  # Do not create a turn.
    cards = fixture.fake_client.card_replies
    assert cards, "渠道内必须有提示卡"
    body = str(cards[-1])
    assert "团队还未配置模型" in body  # Role-neutral IM catalog wording (zh default).


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locale, selected, choice, approval",
    [
        (
            "de-DE",
            "Der Raum wurde gewechselt. Senden Sie Ihre Frage erneut.",
            "Ihre Auswahl wurde übermittelt.",
            "Schließen Sie diese Genehmigung in Bowhead Web ab.",
        ),
        ("fr-FR", "L’espace a été changé. Renvoyez votre question.", "Votre choix a été envoyé.", "Effectuez cette approbation dans Bowhead Web."),
    ],
)
async def test_card_snapshot_then_saved_preference_reach_real_card_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    selected: str,
    choice: str,
    approval: str,
) -> None:
    from bowhead_core.user_settings_store import UserSettingsStore
    from bowhead_gateway.im.error_messages import im_interface_message

    # The account column disagrees on purpose: only the saved preference selects card copy.
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, locale="zh-CN")
    settings = UserSettingsStore(fixture.feishu_store.db_path)
    await settings.init()
    await settings.set(fixture.user.id, "ui_language", locale)
    fixture.bridge.app.state.user_settings_store = settings
    await fixture.feishu_store.upsert_binding(
        tenant_key="tenant_1",
        open_id="ou_user",
        union_id="on_user",
        email="alice@example.com",
        bowhead_user_id=fixture.user.id,
        binding_method="email",
    )

    async def click(action: str, **params: Any) -> dict[str, Any]:
        # Cards rendered before buttons carried a locale snapshot: the saved preference applies.
        return await fixture.bridge.handle_card_callback(
            {
                "event": {
                    "operator": {"operator_id": {"open_id": "ou_user"}},
                    "action": {"value": {"bowhead_action": action, "tenant_key": "tenant_1", **params}},
                },
            }
        )

    # A card's own snapshot wins over the saved preference.
    assert (await click("unknown", locale="zh-CN"))["toast"]["content"] == im_interface_message("unknown_action", "zh-CN")

    for action, params, key in [
        ("workspace_select", {}, "space_selection_missing"),
        ("workspace_select", {"chat_id": "oc_chat", "workspace_id": "ws_missing"}, "space_access_lost"),
        ("clarification_resolve", {}, "clarification_missing"),
        ("retry_step", {}, "step_missing"),
        ("approval_open_web", {"allowed_open_id": "ou_original"}, "owner_only"),
        ("unknown", {}, "unknown_action"),
    ]:
        response = await click(action, **params)
        assert response == {"toast": {"type": "warning", "content": im_interface_message(key, locale)}}
    assert (await click("approval_open_web"))["toast"]["content"] == approval
    turn_log_store = fixture.bridge.app.state.turn_log_store
    fixture.bridge.app.state.turn_log_store = None
    assert (await click("retry_step", turn_id="turn_1", step_id="step_1"))["toast"]["content"] == im_interface_message(
        "step_service_unavailable", locale
    )

    response = await click("workspace_select", chat_id="oc_chat", workspace_id=fixture.workspace_ids[0])
    assert response == {"toast": {"type": "success", "content": selected}}
    binding = await fixture.feishu_store.get_workspace_binding(tenant_key="tenant_1", chat_id="oc_chat", open_id="ou_user")
    assert binding is not None and binding.workspace_id == fixture.workspace_ids[0]

    fixture.bridge.app.state.turn_log_store = turn_log_store
    clarifications = ClarificationStore(fixture.feishu_store.db_path)
    await clarifications.initialize()
    fixture.bridge.app.state.clarification_store = clarifications
    pending = await clarifications.put(
        turn_id="turn_localized_choice",
        user_id=fixture.user.id,
        workspace_id=fixture.workspace_ids[0],
        token="metric",
        rationale="Choose a metric",
        candidates=[ClarificationCandidate(value="revenue", label="Revenue")],
        kind="options",
    )
    response = await click("clarification_resolve", request_id=pending.request_id, chosen_value="revenue", allowed_open_id="ou_user")
    assert response == {"toast": {"type": "success", "content": choice}}
    resolved = await clarifications.get(pending.request_id)
    assert resolved is not None and resolved.status == "resolved" and resolved.chosen_value == "revenue"
    await get_event_registry().close("turn_localized_choice")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locale, processing, answer",
    [("de-DE", "Bowhead arbeitet", "Abschließende Bowhead-Antwort"), ("fr-FR", "Bowhead travaille", "Réponse finale de Bowhead")],
)
async def test_saved_interface_language_reaches_mirrored_cards_and_answer_language_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    processing: str,
    answer: str,
) -> None:
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_settings_store import UserSettingsStore

    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    # The account locale column is not a saved preference; the saved interface setting is.
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, locale="zh-CN")
    settings = UserSettingsStore(fixture.feishu_store.db_path)
    await settings.init()
    await settings.set(fixture.user.id, "ui_language", locale)
    await settings.set(fixture.user.id, "output_language", "auto")
    fixture.bridge.app.state.user_settings_store = settings
    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "Revenue"}))
    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    assert fixture.fake_client.card_replies[0]["content"]["header"]["title"]["content"] == processing
    assert _answer_cards(fixture.fake_client)[0]["content"]["header"]["title"]["content"] == answer
    request = fixture.orchestrator.requests[0]
    assert request.language_context.ui_language == locale
    assert request.language_context.output_language == locale
    # Model-authored content keeps its persisted language; only static card copy is localized.
    assert "这是最终答案" in json.dumps(_answer_cards(fixture.fake_client)[0], ensure_ascii=False)
    await _wait_for_dispatch_settlement_and_close(fixture.orchestrator, request.turn_id)

    inbound = parse_message_event(_message_event(), bot_open_id="ou_bot")
    assert inbound is not None
    bound, status = await fixture.bridge._resolve_identity_and_workspace(inbound)
    assert bound is not None and status == "ok"
    await settings.set(fixture.user.id, "output_language", "en-US")
    context = await fixture.bridge._resolve_im_language_context(bound, "Revenue", request.session_id)
    assert context.ui_language == locale and context.output_language == "en-US"
    team_settings = TeamSettingsStore(fixture.feishu_store.db_path)
    await team_settings.init()
    await team_settings.set(
        fixture.user.team_id,
        "language_policy",
        json.dumps({"defaults": {"output_language": "zh-CN"}, "locks": {"output_language": True}}),
        "test_admin",
    )
    fixture.bridge.app.state.team_settings_store = team_settings
    context = await fixture.bridge._resolve_im_language_context(bound, "Revenue", request.session_id)
    assert context.ui_language == locale and context.output_language == "zh-CN"


@pytest.mark.asyncio
async def test_answer_card_title_follows_answer_language_while_other_cards_follow_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.user_settings_store import UserSettingsStore

    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, locale="zh-CN")
    settings = UserSettingsStore(fixture.feishu_store.db_path)
    await settings.init()
    await settings.set(fixture.user.id, "ui_language", "zh-CN")
    await settings.set(fixture.user.id, "output_language", "en-US")
    fixture.bridge.app.state.user_settings_store = settings
    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看一下上个月的 GMV"}))
    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    request = fixture.orchestrator.requests[0]
    assert (request.language_context.ui_language, request.language_context.output_language) == ("zh-CN", "en-US")
    assert fixture.fake_client.card_replies[0]["content"]["header"]["title"]["content"] == "Bowhead 正在处理"
    assert _answer_cards(fixture.fake_client)[0]["content"]["header"]["title"]["content"] == "Bowhead final answer"
    await _wait_for_dispatch_settlement_and_close(fixture.orchestrator, request.turn_id)


def test_feishu_answer_card_uses_answer_locale_for_title_and_empty_fallback() -> None:
    card = renderer.answer_card("", locale="zh-CN", answer_locale="en-US", sources_lines=["[1] a.md"])
    dumped = json.dumps(card, ensure_ascii=False)
    assert card["header"]["title"]["content"] == "Bowhead final answer"
    assert "Done." in dumped and "**来源**" in dumped
    assert renderer.answer_card("", locale="zh-CN")["header"]["title"]["content"] == "Bowhead 最终答案"


@pytest.mark.asyncio
async def test_unset_language_follows_the_feishu_message_then_the_previous_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway.im.error_messages import im_interface_message

    # The account locale column says English; nothing is saved, so the Chinese question wins.
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, locale="en-US")
    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看一下上个月的 GMV"}))
    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    request = fixture.orchestrator.requests[0]
    assert (request.language_context.ui_language, request.language_context.output_language) == ("zh-CN", "zh-CN")
    assert fixture.fake_client.card_replies[0]["content"]["header"]["title"]["content"] == im_interface_message("processing_title", "zh-CN")
    assert _answer_cards(fixture.fake_client)[0]["content"]["header"]["title"]["content"] == "Bowhead 最终答案"
    await _wait_for_dispatch_settlement_and_close(fixture.orchestrator, request.turn_id)

    inbound = parse_message_event(_message_event(), bot_open_id="ou_bot")
    assert inbound is not None
    bound, status = await fixture.bridge._resolve_identity_and_workspace(inbound)
    assert bound is not None and status == "ok"
    # A bare choice carries no language; it follows the Chinese answer persisted in this session.
    follow_up = await fixture.bridge._resolve_im_language_context(bound, "1", request.session_id)
    assert follow_up.output_language == "zh-CN"
    switched = await fixture.bridge._resolve_im_language_context(bound, "Show revenue by region", request.session_id)
    assert switched.output_language == "en-US"


@pytest.mark.asyncio
async def test_unbound_feishu_messages_and_callbacks_default_to_english(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1, locale=None)
    result = await fixture.bridge.handle_message_event(_message_event(content={"text": ""}))
    assert result["status"] == "empty_message"
    card = fixture.fake_client.card_replies[-1]["content"]
    assert card["header"]["title"]["content"] == "Empty message"
    assert card["elements"][0]["text"]["content"] == "Send a text question."
    response = await fixture.bridge.handle_card_callback(
        {
            "tenant_key": "tenant_1",
            "event": {"operator": {"open_id": "ou_unbound"}, "action": {"value": {"bowhead_action": "clarification_resolve", "request_id": "req_1"}}},
        }
    )
    assert response == {"toast": {"type": "warning", "content": "Link your Bowhead account first."}}


@pytest.mark.parametrize("locale, title", [("de-DE", "Daten abfragen"), ("fr-FR", "Interroger les données")])
def test_feishu_step_headers_localize_generated_names_and_preserve_custom_titles(locale: str, title: str) -> None:
    state = renderer.FeishuTurnCardState(locale=locale)
    payload = {"step_id": "step_1", "tool_name": "bi_ask_question", "header_zh": "查询数据"}
    assert renderer.apply_event_to_state(state, "step_started", payload)
    assert state.steps["step_1"].title == title
    renderer.apply_event_to_state(
        state, "step_started", {"step_id": "team_step", "tool_name": "teammcp.connection.read", "header_zh": "团队工具 · read"}
    )
    expected_prefix = "Teamwerkzeug" if locale == "de-DE" else "Outil d’équipe"
    assert state.steps["team_step"].title == f"{expected_prefix} · read"
    payload["header_zh"] = "Profit < Target"
    renderer.apply_event_to_state(state, "step_started", payload)
    assert state.steps["step_1"].title == "Profit < Target"


@pytest.mark.parametrize(
    ("locale", "remember", "recall"),
    [
        ("zh-CN", "存入记忆", "查找记忆"),
        ("en-US", "Save to memory", "Recall memory"),
        ("de-DE", "Im Gedächtnis speichern", "Erinnerungen abrufen"),
        ("fr-FR", "Enregistrer en mémoire", "Retrouver en mémoire"),
    ],
)
def test_feishu_step_titles_name_memory_tools(locale: str, remember: str, recall: str) -> None:
    """remember/recall used to show their bare tool names on status cards."""
    state = renderer.FeishuTurnCardState(locale=locale)
    for step_id, tool_name, header in (("s_remember", "remember", "remember"), ("s_recall", "recall", "查找记忆")):
        renderer.apply_event_to_state(state, "step_started", {"step_id": step_id, "tool_name": tool_name, "header_zh": header})
    assert state.steps["s_remember"].title == remember
    assert state.steps["s_recall"].title == recall


@pytest.mark.parametrize("locale, phrase", [("de-DE", "Datenzeilen"), ("fr-FR", "lignes de données")])
def test_localized_chart_notes_preserve_comparison_characters_and_exit(locale: str, phrase: str) -> None:
    artifact = _sample_inline_chart_artifact()
    artifact["spec"]["title"] = "Profit < Target"
    artifact["spec"]["rows"] = [{"month": "Jan", "value": 1}] * (renderer.MAX_CHART_ROWS + 1)
    card = renderer.answer_card("Results", artifacts=[artifact], locale=locale)
    dumped = json.dumps(card, ensure_ascii=False)
    assert "Profit < Target" in dumped
    assert phrase in dumped
    assert "Bowhead Web" in dumped
    assert not any(element["tag"] == "chart" for element in card["elements"])


@pytest.mark.parametrize("locale, expected", [("de-DE", "Abgebrochen."), ("fr-FR", "Annulé.")])
def test_feishu_step_errors_use_safe_localized_copy(locale: str, expected: str) -> None:
    state = renderer.FeishuTurnCardState(locale=locale)
    renderer.apply_event_to_state(state, "step_started", {"step_id": "step_1"})
    renderer.apply_event_to_state(
        state, "step_done", {"step_id": "step_1", "state": "failure", "error": {"code": "user_cancel", "message_zh": "HTTP 500 raw secret"}}
    )
    assert state.steps["step_1"].output == expected
    renderer.apply_event_to_state(state, "step_done", {"step_id": "step_1", "state": "failure", "error": {"message_zh": "HTTP 500 raw secret"}})
    assert "raw secret" not in state.steps["step_1"].output
