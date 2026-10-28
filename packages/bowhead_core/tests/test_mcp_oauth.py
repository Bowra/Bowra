"""tools sprint WP2b — remote MCP OAuth 2.1 (mcp_oauth.py).

asyncio.run wrappers (no pytest-asyncio dep). Covers WP2b-1 (Fernet TokenStorage round-trip +
no-plaintext-leak + runtime provider fail-closed) and WP2b-2 (server-side authorize/exchange
driver against a MockTransport IdP: PKCE + state in the auth URL, token exchange + storage, PKCE
integrity, HTTPS remote endpoints with HTTP/HTTPS Bowhead callbacks) + the single-use / expiry
pending-auth store.

The driver's network path is exercised by monkeypatching ``build_guarded_async_client`` to install
an ``httpx2.MockTransport`` fake IdP — so the SDK's real discovery / registration / token message
shaping runs, without a live server. The full real-server e2e is in the WP2b-2 verify script.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from cryptography.fernet import Fernet
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class _FakeAuthStore:
    """Mimics UserStore's two auth methods with REAL Fernet encrypt/decrypt (the F14 path)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    async def get_team_tool_auth_decrypted(self, team_id: str, source_id: str) -> dict[str, Any]:
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        blob = self.rows.get((team_id, source_id))
        return decrypt_llm_config(blob) if blob else {}

    async def update_team_tool_source_auth(self, team_id: str, source_id: str, *, auth_type: str, auth: dict[str, Any]) -> None:
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        assert auth_type == "oauth"
        token = encrypt_llm_config(auth or {})
        assert not token.startswith("PLAIN:"), "F14: refusing plaintext"
        self.rows[(team_id, source_id)] = token


# ── WP2b-1: FernetTokenStorage ──────────────────────────────────────────────


def test_token_storage_roundtrip_and_no_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage

    store = _FakeAuthStore()
    st = FernetTokenStorage(store, "org1", "src1")
    assert _run(st.get_tokens()) is None
    assert _run(st.get_client_info()) is None

    tok = OAuthToken(access_token="at_secret", token_type="Bearer", expires_in=3600, refresh_token="rt_secret", scope="repo")
    _run(st.set_tokens(tok))
    _run(st.set_client_info(OAuthClientInformationFull(client_id="cid_1", redirect_uris=["https://app/cb"])))

    blob = store.rows[("org1", "src1")]
    assert blob.startswith("FERNET:")
    assert "at_secret" not in blob and "rt_secret" not in blob  # ciphertext hides the token

    fresh = FernetTokenStorage(store, "org1", "src1")  # reads from the store, not memory
    got = _run(fresh.get_tokens())
    assert got is not None and got.access_token == "at_secret" and got.refresh_token == "rt_secret"
    ci = _run(fresh.get_client_info())
    assert ci is not None and ci.client_id == "cid_1"


def test_runtime_provider_fails_closed_when_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime provider (no interactive handlers) with no stored tokens must NOT emit an
    authenticated request — driving its auth flow raises rather than passing the request through."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage, build_mcp_oauth_provider

    prov = build_mcp_oauth_provider("https://mcp.example", FernetTokenStorage(_FakeAuthStore(), "o", "s"))
    assert isinstance(prov, httpx2.Auth)

    async def _drive() -> bool:
        gen = prov.async_auth_flow(httpx2.Request("POST", "https://mcp.example/mcp"))
        try:
            req = await gen.asend(None)
            resp = httpx2.Response(401, request=req, headers={"WWW-Authenticate": 'Bearer realm="x"'})
            while True:
                req = await gen.asend(resp)
                resp = httpx2.Response(401, request=req)
        except StopAsyncIteration:
            return False
        except Exception:
            return True
        finally:
            await gen.aclose()

    assert _run(_drive()) is True


# ── WP2b-2: authorize / exchange driver against a MockTransport IdP ──────────

_IDP = "https://idp.example"
_MCP = "https://mcp.example/mcp"


