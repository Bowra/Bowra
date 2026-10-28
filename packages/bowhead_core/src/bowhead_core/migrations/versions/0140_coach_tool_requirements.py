"""Retain Tool requirements and exact resource observations for Coach handoffs."""

from alembic import context

revision = "0140_coach_tool_requirements"
down_revision = "0139_coach_skill_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT coach_tool_requirements")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT coach_tool_requirements")
        db.exec_driver_sql("RELEASE SAVEPOINT coach_tool_requirements")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT coach_tool_requirements")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    """CREATE TABLE coach_tool_requirement (
       id TEXT PRIMARY KEY CHECK(id LIKE 'ctrq_%'), team_id TEXT NOT NULL,
       user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       workspace_id TEXT NOT NULL, origin_session_id TEXT NOT NULL, origin_turn_id TEXT NOT NULL,
       key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
       parent_json TEXT NOT NULL CHECK(json_valid(parent_json)),
       parent_context_json TEXT NOT NULL CHECK(json_valid(parent_context_json)),
       requirement_json TEXT NOT NULL CHECK(json_valid(requirement_json)),
       onboarding_operation_id TEXT UNIQUE, onboarding_requested_at TEXT,
       created_at TEXT NOT NULL,
       UNIQUE(team_id,user_id,origin_turn_id,key_hash),
       CHECK((onboarding_operation_id IS NULL)=(onboarding_requested_at IS NULL))
    )""",
    "CREATE INDEX idx_coach_tool_requirement_owner ON coach_tool_requirement(team_id,user_id,workspace_id)",
    """CREATE TABLE coach_tool_observation (
       id TEXT PRIMARY KEY CHECK(id LIKE 'ctro_%'), requirement_id TEXT NOT NULL REFERENCES coach_tool_requirement(id) ON DELETE CASCADE,
       request_key_hash TEXT NOT NULL, reference_json TEXT NOT NULL CHECK(json_valid(reference_json)),
       observed_at TEXT NOT NULL, UNIQUE(requirement_id,request_key_hash)
    )""",
    """CREATE TRIGGER trg_coach_tool_requirement_identity BEFORE UPDATE ON coach_tool_requirement
       WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.user_id IS NOT OLD.user_id
         OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.origin_session_id IS NOT OLD.origin_session_id
         OR NEW.origin_turn_id IS NOT OLD.origin_turn_id OR NEW.key_hash IS NOT OLD.key_hash
         OR NEW.request_hash IS NOT OLD.request_hash OR NEW.parent_json IS NOT OLD.parent_json
         OR NEW.parent_context_json IS NOT OLD.parent_context_json
         OR NEW.requirement_json IS NOT OLD.requirement_json OR NEW.created_at IS NOT OLD.created_at
         OR (OLD.onboarding_operation_id IS NOT NULL AND (NEW.onboarding_operation_id IS NOT OLD.onboarding_operation_id
            OR NEW.onboarding_requested_at IS NOT OLD.onboarding_requested_at))
       BEGIN SELECT RAISE(ABORT,'Coach Tool requirement identity is immutable'); END""",
    """CREATE TRIGGER trg_coach_tool_observation_immutable BEFORE UPDATE ON coach_tool_observation
       BEGIN SELECT RAISE(ABORT,'Coach Tool observation is immutable'); END""",
)
