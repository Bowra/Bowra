"""Keep resumable subagent runs (agent run limits, phase 2 §6: in-place resume).

Adds two core tables.

`subagent_resume_checkpoints` holds one row per completed or partial helper run. The row stores the
run's message list and reference inventory as a zlib-compressed, Fernet-sealed payload (`ciphertext`,
derived from `BOWHEAD_FERNET_KEY`), plus the plaintext facts Lead and the step list already show (task,
completion, stop reason, done/remaining). Resuming a run claims its row for the resuming turn; the
resumed run's own checkpoint supersedes it. Superseded, expired (30 days) and deleted-session rows keep
their metadata and lose the ciphertext.

`subagent_resume_requests` records the checkpoints a Continue click asked a turn to resume. The send route
writes it before admission creates the Turn row, so it has no foreign key; recovery and replay read it by
`turn_id`.

Session soft-delete purges the ciphertext through a trigger; account erasure follows the `turn_id` and
`session_id` edges. Only core installs DDL; every other target advances its ledger unchanged.
"""

from alembic import context

revision = "0193_subagent_resume_checkpoints"
down_revision = "0192_llm_request_records"
branch_labels = None
depends_on = None

CHECKPOINTS_DDL = """
CREATE TABLE subagent_resume_checkpoints (
  id TEXT PRIMARY KEY CHECK(id LIKE 'rsc\\_%' ESCAPE '\\' AND length(id) BETWEEN 8 AND 64),
  team_id TEXT NOT NULL,
  user_id TEXT NOT NULL CHECK(length(user_id) > 0),
  workspace_id TEXT NOT NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  run_subagent_id TEXT NOT NULL CHECK(length(run_subagent_id) BETWEEN 1 AND 128),
  profile TEXT NOT NULL CHECK(length(profile) BETWEEN 1 AND 128),
  agent_id TEXT,
  agent_revision_id TEXT,
  tool_names_hash TEXT NOT NULL,
  task TEXT NOT NULL CHECK(length(task) <= 4000),
  completion TEXT NOT NULL CHECK(completion IN ('complete','partial')),
  stop_reason TEXT,
  partial_done TEXT CHECK(partial_done IS NULL OR length(partial_done) <= 4000),
  partial_remaining TEXT CHECK(partial_remaining IS NULL OR length(partial_remaining) <= 4000),
  state TEXT NOT NULL DEFAULT 'live' CHECK(state IN ('live','claimed','superseded','purged')),
  claimed_by_turn_id TEXT,
  ciphertext TEXT,
  payload_bytes INTEGER NOT NULL CHECK(payload_bytes >= 0),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  CHECK((state IN ('superseded','purged')) = (ciphertext IS NULL)),
  CHECK((state = 'claimed') <= (claimed_by_turn_id IS NOT NULL))
)
"""

REQUESTS_DDL = """
CREATE TABLE subagent_resume_requests (
  turn_id TEXT NOT NULL CHECK(length(turn_id) BETWEEN 1 AND 128),
  seq INTEGER NOT NULL CHECK(seq BETWEEN 0 AND 3),
  checkpoint_id TEXT NOT NULL CHECK(length(checkpoint_id) BETWEEN 1 AND 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(turn_id, seq),
  UNIQUE(turn_id, checkpoint_id)
)
"""

INDEXES = (
    "CREATE INDEX idx_subagent_resume_session ON subagent_resume_checkpoints(session_id, user_id, state, created_at)",
    "CREATE INDEX idx_subagent_resume_lineage ON subagent_resume_checkpoints(session_id, run_subagent_id, state)",
    "CREATE INDEX idx_subagent_resume_expiry ON subagent_resume_checkpoints(expires_at) WHERE ciphertext IS NOT NULL",
)

SESSION_DELETE_TRIGGER = """
CREATE TRIGGER trg_subagent_resume_session_delete AFTER UPDATE OF deleted_at ON sessions
WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
BEGIN
  UPDATE subagent_resume_checkpoints SET state='purged', ciphertext=NULL
    WHERE session_id=NEW.id AND ciphertext IS NOT NULL;
END
"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT subagent_resume_checkpoints")
    try:
        names = {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master").fetchall()}
        if "sessions" not in names or "turns" not in names:
            raise RuntimeError("sessions/turns are missing; upgrade through the 1.0.0 baseline first")
        if "subagent_resume_checkpoints" not in names:
            connection.exec_driver_sql(CHECKPOINTS_DDL)
        if "subagent_resume_requests" not in names:
            connection.exec_driver_sql(REQUESTS_DDL)
        for ddl in INDEXES:
            if ddl.split()[2] not in names:
                connection.exec_driver_sql(ddl)
        if "trg_subagent_resume_session_delete" not in names:
            connection.exec_driver_sql(SESSION_DELETE_TRIGGER)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT subagent_resume_checkpoints")
        connection.exec_driver_sql("RELEASE SAVEPOINT subagent_resume_checkpoints")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT subagent_resume_checkpoints")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