def _idp_handler(state: dict[str, Any]) -> Any:
    """MockTransport handler = a minimal RFC 9728/8414/7591/6749 IdP. ``state`` captures what it saw
    and holds the challenge to validate PKCE at /token."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = urlparse(str(request.url)).path
        if path == "/mcp":
            return httpx2.Response(401, headers={"WWW-Authenticate": "Bearer"})
        if path == "/.well-known/oauth-protected-resource" or path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(200, json={"resource": _MCP, "authorization_servers": [_IDP], "scopes_supported": ["repo"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": _IDP,
                    "authorization_endpoint": f"{_IDP}/authorize",
                    "token_endpoint": f"{_IDP}/token",
                    "registration_endpoint": f"{_IDP}/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["repo"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if path == "/register":
            import json as _json

            body = _json.loads(request.content.decode() or "{}")
            state["reg_auth_method"] = body.get("token_endpoint_auth_method")
            state["reg_redirect_uris"] = body.get("redirect_uris")
            return httpx2.Response(
                201,
                json={
                    "client_id": "cid_test",
                    "redirect_uris": body.get("redirect_uris") or ["https://app/cb"],
                    "token_endpoint_auth_method": "none",
                },
            )
        if path == "/token":
            form = parse_qs(request.content.decode())
            verifier = form.get("code_verifier", [""])[0]
            if form.get("grant_type", [""])[0] != "authorization_code":
                return httpx2.Response(400, json={"error": "unsupported_grant_type"})
            if _s256(verifier) != state.get("challenge"):
                return httpx2.Response(400, json={"error": "invalid_grant"})  # PKCE integrity
            if form.get("redirect_uri", [""])[0] != state.get("redirect_uri"):
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(
                200, json={"access_token": "at_final", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt_final", "scope": "repo"}
            )
        return httpx2.Response(404)

    return handler


def _patch_guarded_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    def fake_build(*, resolver: Any = None, **kwargs: Any) -> httpx2.AsyncClient:
        kwargs.pop("auth", None)
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bowhead_core.mcp_oauth.build_guarded_async_client", fake_build)


def test_begin_and_exchange_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage, begin_mcp_oauth, exchange_mcp_oauth_code
    from mcp.client.auth.oauth2 import PKCEParameters

    idp_state: dict[str, Any] = {}
    _patch_guarded_client(monkeypatch, _idp_handler(idp_state))

    store = _FakeAuthStore()
    storage = FernetTokenStorage(store, "org1", "src1")
    pkce = PKCEParameters.generate()
    redirect_uri = "https://app/cb"
    idp_state["challenge"] = pkce.code_challenge
    idp_state["redirect_uri"] = redirect_uri

    begun = _run(begin_mcp_oauth(_MCP, storage, redirect_uri, state="st_ABC", code_challenge=pkce.code_challenge))

    q = parse_qs(urlparse(begun.authorization_url).query)
    assert urlparse(begun.authorization_url).path == "/authorize"
    assert q["state"][0] == "st_ABC"
    assert q["code_challenge"][0] == pkce.code_challenge and q["code_challenge_method"][0] == "S256"
    assert q["client_id"][0] == "cid_test" and q["redirect_uri"][0] == redirect_uri
    assert q["resource"][0] == _MCP  # RFC 8707 binding from PRM
    assert begun.token_endpoint == f"{_IDP}/token" and begun.resource == _MCP
    assert idp_state["reg_auth_method"] == "none"  # registered as a public PKCE client
    # client_info persisted
    assert (_run(FernetTokenStorage(store, "org1", "src1").get_client_info())).client_id == "cid_test"  # type: ignore[union-attr]

    tok = _run(
        exchange_mcp_oauth_code(
            storage,
            code="authcode",
            code_verifier=pkce.code_verifier,
            redirect_uri=redirect_uri,
            token_endpoint=begun.token_endpoint,
            client_id=begun.client_id,
            resource=begun.resource,
        )
    )
    assert tok.access_token == "at_final"
    stored = _run(FernetTokenStorage(store, "org1", "src1").get_tokens())
    assert stored is not None and stored.access_token == "at_final"
    assert "at_final" not in store.rows[("org1", "src1")]  # no plaintext leak at rest


def test_begin_allows_http_public_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage, begin_mcp_oauth
    from mcp.client.auth.oauth2 import PKCEParameters

    idp_state: dict[str, Any] = {}
    _patch_guarded_client(monkeypatch, _idp_handler(idp_state))
    pkce = PKCEParameters.generate()
    redirect_uri = "http://bowhead.example/callback"
    begun = _run(
        begin_mcp_oauth(
            _MCP,
            FernetTokenStorage(_FakeAuthStore(), "org1", "src1"),
            redirect_uri,
            state="st_http",
            code_challenge=pkce.code_challenge,
        )
    )

    query = parse_qs(urlparse(begun.authorization_url).query)
    assert query["redirect_uri"] == [redirect_uri]
    assert idp_state["reg_redirect_uris"] == [redirect_uri]


def test_exchange_rejects_wrong_pkce_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage, exchange_mcp_oauth_code
    from mcp.client.auth.oauth2 import PKCEParameters

    idp_state: dict[str, Any] = {"challenge": PKCEParameters.generate().code_challenge, "redirect_uri": "https://app/cb"}
    _patch_guarded_client(monkeypatch, _idp_handler(idp_state))
    storage = FernetTokenStorage(_FakeAuthStore(), "o", "s")
    with pytest.raises(ValueError, match="token exchange failed"):
        _run(
            exchange_mcp_oauth_code(
                storage,
                code="c",
                code_verifier="z" * 128,  # wrong verifier → IdP 400
                redirect_uri="https://app/cb",
                token_endpoint=f"{_IDP}/token",
                client_id="cid_test",
                resource=None,
            )
        )


def test_begin_refuses_insecure_http_mcp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    from bowhead_core.mcp_oauth import FernetTokenStorage, begin_mcp_oauth

    storage = FernetTokenStorage(_FakeAuthStore(), "o", "s")
    with pytest.raises(ValueError, match="HTTPS"):
        _run(begin_mcp_oauth("http://evil.example/mcp", storage, "https://app/cb", state="s", code_challenge="c" * 43))


# ── WP2b-3c: side-effect-free OAuth-capability probe ────────────────────────


def test_probe_detects_oauth_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import probe_mcp_oauth

    _patch_guarded_client(monkeypatch, _idp_handler({}))
    needs, provider = _run(probe_mcp_oauth(_MCP))
    assert needs is True and provider == "idp.example"  # PRM authorization_servers[0] host


def test_probe_non_oauth_server_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import probe_mcp_oauth

    _patch_guarded_client(monkeypatch, lambda request: httpx2.Response(200, json={}))  # open server, no 401 / no PRM
    needs, provider = _run(probe_mcp_oauth("https://open.example/mcp"))
    assert needs is False and provider is None


def test_probe_401_without_prm_is_not_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """review P2: a static bearer/api_key/basic MCP server 401s too — a bare 401 with no discoverable
    protected-resource metadata must NOT be misrouted as OAuth."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import probe_mcp_oauth

    # 401 on every path (incl. the PRM discovery URLs) → no PRM ever parsed → not oauth
    _patch_guarded_client(monkeypatch, lambda request: httpx2.Response(401, headers={"WWW-Authenticate": "Bearer"}))
    needs, provider = _run(probe_mcp_oauth("https://static-auth.example/mcp"))
    assert needs is False and provider is None


