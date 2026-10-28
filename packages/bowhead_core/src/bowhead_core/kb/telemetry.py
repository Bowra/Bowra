"""KB telemetry helpers (audit events + Prometheus metrics).

Tech design v3.6 §13. 22 个 audit event (V1).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─── audit events (write audit_log table) ────────────────────
# 18+ events total (P1-O11 round 3 additions + round 6 search/rerank timeout).
KB_AUDIT_EVENTS = frozenset(
    {
        # ─── lifecycle ───
        "kb.uploaded",
        "kb.uploaded.bundle",  # 0.24.4: zip bundle summary (per-bundle)
        "kb.uploaded.bundle_entry",  # 0.24.4: zip bundle per-file entry
        "kb.parse_started",
        "kb.parse_done",
        "kb.parse_failed",
        "kb.deleted",
        "kb.scope_changed",
        "kb.searched_chunks",
        "kb.previewed",
        "kb.downloaded",
        # ─── security ───
        "kb.unauthorized_access_attempt",
        # untrusted_parse cluster B: zip preflight blocks OOXML decompression bombs (compression ratio over threshold)
        "kb.zip_bomb_rejected",
        # ─── embed / parse details ───
        "kb.embed_text_truncated",  # P1-M truncate chunk >8K
        "kb.embed_failed",  # Embedder failure
        "kb.embed_chunk_skipped",  # P1-O7 skip single bad chunk
        "kb.parse_count_mismatch_critical",  # P0-D >5%
        "kb.parse_count_mismatch_minor",  # P0-D ≤5%
        # ─── lifecycle background ───
        "kb.hard_deleted_after_grace",  # P1-N3 90 day cron
        "kb.consistency_drift_detected",  # §8.5 daily audit script
        # ─── scope change cross-coll migration ───
        "kb.chunks_migrated_cross_collection",  # P0-O2
        "kb.scope_change_migration_failed",  # P0-O2 add-stage failure
        "kb.scope_change_migration_partial",  # P0-O2 delete-stage failure
        # ─── timeout ───
        "kb.search_timeout",  # P1-Q3
        "kb.rerank_timeout",  # P1-R3
        # ─── kb-knowledge-tables sprint v1.0: structured extraction lifecycle ───
        "kb.extract_done",  # worker StructuredExtractor success
        "kb.extract_skipped",  # doc_type missing / no fields / empty text
        "kb.extract_failed",  # extractor exception (parse stays done)
        # ─── α-2 retrieval-quick-wins sprint ───
        "kb.junk_chunks_filtered",  # Retriever-filtered noise (empty header / short)
        # retriever review F6 oversampling/filtering/diversification observability (fetched → survivable → pool).
        # Previously unregistered → every production retrieval warned "emit_audit unknown event_type", losing telemetry.
        "kb.candidate_diversify",
        # ─── kb-knowledge-tables v2 sprint: schema v1 → v2 migration ───
        "kb.migrate_v1_to_v2_done",  # lifespan migration success (per-cold-boot)
        "kb.migrate_v1_to_v2_failed",  # migration exception path (degraded, not crash)
        "kb.record_v1_to_v2_migrate",  # per-record lazy migrate via update_field
        "kb.field_index_failed",  # _rebuild_field_indexes exception (degraded)
        # ─── kb-structured-tables Sprint 2: Phase A (catalog + migrate) ───
        "kb.migrate_doc_types_add_columns_done",  # doc_types/files ALTER fn (Phase A)
        "kb.catalog_seed_inserted",  # catalog_seed.seed_catalog_for_ws — N inserted
        "kb.catalog_seed_already_exists",  # UNIQUE collision on a catalog name (user pre-created)
        # ─── kb-structured-tables Sprint 2: Phase B (classifier + extract + backfill) ───
        "kb.classify_fast_path_hit",  # sample_keywords ≥3 distinct match
        "kb.classify_llm_call",  # LLM classify path invoked
        "kb.classify_no_match",  # classifier returned None
        "kb.classify_low_confidence",  # LLM returned confidence < 0.7
        "kb.classify_skipped_disabled",  # auto_extract_enabled=false
        "kb.classify_skipped_per_row_v1",  # cardinality=many (Sprint 2 not extracting)
        "kb.classify_failed",  # classifier exception (sanitized error_code)
        "kb.classify_circuit_open",  # circuit breaker tripped
        "kb.classify_invalid_name",  # LLM returned chosen_name not in candidates
        "kb.extract_duplicate_skipped",  # create() dedup_check raised
        "kb.extract_failed_classified",  # classified-path extract failed (auto col cleared)
        "kb.backfill_admin_invoked",  # POST /admin/kb/files/backfill
        "kb.backfill_completed",  # task done success
        "kb.backfill_failed",  # task done failure
        # ─── kb-per-row-extract Sprint 25 (cardinality=many per-row LLM extraction) ──
        "kb.row_extract_dispatched",  # worker pre-write queued job + spawn task
        "kb.row_extract_started",  # task running
        "kb.row_extract_batch_done",  # per batch (throttled)
        "kb.row_extract_completed",  # all rows done
        "kb.row_extract_failed",  # terminal failure
        "kb.row_extract_resumed",  # heartbeat scanner / lifespan resume
        "kb.row_extract_quota_exceeded",  # per-ws monthly token quota hit
        "kb.row_extract_force_trigger",  # user force=true admin endpoint
        "kb.row_extract_truncated",  # max_rows cap hit
        "kb.row_extract_zombie_detected",  # scanner found 15min no heartbeat
        "kb.row_extract_duplicate_skipped_summary",  # per batch dedup count (summary)
        "kb.row_extract_force_rate_limit",  # 5/day force exceeded
        "kb.row_extract_llm_mismatch_len",  # LLM returned ≠ expected batch size (P2-SEC)
        "kb.migrate_kb_extracted_rows_done",  # row store schema init
        "kb.migrate_kb_extracted_rows_failed",  # migration exception
        "kb.row_field_index_failed",  # _rebuild_row_field_indexes exception
        # ─── kb-matrix M2 (§9): document matrix—each has real emit + UT assertion (§0.2 discipline,
        # no more registered-but-never-emitted dead events like row_extract_batch_done) ──
        "kb.matrix_created",
        "kb.matrix_files_changed",
        "kb.matrix_column_added",
        "kb.matrix_column_edited",
        "kb.matrix_column_promoted",
        "kb.matrix_column_deleted",
        "kb.matrix_deleted",
        "kb.matrix_materialized",  # Save as matrix (from_template)
        "kb.matrix_run_started",
        "kb.matrix_run_progress",  # Throttle = % max(1, total_calls // 5)
        "kb.matrix_run_finished",  # Credits estimate/charged + four cell counts
        "kb.matrix_run_cancelled",
        "kb.matrix_run_resumed",  # boot resume
        "kb.matrix_cell_failed_summary",  # Run convergence emits one per reason, not per cell
        "kb.matrix_cell_edited",
        "kb.matrix_cell_edit_superseded",  # Single-cell rerun replaces manual value (§5.5 light undo)
        "kb.matrix_cell_kept_as_text",
        "kb.matrix_cell_verified",
        "kb.matrix_cell_verify_reverted",  # kb-matrix M3 FR-12: undo verification (no-op emits nothing)
        "kb.matrix_cell_edit_reverted",  # kb-matrix M3 FR-9: undo edit flag
        "kb.matrix_provider_shortcircuit",
        "kb.matrix_graduated",  # kb-matrix M4 FR-40: graduation (flat counts + mode; never cell content/column definitions)
        "kb.matrix_graduation_swept",  # kb-matrix M4: pending self-healing sweep (flip/reclaim counts)
        "kb.template_seeded",  # kb-matrix M4 FR-41: built-in column templates per-ws seed (counts)
        # ─── kb-matrix-sharing (§5/§7): organization template library—publish/copy/revoke (payload only flat
        # counts + id; never field definitions/names/descriptions; see §10 audit payload discipline) ──
        "kb.team_template_published",
        "kb.team_template_republished",  # Idempotent republish refreshes snapshot (same source)
        "kb.team_template_revoked",  # Explicit revoke or source-delete cascade
        "kb.team_template_copied",  # Materialize kind=template clone in recipient space
    }
)


# Sprint 25 P0-TD-SEC-3: runtime audit payload whitelist enforce.
# Block known sensitive key names that could leak LLM raw response / cell content / prompt body
# / user financial data into stdout / log aggregation. Tested via mock-bus UT.
_BANNED_AUDIT_KEYS = frozenset(
    {
        "row_content",
        "cell_text",
        "fields_normalized",
        "prompt_body",
        "llm_response_text",
        "cell_value",
        "source_excerpt",
        "raw_data",
        "llm_raw",
        "source_cell_text",  # Sprint 25 specific — LLM-returned cell text
        "checkpoint_json",  # Sprint 25 — checkpoint json contains state, not for audit
        "fields_json",  # Sprint 25 — full field json shouldn't be audited
        "question_text",  # kb-matrix M2 §9—column questions are user content; pass only matrix_id/column_id
        "options",  # kb-matrix M3 §6—options also user content (preventive; acknowledge exact top-level key matching limits)
    }
)


def emit_audit(event_type: str, payload: dict[str, Any]) -> None:
    """Emit KB audit event.

    V1 实现: 走 logger.info (跟现 audit pattern). V2 走 audit_log SQL 表.
    Caller validation: event_type 必在 KB_AUDIT_EVENTS (catch typo).
    Sprint 25 P0-TD-SEC-3: payload key 不能含敏感 key (LLM raw / cell content / etc).
    Banned keys 自动 filter + log.error (caller bug, but don't crash).
    """
    if event_type not in KB_AUDIT_EVENTS:
        logger.warning("emit_audit unknown event_type=%s payload=%r", event_type, payload)
        # Still record (forward compatibility: new V2 events must not hard-fail), but warn

    # P0-TD-SEC-3: filter banned keys
    if payload:
        bad = _BANNED_AUDIT_KEYS & set(payload.keys())
        if bad:
            logger.error(
                "emit_audit BLOCKED banned keys=%s event=%s — caller bug, fix payload",
                sorted(bad),
                event_type,
            )
            payload = {k: v for k, v in payload.items() if k not in _BANNED_AUDIT_KEYS}

    logger.info("kb_audit", extra={"event_type": event_type, "payload": payload})
