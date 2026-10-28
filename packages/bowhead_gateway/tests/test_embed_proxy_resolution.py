"""embed-via-openapi — gateway per-team embed-proxy client 解析（镜像 datarc resolution 测试）.

`chat._resolve_team_embed_client`：team 连接 → datarc-proxy VectorClient（BI 语义词典）。
与 datarc 解析相反的失败策略：embed 是 nice-to-have，**永不 raise**，一切 miss 返 None
（turn_runner 回退全局直连 embed client）。R7.1 同构：store 读 team-bound。
"""

from __future__ import annotations

import types

import httpx2
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_gateway.routes.chat import _resolve_team_embed_client
from cryptography.fernet import Fernet

from bowhead_memory import EmbedProxyClientFactory, VectorClient


def _set_fernet(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())


def _request(store, factory):
    state = types.SimpleNamespace(
        datarc_connection_store=store,
        embed_client_factory=factory,
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


async def _store(tmp_path) -> DatarcConnectionStore:
    s = DatarcConnectionStore(str(tmp_path / "bowhead.db"))
    await s.initialize()
    return s


def _mock_probe(factory: EmbedProxyClientFactory, base_url: str, auth_key: str, *, ok: bool) -> None:
    """预热池中 client 并把 transport 换成 mock（探测 health 返 ok/403）."""

    def h(_req: httpx2.Request) -> httpx2.Response:
        if ok:
            return httpx2.Response(
                200,
                json={"errcode": 200, "errmsg": "success", "data": {"status": "healthy"}},
            )
        return httpx2.Response(403, json={"detail": "无当前接口权限"})

    client = factory.get_client(base_url, auth_key)
    client._http = httpx2.AsyncClient(  # type: ignore[attr-defined]
        base_url=base_url,
        transport=httpx2.MockTransport(h),
    )


async def test_no_team_id_returns_none(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    req = _request(await _store(tmp_path), EmbedProxyClientFactory())
    assert await _resolve_team_embed_client(req, None) is None
    assert await _resolve_team_embed_client(req, "") is None


async def test_team_without_connection_returns_none(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    req = _request(await _store(tmp_path), EmbedProxyClientFactory())
    assert await _resolve_team_embed_client(req, "team_nothing") is None


async def test_state_missing_returns_none() -> None:
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    assert await _resolve_team_embed_client(req, "team_a") is None


async def test_resolves_to_verified_proxy_client(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    factory = EmbedProxyClientFactory()
    await store.create_connection(
        team_id="team_a",
        name="prod",
        base_url="https://a.datarc.cn",
        auth_key="key-aaaa",
    )
    _mock_probe(factory, "https://a.datarc.cn", "key-aaaa", ok=True)
    req = _request(store, factory)
    try:
        client = await _resolve_team_embed_client(req, "team_a")
        assert isinstance(client, VectorClient)
        assert client.datarc_proxy is True
        # Resolve twice → same pooled instance (positive capability cache).
        again = await _resolve_team_embed_client(req, "team_a")
        assert client is again
    finally:
        await factory.aclose()


async def test_probe_403_returns_none_never_raises(tmp_path, monkeypatch) -> None:
    """key 没开 embed 权限 → None（回退全局），绝不 raise / 不 gate turn."""
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    factory = EmbedProxyClientFactory()
    await store.create_connection(
        team_id="team_a",
        name="prod",
        base_url="https://a.datarc.cn",
        auth_key="key-aaaa",
    )
    _mock_probe(factory, "https://a.datarc.cn", "key-aaaa", ok=False)
    req = _request(store, factory)
    try:
        assert await _resolve_team_embed_client(req, "team_a") is None
    finally:
        await factory.aclose()


async def test_store_error_returns_none_never_raises(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)

    class _BoomStore:
        async def list_connections(self, _team_id):
            raise RuntimeError("db exploded")

    req = _request(_BoomStore(), EmbedProxyClientFactory())
    assert await _resolve_team_embed_client(req, "team_a") is None


async def test_cross_team_isolation(tmp_path, monkeypatch) -> None:
    """team_b 不能拿到 team_a 的连接（store team-bound → R7.1 by construction）."""
    _set_fernet(monkeypatch)
    store = await _store(tmp_path)
    factory = EmbedProxyClientFactory()
    await store.create_connection(
        team_id="team_a",
        name="prod",
        base_url="https://a.datarc.cn",
        auth_key="key-aaaa",
    )
    req = _request(store, factory)
    try:
        assert await _resolve_team_embed_client(req, "team_b") is None
    finally:
        await factory.aclose()
