"""Record per-field undo and reapply of Coach changes written into the draft."""

from alembic import context

revision = "0180_coach_draft_undo"
down_revision = "0179_account_deletion"
branch_labels = None
depends_on = None

_KINDS = "'patch_accept','patch_dismiss','cases_accept','cases_dismiss','patch_undo','patch_reapply'"
_CONTENT_KINDS = "'patch_accept','patch_undo','patch_reapply'"

_DDL = (
    f"""
    CREATE TABLE agent_change_proposal_decision_next (
      id TEXT PRIMARY KEY CHECK(id LIKE 'acpd_%'),
      proposal_id TEXT NOT NULL REFERENCES agent_change_proposal(id) ON DELETE RESTRICT,
      decision_kind TEXT NOT NULL CHECK(decision_kind IN ({_KINDS})),
      selected_group_ids_json TEXT NOT NULL CHECK(json_valid(selected_group_ids_json) AND json_type(selected_group_ids_json)='array'),
      selected_case_ids_json TEXT NOT NULL CHECK(json_valid(selected_case_ids_json) AND json_type(selected_case_ids_json)='array'),
      decision_reason_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(decision_reason_json) AND json_type(decision_reason_json)='object'),
      result_working_copy_id TEXT,
      result_working_copy_version INTEGER,
      result_working_copy_hash TEXT CHECK(result_working_copy_hash IS NULL OR result_working_copy_hash LIKE 'sha256:%'),
      result_suite_revision_id TEXT,
      actor_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at TEXT NOT NULL,
      result_content_hash TEXT CHECK(result_content_hash IS NULL OR result_content_hash LIKE 'sha256:%'),
      CHECK((result_working_copy_id IS NULL)=(result_working_copy_version IS NULL)),
      CHECK((result_working_copy_id IS NULL)=(result_working_copy_hash IS NULL))
    )
    """,
    """
    INSERT INTO agent_change_proposal_decision_next(
      id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,decision_reason_json,
      result_working_copy_id,result_working_copy_version,result_working_copy_hash,result_suite_revision_id,
      actor_user_id,created_at,result_content_hash
    )
    SELECT id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,decision_reason_json,
      result_working_copy_id,result_working_copy_version,result_working_copy_hash,result_suite_revision_id,
      actor_user_id,created_at,result_content_hash
    FROM agent_change_proposal_decision
    """,
    "DROP TABLE agent_change_proposal_decision",
    "ALTER TABLE agent_change_proposal_decision_next RENAME TO agent_change_proposal_decision",
    "CREATE INDEX idx_agent_change_proposal_decisions ON agent_change_proposal_decision(proposal_id,created_at)",
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
    f"""CREATE TRIGGER trg_agent_proposal_content_receipt_shape BEFORE INSERT ON agent_change_proposal_decision
       WHEN NEW.result_content_hash IS NOT NULL AND NEW.decision_kind NOT IN ({_CONTENT_KINDS})
       BEGIN SELECT RAISE(ABORT,'Accepted content belongs to a definition acceptance'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT coach_draft_undo")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
        if db.exec_driver_sql("PRAGMA foreign_key_check(agent_change_proposal_decision)").fetchall():
            raise RuntimeError("agent_change_proposal_decision foreign keys broken by rebuild")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT coach_draft_undo")
        db.exec_driver_sql("RELEASE SAVEPOINT coach_draft_undo")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT coach_draft_undo")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
