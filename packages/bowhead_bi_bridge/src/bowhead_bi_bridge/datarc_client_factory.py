"""workspace-datarc-decouple P2a — per-connection DatarcClient pool.

③ multi-tenant: a turn uses whichever team datarc connection applies, so DatarcClient is no
longer a process singleton. The factory caches one DatarcClient per distinct connection
(base_url + auth_key), keyed by a non-reversible hash so the raw secret is never a dict key.
Each cached entry comes from a team connection. The gateway
resolves an team's connection (base_url, auth_key) from DatarcConnectionStore, then asks the
factory for the client — the bi-bridge never touches the team/connection store (layering).
"""

from __future__ import annotations

import hashlib

from bowhead_bi_bridge.datarc_client import DatarcClient


class DatarcClientFactory:
    """Process-wide pool of DatarcClient instances, one per (base_url, auth_key) connection."""

    def __init__(self, *, timeout_sec: int = 60) -> None:
        self._timeout = timeout_sec
        self._pool: dict[str, DatarcClient] = {}

    @staticmethod
    def _key(base_url: str, auth_key: str) -> str:
        # hash so the raw auth_key is never used as (or recoverable from) the cache key
        return hashlib.sha256(f"{base_url}\x00{auth_key}".encode()).hexdigest()

    def get_client(self, base_url: str, auth_key: str) -> DatarcClient:
        """Cached DatarcClient for this connection — built lazily on first use, reused after."""
        key = self._key(base_url, auth_key)
        client = self._pool.get(key)
        if client is None:
            # ssrf_guard=True: these are admin-supplied (untrusted) per-team base_urls, so every
            # outbound request re-validates the resolved IP (review P1-b — DNS rebinding defence).
            # Team settings deliberately permit HTTP(S), including private-network on-prem
            # services. This opt-in is exclusive to the trusted team-admin connection path;
            # blocked ports and metadata/link-local/reserved targets remain forbidden.
            client = DatarcClient(
                base_url=base_url,
                auth_key=auth_key,
                timeout_sec=self._timeout,
                ssrf_guard=True,
            )
            self._pool[key] = client
        return client

    def size(self) -> int:
        return len(self._pool)

    async def aclose(self) -> None:
        for client in self._pool.values():
            await client.aclose()
        self._pool.clear()
