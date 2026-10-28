"""Recover exact personal Skill package saves across filesystem/SQL boundaries."""

from alembic import context

revision = "0139_coach_skill_operations"
down_revision = "0138_agent_coach_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT coach_skill_operations")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT coach_skill_operations")
        db.exec_driver_sql("RELEASE SAVEPOINT coach_skill_operations")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT coach_skill_operations")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database and package snapshot to roll back.")


_DDL = (
    """CREATE TABLE coach_skill_operation (
       id TEXT PRIMARY KEY CHECK(id LIKE 'csw_%'), team_id TEXT NOT NULL,
       user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       workspace_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
       request_hash TEXT NOT NULL CHECK(length(request_hash)=71 AND request_hash LIKE 'sha256:%'),
       target_json TEXT NOT NULL CHECK(json_valid(target_json)),
       state TEXT NOT NULL CHECK(state IN ('reserved','prepared','succeeded','failed')),
       plan_json TEXT CHECK(plan_json IS NULL OR json_valid(plan_json)),
       result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
       CHECK((state='reserved' AND plan_json IS NULL AND result_json IS NULL)
          OR (state='prepared' AND plan_json IS NOT NULL AND result_json IS NULL)
          OR (state='succeeded' AND plan_json IS NOT NULL AND result_json IS NOT NULL)
          OR (state='failed' AND result_json IS NOT NULL))
    )""",
    "CREATE INDEX idx_coach_skill_operation_user_state ON coach_skill_operation(user_id,state)",
    """CREATE TRIGGER trg_coach_skill_identity BEFORE UPDATE ON coach_skill_operation
       WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.user_id IS NOT OLD.user_id
         OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.session_id IS NOT OLD.session_id
         OR NEW.turn_id IS NOT OLD.turn_id OR NEW.request_hash IS NOT OLD.request_hash
         OR NEW.target_json IS NOT OLD.target_json OR NEW.created_at IS NOT OLD.created_at
         OR ((OLD.plan_json IS NOT NULL OR OLD.state IN ('succeeded','failed')) AND NEW.plan_json IS NOT OLD.plan_json)
       BEGIN SELECT RAISE(ABORT,'Coach Skill operation identity is immutable'); END""",
    """CREATE TRIGGER trg_coach_skill_transition BEFORE UPDATE ON coach_skill_operation
       WHEN NOT ((NEW.state=OLD.state AND NEW.result_json IS OLD.result_json)
         OR (OLD.state='reserved' AND NEW.state IN ('prepared','failed'))
         OR (OLD.state='prepared' AND NEW.state IN ('succeeded','failed')))
       BEGIN SELECT RAISE(ABORT,'Invalid Coach Skill operation transition'); END""",
    """CREATE TRIGGER trg_coach_skill_start BEFORE INSERT ON coach_skill_operation
       WHEN NEW.state!='reserved'
       BEGIN SELECT RAISE(ABORT,'Coach Skill operation must reserve before writing'); END""",
)
