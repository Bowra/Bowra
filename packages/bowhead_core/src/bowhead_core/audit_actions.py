"""Sprint β G8 — admin_audit_log action enum + D-3=B tiered atomicity classification.

Per PRD G8 §3.3 + v9.2 design F-5 (canonical snake_case).

D-3=B tiered model:
- CRITICAL: must succeed before main op commits (atomic via audit_critical_action helper)
- OPS: best-effort write (log warn on fail, don't block main op)

A future audit row that doesn't match either tier is implicit OPS.
"""

from __future__ import annotations

from typing import Final


class AuditAction:
    """Canonical action enum. Use these constants — never string literals at call sites."""

    # ─── Critical actions (D-3=B tier 1 — atomic with main op) ───
    ROLE_CHANGE: Final = "role_change"  # G5 promote/demote
    LLM_CONFIG_ROTATE: Final = "llm_config_rotate"  # G7 key rotation
    LLM_PROVIDER_ADD: Final = "llm_provider_add"
    LLM_PROVIDER_DELETE: Final = "llm_provider_delete"
    LLM_V2_ENROLLED: Final = "llm_v2_enrolled"
    LLM_CONFIG_APPLY: Final = "llm_config_apply"
    LLM_MIGRATION_BASELINE_INVALIDATED: Final = "llm_migration_baseline_invalidated"
    PASSWORD_CHANGE: Final = "password_change"  # G10
    TEAM_SETTINGS_CHANGED: Final = "team_settings_changed"
    CREDIT_QUOTA_CHANGE: Final = "credit_quota_change"  # G7 quota adjust
    MARKETPLACE_FORCE_UNPUBLISH: Final = "marketplace_force_unpublish"  # γ
    TEAM_MEMBER_DEACTIVATED: Final = "team_member_deactivated"  # licensing §4.3 — admin deactivates member, frees a seat
    # licensing §7.1 — admin setters mutating the trusted entitlement fields (saas only; local = managed_by_license)
    TEAM_STATUS_CHANGED: Final = "team_status_changed"
    TEAM_CONTRACT_CHANGED: Final = "team_contract_changed"
    TEAM_SEATS_CHANGED: Final = "team_seats_changed"
    TEAM_TIER_CREDITS_CHANGED: Final = "team_tier_credits_changed"
    LICENSE_UPLOADED: Final = "license_uploaded"  # licensing §6.5 — customer-side on-prem license activation
    TEAM_BRANDING_PUBLISHED: Final = "team_branding_published"
    TEAM_BRANDING_ROLLED_BACK: Final = "team_branding_rolled_back"
    TEAM_BRANDING_RESTORED: Final = "team_branding_restored"
    TEAM_BRANDING_DRAFT_PRESERVED: Final = "team_branding_draft_preserved"
    TEAM_BRANDING_DRAFT_COPY_CREATED_AS_DRAFT: Final = "team_branding_draft_copy_created_as_draft"
    TEAM_BRANDING_DRAFT_COPY_DELETED: Final = "team_branding_draft_copy_deleted"
    TEAM_SUBAGENT_QUARANTINED: Final = "team_subagent_quarantined"
    AGENT_RELEASE_CANDIDATE_PREPARED: Final = "agent_release_candidate_prepared"
    AGENT_RELEASE_CANDIDATE_SUBMITTED: Final = "agent_release_candidate_submitted"
    AGENT_RELEASE_CANDIDATE_REJECTED: Final = "agent_release_candidate_rejected"
    AGENT_RELEASE_CANDIDATE_WITHDRAWN: Final = "agent_release_candidate_withdrawn"
    AGENT_RELEASE_CANDIDATE_STALE: Final = "agent_release_candidate_stale"
    AGENT_EVAL_COMPARISON_STARTED: Final = "agent_eval_comparison_started"
    AGENT_EVAL_COMPARISON_CANCELLED: Final = "agent_eval_comparison_cancelled"
    AGENT_EVAL_COMPARISON_FINALIZED: Final = "agent_eval_comparison_finalized"
    AGENT_EVAL_OVERRIDE_CREATED: Final = "agent_eval_override_created"
    AGENT_RELEASE_CANDIDATE_VIEWED: Final = "agent_release_candidate_viewed"
    AGENT_RELEASE_ACTIVATED: Final = "agent_release_activated"
    AGENT_AUDIENCE_CONTRACTED: Final = "agent_audience_contracted"
    AGENT_RELEASE_NOTIFICATION_REPLAYED: Final = "agent_release_notification_replayed"
    AGENT_SAFETY_CHECK_RUN: Final = "agent_safety_check_run"
    AGENT_ARCHIVED: Final = "agent_archived"
    AGENT_RESTORED: Final = "agent_restored"
    AGENT_FORKED: Final = "agent_forked"
    AGENT_FORK_POLICY_CHANGED: Final = "agent_fork_policy_changed"
    AGENT_AUTOMATION_GRANT_REPLACED: Final = "agent_automation_grant_replaced"
    TERMINAL_EFFECT_MANUAL_SETTLED: Final = "terminal_effect_manual_settled"
    TERMINAL_EFFECT_PROVIDER_PROBED: Final = "terminal_effect_provider_probed"

    # ─── Ops actions (D-3=B tier 2 — best-effort) ───
    # An operator decrypted a diagnostic LLM request record on the host (`bowhead llm-request show`).
    LLM_REQUEST_RECORD_VIEWED: Final = "llm_request_record_viewed"
    MEMBER_INVITE: Final = "member_invite"  # G1
    MEMBER_REMOVED: Final = "member_removed"
    # Config copilot batch 0.5 (tech-design-batch05-chat.md §8): chat-initiated writes to workspace settings
    # (audit only origin="chat"; page paths retain no auditing). OPS tier; follows TEAM_SETTINGS_CHANGED naming.
    WORKSPACE_SETTINGS_CHANGED: Final = "workspace_settings_changed"
    INVITE_EXPIRED: Final = "invite_expired"
    INVITE_RESEND: Final = "invite_resend"
    INVITE_REVOKED: Final = "invite_revoked"
    WORKSPACE_CREATED: Final = "workspace_created"
    SESSION_REVOKED: Final = "session_revoked"  # G10
    ENDPOINT_403_DENIAL: Final = "endpoint_403_denial"  # α
    PASSWORD_CHANGE_FAILED: Final = "password_change_failed"
    LLM_CONFIG_ROTATE_ATTEMPT: Final = "llm_config_rotate_attempt"  # G7 P0-S-2
    # WS4 (models-settings) — test-connection probe of an team-supplied provider endpoint.
    # OPS tier (read-only probe, no mutation) but security-relevant: an team_admin can
    # aim the SSRF-guarded client at arbitrary base_urls, so the attempt leaves a trace.
    LLM_PROVIDER_VERIFY: Final = "llm_provider_verify"
    LLM_VERIFICATION_BATCH_STARTED: Final = "llm_verification_batch_started"
    LLM_VERIFICATION_BATCH_CANCELLED: Final = "llm_verification_batch_cancelled"
    LLM_VERIFICATION_BATCH_COMPLETED: Final = "llm_verification_batch_completed"
    # cca P2 — team tool source authoring (an team-wide tool that can call external APIs with stored
    # creds). OPS tier for P2.1 (best-effort audit written from the route); elevate to CRITICAL/
    # retained-forever (atomic) in P2.2 if the governance bar requires it.
    TEAM_TOOL_SOURCE_CREATED: Final = "team_tool_source_created"
    TEAM_TOOL_SOURCE_UPDATED: Final = "team_tool_source_updated"  # tools sprint WP1b — patch (rename/enable-disable/re-select/rotate-auth)
    TEAM_TOOL_SOURCE_DELETED: Final = "team_tool_source_deleted"  # tools sprint WP1b
    # cca P3 — team subagent authoring (a routable expert assembled from a SUBSET of team
    # tools). OPS tier, best-effort audit from the route. NO secret (subagent holds none).
    TEAM_SUBAGENT_CREATED: Final = "team_subagent_created"
    TEAM_SUBAGENT_UPDATED: Final = "team_subagent_updated"
    SUBAGENT_AUDIENCE_CHANGED: Final = "subagent_audience_changed"
    TEAM_SUBAGENT_DELETED: Final = "team_subagent_deleted"
    # Built-in subagent management: team admin overrides system subagents (disable / routing description / model / skills /
    # additional instructions / iteration cap). Base remains the code manifest; this action records delta changes.
    # OPS tier, in the override-write transaction (governance must leave an audit trail without blocking the Turn).
    BUILTIN_SUBAGENT_OVERRIDE_UPDATED: Final = "builtin_subagent_override_updated"
    BUILTIN_SUBAGENT_OVERRIDE_CLEARED: Final = "builtin_subagent_override_cleared"
    AGENT_HIRED: Final = "agent_hired"
    AGENT_PERSONAL_STATUS_CHANGED: Final = "agent_personal_status_changed"
    AGENT_UNHIRED: Final = "agent_unhired"
    AGENT_ASSIGNMENT_BACKFILLED: Final = "agent_assignment_backfilled"
    AGENT_TEAM_DEFAULT_SET: Final = "agent_team_default_set"
    AGENT_TEAM_DEFAULT_REMOVED: Final = "agent_team_default_removed"
    AGENT_LOCALIZATION_DRAFT_SAVED: Final = "agent_localization_draft_saved"
    AGENT_LOCALIZATION_CONFIRMED: Final = "agent_localization_confirmed"
    AGENT_TAXONOMY_SUGGESTED: Final = "agent_taxonomy_suggested"
    AGENT_TAXONOMY_CONFIRMED: Final = "agent_taxonomy_confirmed"
    AGENT_TEAM_TAG_CREATED: Final = "agent_team_tag_created"
    AGENT_TEAM_TAG_STATUS_CHANGED: Final = "agent_team_tag_status_changed"
    ACCESS_GROUP_CREATED: Final = "access_group_created"
    ACCESS_GROUP_MEMBER_ADDED: Final = "access_group_member_added"
    ACCESS_GROUP_MEMBER_REMOVED: Final = "access_group_member_removed"
    RESOURCE_GRANT_UPSERTED: Final = "resource_grant_upserted"
    RESOURCE_GRANT_REVOKED: Final = "resource_grant_revoked"
    # content-permissions wave 2 — access-request decisions (deny has no grant row,
    # so it needs its own audit action)
    ACCESS_REQUEST_APPROVED: Final = "access_request_approved"
    ACCESS_REQUEST_DENIED: Final = "access_request_denied"
    # access-request expansion — lifecycle events (real requests only; shadow rows are
    # counted by metrics, never audited, so probing cannot flood the audit log)
    ACCESS_REQUEST_SUBMITTED: Final = "access_request_submitted"
    ACCESS_REQUEST_WITHDRAWN: Final = "access_request_withdrawn"
    ACCESS_REQUEST_EXPIRED: Final = "access_request_expired"
    # email-config sprint — team-level SMTP config (payload never contains password, OPS tier)
    EMAIL_CONFIG_SET: Final = "email_config_set"
    EMAIL_CONFIG_CLEARED: Final = "email_config_cleared"
    EMAIL_TEST_SENT: Final = "email_test_sent"
    RESOURCE_ACCESS_DENIED: Final = "resource_access_denied"
    # One row per list request that filtered out unreadable resources (count, reasons, capped id sample);
    # per-id routes keep RESOURCE_ACCESS_DENIED rows.
    RESOURCE_LIST_ACCESS_FILTERED: Final = "resource_list_access_filtered"
    TEAM_BRAND_ASSET_UPLOADED: Final = "team_brand_asset_uploaded"
    TEAM_BRAND_ASSET_DELETED: Final = "team_brand_asset_deleted"
    TEAM_THEME_LIBRARY_CHANGED: Final = "team_theme_library_changed"
    TEAM_CHART_STYLE_CREATED: Final = "team_chart_style_created"
    TEAM_CHART_STYLE_PUBLISHED: Final = "team_chart_style_published"
    TEAM_CHART_STYLE_RESTORED: Final = "team_chart_style_restored"
    TEAM_CHART_STYLE_DEACTIVATED: Final = "team_chart_style_deactivated"
    TEAM_CHART_STYLE_REACTIVATED: Final = "team_chart_style_reactivated"
    TEAM_CHART_STYLE_DRAFT_DELETED: Final = "team_chart_style_draft_deleted"

    # workflow-solidify S4 — team shared recipe authoring (solidified reusable SKILL.md). OPS tier,
    # best-effort audit from the route. NO secret (a recipe holds none).
    TEAM_SKILL_CREATED: Final = "team_skill_created"
    TEAM_SKILL_UPDATED: Final = "team_skill_updated"
    TEAM_SKILL_DELETED: Final = "team_skill_deleted"
    # skill-team-governance P1b: three broadcast-audience approval actions (PRD §3.3; actor=submitter/approver).
    TEAM_SKILL_SUBMITTED: Final = "team_skill_submitted"
    TEAM_SKILL_APPROVED: Final = "team_skill_approved"
    TEAM_SKILL_REJECTED: Final = "team_skill_rejected"

    # ─── Special: NOT in admin_audit_log (moved to user_audit_log per P1-O-3) ───
    # WORKSPACE_SWITCHED — user-scoped event, lives in user_audit_log


