"""Add parent-owned collaboration questions and original-operation receipts.

This storage revision does not advertise a Tool or upgrade historical Turns.
"""

from alembic import context

revision = "0165_agent_collaboration"
down_revision = "0164_ordinary_attachment_reads"
branch_labels = None
depends_on = None


STATEMENTS = (
    """CREATE TABLE agent_collaboration_turn (
        turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
        protocol_version INTEGER NOT NULL CHECK(protocol_version=1),
        owner_execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
        policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
        deadline REAL NOT NULL,
        created_at TEXT NOT NULL,
        model_attempts INTEGER NOT NULL DEFAULT 0 CHECK(model_attempts BETWEEN 0 AND 100),
        tool_attempts INTEGER NOT NULL DEFAULT 0 CHECK(tool_attempts BETWEEN 0 AND 256)
    )""",
    """CREATE TABLE agent_collaboration_participant (
        execution_id TEXT PRIMARY KEY REFERENCES turn_agent_execution(id) ON DELETE CASCADE,
        turn_id TEXT NOT NULL REFERENCES agent_collaboration_turn(turn_id) ON DELETE CASCADE,
        assignment_ciphertext TEXT NOT NULL,
        assignment_hash TEXT NOT NULL,
        wait_operation_id TEXT,
        wait_revision INTEGER,
        coordination_attempts INTEGER NOT NULL DEFAULT 0 CHECK(coordination_attempts BETWEEN 0 AND 8),
        UNIQUE(turn_id,execution_id)
    )""",
    """CREATE TABLE agent_collaboration_operation (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE CHECK(id LIKE 'aco_%'),
        turn_id TEXT NOT NULL REFERENCES agent_collaboration_turn(turn_id) ON DELETE CASCADE,
        execution_id TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        frame_id TEXT NOT NULL,
        model_step_id TEXT NOT NULL,
        native_call_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('ask','reply','resolve','update','wait','expire','model_attempt','tool_attempt')),
        arguments_hash TEXT NOT NULL,
        content_ciphertext TEXT NOT NULL,
        receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
        question_id TEXT REFERENCES agent_collaboration_operation(id),
        family_id TEXT REFERENCES agent_collaboration_operation(id),
        required INTEGER NOT NULL CHECK(required IN (0,1)),
        response_status TEXT CHECK(response_status IN ('pending','answered','unavailable','cancelled')),
        dependency_status TEXT CHECK(dependency_status IN ('open','resolved','waived','blocked','cancelled')),
        reply_id TEXT,
        deadline REAL,
        event_id TEXT NOT NULL UNIQUE,
        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
        created_at TEXT NOT NULL,
        UNIQUE(execution_id,frame_id,model_step_id,native_call_id),
        FOREIGN KEY(turn_id,execution_id) REFERENCES agent_collaboration_participant(turn_id,execution_id),
        FOREIGN KEY(turn_id,recipient_id) REFERENCES agent_collaboration_participant(turn_id,execution_id),
        CHECK((action='ask') = (response_status IS NOT NULL)),
        CHECK(action='ask' OR (dependency_status IS NULL AND deadline IS NULL)),
        CHECK(action!='ask' OR (deadline IS NOT NULL AND (required=0 OR dependency_status IS NOT NULL)))
    )""",
    """CREATE INDEX idx_agent_collaboration_pending
        ON agent_collaboration_operation(turn_id,recipient_id,response_status,dependency_status) WHERE action='ask'""",
    """CREATE INDEX idx_agent_collaboration_publication
        ON agent_collaboration_operation(turn_id,seq) WHERE published=0""",
    """CREATE TRIGGER trg_agent_collaboration_turn_identity BEFORE UPDATE ON agent_collaboration_turn
        WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.protocol_version IS NOT OLD.protocol_version
          OR NEW.owner_execution_id IS NOT OLD.owner_execution_id OR NEW.policy_json IS NOT OLD.policy_json
          OR NEW.deadline IS NOT OLD.deadline OR NEW.created_at IS NOT OLD.created_at
          OR NEW.model_attempts<OLD.model_attempts OR NEW.tool_attempts<OLD.tool_attempts
        BEGIN SELECT RAISE(ABORT,'collaboration admission is immutable'); END""",
    """CREATE TRIGGER trg_agent_collaboration_assignment_identity BEFORE UPDATE ON agent_collaboration_participant
        WHEN NEW.execution_id IS NOT OLD.execution_id OR NEW.turn_id IS NOT OLD.turn_id
          OR NEW.assignment_ciphertext IS NOT OLD.assignment_ciphertext OR NEW.assignment_hash IS NOT OLD.assignment_hash
          OR NEW.coordination_attempts<OLD.coordination_attempts
        BEGIN SELECT RAISE(ABORT,'collaboration assignment is immutable'); END""",
    """CREATE TRIGGER trg_agent_collaboration_operation_identity BEFORE UPDATE ON agent_collaboration_operation
        WHEN NEW.id IS NOT OLD.id OR NEW.turn_id IS NOT OLD.turn_id OR NEW.execution_id IS NOT OLD.execution_id
          OR NEW.recipient_id IS NOT OLD.recipient_id OR NEW.frame_id IS NOT OLD.frame_id
          OR NEW.model_step_id IS NOT OLD.model_step_id OR NEW.native_call_id IS NOT OLD.native_call_id
          OR NEW.action IS NOT OLD.action OR NEW.arguments_hash IS NOT OLD.arguments_hash
          OR NEW.content_ciphertext IS NOT OLD.content_ciphertext OR NEW.receipt_json IS NOT OLD.receipt_json
          OR NEW.question_id IS NOT OLD.question_id OR NEW.family_id IS NOT OLD.family_id
          OR NEW.required IS NOT OLD.required OR NEW.deadline IS NOT OLD.deadline
          OR NEW.event_id IS NOT OLD.event_id OR NEW.created_at IS NOT OLD.created_at OR NEW.seq IS NOT OLD.seq
          OR NEW.published<OLD.published
          OR (OLD.reply_id IS NOT NULL AND NEW.reply_id IS NOT OLD.reply_id)
          OR (OLD.response_status IS NOT NULL AND OLD.response_status!='pending' AND NEW.response_status IS NOT OLD.response_status)
        BEGIN SELECT RAISE(ABORT,'collaboration operation identity is immutable'); END""",
    """CREATE TRIGGER trg_agent_collaboration_execution_complete BEFORE UPDATE OF status ON turn_agent_execution
        WHEN NEW.status='completed' AND EXISTS (
          SELECT 1 FROM agent_collaboration_operation q
          WHERE q.turn_id=NEW.turn_id AND q.action='ask' AND q.required=1 AND (
            (q.recipient_id=NEW.id AND q.response_status='pending') OR
            (q.execution_id=NEW.id AND q.dependency_status='open') OR
            (EXISTS (SELECT 1 FROM agent_collaboration_turn c WHERE c.turn_id=NEW.turn_id AND c.owner_execution_id=NEW.id)
              AND (q.response_status='pending' OR q.dependency_status='open'))))
        BEGIN SELECT RAISE(ABORT,'unresolved collaboration obligations'); END""",
    """CREATE TRIGGER trg_agent_collaboration_turn_complete BEFORE UPDATE OF status ON turns
        WHEN NEW.status='completed' AND EXISTS (
          SELECT 1 FROM agent_collaboration_operation q WHERE q.turn_id=NEW.id AND q.action='ask'
            AND q.required=1 AND (q.response_status='pending' OR q.dependency_status='open'))
        BEGIN SELECT RAISE(ABORT,'unresolved collaboration obligations'); END""",
    """CREATE TRIGGER trg_agent_collaboration_execution_closed AFTER UPDATE OF status ON turn_agent_execution
        WHEN NEW.status IN ('completed','failed','blocked','cancelled') AND NEW.status IS NOT OLD.status
        BEGIN
          UPDATE agent_collaboration_operation SET response_status='unavailable'
            WHERE turn_id=NEW.turn_id AND recipient_id=NEW.id AND action='ask' AND response_status='pending';
          UPDATE agent_collaboration_participant SET wait_operation_id=NULL,wait_revision=NULL WHERE execution_id=NEW.id;
        END""",
    """CREATE TRIGGER trg_agent_collaboration_turn_closed AFTER UPDATE OF status ON turns
        WHEN NEW.status IN ('completed','failed','cancelled','aborted') AND NEW.status IS NOT OLD.status
        BEGIN
          UPDATE agent_collaboration_operation SET response_status=CASE WHEN response_status='pending'
              THEN CASE WHEN NEW.status='completed' THEN 'unavailable' ELSE 'cancelled' END ELSE response_status END,
            dependency_status=CASE WHEN dependency_status='open' THEN 'cancelled' ELSE dependency_status END
            WHERE turn_id=NEW.id AND action='ask';
          UPDATE agent_collaboration_participant SET wait_operation_id=NULL,wait_revision=NULL WHERE turn_id=NEW.id;
        END""",
)


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT agent_collaboration")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE SAVEPOINT agent_collaboration")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_collaboration")
        db.exec_driver_sql("RELEASE SAVEPOINT agent_collaboration")
        raise


def downgrade():
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
