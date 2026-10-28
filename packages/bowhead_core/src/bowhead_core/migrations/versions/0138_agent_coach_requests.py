"""Retain exact Owner Coach generation requests before paid model work."""

from alembic import context

revision = "0138_agent_coach_requests"
down_revision = "0137_agent_automation_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT agent_coach_requests")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_coach_requests")
        db.exec_driver_sql("RELEASE SAVEPOINT agent_coach_requests")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT agent_coach_requests")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    """CREATE TABLE agent_coach_generation_request (
       id TEXT PRIMARY KEY CHECK(id LIKE 'acgr_%'), team_id TEXT NOT NULL, agent_id TEXT NOT NULL,
       owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       key_hash TEXT NOT NULL CHECK(length(key_hash)=71 AND key_hash LIKE 'sha256:%'),
       request_hash TEXT NOT NULL CHECK(length(request_hash)=71 AND request_hash LIKE 'sha256:%'),
       source_ref TEXT NOT NULL CHECK(length(source_ref) BETWEEN 1 AND 240),
       working_copy_id TEXT NOT NULL, working_copy_version INTEGER NOT NULL CHECK(working_copy_version>=1),
       base_release_id TEXT, state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed','unknown')),
       result_proposal_id TEXT REFERENCES agent_change_proposal(id) ON DELETE RESTRICT,
       failure_code TEXT CHECK(failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 160),
       created_at TEXT NOT NULL, response_deadline TEXT NOT NULL, provider_started_at TEXT, updated_at TEXT NOT NULL,
       UNIQUE(team_id,agent_id,owner_user_id,key_hash), UNIQUE(team_id,agent_id,source_ref),
       FOREIGN KEY(team_id,agent_id) REFERENCES team_subagent(team_id,id) ON DELETE CASCADE,
       CHECK((state='running' AND result_proposal_id IS NULL AND failure_code IS NULL)
          OR (state='succeeded' AND result_proposal_id IS NOT NULL AND failure_code IS NULL)
          OR (state IN ('failed','unknown') AND result_proposal_id IS NULL AND failure_code IS NOT NULL))
    )""",
    """CREATE TRIGGER trg_agent_coach_request_identity BEFORE UPDATE ON agent_coach_generation_request
       WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.agent_id IS NOT OLD.agent_id
         OR NEW.owner_user_id IS NOT OLD.owner_user_id OR NEW.key_hash IS NOT OLD.key_hash
         OR NEW.request_hash IS NOT OLD.request_hash OR NEW.source_ref IS NOT OLD.source_ref
         OR NEW.working_copy_id IS NOT OLD.working_copy_id OR NEW.working_copy_version IS NOT OLD.working_copy_version
         OR NEW.base_release_id IS NOT OLD.base_release_id OR NEW.created_at IS NOT OLD.created_at
         OR NEW.response_deadline IS NOT OLD.response_deadline
         OR (OLD.provider_started_at IS NOT NULL AND NEW.provider_started_at IS NOT OLD.provider_started_at)
       BEGIN SELECT RAISE(ABORT,'Coach request identity is immutable'); END""",
    """CREATE TRIGGER trg_agent_coach_request_terminal BEFORE UPDATE ON agent_coach_generation_request
       WHEN OLD.state IN ('succeeded','failed') AND (
         NEW.state IS NOT OLD.state OR NEW.result_proposal_id IS NOT OLD.result_proposal_id OR NEW.failure_code IS NOT OLD.failure_code)
       BEGIN SELECT RAISE(ABORT,'Coach request result is immutable'); END""",
    """CREATE TRIGGER trg_agent_coach_request_start BEFORE INSERT ON agent_coach_generation_request
       WHEN NEW.state!='running'
       BEGIN SELECT RAISE(ABORT,'Coach request must begin before generation'); END""",
    """CREATE TRIGGER trg_agent_coach_request_no_restart BEFORE UPDATE ON agent_coach_generation_request
       WHEN NEW.state='running' AND OLD.state!='running'
       BEGIN SELECT RAISE(ABORT,'Coach generation cannot restart'); END""",
    """CREATE TRIGGER trg_agent_coach_request_result BEFORE UPDATE ON agent_coach_generation_request
       WHEN NEW.result_proposal_id IS NOT NULL AND (NEW.provider_started_at IS NULL OR NOT EXISTS(
         SELECT 1 FROM agent_change_proposal p WHERE p.id=NEW.result_proposal_id AND p.team_id=NEW.team_id
           AND p.agent_id=NEW.agent_id AND p.source_type='coach' AND p.source_ref=NEW.source_ref
           AND p.created_by_kind='user' AND p.created_by_id=NEW.owner_user_id
           AND json_extract(p.provenance_json,'$.generation_request_id')=NEW.id))
       BEGIN SELECT RAISE(ABORT,'Coach request result scope mismatch'); END""",
)
