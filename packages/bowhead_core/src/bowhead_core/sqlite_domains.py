"""Reviewed final table placement for the SQLite domain layout."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

LEGACY_LAYOUT = "sqlite-legacy-v1"
DOMAIN_LAYOUT = "sqlite-domains-v1"

ORIGINAL_TARGET_FILES: dict[str, str] = {
    "core": "bowhead.db",
    "files": "kb_files.db",
    "doc_types": "kb_doc_types.db",
    "extracted_records": "kb_extracted_records.db",
    "extracted_rows": "kb_extracted_rows.db",
    "per_row_jobs": "kb_per_row_jobs.db",
    "team_templates": "kb_team_templates.db",
    "matrix": "kb_matrix.db",
}

DOMAIN_TARGET_FILES: dict[str, str] = {
    **ORIGINAL_TARGET_FILES,
    "execution": "bowhead_execution.db",
    "content": "bowhead_content.db",
    "notifications": "bowhead_notifications.db",
    "observability": "bowhead_observability.db",
}

RETIRED_CORE_TABLES = frozenset({"business_rules", "mcp_servers", "artifact_stdout", "pbi_connection_legacy_0022"})
INFRASTRUCTURE_TABLES = frozenset({"alembic_version", "_phase0_probe"})

DOMAIN_TABLES: dict[str, tuple[str, ...]] = {
    "core": (
        "access_policy_state",
        "access_requests",
        "account_deletion_objects",
        "account_deletion_rows",
        "account_deletions",
        "account_owned_artifact_writes",
        "account_protocol_erasure_receipts",
        "admin_audit_log",
        "agent_answer_feedback",
        "agent_assignment",
        "agent_assignment_backfill_expected",
        "agent_assignment_backfill_run",
        "agent_catalog_projection",
        "agent_change_proposal",
        "agent_change_proposal_decision",
        "agent_coach_generation_request",
        "agent_command_receipt",
        "agent_default_opt_out",
        "agent_default_policy",
        "agent_dependency_update_policy",
        "agent_eval_alignment_loop",
        "agent_eval_alignment_question",
        "agent_eval_attempt_identity",
        "agent_eval_candidate_control",
        "agent_eval_case_job",
        "agent_eval_case_result",
        "agent_eval_case_revision",
        "agent_eval_comparison_execution",
        "agent_eval_comparison_run",
        "agent_eval_conversation_share",
        "agent_eval_evidence_state",
        "agent_eval_notification_outbox",
        "agent_eval_override",
        "agent_eval_plan",
        "agent_eval_plan_suite",
        "agent_eval_point_calibration",
        "agent_eval_point_example",
        "agent_eval_policy_revision",
        "agent_eval_regression_outbox",
        "agent_eval_run",
        "agent_eval_run_execution",
        "agent_eval_scratch_turn",
        "agent_eval_snapshot",
        "agent_eval_suite",
        "agent_eval_suite_review",
        "agent_eval_suite_revision",
        "agent_eval_trigger_policy",
        "agent_listing_safety_finding",
        "agent_listing_safety_report",
        "agent_marketplace_schema_meta",
        "agent_name_claim",
        "agent_name_conflict_group",
        "agent_ranking_coverage",
        "agent_ranking_snapshot",
        "agent_release_baseline_expected",
        "agent_release_baseline_run",
        "agent_release_candidate_name_claim",
        "agent_release_eval_evidence",
        "agent_revision_compatibility",
        "agent_role_preference",
        "agent_safety_report",
        "agent_taxonomy_suggestion",
        "agent_team_tag",
        "agent_version_automation_grant",
        "agent_version_automation_preparation",
        "agent_version_notification_outbox",
        "api_tokens",
        "bowra_connect_pending",
        "builtin_pbi_template",
        "builtin_tool_oauth_state",
        "builtin_tool_user_grant",
        "catalog_localized_listing",
        "coach_ci_delegation",
        "coach_ci_invocation",
        "coach_plugin_operation",
        "coach_skill_operation",
        "coach_tool_observation",
        "coach_tool_requirement",
        "config_copilot_draft",
        "credential_recovery_tokens",
        "credit_attention_state",
        "credit_exhaustion_events",
        "credit_ledger",
        "credit_model_usage",
        "custom_subagents",
        "datarc_connection",
        "entitlement_clock",
        "feedback_candidate_suppressions",
        "feedback_candidates",
        "feedback_casework",
        "feishu_app_configs",
        "feishu_binding_sessions",
        "feishu_message_dedup",
        "feishu_setup_sessions",
        "feishu_turn_links",
        "feishu_user_bindings",
        "feishu_workspace_bindings",
        "group_memberships",
        "groups",
        "home_proposal_cache",
        "identity_provider_realms",
        "installation_identity",
        "invite_intents",
        "invite_tokens",
        "llm_usage_daily",
        "local_oidc_config_revisions",
        "local_oidc_configs",
        "mcp_oauth_pending",
        "memory_applied_deletion_receipts",
        "memory_candidate_suppressions",
        "memory_candidates",
        "memory_destructive_operation_items",
        "memory_destructive_operations",
        "memory_entries",
        "memory_gc_state",
        "memory_migration_state",
        "memory_mutation_idempotency",
        "memory_note_anchors",
        "memory_notes",
        "memory_pack_snapshots",
        "memory_privacy_state",
        "memory_reset_snapshot_items",
        "memory_reset_snapshots",
        "memory_resource_deletion_tombstones",
        "memory_turn_requests",
        "oauth_state",
        "oidc_audit_events",
        "oidc_auth_sessions",
        "oidc_callback_routes",
        "oidc_client_registration_revisions",
        "oidc_client_registrations",
        "oidc_client_secret_versions",
        "oidc_identities",
        "oidc_jwks_snapshots",
        "oidc_login_transactions",
        "oidc_metadata_snapshots",
        "oidc_public_entry_bindings",
        "personal_mcp_binding",
        "personal_mcp_connection",
        "personal_mcp_invocation",
        "personal_mcp_manifest",
        "personal_mcp_session_tool_allowance",
        "personal_mcp_tool",
        "personal_slack_oauth_states",
        "public_entries",
        "public_entry_audit",
        "public_entry_challenge_attempts",
        "rate_limit_buckets",
        "ref_audit_log",
        "refresh_tokens",
        "registered_facts",
        "registered_facts_history",
        "resource_change_receipt",
        "resource_dependency_edge",
        "resource_dependency_subject_projection",
        "resource_file_cleanup_receipt",
        "resource_grants",
        "resource_refresh_candidate",
        "security_key_epochs",
        "skill_instances",
        "slack_connections",
        "slack_event_dedup",
        "slack_identity_bindings",
        "slack_oauth_states",
        "slack_workspace_bindings",
        "smtp_outbox",
        "space_instruction_versions",
        "system_agent_effective_revision",
        "system_agent_revision_behavior",
        "team_brand_asset_metadata",
        "team_branding_draft_copies",
        "team_branding_drafts",
        "team_branding_revisions",
        "team_branding_state",
        "team_builtin_subagent_override",
        "team_builtin_tool",
        "team_capability",
        "team_capability_resource",
        "team_capability_revision",
        "team_chart_style_drafts",
        "team_chart_style_registry_state",
        "team_chart_style_revisions",
        "team_chart_styles",
        "team_identity_provider_binding_revisions",
        "team_identity_provider_bindings",
        "team_invite_tokens",
        "team_invite_workspace_grants",
        "team_license",
        "team_llm_apply_snapshots",
        "team_llm_capability_evidence",
        "team_llm_provider_identities",
        "team_llm_risk_acceptance_retirements",
        "team_llm_risk_acceptances",
        "team_llm_v2_enrollments",
        "team_llm_v2_migration_baseline_invalidations",
        "team_llm_v2_migration_baselines",
        "team_llm_verification_batches",
        "team_llm_verification_items",
        "team_llm_verification_plans",
        "team_members",
        "team_openapi_call_logs",
        "team_openapi_keys",
        "team_reference_retention_policies",
        "team_settings",
        "team_skill",
        "team_subagent",
        "team_subagent_app_compatibility",
        "team_subagent_authoring_context",
        "team_subagent_capability_binding",
        "team_subagent_draft",
        "team_subagent_draft_request",
        "team_subagent_draft_suggestion",
        "team_subagent_draft_usage",
        "team_subagent_fork_lineage",
        "team_subagent_fork_policy",
        "team_subagent_notification_outbox",
        "team_subagent_release",
        "team_subagent_release_candidate",
        "team_subagent_release_event",
        "team_subagent_revision",
        "team_subagent_revision_behavior",
        "team_subagent_revision_extensions",
        "team_subagent_schema_meta",
        "team_subagent_working_copy",
        "terminal_authority_scope_epochs",
        "team_tool_call_log",
        "team_tool_source",
        "teams",
        "teams_activity_dedup",
        "teams_connections",
        "teams_identity_bindings",
        "teams_workspace_bindings",
        "user_audit_log",
        "user_federated_identities",
        "user_settings",
        "user_store_migrations",
        "users",
        "workspace_members",
        "workspace_settings",
        "workspaces",
    ),
    "files": (
        "account_retention_holds",
        "kb_file_generations",
        "kb_files",
        "kb_maintenance_state",
        "kb_membership_operations",
        "kb_rebuild_jobs",
        "kb_source_categories",
        "kb_source_category_files",
        "kb_source_description_jobs",
        "kb_source_descriptions",
        "kb_source_navigation",
    ),
    "doc_types": (
        "kb_doc_types",
        "kb_lifespan_markers",
    ),
    "extracted_records": ("kb_extracted_records",),
    "extracted_rows": ("kb_extracted_rows",),
    "per_row_jobs": (
        "kb_per_row_jobs",
        "kb_workspace_quotas",
    ),
    "team_templates": ("kb_team_templates",),
    "matrix": (
        "kb_matrices",
        "kb_matrix_cells",
        "kb_matrix_runs",
    ),
    "execution": (
        "agent_collaboration_operation",
        "agent_collaboration_participant",
        "agent_collaboration_turn",
        "approvals",
        "chat_attachment_bundle_inventory",
        "chat_attachment_bundle_members",
        "chat_auth_session_generations",
        "chat_auth_user_generation_fences",
        "chat_reference_bi_continuations",
        "chat_reference_content_snapshots",
        "chat_reference_evidence",
        "chat_reference_extension_checks",
        "chat_reference_lead_postdispatch_attempts",
        "chat_reference_mcp_calls",
        "chat_reference_model_contexts",
        "chat_reference_model_invocations",
        "chat_reference_plans",
        "chat_reference_primary_continuations",
        "chat_reference_queued_inputs",
        "chat_reference_resolutions",
        "chat_submission_abandon_tombstones",
        "chat_submission_fingerprint_epochs",
        "chat_submission_inflight",
        "chat_submission_receipts",
        "chat_turn_spawn_dispatch",
        "chat_turn_user_sources",
        "clarification_document_continuations",
        "clarification_invalidation_outbox",
        "clarification_requests",
        "clarification_resolution_outbox",
        "composer_telemetry_dedupe",
        "continuation_payload_segments",
        "document_artifact_refs",
        "document_canvas_inputs",
        "document_command_receipts",
        "document_confirmations",
        "document_continuations",
        "document_dispatches",
        "document_draft_files",
        "document_executions",
        "document_input_copies",
        "document_input_sets",
        "document_job_events",
        "document_job_links",
        "document_jobs",
        "document_model_calls",
        "document_physical_receipt_links",
        "document_planning_attempts",
        "document_revision_inputs",
        "document_revisions",
        "document_turn_dispatches",
        "document_usage_receipts",
        "llm_usage_events",
        "model_context_conversation_heads",
        "model_context_generations",
        "model_context_heads",
        "model_context_segments",
        "plan_steps",
        "plans",
        "scheduled_job_agent_binding",
        "scheduled_job_runs",
        "scheduled_jobs",
        "session_attachments",
        "session_lifecycle_operations",
        "session_lifecycle_targets",
        "sessions",
        "subagent_resume_checkpoints",
        "subagent_resume_requests",
        "terminal_auth_scope_epochs",
        "terminal_protocol_deployment_attestations",
        "terminal_protocol_writer_lease",
        "turn_agent_binding",
        "turn_agent_execution",
        "turn_attachments",
        "turn_cancel_receipts",
        "turn_capability_execution",
        "turn_citations",
        "turn_control_decisions",
        "turn_decision_effects",
        "turn_delivery_intents",
        "turn_effect_attempts",
        "turn_effect_evidence_links",
        "turn_effect_retention_member_keys",
        "turn_effect_retention_tombstones",
        "turn_effect_settlements",
        "turn_event_proposals",
        "turn_events",
        "turn_generations",
        "turn_kb_source_checks",
        "turn_operation_usage",
        "turn_recovery_records",
        "turn_space_instructions",
        "turn_summaries",
        "turn_terminal_candidate_events",
        "turn_terminal_candidate_variants",
        "turn_terminal_candidates",
        "turn_terminal_deletion_ledgers",
        "turn_terminal_receipts",
        "turn_terminal_session_mutations",
        "turn_trace_steps",
        "turns",
        "user_session_archives",
        "user_session_pins",
        "user_session_views",
    ),
    "content": (
        "artifact_purge_claims",
        "artifact_upload_bindings",
        "artifact_upload_confirmations",
        "artifact_workspace_schema_meta",
        "artifacts",
        "canvas_export_job_events",
        "canvas_export_jobs",
        "canvas_export_snapshot_assets",
        "canvas_export_snapshots",
        "canvas_fts",
        "canvas_fts_config",
        "canvas_fts_content",
        "canvas_fts_data",
        "canvas_fts_docsize",
        "canvas_fts_idx",
        "canvas_revisions",
        "canvas_sections",
        "canvases",
        "design_flatten_exports",
    ),
    "notifications": (
        "notification_items",
        "notification_recipients",
    ),
    "observability": (
        "llm_request_changes",
        "llm_request_chunks",
        "llm_request_records",
        "metrics_event_log",
    ),
}

TABLE_DOMAINS = {table: domain for domain, tables in DOMAIN_TABLES.items() for table in tables}


def resolve_domain_database(core_path: str | Path, domain: str) -> Path:
    """Resolve an explicit domain beside the configured core database."""
    if domain not in DOMAIN_TARGET_FILES:
        raise ValueError("unknown SQLite database domain")
    path = Path(core_path)
    return path if domain == "core" else path.parent / DOMAIN_TARGET_FILES[domain]


@lru_cache(maxsize=32)
def _record_admits_domains(directory: str, identity: tuple[int, int, int, int]) -> bool:
    """Cache only a validated record; atomic replacement changes its identity."""
    from bowhead_core.sqlite_upgrade import _load_state

    state = _load_state(Path(directory))
    if state is None:
        raise RuntimeError("SQLite layout record disappeared during connection admission.")
    if state["layout"] == LEGACY_LAYOUT:
        return False
    if state["layout"] != DOMAIN_LAYOUT or state["status"] != "ready":
        raise RuntimeError("SQLite domain migration is incomplete; finish startup migration before opening business connections.")
    return True


def domain_layout_active(path: str | Path) -> bool:
    """Admit only registered files from a completely migrated domain layout."""
    path = Path(path)
    if path.name not in DOMAIN_TARGET_FILES.values():
        return False
    manifest = path.parent / "sqlite-layout.json"
    try:
        stat = manifest.stat()
    except FileNotFoundError:
        if any((path.parent / filename).exists() for name, filename in DOMAIN_TARGET_FILES.items() if name not in ORIGINAL_TARGET_FILES):
            raise RuntimeError(
                "SQLite domain files have no matching layout record; recover the migration record before opening business connections."
            )
        return False
    try:
        return _record_admits_domains(str(path.parent.resolve()), (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError) as exc:
        raise RuntimeError("SQLite layout record is unreadable; stop startup and inspect the migration record.") from exc


def domain_database_path(path: str | Path, domain: str) -> str:
    """Keep legacy constructor paths while resolving admitted domain siblings."""
    if domain not in DOMAIN_TARGET_FILES:
        raise ValueError("unknown SQLite database domain")
    path = Path(path)
    if not domain_layout_active(path):
        return str(path)
    return str(path.parent / DOMAIN_TARGET_FILES[domain])


async def require_domain_schema(db: aiosqlite.Connection, domain: str) -> None:
    """Check migrated tables without repeating legacy initializer DDL."""
    if domain not in DOMAIN_TABLES:
        raise ValueError("unknown SQLite database domain")
    async with db.execute("SELECT name FROM main.sqlite_schema WHERE type='table'") as cursor:
        names = {row[0] for row in await cursor.fetchall()}
    missing = set(DOMAIN_TABLES[domain]) - names
    extra = names - set(DOMAIN_TABLES[domain]) - INFRASTRUCTURE_TABLES
    extra = {name for name in extra if not name.startswith("sqlite_")}
    await verify_domain_schema_digest(db, domain)
    if missing or extra:
        raise RuntimeError(
            f"SQLite {domain} schema differs from the admitted layout (missing={sorted(missing)}, unexpected={sorted(extra)}); stop startup and inspect migration verification."
        )


async def verify_domain_schema_digest(db: aiosqlite.Connection, domain: str) -> None:
    """Require the startup coordinator's exact reviewed persistent schema."""
    async with db.execute("PRAGMA database_list") as cursor:
        databases = await cursor.fetchall()
    path = next((Path(row[2]) for row in databases if row[1] == "main" and row[2]), None)
    if path is None:
        raise RuntimeError("SQLite domain connection has no persisted main database.")
    state = json.loads((path.parent / "sqlite-layout.json").read_text(encoding="utf-8"))
    expected = state.get("targets", {}).get(domain, {}).get("schema_digest")
    async with db.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
    ) as cursor:
        rows = [list(row) for row in await cursor.fetchall()]
    from bowhead_core.sqlite_schema_identity import static_schema_digest

    actual = static_schema_digest(rows)
    if not isinstance(expected, str) or actual != expected:
        raise RuntimeError(f"SQLite {domain} persistent schema verification failed; stop startup and inspect the migration record.")
