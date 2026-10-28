"""Let Lead continue a completed @Agent primary within the same reference Turn.

The first Lead dispatch keeps its existing ledger: one ``delegate_started`` and
one ``output_received`` evidence row for the admitted primary execution. Each
further dispatch (up to ``PRIMARY_DISPATCH_LIMIT`` per Turn) appends
one row here and reopens that same execution from ``completed`` to ``running``
(the only reopen the execution transition trigger admits),
so every existing scope fence (Skill reads, model contexts, MCP calls) keeps
checking the one admitted binding. No new execution, grant or composition is
minted. Settling a continuation always returns the execution to ``completed``
with its original ``ended_at`` (kept in ``primary_ended_at``): the first
dispatch already completed the primary task, and duration readers must not
count Lead time between dispatches. Each continuation's own duration is its
``started_at``/``ended_at``.
"""

from importlib import import_module

from alembic import context

revision = "0189_reference_primary_continuation"
down_revision = "0188_agent_eval_suite_revision_restore"
branch_labels = None
depends_on = None

BINDING = import_module("bowhead_core.migrations.versions.0034_agent_turn_binding")
FOLLOW_UP = import_module("bowhead_core.migrations.versions.0092_reference_follow_up_runtime")

# Frozen at upgrade time; chat_reference_delegation.PRIMARY_DISPATCH_LIMIT must not exceed it.
DISPATCH_LIMIT = 3
TRANSITION_NAME = "trg_turn_agent_execution_status_transition"
TRANSITION_CURRENT = next(ddl for kind, name, ddl in BINDING._OBJECTS if kind == "trigger" and name == TRANSITION_NAME)
TRANSITION_NEEDLE = """          OR (
            OLD.status='running' AND NEW.status IN ('completed','failed','blocked','cancelled')"""
if TRANSITION_CURRENT.count(TRANSITION_NEEDLE) != 1:
    raise RuntimeError("reference primary continuation source drift")
TRANSITION_DDL = TRANSITION_CURRENT.replace(
    TRANSITION_NEEDLE,
    """          OR (
            OLD.status='completed' AND NEW.status='running'
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NULL
            AND NEW.failure_code IS NULL
            AND EXISTS (
              SELECT 1 FROM chat_reference_primary_continuations continuation
              WHERE continuation.execution_id=NEW.id AND continuation.state='started'
            )
          )
"""
    + TRANSITION_NEEDLE,
)

