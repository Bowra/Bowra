"""Persist account deletion deadlines and narrowly scoped erasure authority."""

import re

from alembic import context

revision = "0179_account_deletion"
down_revision = "0178_space_instructions"
branch_labels = None
depends_on = None

OWNED_COLUMNS = {
    "agent_assignment": ["user_id"],
    "agent_coach_generation_request": ["owner_user_id"],
    "agent_default_opt_out": ["user_id"],
    "agent_eval_notification_outbox": ["recipient_user_id"],
    "agent_role_preference": ["user_id"],
    "agent_version_automation_grant": ["owner_user_id"],
    "agent_version_notification_outbox": ["recipient_user_id"],
    "api_tokens": ["user_id"],
    "approvals": ["user_id"],
    "artifact_upload_bindings": ["user_id"],
    "artifact_upload_confirmations": ["user_id"],
    "artifacts": ["created_by"],
    "builtin_tool_oauth_state": ["user_id"],
    "builtin_tool_user_grant": ["user_id"],
    "canvas_export_jobs": ["requested_by"],
    "canvas_export_snapshots": ["requested_by"],
    "canvases": ["user_id"],
    "chat_attachment_bundle_inventory": ["created_by"],
    "chat_auth_session_generations": ["user_id"],
    "chat_auth_user_generation_fences": ["user_id"],
    "chat_reference_queued_inputs": ["user_id"],
    "chat_submission_abandon_tombstones": ["user_id"],
    "chat_submission_inflight": ["user_id"],
    "chat_submission_receipts": ["user_id"],
    "chat_turn_user_sources": ["user_id"],
    "clarification_requests": ["user_id"],
    "coach_ci_delegation": ["owner_user_id"],
    "coach_plugin_operation": ["user_id"],
    "coach_skill_operation": ["user_id"],
    "coach_tool_requirement": ["user_id"],
    "composer_telemetry_dedupe": ["user_id"],
    "config_copilot_draft": ["owner_user_id"],
    "credential_recovery_tokens": ["user_id"],
    "credit_attention_state": ["user_id"],
    "credit_exhaustion_events": ["user_id"],
    "credit_ledger": ["user_id"],
    "document_artifact_refs": ["owner_user_id"],
    "document_command_receipts": ["owner_user_id"],
    "document_confirmations": ["user_id"],
    "document_input_sets": ["owner_user_id"],
    "document_job_links": ["owner_user_id"],
    "document_jobs": ["requested_by"],
    "feishu_binding_sessions": ["bound_user_id"],
    "feishu_user_bindings": ["bowhead_user_id"],
    "feishu_workspace_bindings": ["bowhead_user_id"],
    "home_proposal_cache": ["user_id"],
    "llm_usage_daily": ["user_id"],
    "llm_usage_events": ["user_id"],
    "mcp_oauth_pending": ["created_by"],
    "mcp_servers": ["user_id"],
    "memory_candidate_suppressions": ["user_id"],
    "memory_candidates": ["user_id"],
    "memory_destructive_operations": ["user_id"],
    "memory_mutation_idempotency": ["user_id"],
    "memory_note_anchors": ["user_id"],
    "memory_pack_snapshots": ["user_id"],
    "memory_privacy_state": ["user_id"],
    "memory_reset_snapshots": ["user_id"],
    "memory_resource_deletion_tombstones": ["user_id"],
    "memory_turn_requests": ["user_id"],
    "metrics_event_log": ["user_id"],
    "notification_recipients": ["user_id"],
    "oidc_auth_sessions": ["user_id"],
    "oidc_identities": ["user_id"],
    "personal_mcp_binding": ["owner_user_id"],
    "personal_mcp_connection": ["owner_user_id"],
    "personal_mcp_invocation": ["owner_user_id"],
    "personal_mcp_manifest": ["owner_user_id"],
    "personal_mcp_session_tool_allowance": ["owner_user_id"],
    "personal_slack_oauth_states": ["owner_user_id"],
    "plans": ["user_id"],
    "ref_audit_log": ["user_id"],
    "refresh_tokens": ["user_id"],
    "scheduled_jobs": ["user_id"],
    "sessions": ["user_id"],
    "skill_instances": ["user_id"],
    "slack_identity_bindings": ["bowhead_user_id"],
    "team_subagent_authoring_context": ["owner_user_id"],
    "team_subagent_draft": ["owner_user_id"],
    "team_subagent_draft_request": ["owner_user_id"],
    "team_subagent_draft_suggestion": ["owner_user_id"],
    "team_subagent_draft_usage": ["owner_user_id"],
    "team_subagent_notification_outbox": ["recipient_user_id"],
    "team_tool_call_log": ["user_id"],
    "teams_identity_bindings": ["bowhead_user_id"],
    "turn_agent_binding": ["user_id"],
    "turn_cancel_receipts": ["user_id"],
    "user_audit_log": ["user_id"],
    "user_federated_identities": ["user_id"],
    "user_glossary": ["user_id"],
    "user_session_archives": ["user_id"],
    "user_session_pins": ["user_id"],
    "user_session_views": ["user_id"],
    "user_settings": ["user_id"],
    "workspace_members": ["user_id"],
}