# ── WP2b-2: single-use / expiry pending-auth store ──────────────────────────


async def _mk_store_with_team() -> Any:
    import tempfile
    from pathlib import Path

    import aiosqlite
    from bowhead_core.user_store import UserStore

    dbp = Path(tempfile.mkdtemp()) / "u.db"
    store = UserStore(str(dbp))
    await store.initialize()
    async with aiosqlite.connect(str(dbp)) as db:
        await db.execute("INSERT INTO teams (id, name, created_at) VALUES (?,?,?)", ("org1", "acme", "2026-01-01T00:00:00"))
        await db.commit()
    return store


def test_pending_store_single_use_and_expiry() -> None:
    async def scenario() -> None:
        store = await _mk_store_with_team()
        kw: dict[str, Any] = {
            "team_id": "org1",
            "source_id": "src1",
            "code_verifier": "v",
            "redirect_uri": "https://app/cb",
            "token_endpoint": "https://idp/token",
            "client_id": "c",
            "resource": None,
            "created_by": "u1",
        }
        await store.create_mcp_oauth_pending(state="s1", ttl_seconds=600, **kw)  # type: ignore[arg-type]
        rec = await store.take_mcp_oauth_pending("s1")
        assert rec is not None and rec["code_verifier"] == "v" and rec["team_id"] == "org1"
        assert await store.take_mcp_oauth_pending("s1") is None  # single-use → replay defeated
        assert await store.take_mcp_oauth_pending("absent") is None  # unknown state
        await store.create_mcp_oauth_pending(state="s2", ttl_seconds=-5, **kw)  # type: ignore[arg-type]
        assert await store.take_mcp_oauth_pending("s2") is None  # expired

    _run(scenario())


