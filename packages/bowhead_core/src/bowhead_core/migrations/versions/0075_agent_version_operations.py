"""Durable Agent proposals, dependency policy, automation grants, and webhooks.

Revision ID: 0075_agent_version_operations
Revises: 0074_teams_personal_connection
"""

from __future__ import annotations

from alembic import context

revision = "0075_agent_version_operations"
down_revision = "0074_teams_personal_connection"
branch_labels = None
depends_on = None


_TABLES = (
    """
    CREATE TABLE agent_change_proposal (
      id TEXT PRIMARY KEY CHECK(id LIKE 'acp_%'),
      team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id TEXT NOT NULL,
      source_type TEXT NOT NULL CHECK(source_type IN ('coach','feedback','regression','dependency','upstream','git')),
      source_ref TEXT NOT NULL CHECK(length(source_ref) BETWEEN 1 AND 240),
      base_release_id TEXT,
      base_working_copy_id TEXT,
      base_working_copy_version INTEGER,
      base_document_json TEXT NOT NULL CHECK(json_valid(base_document_json) AND json_type(base_document_json)='object'),
      base_document_hash TEXT NOT NULL CHECK(base_document_hash LIKE 'sha256:%'),
      patch_json TEXT NOT NULL CHECK(json_valid(patch_json) AND json_type(patch_json)='array' AND length(patch_json)<=524288),
      patch_hash TEXT NOT NULL CHECK(patch_hash LIKE 'sha256:%'),
      atomic_groups_json TEXT NOT NULL CHECK(json_valid(atomic_groups_json) AND json_type(atomic_groups_json)='array' AND length(atomic_groups_json)<=262144),
      suggested_cases_json TEXT NOT NULL CHECK(json_valid(suggested_cases_json) AND json_type(suggested_cases_json)='array' AND length(suggested_cases_json)<=262144),
      rationale TEXT NOT NULL CHECK(length(rationale)<=4000),
      provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json) AND json_type(provenance_json)='object' AND length(provenance_json)<=262144),
      status TEXT NOT NULL CHECK(status IN ('open','accepted','dismissed','stale','superseded')),
      row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
      created_by_kind TEXT NOT NULL CHECK(created_by_kind IN ('user','automation','system')),
      created_by_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      decided_at TEXT,
      UNIQUE(team_id, agent_id, source_type, source_ref, patch_hash),
      UNIQUE(team_id, agent_id, id),
      FOREIGN KEY(team_id,agent_id) REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id,agent_id,base_release_id) REFERENCES team_subagent_release(team_id,subagent_id,id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_change_proposal_decision (
      id TEXT PRIMARY KEY CHECK(id LIKE 'acpd_%'),
      proposal_id TEXT NOT NULL REFERENCES agent_change_proposal(id) ON DELETE RESTRICT,
      decision_kind TEXT NOT NULL CHECK(decision_kind IN ('patch_accept','patch_dismiss','cases_accept','cases_dismiss')),
      selected_group_ids_json TEXT NOT NULL CHECK(json_valid(selected_group_ids_json) AND json_type(selected_group_ids_json)='array'),
      selected_case_ids_json TEXT NOT NULL CHECK(json_valid(selected_case_ids_json) AND json_type(selected_case_ids_json)='array'),
      decision_reason_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(decision_reason_json) AND json_type(decision_reason_json)='object'),
      result_working_copy_id TEXT,
      result_working_copy_version INTEGER,
      result_working_copy_hash TEXT CHECK(result_working_copy_hash IS NULL OR result_working_copy_hash LIKE 'sha256:%'),
      result_suite_revision_id TEXT,
      actor_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at TEXT NOT NULL,
      CHECK((result_working_copy_id IS NULL)=(result_working_copy_version IS NULL)),
      CHECK((result_working_copy_id IS NULL)=(result_working_copy_hash IS NULL))
    )
    """,
    """
    CREATE TABLE agent_dependency_update_policy (
      team_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('skill','tool','model','fork')),
      dependency_ref TEXT NOT NULL CHECK(length(dependency_ref) BETWEEN 1 AND 240),
      policy TEXT NOT NULL CHECK(policy IN ('pinned','compatible','notify_only')),
      row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
      updated_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(team_id,agent_id,dependency_kind,dependency_ref),
      FOREIGN KEY(team_id,agent_id) REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_version_automation_grant (
      id TEXT PRIMARY KEY CHECK(id LIKE 'avag_%'),
      team_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      principal_id TEXT NOT NULL,
      scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json) AND json_type(scopes_json)='array'),
      repository_rules_json TEXT NOT NULL CHECK(json_valid(repository_rules_json) AND json_type(repository_rules_json)='object'),
      repository_rules_hash TEXT NOT NULL CHECK(repository_rules_hash LIKE 'sha256:%'),
      expires_at TEXT NOT NULL,
      revoked_at TEXT,
      last_used_at TEXT,
      row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(team_id,agent_id,id),
      FOREIGN KEY(team_id,agent_id) REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_version_webhook_subscription (
      id TEXT PRIMARY KEY CHECK(id LIKE 'avws_%'),
      grant_id TEXT NOT NULL REFERENCES agent_version_automation_grant(id) ON DELETE RESTRICT,
      endpoint_ciphertext TEXT NOT NULL,
      endpoint_fingerprint TEXT NOT NULL CHECK(endpoint_fingerprint LIKE 'sha256:%'),
      secret_ciphertext TEXT NOT NULL,
      event_types_json TEXT NOT NULL CHECK(json_valid(event_types_json) AND json_type(event_types_json)='array'),
      status TEXT NOT NULL CHECK(status IN ('active','disabled')),
      row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE agent_version_webhook_event (
      id TEXT PRIMARY KEY CHECK(id LIKE 'avwe_%'),
      team_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      event_type TEXT NOT NULL CHECK(event_type IN (
        'proposal.created','candidate.prepared','comparison.completed','candidate.ready',
        'candidate.blocked','candidate.unknown','candidate.stale','review.submitted',
        'review.rejected','release.activated','release.regression'
      )),
      object_id TEXT NOT NULL,
      safe_payload_json TEXT NOT NULL CHECK(json_valid(safe_payload_json) AND json_type(safe_payload_json)='object' AND length(safe_payload_json)<=65536),
      payload_hash TEXT NOT NULL CHECK(payload_hash LIKE 'sha256:%'),
      created_at TEXT NOT NULL,
      UNIQUE(team_id,agent_id,event_type,object_id),
      FOREIGN KEY(team_id,agent_id) REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_version_webhook_delivery (
      id TEXT PRIMARY KEY CHECK(id LIKE 'avwd_%'),
      subscription_id TEXT NOT NULL REFERENCES agent_version_webhook_subscription(id) ON DELETE RESTRICT,
      event_id TEXT NOT NULL REFERENCES agent_version_webhook_event(id) ON DELETE RESTRICT,
      attempt INTEGER NOT NULL CHECK(attempt>=1),
      payload_hash TEXT NOT NULL CHECK(payload_hash LIKE 'sha256:%'),
      status TEXT NOT NULL CHECK(status IN ('pending','leased','delivered','retry','failed')),
      response_code INTEGER,
      lease_owner TEXT,
      lease_expires_at TEXT,
      next_attempt_at TEXT NOT NULL,
      created_at TEXT NOT NULL,
      settled_at TEXT,
      UNIQUE(subscription_id,event_id)
    )
    """,
)

