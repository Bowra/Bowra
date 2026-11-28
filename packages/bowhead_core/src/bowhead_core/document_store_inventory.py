"""Reading a stored Word document and its edit inventory (docs/sprints/docx-inline-edit-2026-11-26/tech-design.md §6.2).

The inventory is parsed in the bounded parse child. An artifact's bytes never change, so the
last inventories are kept in memory by artifact ID and size, as ``presentation_deck`` does for decks.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore

_INVENTORY_CACHE_SIZE = 16
_inventory_cache: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()


class DocumentUnavailable(Exception):
    """The document could not be read or parsed.

    ``code`` is ``document_unreadable`` (the file itself, ``reason`` names why),
    ``file_unavailable`` (its bytes could not be read) or ``parse_unavailable`` (the
    bounded child failed, ``reason`` is the child's failure such as ``timeout``).
    """

    def __init__(self, code: str, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}:{reason}" if reason else code)


async def read_document_bytes(store: ArtifactStore, art: Artifact) -> bytes:
    """All bytes of a stored document, checked against its recorded size."""
    from bowhead_core.presentation_deck import DeckUnavailable, read_deck_bytes

    try:
        return await read_deck_bytes(store, art)
    except DeckUnavailable as exc:
        code = "document_unreadable" if exc.code == "presentation_unreadable" else exc.code
        raise DocumentUnavailable(code, exc.reason) from exc


async def document_inventory(store: ArtifactStore, art: Artifact) -> dict[str, Any]:
    """The edit inventory of one stored Word document, from the bounded child or the memory cache."""
    from bowhead_core.untrusted_parse import run_worker_json

    key = (art.id, art.size)
    cached = _inventory_cache.get(key)
    if cached is not None:
        _inventory_cache.move_to_end(key)
        return cached
    data = await read_document_bytes(store, art)
    result, reason = await run_worker_json("docx_edit_inventory", {}, data=data)
    if result is None:
        raise DocumentUnavailable("parse_unavailable", reason)
    if "unreadable" in result:
        raise DocumentUnavailable("document_unreadable", str(result["unreadable"]))
    _inventory_cache[key] = result
    while len(_inventory_cache) > _INVENTORY_CACHE_SIZE:
        _inventory_cache.popitem(last=False)
    return result