STATEMENTS = (
    """CREATE TABLE account_owned_artifact_writes (
        write_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE account_deletions (
        user_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        requested_at TEXT NOT NULL,
        purge_after TEXT NOT NULL,
        time_source TEXT NOT NULL CHECK(time_source IN ('request','audit','adoption')),
        state TEXT NOT NULL DEFAULT 'retained'
          CHECK(state IN ('retained','quarantined','purging','verifying','completed')),
        lease_owner TEXT,
        lease_epoch INTEGER NOT NULL DEFAULT 0,
        lease_expires_at TEXT,
        next_attempt_at TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_code TEXT,
        quarantined_at TEXT,
        completed_at TEXT,
        CHECK(julianday(purge_after) >= julianday(requested_at) + 30)
    )""",
    """CREATE INDEX idx_account_deletions_due
       ON account_deletions(state, next_attempt_at, purge_after)""",
    """CREATE TABLE account_deletion_rows (
        operation_id TEXT NOT NULL REFERENCES account_deletions(operation_id),
        table_name TEXT NOT NULL,
        row_key TEXT NOT NULL,
        PRIMARY KEY(operation_id,table_name,row_key)
    )""",
    """CREATE TABLE account_deletion_objects (
        operation_id TEXT NOT NULL REFERENCES account_deletions(operation_id),
        storage_kind TEXT NOT NULL,
        object_key TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY(operation_id,storage_kind,object_key)
    )""",
    """CREATE TABLE account_protocol_erasure_receipts (
        identity_digest TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        identity_kind TEXT NOT NULL CHECK(identity_kind IN ('effect','generation')),
        state TEXT NOT NULL,
        outcome_digest TEXT,
        erased_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER trg_account_deletion_identity BEFORE UPDATE ON account_deletions
       WHEN NEW.user_id IS NOT OLD.user_id OR NEW.operation_id IS NOT OLD.operation_id
         OR NEW.requested_at IS NOT OLD.requested_at OR NEW.purge_after IS NOT OLD.purge_after
         OR NEW.time_source IS NOT OLD.time_source
         OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
       BEGIN SELECT RAISE(ABORT,'account deletion identity is immutable'); END""",
    """CREATE TRIGGER trg_account_deletion_no_revive BEFORE UPDATE ON users
       WHEN EXISTS(SELECT 1 FROM account_deletions WHERE user_id=OLD.id)
         AND (NEW.status!='deleted' OR NEW.email NOT LIKE '%@deleted.invalid')
       BEGIN SELECT RAISE(ABORT,'deleted account cannot be revived'); END""",
    """CREATE TRIGGER trg_account_deletion_no_session BEFORE INSERT ON sessions
       WHEN EXISTS(SELECT 1 FROM account_deletions WHERE user_id=NEW.user_id)
       BEGIN SELECT RAISE(ABORT,'account is deleted'); END""",
    """CREATE TRIGGER trg_account_deletion_no_turn BEFORE INSERT ON turns
       WHEN EXISTS(SELECT 1 FROM sessions s JOIN account_deletions d ON d.user_id=s.user_id WHERE s.id=NEW.session_id)
       BEGIN SELECT RAISE(ABORT,'account is deleted'); END""",
    """CREATE TRIGGER trg_account_deletion_file_retention BEFORE INSERT ON artifact_purge_claims
       WHEN EXISTS(SELECT 1 FROM account_deletions d WHERE d.user_id=NEW.created_by AND julianday(d.purge_after)>julianday('now'))
       BEGIN SELECT RAISE(ABORT,'account data retention has not expired'); END""",
)


