"""Single-flight OAuth access tokens for stored grants (provider-agnostic).

A broker keeps access tokens in process memory only and refreshes each grant under its
own lock, so concurrent callers share one refresh. Providers that rotate refresh tokens
reject a second refresh with the same refresh token, and some then revoke the whole
grant; without the lock, simultaneous calls at expiry fail (personal MCP OAuth spike,
2026-11-24: 1 of 8 calls succeeded on a rotating server, 1 of 4 on n8n).

A grant's stored refresh token is versioned by its ciphertext. A rotated refresh token is
written only over the version it replaced, and a rejected refresh marks the grant for
re-authorization only at that version, so a refresh that started before someone authorized
again never overwrites or breaks the new grant. ``forget`` bumps a generation counter so a
refresh already in flight does not cache a token from the old grant.

The locks are per process. Bowhead runs one application process (``deploy/start-app.sh``);
a deployment where several processes refresh the same grant would need a database lease.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Protocol

import anyio

logger = logging.getLogger(__name__)

EXPIRY_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class RefreshedToken:
    """One refresh response. ``refresh_token`` is set only when the provider returned one."""

    access_token: str
    expires_in: int | None
    refresh_token: str | None = None


class OAuthGrantRejected(Exception):
    """The provider will not renew this grant (or none is stored); someone must authorize again."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"OAuth grant rejected: {reason}")
        self.reason = reason


class OAuthRefreshUnavailable(Exception):
    """The refresh did not complete for a transient reason; the stored grant is unchanged."""

    def __init__(self, reason: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(f"OAuth refresh unavailable: {reason}")
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class OAuthGrant(Protocol):
    """One stored grant, adapted for the broker."""

    async def read(self) -> tuple[str, str]:
        """Return (refresh token, version). Raise ``OAuthGrantRejected`` when no usable grant is stored."""
        ...

    async def refresh(self, refresh_token: str) -> RefreshedToken:
        """Call the token endpoint. Raise ``OAuthGrantRejected`` or ``OAuthRefreshUnavailable`` on failure."""
        ...

    async def store_rotated(self, refreshed: RefreshedToken, expected_version: str) -> bool:
        """Persist a rotated refresh token over ``expected_version``; False when the grant changed meanwhile."""
        ...

    async def mark_rejected(self, reason: str, expected_version: str | None) -> None:
        """Record that the grant needs re-authorization, only at ``expected_version`` when given."""
        ...

    async def used(self) -> None:
        """Best-effort usage bookkeeping after a successful refresh."""
        ...


@dataclass
class _CachedToken:
    token: str
    expires_at: float


def _expires_at(expires_in: int | None) -> float:
    # Without an expiry the token is used until a 401 makes the caller invalidate it.
    if expires_in is None:
        return math.inf
    return time.monotonic() + max(expires_in - EXPIRY_MARGIN_SECONDS, 0)


class OAuthTokenBroker:
    """Process-wide access-token cache and single-flight refresh, keyed by grant."""

    def __init__(self) -> None:
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, anyio.Lock] = {}
        self._generation: dict[str, int] = {}
        # A rotated refresh token the store could not save: (version it replaces, token).
        # Reused while the stored version is still the one it replaces, so the grant survives.
        self._unsaved: dict[str, tuple[str, str]] = {}

    def forget(self, key: str) -> None:
        """Drop the cached access token; a refresh already in flight will not cache its result."""
        self._cache.pop(key, None)
        self._generation[key] = self._generation.get(key, 0) + 1

    def invalidate(self, key: str, token: str) -> None:
        """Forget ``token`` after the service rejected it, unless another caller already replaced it."""
        cached = self._cache.get(key)
        if cached is None or cached.token == token:
            self.forget(key)

    def prime(self, key: str, token: str, expires_in: int | None) -> None:
        """Cache the access token of a grant that was just authorized."""
        self.forget(key)
        self._unsaved.pop(key, None)
        self._cache[key] = _CachedToken(token, _expires_at(expires_in))

    def _cached(self, key: str) -> str | None:
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.token
        return None

    async def access_token(self, key: str, grant: OAuthGrant) -> str:
        token = self._cached(key)
        if token is not None:
            return token
        lock = self._locks.setdefault(key, anyio.Lock())
        async with lock:
            token = self._cached(key)
            if token is not None:
                return token
            generation = self._generation.get(key, 0)
            try:
                refresh_token, version = await grant.read()
            except OAuthGrantRejected as rejected:
                await grant.mark_rejected(rejected.reason, None)
                raise
            unsaved = self._unsaved.get(key)
            if unsaved is not None and unsaved[0] == version:
                refresh_token = unsaved[1]
            try:
                refreshed = await grant.refresh(refresh_token)
            except OAuthGrantRejected as rejected:
                await grant.mark_rejected(rejected.reason, version)
                self._unsaved.pop(key, None)
                self.forget(key)
                raise
            current = True
            if refreshed.refresh_token and refreshed.refresh_token != refresh_token:
                try:
                    current = await grant.store_rotated(refreshed, version)
                except Exception as exc:  # noqa: BLE001 - keep the rotated token rather than lose the grant
                    logger.warning("oauth_token_broker.rotated_refresh_token_unsaved key=%s error=%s", key, type(exc).__name__)
                    self._unsaved[key] = (version, refreshed.refresh_token)
                else:
                    self._unsaved.pop(key, None)
            if current and self._generation.get(key, 0) == generation:
                self._cache[key] = _CachedToken(refreshed.access_token, _expires_at(refreshed.expires_in))
            await grant.used()
            return refreshed.access_token
