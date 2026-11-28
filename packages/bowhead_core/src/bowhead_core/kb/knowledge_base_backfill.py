"""Resumable per-Team backfill of the knowledge base key in chunk metadata.

Chunks written before revision ``0239_knowledge_bases`` carry no ``knowledge_base_id``. Chat
search does not need it (an exclusion filter matches chunks without the key), but selecting
knowledge bases for an Agent filters by inclusion, so it may be offered only after this job has
finished for the Team. The job walks the Team's knowledge files in file ID order, writes each
file's knowledge base into its chunks in the Team's current index, and records its cursor and
completion in ``kb_maintenance_state``. It is not scheduled; maintenance calls it explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.kb.generations import now_iso
from bowhead_core.kb.knowledge_base_store import knowledge_base_key
from bowhead_core.kb.maintenance import KbMaintenanceStore

STATE_KEY_PREFIX = "knowledge_base_chunk_key:"
STATE_VERSION = 1
_IDENTITY_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ChunkKeyBackfillProgress:
    team_id: str
    index_version: str
    completed: bool
    after: str
    files: int
    chunks_updated: int


def state_key(team_id: str) -> str:
    return STATE_KEY_PREFIX + team_id


def _identity(file: Any) -> tuple[str, str, str, str, str]:
    return (file.scope, file.scope_id, file.workspace_id, file.team_id, knowledge_base_key(file))


def _progress(team_id: str, state: dict[str, Any]) -> ChunkKeyBackfillProgress:
    return ChunkKeyBackfillProgress(
        team_id=team_id,
        index_version=str(state["index_version"]),
        completed=bool(state.get("completed_at")),
        after=str(state.get("after") or ""),
        files=int(state.get("files") or 0),
        chunks_updated=int(state.get("chunks_updated") or 0),
    )


async def chunk_key_backfill_completed(files_db_path: str, team_id: str, index_version: str) -> bool:
    """Whether every chunk of the Team's index carries the knowledge base key."""
    state = await KbMaintenanceStore(files_db_path).state_value(state_key(team_id))
    return bool(state and state.get("version") == STATE_VERSION and state.get("index_version") == index_version and state.get("completed_at"))


async def _backfill_file(files_store: Any, kb_store: Any, file: Any) -> int:
    """Write one file's key, then confirm the row did not move meanwhile; a moved file is redone."""
    updated = 0
    for _ in range(_IDENTITY_ATTEMPTS):
        target = (await files_store.effective_knowledge_base_ids([file]))[file.file_id]
        updated += await kb_store.set_chunks_knowledge_base(file, target)
        latest = await files_store.get(file.file_id)
        if latest is None or _identity(latest) == _identity(file):
            return updated
        file = latest
    raise RuntimeError(f"Knowledge file {file.file_id} kept moving during the chunk key backfill; run the backfill again once it is idle.")


async def backfill_chunk_knowledge_base_ids(
    files_store: Any,
    kb_store: Any,
    team_id: str,
    *,
    page_size: int = 100,
    max_files: int | None = None,
) -> ChunkKeyBackfillProgress:
    """Advance the Team's backfill by at most ``max_files`` files (all when None) and record progress.

    Rerunning a finished backfill does nothing. A change of the Team's index version restarts it,
    because the new index is a different set of chunks.
    """
    if not team_id:
        raise ValueError("team required")
    if page_size < 1 or (max_files is not None and max_files < 1):
        raise ValueError("Invalid backfill batch size")
    maintenance = KbMaintenanceStore(files_store._db_path)
    index_version = kb_store.index_version
    state = await maintenance.state_value(state_key(team_id))
    if not state or state.get("version") != STATE_VERSION or state.get("index_version") != index_version:
        state = {"version": STATE_VERSION, "index_version": index_version, "after": "", "files": 0, "chunks_updated": 0, "completed_at": None}
    if state.get("completed_at"):
        return _progress(team_id, state)
    visited = 0
    while max_files is None or visited < max_files:
        limit = page_size if max_files is None else min(page_size, max_files - visited)
        page = await maintenance.team_files_page(team_id, after=str(state["after"]), limit=limit)
        if not page:
            state["completed_at"] = now_iso()
            break
        for file in page:
            state["chunks_updated"] = int(state["chunks_updated"]) + await _backfill_file(files_store, kb_store, file)
        visited += len(page)
        state.update(after=page[-1].file_id, files=int(state["files"]) + len(page))
        await maintenance.save_state_value(state_key(team_id), state)
    await maintenance.save_state_value(state_key(team_id), state)
    return _progress(team_id, state)
