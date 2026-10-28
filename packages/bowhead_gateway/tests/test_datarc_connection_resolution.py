"""ws-datarc-decouple P2a (③/R7.1) — gateway per-team datarc client resolution.

Drives chat._resolve_team_datarc_client directly: it resolves the CALLER team's connection (via the
team-bound DatarcConnectionStore) into a per-connection DatarcClient, NEVER returns another team's
connection (R7.1), returns None for the BENIGN cases (no team / no connection; there is no global fallback),
and FAILS CLOSED — raises DatarcResolutionError — when the team HAS a connection but it can't be
resolved/secured (review P1-b: must not silently degrade to the shared global datarc).
"""

from __future__ import annotations

import types

import pytest
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_gateway._turn_runtime import _resolve_team_datarc_client as _resolve_im_scheduled_datarc_client
from bowhead_gateway.routes.chat import DatarcResolutionError, _resolve_team_datarc_client
from cryptography.fernet import Fernet

from bowhead_bi_bridge import DatarcClient, DatarcClientFactory


def _set_fernet(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())


def _request(store, factory):
    state = types.SimpleNamespace(datarc_connection_store=store, datarc_client_factory=factory)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


async def _store(tmp_path) -> DatarcConnectionStore:
    s = DatarcConnectionStore(str(tmp_path / "bowhead.db"))
    await s.initialize()
    return s


async def test_no_team_id_returns_none(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    req = _request(await _store(tmp_path), DatarcClientFactory())
    assert await _resolve_team_datarc_client(req, None) is None
    assert await _resolve_team_datarc_client(req, "") is None


# Web chat resolves through datarc_context; IM and scheduled turns through _turn_runtime.
@pytest.mark.parametrize("resolve", [_resolve_team_datarc_client, _resolve_im_scheduled_datarc_client], ids=["web", "im_scheduled"])
async def test_team_without_connection_returns_none(tmp_path, monkeypatch, resolve) -> None:
    """None even when deployment-level Datarc is wired: no app.state client, no env settings."""
    _set_fernet(monkeypatch)
    for name in (
        "BOWHEAD_BI_BASE_URL",
        "BOWHEAD_DATARC_URL",
        "BOWHEAD_DATARC_BASE_URL",
    ):
        monkeypatch.setenv(name, "http://datarc-deploy.example/")
    monkeypatch.setenv("BOWHEAD_DATARC_DEV_AUTH_KEY", "deploy_env_auth")
    monkeypatch.setenv("BOWHEAD_DATARC_DEV_MEMBER_UID", "deploy_member")
    factory = DatarcClientFactory()
    req = _request(await _store(tmp_path), factory)
    req.app.state.datarc = DatarcClient("http://datarc-deploy.example/", "deploy_state_auth")
    try:
        assert await resolve(req, "team_with_nothing") is None
        assert factory.size() == 0
    finally:
        await req.app.state.datarc.aclose()


async def test_stores_missing_returns_none() -> None:
    # app.state without the stores wired (e.g. early boot) → graceful None, never raises
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    assert await _resolve_team_datarc_client(req, "team_a") is None


async def test_resolves_connection_to_cached_client(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    factory = DatarcClientFactory()
    await store.create_connection(
        team_id="team_a",
        name="prod",
        base_url="https://a.datarc.cn",
        auth_key="key-aaaa",
    )
    req = _request(store, factory)
    client = await _resolve_team_datarc_client(req, "team_a")
    assert isinstance(client, DatarcClient)
    # factory caches → resolving again yields the SAME instance (no per-turn client churn)
    again = await _resolve_team_datarc_client(req, "team_a")
    assert client is again
    assert factory.size() == 1
    await factory.aclose()


async def test_cross_team_isolation_r71(tmp_path, monkeypatch) -> None:
    """team_b must NOT receive team_a's connection — store reads are team-bound (fail-closed)."""
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    factory = DatarcClientFactory()
    await store.create_connection(
        team_id="team_a",
        name="a",
        base_url="https://a.datarc.cn",
        auth_key="key-aaaa",
    )
    req = _request(store, factory)
    assert await _resolve_team_datarc_client(req, "team_a") is not None
    assert await _resolve_team_datarc_client(req, "team_b") is None  # no leak across teams
    await factory.aclose()


async def test_connection_without_auth_fails_closed(tmp_path, monkeypatch) -> None:
    """[review P1-b] connection EXISTS but has no usable auth → DatarcResolutionError, NOT None.
    Returning None here would silently route the team's queries to the shared global datarc."""
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    # active connection with empty auth_key → auth_ref stored as NULL → get_connection_auth → None
    await store.create_connection(
        team_id="team_a",
        name="broken",
        base_url="https://a.datarc.cn",
        auth_key="",
    )
    req = _request(store, DatarcClientFactory())
    with pytest.raises(DatarcResolutionError):
        await _resolve_team_datarc_client(req, "team_a")


async def test_store_error_fails_closed() -> None:
    """[review P1-b] the team HAS a connection but the store read blows up → fail closed.
    A DB/decrypt error must surface as DatarcResolutionError, never a silent global fallback."""

    class _BoomStore:
        async def list_connections(self, team_id, **kw):
            raise RuntimeError("db down")

    req = _request(_BoomStore(), DatarcClientFactory())
    with pytest.raises(DatarcResolutionError):
        await _resolve_team_datarc_client(req, "team_a")


async def test_ssrf_guard_build_failure_fails_closed(tmp_path, monkeypatch) -> None:
    """[review P1-b#1] if the factory can't build a guarded client, fail closed — the team's
    queries must not fall through to the unguarded global datarc."""
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    await store.create_connection(
        team_id="team_a",
        name="prod",
        base_url="https://a.datarc.cn",
        auth_key="key-aaaa",
    )

    class _BoomFactory:
        def get_client(self, base_url, auth_key):
            raise RuntimeError("ssrf-guard transport build failed")

    req = _request(store, _BoomFactory())
    with pytest.raises(DatarcResolutionError):
        await _resolve_team_datarc_client(req, "team_a")