def upgrade() -> None:
    target = context.config.attributes["bowhead_target"]
    if target == "files":
        db = context.get_bind()
        db.exec_driver_sql("CREATE TABLE account_retention_holds(user_id TEXT PRIMARY KEY,purge_after TEXT NOT NULL)")
        db.exec_driver_sql(
            """CREATE TRIGGER trg_account_retention_kb_delete BEFORE DELETE ON kb_files
            WHEN EXISTS(SELECT 1 FROM account_retention_holds h WHERE h.user_id=OLD.uploaded_by
              AND OLD.scope='personal' AND julianday(h.purge_after)>julianday('now'))
            BEGIN SELECT RAISE(ABORT,'account data retention has not expired'); END"""
        )
        return
    if target != "core":
        return
    db = context.get_bind()
    for statement in STATEMENTS:
        db.exec_driver_sql(statement)
    db.exec_driver_sql(
        """CREATE TRIGGER trg_account_retired_pending_artifact BEFORE INSERT ON account_owned_artifact_writes
        WHEN EXISTS(SELECT 1 FROM account_deletions WHERE user_id=NEW.user_id)
        BEGIN SELECT RAISE(ABORT,'account is deleted'); END"""
    )
    for table, columns in OWNED_COLUMNS.items():
        if table == "user_audit_log" or table.startswith(("memory_", "chat_auth_")):
            continue
        predicate = " OR ".join(f'EXISTS(SELECT 1 FROM account_deletions WHERE user_id=NEW."{column}")' for column in columns)
        db.exec_driver_sql(
            f'CREATE TRIGGER trg_account_retired_{table}_insert BEFORE INSERT ON "{table}" '
            f"WHEN {predicate} BEGIN SELECT RAISE(ABORT,'account is deleted'); END"
        )
    # Ordinary journal immutability remains intact. Only exact rows frozen by
    # a due, purging account operation may bypass an ordinary DELETE guard.
    triggers = db.exec_driver_sql("SELECT name,tbl_name,sql FROM sqlite_schema WHERE type='trigger'").fetchall()
    for name, table, sql in triggers:
        match = re.search(r"\bBEFORE\s+(DELETE|UPDATE)\s+ON\s+[^\s]+\s+(.*?)\bBEGIN\b", sql, re.I | re.S)
        if not match or table.startswith("account_"):
            continue
        if match.group(1).upper() == "UPDATE" and table != "admin_audit_log":
            continue
        columns = db.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not primary:
            continue
        key = "json_array(" + ",".join(f'OLD."{column}"' for column in primary) + ")"
        permitted = (
            "EXISTS(SELECT 1 FROM account_deletion_rows r JOIN account_deletions d "
            f"ON d.operation_id=r.operation_id WHERE r.table_name='{table}' AND r.row_key={key} "
            "AND d.state='purging' AND julianday(d.purge_after)<=julianday('now') "
            "AND d.lease_owner IS NOT NULL AND julianday(d.lease_expires_at)>julianday('now') "
            "AND EXISTS(SELECT 1 FROM users u WHERE u.id=d.user_id AND u.status='deleted'))"
        )
        if match.group(1).upper() == "UPDATE":
            preserved = " AND ".join(
                f'NEW."{row[1]}" IS OLD."{row[1]}"'
                for row in columns
                if row[1] not in {"actor_user_id", "target_id", "payload_json", "ip", "user_agent"}
            )
            permitted = permitted[:-1] + (
                " AND (NEW.actor_user_id IS OLD.actor_user_id OR (OLD.actor_user_id=d.user_id AND NEW.actor_user_id='retired'))"
                " AND NEW.payload_json='{}' AND NEW.ip IS NULL AND NEW.user_agent IS NULL"
                " AND (NEW.target_id IS OLD.target_id OR (OLD.target_kind='user' AND OLD.target_id=d.user_id AND NEW.target_id IS NULL))"
                f" AND {preserved})"
            )
        original = match.group(2).strip()
        condition = f"({original[4:]}) AND NOT {permitted}" if original.upper().startswith("WHEN") else f"NOT {permitted}"
        replacement = sql[: match.start(2)] + f"WHEN {condition} " + sql[match.end(2) :]
        db.exec_driver_sql(f'DROP TRIGGER "{name}"')
        db.exec_driver_sql(replacement)


def downgrade() -> None:
    raise NotImplementedError("Restore matching database snapshots to roll back.")
