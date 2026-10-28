"""Preserve evaluation authority and exact CI operation provenance."""

from alembic import context

revision = "0137_agent_automation_context"
down_revision = "0136_clarification_rounds"
branch_labels = None
depends_on = None


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT agent_automation_context")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_automation_context")
        db.exec_driver_sql("RELEASE SAVEPOINT agent_automation_context")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT agent_automation_context")


def downgrade():
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    "ALTER TABLE agent_version_automation_grant ADD COLUMN evaluation_workspace_id TEXT REFERENCES workspaces(id) ON DELETE RESTRICT",
    "CREATE INDEX idx_agent_automation_grant_workspace ON agent_version_automation_grant(evaluation_workspace_id)",
    """CREATE TRIGGER trg_agent_automation_workspace_insert BEFORE INSERT ON agent_version_automation_grant
       WHEN NEW.evaluation_workspace_id IS NOT NULL AND NOT EXISTS(
         SELECT 1 FROM workspaces WHERE id=NEW.evaluation_workspace_id AND team_id=NEW.team_id)
       BEGIN SELECT RAISE(ABORT,'automation evaluation Space scope mismatch'); END""",
    """CREATE TRIGGER trg_agent_automation_workspace_immutable BEFORE UPDATE ON agent_version_automation_grant
       WHEN NEW.evaluation_workspace_id IS NOT OLD.evaluation_workspace_id
       BEGIN SELECT RAISE(ABORT,'automation evaluation Space is immutable'); END""",
    "ALTER TABLE agent_eval_comparison_execution ADD COLUMN client_intent_hash TEXT CHECK(client_intent_hash IS NULL OR client_intent_hash LIKE 'sha256:%')",
    "ALTER TABLE agent_eval_comparison_execution ADD COLUMN automation_context_json TEXT CHECK(automation_context_json IS NULL OR (json_valid(automation_context_json) AND json_type(automation_context_json)='object' AND length(automation_context_json)<=4096))",
    """CREATE TRIGGER trg_agent_comparison_origin_immutable BEFORE UPDATE ON agent_eval_comparison_execution
       WHEN NEW.client_intent_hash IS NOT OLD.client_intent_hash OR NEW.automation_context_json IS NOT OLD.automation_context_json
       BEGIN SELECT RAISE(ABORT,'comparison automation origin is immutable'); END""",
    """CREATE TRIGGER trg_agent_comparison_origin_insert BEFORE INSERT ON agent_eval_comparison_execution
       WHEN (NEW.actor_kind='automation' AND NEW.automation_context_json IS NULL)
         OR (NEW.client_intent_hash IS NULL)!=(NEW.automation_context_json IS NULL)
         OR (NEW.automation_context_json IS NOT NULL AND (
           NEW.actor_kind!='automation'
           OR json_extract(NEW.automation_context_json,'$.schema_version') IS NOT 1
           OR json_extract(NEW.automation_context_json,'$.protocol_version') IS NOT 2
           OR json_type(NEW.automation_context_json,'$.repository') IS NOT 'text'
           OR json_type(NEW.automation_context_json,'$.ref') IS NOT 'text'
           OR NOT EXISTS(SELECT 1 FROM agent_version_automation_grant g JOIN agent_eval_plan p
             ON p.id=NEW.plan_id AND p.team_id=g.team_id AND p.agent_id=g.agent_id
             WHERE g.id=NEW.automation_grant_id AND g.team_id=NEW.team_id AND g.agent_id=NEW.agent_id
               AND g.principal_id=NEW.actor_ref
               AND g.owner_user_id=json_extract(NEW.automation_context_json,'$.effective_owner_user_id')
               AND g.evaluation_workspace_id IS json_extract(NEW.automation_context_json,'$.evaluation_workspace_id')
               AND (g.evaluation_workspace_id IS NULL OR g.evaluation_workspace_id=p.execution_workspace_id))))
       BEGIN SELECT RAISE(ABORT,'comparison automation origin mismatch'); END""",
    """CREATE TABLE agent_version_automation_preparation (
       team_id TEXT NOT NULL, agent_id TEXT NOT NULL, principal_id TEXT NOT NULL, grant_id TEXT NOT NULL,
       proposal_id TEXT NOT NULL, operation_key_hash TEXT NOT NULL CHECK(operation_key_hash LIKE 'sha256:%'),
       request_hash TEXT NOT NULL CHECK(request_hash LIKE 'sha256:%'),
       context_hash TEXT CHECK(context_hash IS NULL OR context_hash LIKE 'sha256:%'),
       protocol_version INTEGER NOT NULL CHECK(protocol_version IN (1,2)),
       candidate_id TEXT NOT NULL UNIQUE, initial_plan_id TEXT NOT NULL, initial_plan_hash TEXT NOT NULL,
       created_at TEXT NOT NULL,
       PRIMARY KEY(team_id,principal_id,agent_id,operation_key_hash),
       CHECK((protocol_version=1 AND context_hash IS NULL) OR (protocol_version=2 AND context_hash IS NOT NULL)),
       FOREIGN KEY(team_id,agent_id,grant_id) REFERENCES agent_version_automation_grant(team_id,agent_id,id) ON DELETE RESTRICT,
       FOREIGN KEY(team_id,agent_id,proposal_id) REFERENCES agent_change_proposal(team_id,agent_id,id) ON DELETE RESTRICT,
       FOREIGN KEY(team_id,agent_id,candidate_id) REFERENCES team_subagent_release_candidate(team_id,subagent_id,id) ON DELETE RESTRICT,
       FOREIGN KEY(team_id,agent_id,initial_plan_id) REFERENCES agent_eval_plan(team_id,agent_id,id) ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_agent_automation_preparation_grant ON agent_version_automation_preparation(team_id,agent_id,grant_id)",
    "CREATE INDEX idx_agent_automation_preparation_proposal ON agent_version_automation_preparation(team_id,agent_id,proposal_id)",
    "CREATE INDEX idx_agent_automation_preparation_plan ON agent_version_automation_preparation(team_id,agent_id,initial_plan_id)",
    """CREATE TRIGGER trg_agent_automation_preparation_insert BEFORE INSERT ON agent_version_automation_preparation
       WHEN NOT EXISTS(SELECT 1 FROM agent_version_automation_grant g
         JOIN agent_change_proposal proposal ON proposal.id=NEW.proposal_id
         JOIN agent_eval_candidate_control control ON control.candidate_id=NEW.candidate_id
         JOIN agent_eval_plan plan ON plan.id=NEW.initial_plan_id
         WHERE g.id=NEW.grant_id AND g.principal_id=NEW.principal_id
           AND g.team_id=NEW.team_id AND g.agent_id=NEW.agent_id
           AND proposal.team_id=NEW.team_id AND proposal.agent_id=NEW.agent_id AND proposal.source_type='git'
           AND proposal.created_by_id=NEW.principal_id
           AND json_extract(proposal.provenance_json,'$.automation_grant_id')=NEW.grant_id
           AND control.prepared_actor_kind='automation' AND control.prepared_actor_ref=NEW.principal_id
           AND control.prepared_automation_grant_id=NEW.grant_id AND control.prepared_user_id=g.owner_user_id
           AND plan.candidate_id=NEW.candidate_id AND plan.plan_hash=NEW.initial_plan_hash
           AND ((g.evaluation_workspace_id IS NULL AND NEW.protocol_version=1)
             OR (g.evaluation_workspace_id=plan.execution_workspace_id AND NEW.protocol_version=2)))
       BEGIN SELECT RAISE(ABORT,'automation preparation provenance mismatch'); END""",
    """CREATE TRIGGER trg_agent_automation_preparation_update BEFORE UPDATE ON agent_version_automation_preparation
       BEGIN SELECT RAISE(ABORT,'automation preparation is immutable'); END""",
    """CREATE TRIGGER trg_agent_automation_preparation_delete BEFORE DELETE ON agent_version_automation_preparation
       BEGIN SELECT RAISE(ABORT,'automation preparation is immutable'); END""",
)
