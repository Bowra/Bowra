"""tools sprint WP2b — remote MCP OAuth 2.1 (mcp_oauth.py).

asyncio.run wrappers (no pytest-asyncio dep). Covers WP2b-1 (Fernet TokenStorage round-trip +
no-plaintext-leak), runtime access tokens through the shared single-flight broker (fail-closed,
refresh, rotation, ended sign-in, no refresh token sent to another server) and WP2b-2 (server-side authorize/exchange
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
    """Mimics UserStore's auth methods with REAL Fernet encrypt/decrypt (the F14 path)."""

    def __init__(self) -> None:
        import uuid

        self.rows: dict[tuple[str, str], str] = {}
        self.db_path = f"fake-{uuid.uuid4().hex}"  # one runtime broker per fake database

    async def get_team_tool_oauth_auth_versioned(self, team_id: str, source_id: str) -> tuple[str, dict[str, Any]] | None:
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        blob = self.rows.get((team_id, source_id))
        return (blob, decrypt_llm_config(blob)) if blob else None

    async def replace_team_tool_oauth_auth(self, team_id: str, source_id: str, auth: dict[str, Any], *, expected_auth_ref: str) -> bool:
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        if self.rows.get((team_id, source_id)) != expected_auth_ref:
            return False
        self.rows[(team_id, source_id)] = encrypt_llm_config(auth)
        return True

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


def test_runtime_headers_fail_closed_when_never_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source with no stored grant never produces a bearer and never calls the provider."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import team_oauth_headers
    from bowhead_core.oauth_token_broker import OAuthGrantRejected

    def no_network(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not run
        raise AssertionError(f"unexpected request {request.url}")

    _patch_guarded_client(monkeypatch, no_network)
    with pytest.raises(OAuthGrantRejected) as caught:
        _run(team_oauth_headers(_FakeAuthStore(), "o", "s", "https://mcp.example/mcp"))
    assert caught.value.reason == "not_authorized"


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
    from bowhead_core.llm_config_crypto import decrypt_llm_config

    # the refresh endpoint is recorded with the grant, so renewals need no discovery
    assert decrypt_llm_config(store.rows[("org1", "src1")])["oauth_meta"] == {"token_endpoint": f"{_IDP}/token", "resource": _MCP}
    assert _run(FernetTokenStorage(store, "org1", "src1").get_client_info()).issuer == _IDP  # type: ignore[union-attr]


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


def _seed_grant(store: _FakeAuthStore, *, meta: bool, issuer: str | None = None, ended: bool = False) -> None:
    async def seed() -> None:
        from bowhead_core.mcp_oauth import FernetTokenStorage

        storage = FernetTokenStorage(store, "team", "source")
        await storage.set_client_info(OAuthClientInformationFull(client_id="cid_test", token_endpoint_auth_method="none", issuer=issuer))
        token = OAuthToken(access_token="expired-access", token_type="Bearer", refresh_token="stored-refresh", scope="repo")
        if meta:
            await storage.set_grant(token, {"token_endpoint": f"{_IDP}/token", "resource": _MCP})
        else:
            await storage.set_tokens(token)  # a source authorized before oauth_meta was recorded
        if ended:
            d = await storage._load()
            d["oauth_grant_state"] = {"ended": True, "reason": "invalid_grant"}
            await storage._save()

    _run(seed())


@pytest.mark.parametrize("scenario", ["unchanged", "rotated", "revoked", "wrong_issuer", "changed_server", "recorded_meta"])
def test_runtime_headers_refresh_stored_credentials(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.llm_config_crypto import decrypt_llm_config
    from bowhead_core.mcp_oauth import FernetTokenStorage, team_oauth_headers
    from bowhead_core.oauth_token_broker import OAuthGrantRejected

    store = _FakeAuthStore()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        if request.url.path == "/mcp":
            return httpx2.Response(
                401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{_MCP.rsplit("/", 1)[0]}/.well-known/oauth-protected-resource"'}
            )
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
        assert str(request.url) == f"{_IDP}/token"
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["refresh_token"]
        assert fields["client_id"] == ["cid_test"]
        assert fields["refresh_token"] == ["stored-refresh"]
        assert fields["resource"] == [_MCP]
        if scenario == "revoked":
            return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "private-provider-detail"})
        token = {"access_token": "renewed-access", "token_type": "Bearer", "expires_in": 3600}
        if scenario == "rotated":
            token["refresh_token"] = "rotated-refresh"
        return httpx2.Response(200, json=token)

    _patch_guarded_client(monkeypatch, handler)
    _seed_grant(store, meta=scenario == "recorded_meta", issuer="https://original.example" if scenario == "changed_server" else None)
    before = store.rows[("team", "source")]

    if scenario in {"revoked", "wrong_issuer", "changed_server"}:
        with pytest.raises(OAuthGrantRejected) as caught:
            _run(team_oauth_headers(store, "team", "source", _MCP))
        assert "private-provider-detail" not in str(caught.value)
        if scenario == "revoked":
            assert caught.value.reason == "invalid_grant"
            assert decrypt_llm_config(store.rows[("team", "source")])["oauth_grant_state"] == {"ended": True, "reason": "invalid_grant"}
        else:
            # never send the refresh token to a server other than the one that issued it
            assert caught.value.reason == "authorization_server_changed"
            assert not any(request.url.path == "/token" for request in requests)
            # the old grant cannot be used with another server: an administrator must authorize again
            state = decrypt_llm_config(store.rows[("team", "source")])["oauth_grant_state"]
            assert state == {"ended": True, "reason": "authorization_server_changed"}
        return

    headers = _run(team_oauth_headers(store, "team", "source", _MCP))
    assert headers == {"Authorization": "Bearer renewed-access"}
    assert _run(team_oauth_headers(store, "team", "source", _MCP)) == headers  # cached: no second refresh
    assert sum(request.url.path == "/token" for request in requests) == 1
    if scenario == "recorded_meta":
        assert [request.url.path for request in requests] == ["/token"]  # no discovery
    restored = _run(FernetTokenStorage(store, "team", "source").get_tokens())
    assert restored is not None
    assert restored.refresh_token == ("rotated-refresh" if scenario == "rotated" else "stored-refresh")
    if scenario == "rotated":
        assert "rotated-refresh" not in store.rows[("team", "source")]  # sealed, never plaintext
    else:
        assert store.rows[("team", "source")] == before  # nothing to write without a rotation


