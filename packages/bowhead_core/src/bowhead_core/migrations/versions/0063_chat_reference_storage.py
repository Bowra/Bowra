"""Reference plan, sealed material and accepted queue source storage.

Revision ID: 0063_chat_reference_storage
Revises: 0062_feedback_casework
Create Date: 2026-09-09

Only storage readers/primitives; no production reference admission or queue adapter.
"""

from __future__ import annotations

from alembic import context

revision = "0063_chat_reference_storage"
down_revision = "0062_feedback_casework"
branch_labels = None
depends_on = None


DDL = (
    """
    CREATE TABLE chat_reference_plans (
      turn_id TEXT NOT NULL PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      source_turn_id TEXT NOT NULL REFERENCES turns(id),
      logical_source_turn_id TEXT NOT NULL REFERENCES turns(id),
      snapshot_owner_turn_id TEXT NOT NULL REFERENCES turns(id),
      source_hash TEXT NOT NULL CHECK(length(source_hash)=71 AND substr(source_hash,1,7)='sha256:'),
      plan_json TEXT NOT NULL CHECK(COALESCE(json_valid(plan_json) AND json_type(plan_json)='object'
        AND json_type(plan_json,'$.schema_version')='integer' AND json_extract(plan_json,'$.schema_version')=1
        AND json_extract(plan_json,'$.source_turn_id')=source_turn_id
        AND json_extract(plan_json,'$.logical_source_turn_id')=logical_source_turn_id
        AND json_extract(plan_json,'$.snapshot_owner_turn_id')=snapshot_owner_turn_id
        AND json_extract(plan_json,'$.source_hash')=source_hash
        AND length(CAST(plan_json AS BLOB))<=1048576,0)),
      created_policy_epoch INTEGER NOT NULL CHECK(created_policy_epoch>=0),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_chat_reference_plans_logical_source ON chat_reference_plans(logical_source_turn_id)",
    "CREATE INDEX idx_chat_reference_plans_expiry ON chat_reference_plans(retention_state,expires_at)",
    """
    CREATE TABLE chat_reference_content_snapshots (
      turn_id TEXT NOT NULL REFERENCES chat_reference_plans(turn_id) ON DELETE CASCADE,
      content_key TEXT NOT NULL,
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      descriptor_json TEXT NOT NULL CHECK(COALESCE(json_valid(descriptor_json) AND json_type(descriptor_json)='object'
        AND json_type(descriptor_json,'$.schema_version')='integer' AND json_extract(descriptor_json,'$.schema_version')=1
        AND json_extract(descriptor_json,'$.content_key')=content_key
        AND json_extract(descriptor_json,'$.content_hash')=content_hash
        AND json_type(descriptor_json,'$.byte_count')='integer' AND json_extract(descriptor_json,'$.byte_count')=byte_count
        AND length(CAST(descriptor_json AS BLOB))<=65536,0)),
      content_hash TEXT NOT NULL CHECK(length(content_hash)=71 AND substr(content_hash,1,7)='sha256:'),
      byte_count INTEGER NOT NULL CHECK(byte_count>=0 AND byte_count<=1048576),
      ciphertext TEXT CHECK(length(CAST(ciphertext AS BLOB))<=2097152),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      created_generation INTEGER NOT NULL CHECK(created_generation>=1),
      created_policy_epoch INTEGER NOT NULL CHECK(created_policy_epoch>=0),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      PRIMARY KEY(turn_id,content_key),
      CHECK((retention_state='available' AND ciphertext IS NOT NULL) OR (retention_state='purged' AND ciphertext IS NULL))
    )
    """,
    "CREATE INDEX idx_chat_reference_content_expiry ON chat_reference_content_snapshots(retention_state,expires_at)",
    """
    CREATE TABLE chat_reference_queued_inputs (
      team_id TEXT NOT NULL REFERENCES teams(id),
      user_id TEXT NOT NULL REFERENCES users(id),
      client_submission_id TEXT NOT NULL,
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES workspaces(id),
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      accepted_parent_turn_id TEXT NOT NULL REFERENCES turns(id),
      accepted_generation INTEGER NOT NULL CHECK(accepted_generation>=1),
      payload_hash TEXT NOT NULL CHECK(length(payload_hash)=71 AND substr(payload_hash,1,7)='sha256:'),
      ciphertext TEXT CHECK(length(CAST(ciphertext AS BLOB))<=2097152),
      state TEXT NOT NULL CHECK(state IN ('queued','materialized','cancelled')),
      row_version INTEGER NOT NULL CHECK(row_version>=1),
      materialized_turn_id TEXT REFERENCES turns(id),
      cancel_reason TEXT CHECK(cancel_reason IN ('user_cancelled','source_deleted','retention_expired')),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      PRIMARY KEY(team_id,user_id,client_submission_id),
      CHECK(materialized_turn_id IS NULL OR materialized_turn_id<>accepted_parent_turn_id),
      CHECK((state='queued' AND materialized_turn_id IS NULL AND cancel_reason IS NULL)
        OR (state='materialized' AND materialized_turn_id IS NOT NULL AND cancel_reason IS NULL)
        OR (state='cancelled' AND materialized_turn_id IS NULL AND cancel_reason IS NOT NULL)),
      CHECK((retention_state='available' AND ciphertext IS NOT NULL)
        OR (retention_state='purged' AND ciphertext IS NULL AND state<>'queued'))
    )
    """,
    "CREATE INDEX idx_chat_reference_queue_session ON chat_reference_queued_inputs(session_id,state,created_at)",
    "CREATE INDEX idx_chat_reference_queue_expiry ON chat_reference_queued_inputs(retention_state,expires_at)",
    "CREATE UNIQUE INDEX idx_chat_reference_queue_materialized ON chat_reference_queued_inputs(materialized_turn_id) WHERE materialized_turn_id IS NOT NULL",
)


# Identity/content can never be replaced, including INSERT OR REPLACE. Physical
# deletion remains available to the existing parent/session retention lifecycle.
IMMUTABLE_COLUMNS = {
    "chat_reference_plans": (
        "turn_id",
        "schema_version",
        "source_turn_id",
        "logical_source_turn_id",
        "snapshot_owner_turn_id",
        "source_hash",
        "plan_json",
        "created_policy_epoch",
        "created_at",
        "expires_at",
    ),
    "chat_reference_content_snapshots": (
        "turn_id",
        "content_key",
        "schema_version",
        "descriptor_json",
        "content_hash",
        "byte_count",
        "created_generation",
        "created_policy_epoch",
        "created_at",
        "expires_at",
    ),
    "chat_reference_queued_inputs": (
        "team_id",
        "user_id",
        "client_submission_id",
        "session_id",
        "workspace_id",
        "schema_version",
        "accepted_parent_turn_id",
        "accepted_generation",
        "payload_hash",
        "created_at",
        "expires_at",
    ),
}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for statement in DDL:
        connection.exec_driver_sql(statement)
    for table, columns in IMMUTABLE_COLUMNS.items():
        changed = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
        changed += " OR (OLD.retention_state='purged' AND NEW.retention_state<>'purged')"
        if table != "chat_reference_plans":
            changed += " OR (NEW.ciphertext IS NOT OLD.ciphertext AND NOT (OLD.retention_state='available' AND NEW.retention_state='purged' AND NEW.ciphertext IS NULL))"
        if table == "chat_reference_queued_inputs":
            changed += " OR NEW.row_version<>OLD.row_version+1"
            changed += " OR (NEW.state IS NOT OLD.state AND NOT (OLD.state='queued' AND NEW.state IN ('materialized','cancelled')))"
            changed += " OR (OLD.state<>'queued' AND (NEW.materialized_turn_id IS NOT OLD.materialized_turn_id OR NEW.cancel_reason IS NOT OLD.cancel_reason))"
        connection.exec_driver_sql(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE ON {table} WHEN {changed} BEGIN SELECT RAISE(ABORT,'reference record is immutable'); END"
        )
        keys = (
            ("team_id", "user_id", "client_submission_id")
            if table.endswith("queued_inputs")
            else (("turn_id", "content_key") if table.endswith("snapshots") else ("turn_id",))
        )
        match = " AND ".join(f"{key}=NEW.{key}" for key in keys)
        connection.exec_driver_sql(
            f"CREATE TRIGGER trg_{table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM {table} WHERE {match}) BEGIN SELECT RAISE(ABORT,'reference record already exists'); END"
        )


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
