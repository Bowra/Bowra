"""Reading a stored deck and its edit inventory (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md §5.2).

The inventory is parsed in the bounded parse child. An artifact's bytes never change, so the
last inventories are kept in memory by artifact ID and size; the deck editor and element
references (§6.9) share them.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore

_INVENTORY_CACHE_SIZE = 16
_inventory_cache: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()


class DeckUnavailable(Exception):
    """The deck could not be read or parsed.

    ``code`` is ``presentation_unreadable`` (the deck itself, ``reason`` names why),
    ``file_unavailable`` (its bytes could not be read) or ``parse_unavailable`` (the
    bounded child failed, ``reason`` is the child's failure such as ``timeout``).
    """

    def __init__(self, code: str, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}:{reason}" if reason else code)


async def read_deck_bytes(store: ArtifactStore, art: Artifact) -> bytes:
    if art.size <= 0:
        raise DeckUnavailable("presentation_unreadable", "empty")
    chunks = bytearray()
    try:
        async for chunk in store.iter_read_range(art, start=0, end=art.size + 1):
            chunks.extend(chunk)
            if len(chunks) > art.size:
                raise DeckUnavailable("file_unavailable", "longer_than_recorded")
    except DeckUnavailable:
        raise
    except Exception as exc:
        raise DeckUnavailable("file_unavailable", type(exc).__name__) from exc
    if len(chunks) != art.size:
        raise DeckUnavailable("file_unavailable", "incomplete")
    return bytes(chunks)


async def deck_inventory(store: ArtifactStore, art: Artifact) -> dict[str, Any]:
    """The edit inventory of one stored deck, from the bounded child or the memory cache."""
    from bowhead_core.untrusted_parse import run_worker_json

    key = (art.id, art.size)
    cached = _inventory_cache.get(key)
    if cached is not None:
        _inventory_cache.move_to_end(key)
        return cached
    data = await read_deck_bytes(store, art)
    result, reason = await run_worker_json("pptx_edit_inventory", {}, data=data)
    if result is None:
        raise DeckUnavailable("parse_unavailable", reason)
    if "unreadable" in result:
        raise DeckUnavailable("presentation_unreadable", str(result["unreadable"]))
    _inventory_cache[key] = result
    while len(_inventory_cache) > _INVENTORY_CACHE_SIZE:
        _inventory_cache.popitem(last=False)
    return result