def test_runtime_headers_single_refresh_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eight callers at expiry share one refresh; the provider rejects any reuse of a rotated token."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import team_oauth_headers

    store = _FakeAuthStore()
    valid = {"stored-refresh"}
    refreshes: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.01)
        token = parse_qs(request.content.decode())["refresh_token"][0]
        refreshes.append(token)
        if token not in valid:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        valid.discard(token)
        rotated = f"rotated-{len(refreshes)}"
        valid.add(rotated)
        return httpx2.Response(
            200, json={"access_token": f"access-{len(refreshes)}", "token_type": "Bearer", "expires_in": 3600, "refresh_token": rotated}
        )

    _patch_guarded_client(monkeypatch, handler)
    _seed_grant(store, meta=True)

    async def scenario() -> list[dict[str, str]]:
        return list(await asyncio.gather(*(team_oauth_headers(store, "team", "source", _MCP) for _ in range(8))))

    assert _run(scenario()) == [{"Authorization": "Bearer access-1"}] * 8
    assert refreshes == ["stored-refresh"]


def test_ended_sign_in_is_not_retried_until_authorized_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.llm_config_crypto import decrypt_llm_config
    from bowhead_core.mcp_oauth import oauth_sign_in_ended, team_oauth_headers
    from bowhead_core.oauth_token_broker import OAuthGrantRejected

    def no_network(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not run
        raise AssertionError(f"unexpected request {request.url}")

    _patch_guarded_client(monkeypatch, no_network)
    store = _FakeAuthStore()
    _seed_grant(store, meta=True, ended=True)
    assert oauth_sign_in_ended(decrypt_llm_config(store.rows[("team", "source")]))
    with pytest.raises(OAuthGrantRejected):
        _run(team_oauth_headers(store, "team", "source", _MCP))


@pytest.mark.parametrize("reason", ["redirect_changed", "client_rejected"])
def test_begin_registers_again_when_the_stored_client_cannot_be_reused(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage, begin_mcp_oauth

    idp_state: dict[str, Any] = {}
    _patch_guarded_client(monkeypatch, _idp_handler(idp_state))
    store = _FakeAuthStore()
    storage = FernetTokenStorage(store, "org1", "src1")

    async def seed() -> None:
        old_callback = "https://old-entry.example/cb" if reason == "redirect_changed" else "https://app/cb"
        await storage.set_client_info(
            OAuthClientInformationFull(client_id="old-client", redirect_uris=[old_callback], token_endpoint_auth_method="none")
        )  # type: ignore[list-item]
        if reason == "client_rejected":
            d = await storage._load()
            d["oauth_grant_state"] = {"ended": True, "reason": "invalid_client"}
            await storage._save()

    _run(seed())
    begun = _run(begin_mcp_oauth(_MCP, FernetTokenStorage(store, "org1", "src1"), "https://app/cb", state="st", code_challenge="c" * 43))
    assert begun.client_id == "cid_test"  # a fresh registration, not the unusable old-client
    assert idp_state["reg_redirect_uris"] == ["https://app/cb"]


def test_begin_reuses_a_usable_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_core.mcp_oauth import FernetTokenStorage, begin_mcp_oauth

    idp_state: dict[str, Any] = {}
    _patch_guarded_client(monkeypatch, _idp_handler(idp_state))
    store = _FakeAuthStore()
    _run(
        FernetTokenStorage(store, "org1", "src1").set_client_info(
            OAuthClientInformationFull(client_id="kept-client", redirect_uris=["https://app/cb"], token_endpoint_auth_method="none")  # type: ignore[list-item]
        )
    )
    begun = _run(begin_mcp_oauth(_MCP, FernetTokenStorage(store, "org1", "src1"), "https://app/cb", state="st", code_challenge="c" * 43))
    assert begun.client_id == "kept-client" and "reg_redirect_uris" not in idp_state