TABLE = f"""
CREATE TABLE chat_reference_primary_continuations (
 turn_id TEXT NOT NULL REFERENCES chat_reference_plans(turn_id) ON DELETE CASCADE,
 dispatch_seq INTEGER NOT NULL CHECK(typeof(dispatch_seq)='integer' AND dispatch_seq BETWEEN 2 AND {DISPATCH_LIMIT}),
 execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
 created_generation INTEGER NOT NULL CHECK(typeof(created_generation)='integer' AND created_generation>=1),
 owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 256),
 call_id TEXT NOT NULL CHECK(length(call_id) BETWEEN 1 AND 256),
 subagent_id TEXT NOT NULL CHECK(length(subagent_id) BETWEEN 1 AND 64),
 task_hash TEXT NOT NULL CHECK(length(task_hash)=71 AND substr(task_hash,1,7)='sha256:'),
 state TEXT NOT NULL CHECK(state IN ('started','completed','failed')),
 output_hash TEXT CHECK(output_hash IS NULL OR (length(output_hash)=71 AND substr(output_hash,1,7)='sha256:')),
 output_ciphertext TEXT CHECK(output_ciphertext IS NULL OR length(CAST(output_ciphertext AS BLOB))<=2097152),
 failure_code TEXT CHECK(failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128),
 retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
 started_at TEXT NOT NULL,
 ended_at TEXT,
 primary_ended_at TEXT NOT NULL,
 PRIMARY KEY(turn_id,dispatch_seq),
 CHECK(retention_state='available' OR output_ciphertext IS NULL),
 CHECK((state='started' AND ended_at IS NULL AND output_hash IS NULL AND output_ciphertext IS NULL AND failure_code IS NULL)
   OR (state='completed' AND ended_at IS NOT NULL AND failure_code IS NULL AND output_hash IS NOT NULL
     AND (output_ciphertext IS NOT NULL OR retention_state='purged'))
   OR (state='failed' AND ended_at IS NOT NULL AND failure_code IS NOT NULL AND output_hash IS NULL AND output_ciphertext IS NULL))
)
"""
INDEX = "CREATE INDEX idx_chat_reference_primary_continuations_execution ON chat_reference_primary_continuations(execution_id,state)"
SCOPE = """
CREATE TRIGGER trg_reference_primary_continuation_scope BEFORE INSERT ON chat_reference_primary_continuations
WHEN NEW.state<>'started' OR NEW.retention_state<>'available'
 OR NEW.dispatch_seq<>2+(SELECT COUNT(*) FROM chat_reference_primary_continuations prior WHERE prior.turn_id=NEW.turn_id)
 OR EXISTS (SELECT 1 FROM chat_reference_primary_continuations open WHERE open.turn_id=NEW.turn_id AND open.state='started')
 OR NOT EXISTS (
  SELECT 1 FROM chat_reference_plans p
  JOIN turns t ON t.id=p.turn_id
  JOIN turn_agent_binding b ON b.turn_id=p.turn_id
  JOIN turn_agent_execution e ON e.id=b.primary_execution_id
  JOIN chat_reference_resolutions actual ON actual.turn_id=p.turn_id AND actual.resolution_seq=2
  JOIN chat_reference_evidence started ON started.turn_id=p.turn_id AND started.execution_id=e.id AND started.kind='delegate_started'
  JOIN chat_reference_evidence output ON output.turn_id=p.turn_id AND output.execution_id=e.id AND output.kind='output_received'
  JOIN chat_turn_spawn_dispatch d ON d.turn_id=p.turn_id AND d.session_id=t.session_id
  WHERE p.turn_id=NEW.turn_id AND p.retention_state='available'
  AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
  AND json_extract(actual.composition_json,'$.runtime_policy')='lead-delegation.v1'
  AND actual.execution_id=e.id AND e.id=NEW.execution_id AND e.status='completed' AND e.ended_at=NEW.primary_ended_at
  AND started.subagent_id=NEW.subagent_id AND output.subagent_id=NEW.subagent_id
  AND output.created_generation=NEW.created_generation
  AND t.status='running' AND t.execution_generation=NEW.created_generation
  AND d.state='spawned' AND d.owner_id=NEW.owner_id AND d.lease_execution_generation=NEW.created_generation
  AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
 )
BEGIN SELECT RAISE(ABORT,'reference primary continuation scope mismatch'); END
"""
IMMUTABLE = """
CREATE TRIGGER trg_reference_primary_continuation_immutable BEFORE UPDATE ON chat_reference_primary_continuations
WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.dispatch_seq IS NOT OLD.dispatch_seq OR NEW.execution_id IS NOT OLD.execution_id
 OR NEW.created_generation IS NOT OLD.created_generation OR NEW.owner_id IS NOT OLD.owner_id OR NEW.call_id IS NOT OLD.call_id
 OR NEW.subagent_id IS NOT OLD.subagent_id OR NEW.task_hash IS NOT OLD.task_hash OR NEW.started_at IS NOT OLD.started_at
 OR NEW.primary_ended_at IS NOT OLD.primary_ended_at
 OR NOT (
  (OLD.state='started' AND NEW.state IN ('completed','failed') AND NEW.retention_state IS OLD.retention_state)
  OR (NEW.state IS OLD.state AND NEW.output_hash IS OLD.output_hash AND NEW.failure_code IS OLD.failure_code
   AND NEW.ended_at IS OLD.ended_at
   AND ((NEW.output_ciphertext IS OLD.output_ciphertext AND NEW.retention_state IS OLD.retention_state)
    OR (OLD.retention_state='available' AND NEW.retention_state='purged' AND NEW.output_ciphertext IS NULL))))
BEGIN SELECT RAISE(ABORT,'reference primary continuation is immutable'); END
"""
PURGE = """
CREATE TRIGGER trg_reference_primary_continuation_purge AFTER UPDATE OF retention_state ON chat_reference_plans
WHEN NEW.retention_state='purged'
BEGIN UPDATE chat_reference_primary_continuations SET retention_state='purged',output_ciphertext=NULL
  WHERE turn_id=NEW.turn_id AND retention_state='available'; END
"""
NEW_OBJECTS = {
    "chat_reference_primary_continuations": TABLE,
    "idx_chat_reference_primary_continuations_execution": INDEX,
    "trg_reference_primary_continuation_scope": SCOPE,
    "trg_reference_primary_continuation_immutable": IMMUTABLE,
    "trg_reference_primary_continuation_purge": PURGE,
}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    normalize = FOLLOW_UP._normalized
    row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (TRANSITION_NAME,)).fetchone()
    if row is None or normalize(row[0]) != normalize(TRANSITION_CURRENT):
        raise RuntimeError("reference primary continuation schema drift")
    if any(db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in NEW_OBJECTS):
        raise RuntimeError("reference primary continuation schema drift")
    db.exec_driver_sql("SAVEPOINT reference_primary_continuation")
    try:
        for sql in NEW_OBJECTS.values():
            db.exec_driver_sql(sql)
        db.exec_driver_sql(f"DROP TRIGGER {TRANSITION_NAME}")
        db.exec_driver_sql(TRANSITION_DDL)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_primary_continuation")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_primary_continuation")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_primary_continuation")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
