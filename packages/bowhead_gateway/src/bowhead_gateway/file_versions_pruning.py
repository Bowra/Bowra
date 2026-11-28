"""Automatic pruning of older file versions (docs/sprints/file-versions-2026-11-10/tech-design.md §7, B4).

Each file keeps its newest three live versions; same-run drafts go first. The store lists
candidates and rechecks every protection inside its write transaction; KB references live
in another database (``kb_files``), so they are checked here. Pruning fails closed: without
the KB store, or when the KB lookup fails, nothing is pruned. It soft-deletes only, and the
nightly purge removes pruned rows after 30 days. A file waits while a Turn of its session is
running or paused.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

log = logging.getLogger(__name__)

_KB_CHUNK = 5000


async def prune_file_versions(app: Any, lineage_roots: Sequence[str] | None = None) -> int:
    """Prune these files (every file with versions when None); returns the number of versions pruned."""
    store = getattr(app.state, "artifact_store", None)
    kb_store = getattr(app.state, "kb_files_store", None)
    if store is None or not hasattr(store, "prune_version_candidates"):
        return 0
    if kb_store is None:
        log.info("file_versions.prune_skipped reason=kb_store_unavailable")
        return 0
    candidates = await store.prune_version_candidates(lineage_roots)
    if not candidates:
        return 0
    protected: set[str] = set()
    for start in range(0, len(candidates), _KB_CHUNK):
        # A failed lookup raises: the caller logs it and nothing is pruned this run.
        protected |= set(await kb_store.artifact_ids_in_kb(candidates[start : start + _KB_CHUNK]))
    pruned = await store.prune_versions(candidates, kb_protected=protected)
    log.info("file_versions.pruned count=%d candidates=%d kb_protected=%d", pruned, len(candidates), len(protected))
    return pruned


async def versions_dropped_by_next_save(app: Any, artifact_id: str) -> list[int]:
    """Version numbers pruning removes once one more version of this file is saved, KB-checked.

    The same rules as ``prune_file_versions``, as a dry run: without the KB store nothing is pruned,
    so nothing is reported either. A failed lookup raises; callers treat the fact as optional.
    """
    store = getattr(app.state, "artifact_store", None)
    kb_store = getattr(app.state, "kb_files_store", None)
    if store is None or kb_store is None or not hasattr(store, "versions_pruned_by_next_version"):
        return []
    found = await store.versions_pruned_by_next_version(artifact_id)
    if not found:
        return []
    protected = set(await kb_store.artifact_ids_in_kb([item_id for item_id, _ in found]))
    return [number for item_id, number in found if item_id not in protected]


#: A version is usually written inside a Turn of its session, so its file waits until no Turn of
#: that session is running or paused; after this long the nightly run prunes it instead.
_DEFER_POLL_SECONDS = 30.0
_DEFER_MAX_WAIT_SECONDS = 3600.0


def schedule_file_version_prune(app: Any, lineage_root: str) -> None:
    """After a version is written or a file restored: prune that file in the background, best-effort.

    A Turn lists the session's files when it starts and checks them again when it publishes
    its history; pruning a listed version under a running or paused Turn fails that Turn
    (116 dogfood N12), so the prune waits for the session to settle. The store rechecks the
    same condition inside its write transaction.
    """
    pending = getattr(app.state, "_file_version_prune_pending", None)
    if pending is None:
        pending = set()
        app.state._file_version_prune_pending = pending
    if lineage_root in pending:
        return
    pending.add(lineage_root)

    async def run() -> None:
        try:
            store = getattr(app.state, "artifact_store", None)
            waited = 0.0
            while store is not None and await store.version_prune_deferred(lineage_root):
                if waited >= _DEFER_MAX_WAIT_SECONDS:
                    log.info("file_versions.prune_deferred root=%s reason=session_turn_active", lineage_root)
                    return
                await asyncio.sleep(_DEFER_POLL_SECONDS)
                waited += _DEFER_POLL_SECONDS
            await prune_file_versions(app, [lineage_root])
        except Exception:
            log.warning("file_versions.prune_failed root=%s", lineage_root, exc_info=True)
        finally:
            pending.discard(lineage_root)

    task = asyncio.create_task(run())
    tasks = getattr(app.state, "_file_version_prune_tasks", None)
    if tasks is None:
        tasks = set()
        app.state._file_version_prune_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


__all__ = ["prune_file_versions", "schedule_file_version_prune", "versions_dropped_by_next_save"]
