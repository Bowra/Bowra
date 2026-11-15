"""Automatic pruning of older file versions (docs/sprints/file-versions-2026-11-10/tech-design.md §7, B4).

Each file keeps its newest three live versions; same-run drafts go first. The store lists
candidates and rechecks every protection inside its write transaction; KB references live
in another database (``kb_files``), so they are checked here. Pruning fails closed: without
the KB store, or when the KB lookup fails, nothing is pruned. It soft-deletes only, and the
nightly purge removes pruned rows after 30 days.
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


def schedule_file_version_prune(app: Any, lineage_root: str) -> None:
    """After a version is written or a file restored: prune that file in the background, best-effort."""

    async def run() -> None:
        try:
            await prune_file_versions(app, [lineage_root])
        except Exception:
            log.warning("file_versions.prune_failed root=%s", lineage_root, exc_info=True)

    task = asyncio.create_task(run())
    tasks = getattr(app.state, "_file_version_prune_tasks", None)
    if tasks is None:
        tasks = set()
        app.state._file_version_prune_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


__all__ = ["prune_file_versions", "schedule_file_version_prune"]
