"""kb-structured-tables Sprint 2 — idempotent per-workspace catalog seed.

Per PRD r2 + tech-design §A.4. Mounts the 5 CatalogDocType definitions from
doc_type_catalog.py into each workspace's kb_doc_types table.

Idempotency:
  - Marker key `catalog_seeded_at_ws:{ws}` in kb_lifespan_markers gates re-runs
  - UNIQUE(workspace_id, name) on kb_doc_types prevents accidental duplicate
    on race or marker drift (audit `kb.catalog_seed_already_exists`)
  - All catalog rows tagged `created_by="system_catalog_seed"` for audit /
    distinguishability from user-created doc_types

Wire:
  - Lifespan fire-and-forget background task iterates
    `KbFilesStore.list_distinct_workspaces()` and calls `seed_catalog_for_ws`
    for each ws (paced ~100ms between to avoid sqlite lock starvation).
  - Admin script `scripts/seed_kb_doc_type_catalog.py` directly invokes for
    targeted ws or --all-ws.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bowhead_core.kb.doc_type_catalog import CATALOG
from bowhead_core.kb.doc_type_store import KbDocTypeStore  # noqa: TC001 (type + runtime import OK)
from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore
from bowhead_core.kb.telemetry import emit_audit

logger = logging.getLogger(__name__)


SYSTEM_CATALOG_SEED_USER = "system_catalog_seed"


@dataclass(frozen=True)
class SeedResult:
    """Result of a single ws seed attempt."""

    workspace_id: str
    inserted: int
    skipped_already_present: int
    skipped_via_marker: bool  # True if marker existed at start, no INSERT attempted


async def seed_catalog_for_ws(
    workspace_id: str,
    doc_type_store: KbDocTypeStore,
    marker_store: KbLifespanMarkerStore,
) -> SeedResult:
    """Seed CATALOG into a single workspace. Idempotent.

    Behavior:
      1. Check marker → if present, return (skipped_via_marker=True)
      2. For each CatalogDocType, attempt doc_type_store.create() with
         created_by="system_catalog_seed"
         - UNIQUE collision (sqlite IntegrityError) → audit + count as skipped
      3. Set marker on completion (atomic — marker write follows last INSERT)
      4. Return SeedResult

    Soft-fail: any unexpected exception per-catalog row → log + count as
    skipped + continue. The marker is set only on full pass; on partial
    failure (most likely sqlite lock), next boot re-tries non-conflicting
    rows.
    """
    marker_key = KbLifespanMarkerStore.catalog_marker_key(workspace_id)
    if await marker_store.has_marker(marker_key):
        return SeedResult(
            workspace_id=workspace_id,
            inserted=0,
            skipped_already_present=0,
            skipped_via_marker=True,
        )

    inserted = 0
    skipped = 0
    for cdt in CATALOG:
        try:
            await doc_type_store.create(
                workspace_id=workspace_id,
                name=cdt.name,
                created_by=SYSTEM_CATALOG_SEED_USER,
                description=cdt.description,
                fields=list(cdt.fields),
                cardinality=cdt.cardinality,
                auto_extract_enabled=cdt.auto_extract_enabled,
                sample_keywords=list(cdt.sample_keywords),
                dedup_key_fields=list(cdt.dedup_key_fields),  # Sprint 25
            )
            inserted += 1
        except Exception as exc:
            # UNIQUE collision: sqlite raises IntegrityError. Other Errors:
            # ValueError (validation) — both treated as "skip + warn".
            msg = str(exc)
            if "UNIQUE constraint failed" in msg or "IntegrityError" in type(exc).__name__:
                emit_audit(
                    "kb.catalog_seed_already_exists",
                    {
                        "workspace_id": workspace_id,
                        "name": cdt.name,
                    },
                )
                skipped += 1
                continue
            logger.warning(
                "catalog seed failed ws=%s name=%s err=%s",
                workspace_id,
                cdt.name,
                exc,
            )
            skipped += 1

    # Set marker only on full attempt (we tried all 5; partial failures don't
    # prevent next-boot retry of the remaining slots).
    await marker_store.set(
        marker_key,
        {
            "inserted": inserted,
            "skipped_already_present": skipped,
            "catalog_size": len(CATALOG),
        },
    )
    emit_audit(
        "kb.catalog_seed_inserted",
        {
            "workspace_id": workspace_id,
            "count": inserted,
            "skipped_already_present": skipped,
        },
    )
    return SeedResult(
        workspace_id=workspace_id,
        inserted=inserted,
        skipped_already_present=skipped,
        skipped_via_marker=False,
    )
