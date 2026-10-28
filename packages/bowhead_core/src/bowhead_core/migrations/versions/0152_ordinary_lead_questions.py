"""Retain the first ordinary Lead answer and its actual resumed worker."""

from alembic import context

revision = "0152_ordinary_lead_questions"
down_revision = "0151_document_planner_v5"
branch_labels = None
depends_on = None

_OLD = "(json_valid(OLD.resume_context_json) AND json_extract(OLD.resume_context_json,'$.__schema_version')=4)"
_NEW = "(json_valid(NEW.resume_context_json) AND json_extract(NEW.resume_context_json,'$.__schema_version')=4)"

DDL = (
    "ALTER TABLE clarification_requests ADD COLUMN continuation_generation INTEGER CHECK(continuation_generation>1)",
    "ALTER TABLE clarification_requests ADD COLUMN continuation_owner_id TEXT",
    f"""CREATE TRIGGER trg_ordinary_question_admission BEFORE INSERT ON clarification_requests
      WHEN {_NEW} AND (
        NEW.status<>'pending' OR NEW.injected_at IS NOT NULL OR NEW.continuation_generation IS NOT NULL OR NEW.continuation_owner_id IS NOT NULL
        OR NEW.answers_json IS NOT NULL OR NEW.chosen_at IS NOT NULL OR NEW.kind<>'ambiguous_turn'
        OR NEW.token<>'' OR NEW.tool IS NOT NULL OR NEW.multi_select<>0 OR NEW.candidates_json<>'[]'
        OR json_extract(NEW.resume_context_json,'$.continuation.kind') IS NOT 'ordinary_lead'
        OR json_extract(NEW.resume_context_json,'$.continuation.contract') IS NOT 'ordinary-lead-question.v1'
        OR json_extract(NEW.resume_context_json,'$.continuation.card_id') IS NOT NEW.request_id
        OR json_extract(NEW.resume_context_json,'$.continuation.creation_generation') IS NOT 1
        OR EXISTS(SELECT 1 FROM clarification_requests WHERE turn_id=NEW.turn_id OR request_id=NEW.request_id)
        OR EXISTS(SELECT 1 FROM document_jobs WHERE origin_turn_id=NEW.turn_id)
        OR NOT EXISTS(SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
          JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id
          JOIN chat_turn_user_sources u ON u.turn_id=t.id
          WHERE t.id=NEW.turn_id AND s.id=NEW.session_id AND s.user_id=NEW.user_id AND s.workspace_id=NEW.workspace_id
            AND s.deleted_at IS NULL AND s.active_turn_id=t.id AND t.status='running' AND COALESCE(t.terminal_protocol_version,0)=0
            AND t.execution_generation=1 AND d.lease_execution_generation=1 AND d.state='spawned'
            AND d.owner_id=json_extract(NEW.resume_context_json,'$.continuation.creation_owner_id')
            AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
            AND json_extract(NEW.resume_context_json,'$.team_id')=w.team_id
            AND json_extract(NEW.resume_context_json,'$.turn_id')=t.id
            AND json_extract(NEW.resume_context_json,'$.session_id')=s.id
            AND json_extract(NEW.resume_context_json,'$.user_id')=s.user_id
            AND json_extract(NEW.resume_context_json,'$.workspace_id')=s.workspace_id))
      BEGIN SELECT RAISE(ABORT,'ordinary question admission unavailable'); END""",
    f"""CREATE TRIGGER trg_ordinary_question_identity BEFORE UPDATE ON clarification_requests
      WHEN ({_NEW} AND NOT COALESCE({_OLD},0)) OR ({_OLD} AND (
        NEW.request_id IS NOT OLD.request_id OR NEW.turn_id IS NOT OLD.turn_id OR NEW.user_id IS NOT OLD.user_id
        OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.session_id IS NOT OLD.session_id
        OR NEW.kind IS NOT OLD.kind OR NEW.token IS NOT OLD.token OR NEW.tool IS NOT OLD.tool
        OR NEW.rationale IS NOT OLD.rationale OR NEW.candidates_json IS NOT OLD.candidates_json
        OR NEW.multi_select IS NOT OLD.multi_select OR NEW.created_at IS NOT OLD.created_at
        OR NEW.resume_context_json IS NOT OLD.resume_context_json
        OR (OLD.status<>'pending' AND NEW.status='pending')
        OR (OLD.chosen_at IS NOT NULL AND (NEW.chosen_value IS NOT OLD.chosen_value OR NEW.chosen_values_json IS NOT OLD.chosen_values_json
          OR NEW.answers_json IS NOT OLD.answers_json OR NEW.chosen_at IS NOT OLD.chosen_at))
        OR (OLD.injected_at IS NOT NULL AND (NEW.injected_at IS NOT OLD.injected_at
          OR NEW.continuation_generation IS NOT OLD.continuation_generation OR NEW.continuation_owner_id IS NOT OLD.continuation_owner_id))))
      BEGIN SELECT RAISE(ABORT,'ordinary question identity is immutable'); END""",
    f"""CREATE TRIGGER trg_ordinary_question_answer BEFORE UPDATE ON clarification_requests
      WHEN {_OLD} AND NEW.status='resolved' AND OLD.status<>'resolved' AND (
        OLD.status<>'pending' OR NEW.answers_json IS NULL OR NOT json_valid(NEW.answers_json) OR NEW.chosen_at IS NULL
        OR NOT EXISTS(SELECT 1 FROM turns t JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id
          WHERE t.id=OLD.turn_id AND t.status='clarification_pending' AND d.state IN ('terminal','failed')
            AND d.lease_execution_generation=t.execution_generation AND d.cancel_requested_at IS NULL AND OLD.injected_at IS NULL))
      BEGIN SELECT RAISE(ABORT,'ordinary question answer unavailable'); END""",
    f"""CREATE TRIGGER trg_ordinary_question_consumption BEFORE UPDATE OF injected_at,continuation_generation,continuation_owner_id ON clarification_requests
      WHEN {_OLD} AND (NEW.injected_at IS NOT OLD.injected_at OR NEW.continuation_generation IS NOT OLD.continuation_generation
        OR NEW.continuation_owner_id IS NOT OLD.continuation_owner_id) AND (
        OLD.injected_at IS NOT NULL OR NEW.injected_at IS NULL OR NEW.status<>'resolved'
        OR NEW.continuation_generation IS NULL OR NEW.continuation_owner_id IS NULL
        OR NOT EXISTS(SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
          JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id
          WHERE t.id=OLD.turn_id AND t.status='running' AND t.execution_generation=NEW.continuation_generation
            AND s.active_turn_id=t.id AND s.deleted_at IS NULL AND d.lease_kind='resume' AND d.state='spawned'
            AND d.lease_execution_generation=t.execution_generation AND d.owner_id=NEW.continuation_owner_id
            AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')))
      BEGIN SELECT RAISE(ABORT,'ordinary question consumption unavailable'); END""",
    f"""CREATE TRIGGER trg_ordinary_question_delete BEFORE DELETE ON clarification_requests
      WHEN {_OLD} AND EXISTS(SELECT 1 FROM turns WHERE id=OLD.turn_id)
      BEGIN SELECT RAISE(ABORT,'ordinary question is retained with its Turn'); END""",
    """CREATE TRIGGER trg_ordinary_question_replace BEFORE INSERT ON clarification_requests
      WHEN EXISTS(SELECT 1 FROM clarification_requests q WHERE (q.request_id=NEW.request_id OR q.turn_id=NEW.turn_id)
        AND json_valid(q.resume_context_json) AND json_extract(q.resume_context_json,'$.__schema_version')=4)
        AND NOT EXISTS(SELECT 1 FROM document_jobs j JOIN document_turn_dispatches d ON d.job_id=j.id
          JOIN clarification_requests q ON q.turn_id=j.origin_turn_id
          WHERE json_valid(NEW.resume_context_json) AND json_extract(NEW.resume_context_json,'$.__schema_version')=3
            AND j.id=json_extract(NEW.resume_context_json,'$.continuation.checkpoint.job_id')
            AND NEW.turn_id=j.origin_turn_id AND NEW.session_id=j.session_id AND NEW.user_id=j.requested_by AND NEW.workspace_id=j.workspace_id
            AND q.request_id<>NEW.request_id AND q.injected_at IS NOT NULL
            AND q.continuation_generation=d.creation_generation AND q.continuation_owner_id=d.owner_id
            AND NOT EXISTS(SELECT 1 FROM clarification_requests other WHERE other.turn_id=NEW.turn_id AND other.request_id<>q.request_id))
      BEGIN SELECT RAISE(ABORT,'ordinary question history cannot be replaced'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT ordinary_lead_questions")
    try:
        for statement in DDL:
            connection.exec_driver_sql(statement)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT ordinary_lead_questions")
        connection.exec_driver_sql("RELEASE SAVEPOINT ordinary_lead_questions")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT ordinary_lead_questions")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