_INDEXES = (
    "CREATE INDEX idx_agent_change_proposal_open ON agent_change_proposal(team_id,agent_id,status,created_at DESC)",
    "CREATE UNIQUE INDEX idx_agent_change_proposal_git_idempotency ON agent_change_proposal(team_id,agent_id,source_ref) WHERE source_type='git'",
    "CREATE INDEX idx_agent_change_proposal_decisions ON agent_change_proposal_decision(proposal_id,created_at)",
    "CREATE INDEX idx_agent_automation_grants ON agent_version_automation_grant(team_id,agent_id,revoked_at,expires_at)",
    "CREATE INDEX idx_agent_webhook_events ON agent_version_webhook_event(team_id,agent_id,created_at)",
    "CREATE INDEX idx_agent_webhook_delivery_claim ON agent_version_webhook_delivery(status,next_attempt_at,lease_expires_at)",
)

_TRIGGERS = (
    """
    CREATE TRIGGER trg_agent_change_proposal_payload_immutable
    BEFORE UPDATE ON agent_change_proposal
    WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.agent_id IS NOT OLD.agent_id
      OR NEW.source_type IS NOT OLD.source_type OR NEW.source_ref IS NOT OLD.source_ref
      OR NEW.base_release_id IS NOT OLD.base_release_id OR NEW.base_working_copy_id IS NOT OLD.base_working_copy_id
      OR NEW.base_working_copy_version IS NOT OLD.base_working_copy_version
      OR NEW.base_document_json IS NOT OLD.base_document_json OR NEW.base_document_hash IS NOT OLD.base_document_hash
      OR NEW.patch_json IS NOT OLD.patch_json OR NEW.patch_hash IS NOT OLD.patch_hash
      OR NEW.atomic_groups_json IS NOT OLD.atomic_groups_json OR NEW.suggested_cases_json IS NOT OLD.suggested_cases_json
      OR NEW.rationale IS NOT OLD.rationale OR NEW.provenance_json IS NOT OLD.provenance_json
      OR NEW.created_by_kind IS NOT OLD.created_by_kind OR NEW.created_by_id IS NOT OLD.created_by_id
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT,'agent_change_proposal payload is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_change_proposal_transition
    BEFORE UPDATE ON agent_change_proposal
    WHEN NEW.status IS NOT OLD.status OR NEW.row_version IS NOT OLD.row_version
    BEGIN
      SELECT CASE WHEN NOT (
        OLD.status='open' AND NEW.status IN ('accepted','dismissed','stale','superseded')
        AND NEW.row_version=OLD.row_version+1 AND NEW.decided_at IS NOT NULL
      ) THEN RAISE(ABORT,'agent_change_proposal transition mismatch') END;
    END
    """,
    """
    CREATE TRIGGER trg_agent_change_proposal_immutable_delete
    BEFORE DELETE ON agent_change_proposal
    BEGIN SELECT RAISE(ABORT,'agent_change_proposal is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_change_proposal_decision_immutable_update
    BEFORE UPDATE ON agent_change_proposal_decision
    BEGIN SELECT RAISE(ABORT,'agent_change_proposal_decision is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_change_proposal_decision_immutable_delete
    BEFORE DELETE ON agent_change_proposal_decision
    BEGIN SELECT RAISE(ABORT,'agent_change_proposal_decision is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_automation_grant_binding_immutable
    BEFORE UPDATE ON agent_version_automation_grant
    WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.agent_id IS NOT OLD.agent_id
      OR NEW.owner_user_id IS NOT OLD.owner_user_id OR NEW.principal_id IS NOT OLD.principal_id
      OR NEW.scopes_json IS NOT OLD.scopes_json OR NEW.repository_rules_json IS NOT OLD.repository_rules_json
      OR NEW.repository_rules_hash IS NOT OLD.repository_rules_hash OR NEW.expires_at IS NOT OLD.expires_at
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT,'agent_version_automation_grant binding is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_automation_grant_cas
    BEFORE UPDATE ON agent_version_automation_grant
    WHEN NEW.row_version!=OLD.row_version+1
    BEGIN SELECT RAISE(ABORT,'agent_version_automation_grant CAS mismatch'); END
    """,
    """
    CREATE TRIGGER trg_agent_automation_grant_immutable_delete
    BEFORE DELETE ON agent_version_automation_grant
    BEGIN SELECT RAISE(ABORT,'agent_version_automation_grant is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_subscription_binding_immutable
    BEFORE UPDATE ON agent_version_webhook_subscription
    WHEN NEW.id IS NOT OLD.id OR NEW.grant_id IS NOT OLD.grant_id OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_subscription binding is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_subscription_cas
    BEFORE UPDATE ON agent_version_webhook_subscription
    WHEN NEW.row_version!=OLD.row_version+1
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_subscription CAS mismatch'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_subscription_immutable_delete
    BEFORE DELETE ON agent_version_webhook_subscription
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_subscription is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_delivery_payload_immutable
    BEFORE UPDATE ON agent_version_webhook_delivery
    WHEN NEW.id IS NOT OLD.id OR NEW.subscription_id IS NOT OLD.subscription_id OR NEW.event_id IS NOT OLD.event_id
      OR NEW.payload_hash IS NOT OLD.payload_hash OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_delivery payload is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_event_immutable_update
    BEFORE UPDATE ON agent_version_webhook_event
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_event is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_event_immutable_delete
    BEFORE DELETE ON agent_version_webhook_event
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_event is immutable'); END
    """,
    """
    CREATE TRIGGER trg_agent_webhook_delivery_immutable_delete
    BEFORE DELETE ON agent_version_webhook_delivery
    BEGIN SELECT RAISE(ABORT,'agent_version_webhook_delivery is immutable'); END
    """,
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for statement in _TABLES:
        connection.exec_driver_sql(statement)
    for statement in (*_INDEXES, *_TRIGGERS):
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    return