CRITICAL_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        AuditAction.ROLE_CHANGE,
        AuditAction.LLM_CONFIG_ROTATE,
        AuditAction.LLM_PROVIDER_ADD,
        AuditAction.LLM_PROVIDER_DELETE,
        AuditAction.LLM_V2_ENROLLED,
        AuditAction.LLM_CONFIG_APPLY,
        AuditAction.LLM_MIGRATION_BASELINE_INVALIDATED,
        AuditAction.PASSWORD_CHANGE,
        AuditAction.TEAM_SETTINGS_CHANGED,
        AuditAction.CREDIT_QUOTA_CHANGE,
        AuditAction.MARKETPLACE_FORCE_UNPUBLISH,
        AuditAction.TEAM_MEMBER_DEACTIVATED,  # security-relevant seat removal — atomic + retained forever
        # §7.1 entitlement-field setters — atomic with the UPDATE (via audit_critical_action) + retained forever
        AuditAction.TEAM_STATUS_CHANGED,
        AuditAction.TEAM_CONTRACT_CHANGED,
        AuditAction.TEAM_SEATS_CHANGED,
        AuditAction.TEAM_TIER_CREDITS_CHANGED,
        AuditAction.LICENSE_UPLOADED,
        AuditAction.TEAM_BRANDING_PUBLISHED,
        AuditAction.TEAM_BRANDING_ROLLED_BACK,
        AuditAction.TEAM_BRANDING_RESTORED,
        AuditAction.TEAM_BRANDING_DRAFT_PRESERVED,
        AuditAction.TEAM_BRANDING_DRAFT_COPY_CREATED_AS_DRAFT,
        AuditAction.TEAM_BRANDING_DRAFT_COPY_DELETED,
        AuditAction.TEAM_SUBAGENT_QUARANTINED,
        AuditAction.AGENT_RELEASE_CANDIDATE_PREPARED,
        AuditAction.AGENT_RELEASE_CANDIDATE_SUBMITTED,
        AuditAction.AGENT_RELEASE_CANDIDATE_REJECTED,
        AuditAction.AGENT_RELEASE_CANDIDATE_WITHDRAWN,
        AuditAction.AGENT_RELEASE_CANDIDATE_STALE,
        AuditAction.AGENT_EVAL_COMPARISON_STARTED,
        AuditAction.AGENT_EVAL_COMPARISON_CANCELLED,
        AuditAction.AGENT_EVAL_COMPARISON_FINALIZED,
        AuditAction.AGENT_EVAL_OVERRIDE_CREATED,
        AuditAction.AGENT_RELEASE_ACTIVATED,
        AuditAction.AGENT_AUDIENCE_CONTRACTED,
        AuditAction.AGENT_FORKED,
        AuditAction.AGENT_FORK_POLICY_CHANGED,
        AuditAction.AGENT_AUTOMATION_GRANT_REPLACED,
        AuditAction.AGENT_RELEASE_NOTIFICATION_REPLAYED,
        AuditAction.TERMINAL_EFFECT_MANUAL_SETTLED,
        AuditAction.TERMINAL_EFFECT_PROVIDER_PROBED,
    }
)

# Retention exception list — keep these forever even after default 90-day prune
RETAINED_FOREVER_ACTIONS: Final[frozenset[str]] = CRITICAL_ACTIONS


def is_critical(action: str) -> bool:
    return action in CRITICAL_ACTIONS
