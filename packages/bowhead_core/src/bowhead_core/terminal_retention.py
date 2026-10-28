"""External evidence purge orchestration for Terminal Protocol S3."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from bowhead_core.terminal_protocol import EvidencePurgeContext, EvidencePurgeReceipt
from bowhead_core.terminal_protocol_store import TerminalProtocolStore

EvidenceResolver = Callable[[EvidencePurgeContext], Awaitable[EvidencePurgeReceipt | None]]


class TerminalRetentionService:
    """Calls the external resolver outside DB transactions and commits verified receipts only."""

    def __init__(self, store: TerminalProtocolStore) -> None:
        self._store = store

    async def purge_evidence_once(
        self,
        *,
        purge_owner: str,
        resolver: EvidenceResolver,
        limit: int = 1,
    ) -> tuple[EvidencePurgeContext, ...]:
        """Purge one claim by default so a slow resolver cannot age later leases in a batch."""

        claims = await self._store.claim_evidence_purge(purge_owner=purge_owner, limit=limit)
        committed: list[EvidencePurgeContext] = []
        for claim in claims:
            receipt = await resolver(claim)
            if receipt is None:
                continue
            committed.append(await self._store.commit_evidence_purge(receipt))
        return tuple(committed)


__all__ = ["TerminalRetentionService"]
