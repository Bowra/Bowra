"""Single-flight OAuth access tokens: one refresh per grant, rotation kept, stale results ignored.

The personal MCP OAuth spike (2026-11-24) found that simultaneous calls at token expiry each
refreshed with the same refresh token; providers that rotate refresh tokens refused all but one
call, and strict ones revoked the grant. These tests pin the broker that serializes refreshes.
"""

from __future__ import annotations

import anyio
import pytest
from bowhead_core.oauth_token_broker import (
    OAuthGrantRejected,
    OAuthRefreshUnavailable,
    OAuthTokenBroker,
    RefreshedToken,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeGrant:
    """In-memory grant whose provider rotates refresh tokens and rejects reuse."""

    def __init__(self, *, rotate: bool = True, expires_in: int | None = 3600) -> None:
        self.refresh_token = "rt-0"
        self.version = 0
        self.valid_refresh = {"rt-0"}
        self.rotate = rotate
        self.expires_in = expires_in
        self.refreshes: list[str] = []
        self.marks: list[tuple[str, str | None]] = []
        self.used_count = 0
        self.store_fails = False
        self.store_conflict = False
        self.reject_next: str | None = None
        self.unavailable_next = False

    async def read(self) -> tuple[str, str]:
        return self.refresh_token, str(self.version)

    async def refresh(self, refresh_token: str) -> RefreshedToken:
        await anyio.sleep(0.01)  # let concurrent callers pile up on the lock
        self.refreshes.append(refresh_token)
        if self.unavailable_next:
            self.unavailable_next = False
            raise OAuthRefreshUnavailable("http_503")
        if self.reject_next or refresh_token not in self.valid_refresh:
            reason, self.reject_next = self.reject_next or "invalid_grant", None
            raise OAuthGrantRejected(reason)
        n = len(self.refreshes)
        if not self.rotate:
            return RefreshedToken(f"at-{n}", self.expires_in)
        self.valid_refresh.discard(refresh_token)
        rotated = f"rt-{n}"
        self.valid_refresh.add(rotated)
        return RefreshedToken(f"at-{n}", self.expires_in, rotated)

    async def store_rotated(self, refreshed: RefreshedToken, expected_version: str) -> bool:
        if self.store_fails:
            raise RuntimeError("store unavailable")
        if self.store_conflict:
            # Someone authorized again while this refresh ran: a different grant is stored now.
            self.refresh_token, self.version = "rt-reauthorized", self.version + 1
            self.valid_refresh.add("rt-reauthorized")
            self.store_conflict = False
            return False
        if expected_version != str(self.version):
            return False
        self.refresh_token = str(refreshed.refresh_token)
        self.version += 1
        return True

    async def mark_rejected(self, reason: str, expected_version: str | None) -> None:
        self.marks.append((reason, expected_version))

    async def used(self) -> None:
        self.used_count += 1


async def test_concurrent_callers_share_one_refresh_and_keep_the_rotated_token() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant()
    tokens: list[str] = []

    async def call() -> None:
        tokens.append(await broker.access_token("g", grant))

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(call)

    assert tokens == ["at-1"] * 8
    assert grant.refreshes == ["rt-0"]
    assert (grant.refresh_token, grant.version) == ("rt-1", 1)
    assert grant.marks == []


async def test_expired_cache_refreshes_with_the_rotated_token() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant(expires_in=30)  # inside the 60 s margin → never cached as fresh
    assert await broker.access_token("g", grant) == "at-1"
    assert await broker.access_token("g", grant) == "at-2"
    assert grant.refreshes == ["rt-0", "rt-1"]


async def test_rejected_refresh_marks_only_the_version_it_read() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant()
    grant.reject_next = "invalid_client"
    with pytest.raises(OAuthGrantRejected) as caught:
        await broker.access_token("g", grant)
    assert caught.value.reason == "invalid_client"
    assert grant.marks == [("invalid_client", "0")]


async def test_unreadable_grant_is_marked_without_a_version() -> None:
    class Missing(FakeGrant):
        async def read(self) -> tuple[str, str]:
            raise OAuthGrantRejected("not_authorized")

    grant = Missing()
    with pytest.raises(OAuthGrantRejected):
        await OAuthTokenBroker().access_token("g", grant)
    assert grant.marks == [("not_authorized", None)] and grant.refreshes == []


async def test_transient_failure_leaves_the_grant_alone() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant()
    grant.unavailable_next = True
    with pytest.raises(OAuthRefreshUnavailable):
        await broker.access_token("g", grant)
    assert grant.marks == []
    assert await broker.access_token("g", grant) == "at-2"


async def test_unsaved_rotated_token_is_used_for_the_next_refresh() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant(expires_in=30)
    grant.store_fails = True
    assert await broker.access_token("g", grant) == "at-1"  # rt-1 issued but not stored
    grant.store_fails = False
    assert await broker.access_token("g", grant) == "at-2"
    assert grant.refreshes == ["rt-0", "rt-1"]  # not the dead rt-0 again
    assert (grant.refresh_token, grant.version) == ("rt-2", 1)


async def test_grant_changed_meanwhile_is_not_cached() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant()
    grant.store_conflict = True
    assert await broker.access_token("g", grant) == "at-1"
    assert await broker.access_token("g", grant) == "at-2"  # nothing cached from the replaced grant
    assert grant.refreshes == ["rt-0", "rt-reauthorized"]


async def test_refresh_started_before_forget_is_not_cached() -> None:
    broker = OAuthTokenBroker()

    class Reconnected(FakeGrant):
        async def refresh(self, refresh_token: str) -> RefreshedToken:
            result = await super().refresh(refresh_token)
            broker.forget("g")  # someone authorized again while this refresh was in flight
            return result

    grant = Reconnected(rotate=False)
    assert await broker.access_token("g", grant) == "at-1"
    assert "g" not in broker._cache


async def test_invalidate_drops_only_the_refused_token() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant(rotate=False, expires_in=None)
    assert await broker.access_token("g", grant) == "at-1"
    broker.invalidate("g", "some-older-token")
    assert await broker.access_token("g", grant) == "at-1"  # kept: no expiry, not the refused one
    broker.invalidate("g", "at-1")
    assert await broker.access_token("g", grant) == "at-2"


async def test_primed_token_is_used_without_a_refresh() -> None:
    broker, grant = OAuthTokenBroker(), FakeGrant()
    broker.prime("g", "fresh-from-callback", 3600)
    assert await broker.access_token("g", grant) == "fresh-from-callback"
    assert grant.refreshes == []
