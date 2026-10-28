"""The attempt scratch store: the only place an evaluation attempt keeps files it produced or read.

Contract: docs/design/agent-eval-sandbox-scratch-mode.md §3.3. One
store per attempt, process memory only, cleared in the attempt's ``finally``. References use the normal
``art_`` grammar but are minted from 128 random bits and known only here: the artifact store never sees
them, so any other consumer gets "not found".

Producers: data routes (the live Datarc route writes its row results through ``EvalScratchArtifactStore``)
and, once that design ships, scratch sandbox outputs. Data entries are never evicted; when the store is
full, the oldest sandbox outputs go first, and a data entry that still does not fit is rejected.
"""

from __future__ import annotations

import hashlib
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_ENTRIES = 64
MAX_BYTES = 32 * 1024 * 1024

ScratchEntryKind = Literal["data", "sandbox_output"]


class ScratchStoreFull(RuntimeError):
    """The entry does not fit in the attempt's bounds; nothing was stored."""

    def __init__(self) -> None:
        super().__init__("agent_eval_scratch_store_full")


@dataclass(frozen=True, slots=True)
class ScratchEntry:
    ref: str
    filename: str
    media_type: str
    size: int
    sha256: str
    kind: ScratchEntryKind
    content: bytes = field(repr=False)


@dataclass(slots=True)
class EvalScratchStore:
    """Bounded per-attempt store; ``clear`` drops every entry and its bytes."""

    max_entries: int = MAX_ENTRIES
    max_bytes: int = MAX_BYTES
    _entries: OrderedDict[str, ScratchEntry] = field(default_factory=OrderedDict)
    evictions: int = 0

    @property
    def size_bytes(self) -> int:
        return sum(entry.size for entry in self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def put(self, *, content: bytes, filename: str, media_type: str, kind: ScratchEntryKind) -> ScratchEntry:
        size = len(content)
        if size > self.max_bytes:
            raise ScratchStoreFull
        # Sandbox outputs make room first; data entries are never evicted.
        while len(self._entries) + 1 > self.max_entries or self.size_bytes + size > self.max_bytes:
            victim = next((ref for ref, entry in self._entries.items() if entry.kind == "sandbox_output"), None)
            if victim is None:
                raise ScratchStoreFull
            del self._entries[victim]
            self.evictions += 1
        entry = ScratchEntry(
            ref=f"art_{secrets.token_hex(16)}",
            filename=filename,
            media_type=media_type,
            size=size,
            sha256=hashlib.sha256(content).hexdigest(),
            kind=kind,
            content=content,
        )
        self._entries[entry.ref] = entry
        return entry

    def get(self, ref: str) -> ScratchEntry | None:
        return self._entries.get(ref)

    def clear(self) -> None:
        self._entries.clear()


@dataclass(frozen=True, slots=True)
class _ScratchArtifact:
    """The shape a Tool reads back from ``ArtifactStore.put_bytes`` (``id`` and ``filename``)."""

    id: str
    filename: str
    mime: str
    size: int


@dataclass(slots=True)
class EvalScratchArtifactStore:
    """An ``artifact_store`` for evaluation Tool calls that writes only to the attempt scratch store.

    It accepts the production ``put_bytes`` keywords and ignores the persistence ones (session, Turn,
    creator, query text): nothing is written to the ``artifacts`` table, a Space file or a Data entry.
    """

    store: EvalScratchStore
    on_rejected: Any = None

    async def put_bytes(self, *, content: bytes, filename: str, mime: str, **_persistence: Any) -> _ScratchArtifact:
        try:
            entry = self.store.put(content=bytes(content), filename=str(filename), media_type=str(mime), kind="data")
        except ScratchStoreFull:
            if callable(self.on_rejected):
                self.on_rejected()
            raise
        return _ScratchArtifact(id=entry.ref, filename=entry.filename, mime=entry.media_type, size=entry.size)


__all__ = [
    "MAX_BYTES",
    "MAX_ENTRIES",
    "EvalScratchArtifactStore",
    "EvalScratchStore",
    "ScratchEntry",
    "ScratchStoreFull",
]
