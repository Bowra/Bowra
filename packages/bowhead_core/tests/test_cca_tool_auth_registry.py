"""cca block ④d — ToolAuthRegistry (tech-design §6.1).

_build_auth_headers (pure) + the cache layer (build / cache-hit / copy-out /
invalidate / fail-closed) against a fake store — no DB / Fernet needed (the real
decrypt round-trip is covered by test_team_tool_source.py).
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from bowhead_core.tool_auth_registry import ToolAuthRegistry, _build_auth_headers


# ─────────────────────────────────────────────────────────────
# _build_auth_headers (pure)
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_build_bearer() -> None:
    assert _build_auth_headers("bearer", {"value": "tok1"}) == {"Authorization": "Bearer tok1"}
    assert _build_auth_headers("bearer", {"token": "tok2"}) == {"Authorization": "Bearer tok2"}
    assert _build_auth_headers("bearer", {}) == {}  # missing → no header (no leak/raise)


@pytest.mark.unit
def test_build_api_key_default_and_custom_header_with_prefix() -> None:
    assert _build_auth_headers("api_key", {"value": "k"}) == {"X-API-Key": "k"}
    assert _build_auth_headers("api_key", {"value": "k", "header_name": "X-Datarc-Key", "prefix": "Token "}) == {"X-Datarc-Key": "Token k"}
    assert _build_auth_headers("api_key", {"header_name": "X"}) == {}  # no value → {}


@pytest.mark.unit
def test_build_basic_and_none() -> None:
    h = _build_auth_headers("basic", {"username": "u", "password": "p"})
    assert h["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert _build_auth_headers("basic", {"username": "u"}) == {}  # incomplete
    assert _build_auth_headers("none", {}) == {}
    assert _build_auth_headers("weird_unknown", {"value": "x"}) == {}


# ─────────────────────────────────────────────────────────────
# cache layer (fake store)
# ─────────────────────────────────────────────────────────────
class _FakeStore:
    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], object] = {}
        self.creds: dict[tuple[str, str], dict] = {}
        self.source_calls = 0
        self.cred_calls = 0

    def add(self, team: str, src: str, auth_type: str, creds: dict) -> None:
        self.sources[(team, src)] = SimpleNamespace(auth_type=auth_type)
        self.creds[(team, src)] = creds

    async def get_team_tool_source(self, team_id: str, source_id: str):
        self.source_calls += 1
        return self.sources.get((team_id, source_id))

    async def get_team_tool_auth_decrypted(self, team_id: str, source_id: str) -> dict:
        self.cred_calls += 1
        return self.creds.get((team_id, source_id), {})


@pytest.mark.asyncio
async def test_get_headers_builds_and_caches() -> None:
    store = _FakeStore()
    store.add("team_a", "ots_1", "bearer", {"value": "secret"})
    reg = ToolAuthRegistry(store)

    h1 = await reg.get_headers("team_a", "ots_1")
    h2 = await reg.get_headers("team_a", "ots_1")
    assert h1 == {"Authorization": "Bearer secret"} == h2
    assert store.source_calls == 1 and store.cred_calls == 1  # 2nd call served from cache


@pytest.mark.asyncio
async def test_get_headers_returns_a_copy() -> None:
    store = _FakeStore()
    store.add("team_a", "ots_1", "api_key", {"value": "k"})
    reg = ToolAuthRegistry(store)

    h = await reg.get_headers("team_a", "ots_1")
    h["X-API-Key"] = "TAMPERED"  # mutate the returned dict
    again = await reg.get_headers("team_a", "ots_1")
    assert again == {"X-API-Key": "k"}  # cache not poisoned


@pytest.mark.asyncio
async def test_invalidate_team_evicts_then_refetches() -> None:
    store = _FakeStore()
    store.add("team_a", "ots_1", "bearer", {"value": "v"})
    store.add("team_b", "ots_2", "bearer", {"value": "w"})
    reg = ToolAuthRegistry(store)

    await reg.get_headers("team_a", "ots_1")
    await reg.get_headers("team_b", "ots_2")
    assert store.source_calls == 2

    reg.invalidate("team_a")  # only team_a dropped
    await reg.get_headers("team_a", "ots_1")  # re-fetch
    await reg.get_headers("team_b", "ots_2")  # still cached
    assert store.source_calls == 3  # team_a re-fetched, team_b not


@pytest.mark.asyncio
async def test_invalidate_source_drops_one() -> None:
    store = _FakeStore()
    store.add("team_a", "ots_1", "bearer", {"value": "v"})
    store.add("team_a", "ots_2", "bearer", {"value": "w"})
    reg = ToolAuthRegistry(store)
    await reg.get_headers("team_a", "ots_1")
    await reg.get_headers("team_a", "ots_2")
    reg.invalidate_source("team_a", "ots_1")
    await reg.get_headers("team_a", "ots_1")  # re-fetch
    await reg.get_headers("team_a", "ots_2")  # cached
    assert store.source_calls == 3


@pytest.mark.asyncio
async def test_auth_none_skips_decrypt() -> None:
    store = _FakeStore()
    store.add("team_a", "ots_1", "none", {})
    reg = ToolAuthRegistry(store)
    assert await reg.get_headers("team_a", "ots_1") == {}
    assert store.cred_calls == 0  # never decrypt when auth_type is none


@pytest.mark.asyncio
async def test_fail_closed_unknown_source_and_falsy_team() -> None:
    store = _FakeStore()
    reg = ToolAuthRegistry(store)
    assert await reg.get_headers("team_a", "ots_missing") == {}  # store returns None
    assert await reg.get_headers("", "ots_1") == {}  # falsy team → no store call
    assert store.source_calls == 1  # only the unknown-source lookup hit the store


@pytest.mark.unit
def test_auth_header_contract_matches_fe_create_body():
    """Pin the FE↔backend auth-credential key contract (pre-merge review P0 CONTRACT-1 / FE-1).

    The create card (create-tool-card.tsx) sends ``{value: secret}`` for BOTH bearer and api_key.
    _build_auth_headers MUST read exactly that key, or the credential is Fernet-stored but never
    sent → every authenticated call 401s with no FE signal. This test fails loudly if either side
    drifts."""
    assert _build_auth_headers("bearer", {"value": "tok"}) == {"Authorization": "Bearer tok"}
    assert _build_auth_headers("api_key", {"value": "k"}) == {"X-API-Key": "k"}
    # basic needs username+password (the card drops Basic for P2 — it has a single secret field)
    assert _build_auth_headers("basic", {"username": "u", "password": "p"})["Authorization"].startswith("Basic ")
    # regression guard — the OLD broken FE shapes produce NO header (the bug the review caught):
    assert _build_auth_headers("api_key", {"api_key": "k"}) == {}  # wrong key for api_key
    assert _build_auth_headers("basic", {"value": "k"}) == {}  # single-field basic can't work
    assert _build_auth_headers("none", {"value": "x"}) == {}