@pytest.mark.parametrize("scenario", ["unchanged", "rotated", "revoked", "wrong_issuer", "changed_server"])
def test_recreated_runtime_provider_refreshes_stored_credentials(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    from bowhead_core.mcp_oauth import FernetTokenStorage, build_mcp_oauth_provider
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthTokenError

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = _FakeAuthStore()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/mcp":
            if request.headers.get("authorization") == "Bearer renewed-access":
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(
                401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{_MCP.rsplit("/", 1)[0]}/.well-known/oauth-protected-resource"'}
            )
        assert "authorization" not in request.headers
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={"resource": _MCP, "authorization_servers": [_IDP]})
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://other.example" if scenario == "wrong_issuer" else _IDP,
                    "authorization_endpoint": f"{_IDP}/authorize",
                    "token_endpoint": f"{_IDP}/token",
                    "response_types_supported": ["code"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if request.url.path == "/register" and scenario == "changed_server":
            assert b"stored-refresh" not in request.content and b"expired-access" not in request.content
            return httpx2.Response(201, json={"client_id": "new-client", "token_endpoint_auth_method": "none"})
        assert str(request.url) == f"{_IDP}/token"
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["refresh_token"]
        assert fields["client_id"] == ["cid_test"]
        assert fields["refresh_token"] == ["stored-refresh"]
        if scenario == "revoked":
            return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "private-provider-detail"})
        token = {"access_token": "renewed-access", "token_type": "Bearer", "expires_in": 3600}
        if scenario == "rotated":
            token["refresh_token"] = "rotated-refresh"
        return httpx2.Response(200, json=token)

    async def exercise() -> None:
        storage = FernetTokenStorage(store, "team", "source")
        await storage.set_tokens(OAuthToken(access_token="expired-access", token_type="Bearer", refresh_token="stored-refresh", scope="repo"))
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id="cid_test", token_endpoint_auth_method="none", issuer="https://original.example" if scenario == "changed_server" else None
            )
        )
        provider = build_mcp_oauth_provider(_MCP, FernetTokenStorage(store, "team", "source"))
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=provider) as client:
            if scenario in {"revoked", "wrong_issuer", "changed_server"}:
                with pytest.raises((OAuthFlowError, OAuthTokenError)) as caught:
                    await client.get(_MCP)
                assert "private-provider-detail" not in str(caught.value)
                if scenario == "revoked":
                    assert isinstance(caught.value, OAuthTokenError)
                else:
                    assert not any(request.url.path == "/token" for request in requests)
                return
            response = await client.get(_MCP)
            assert response.status_code == 200
        restored = await FernetTokenStorage(store, "team", "source").get_tokens()
        assert restored is not None and restored.access_token == "renewed-access"
        assert restored.refresh_token == ("rotated-refresh" if scenario == "rotated" else "stored-refresh")
        assert restored.scope == "repo"
        assert sum(request.url.path == "/token" for request in requests) == 1
        assert "renewed-access" not in store.rows[("team", "source")]

    _run(exercise())
