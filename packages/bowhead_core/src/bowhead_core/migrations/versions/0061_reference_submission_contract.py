"""Expand submission receipt identities for chat-references.v1; no execution writer.

Revision ID: 0061_reference_submission_contract
Revises: 0060_bundle_inventory
Create Date: 2026-09-09

Forward-only copy migration. Preserve live leases, receipts and all old checks.
"""

from __future__ import annotations

from alembic import context

revision = "0061_reference_submission_contract"
down_revision = "0060_bundle_inventory"
branch_labels = None
depends_on = None

_RECEIPTS_DDL = """
        CREATE TABLE chat_submission_receipts_references_new (
          team_id                      TEXT NOT NULL,
          user_id                      TEXT NOT NULL,
          client_submission_id         TEXT NOT NULL,
          workspace_id                 TEXT NOT NULL,
          requested_session_id         TEXT,
          actual_session_id            TEXT,
          turn_id                      TEXT,
          submission_kind              TEXT NOT NULL,
          submission_contract_version  TEXT NOT NULL,
          model_control_batch          TEXT NOT NULL,
          request_fingerprint          TEXT NOT NULL,
          fingerprint_key_id           TEXT NOT NULL,
          auth_session_generation_hash TEXT NOT NULL,
          outcome                      TEXT NOT NULL,
          response_json                TEXT NOT NULL,
          created_at                   TEXT NOT NULL,
          expires_at                   TEXT NOT NULL,
          PRIMARY KEY (team_id, user_id, client_submission_id),
          CHECK (submission_kind IN ('start','replay','steer')),
          CHECK (submission_contract_version IN
            ('chat-submit.v1','chat-model.v2','chat-model-effort.v3','chat-references.v1')),
          CHECK (model_control_batch IN ('a','b','c','references')),
          CHECK ((submission_contract_version='chat-submit.v1') = (model_control_batch='a')),
          CHECK ((submission_contract_version='chat-model.v2') = (model_control_batch='b')),
          CHECK ((submission_contract_version='chat-model-effort.v3') = (model_control_batch='c')),
          CHECK (outcome IN ('started','buffered','cancelled','completed','rejected')),
          CHECK ((submission_contract_version='chat-references.v1') = (model_control_batch='references')),
          CHECK (length(request_fingerprint)=64),
          CHECK (length(fingerprint_key_id)=64),
          CHECK (length(auth_session_generation_hash)=64)
        )
        """

_INFLIGHT_DDL = """
        CREATE TABLE chat_submission_inflight_references_new (
          team_id                      TEXT NOT NULL,
          user_id                      TEXT NOT NULL,
          client_submission_id         TEXT NOT NULL,
          workspace_id                 TEXT NOT NULL,
          requested_session_id         TEXT,
          submission_kind              TEXT NOT NULL,
          submission_contract_version  TEXT NOT NULL,
          model_control_batch          TEXT NOT NULL,
          request_fingerprint          TEXT NOT NULL,
          fingerprint_key_id           TEXT NOT NULL,
          auth_session_generation_hash TEXT NOT NULL,
          lease_owner                  TEXT NOT NULL,
          lease_version                INTEGER NOT NULL,
          lease_expires_at             TEXT NOT NULL,
          created_at                   TEXT NOT NULL,
          PRIMARY KEY (team_id, user_id, client_submission_id),
          CHECK (submission_kind IN ('start','replay','steer')),
          CHECK (submission_contract_version IN
            ('chat-submit.v1','chat-model.v2','chat-model-effort.v3','chat-references.v1')),
          CHECK (model_control_batch IN ('a','b','c','references')),
          CHECK ((submission_contract_version='chat-submit.v1') = (model_control_batch='a')),
          CHECK ((submission_contract_version='chat-model.v2') = (model_control_batch='b')),
          CHECK ((submission_contract_version='chat-model-effort.v3') = (model_control_batch='c')),
          CHECK ((submission_contract_version='chat-references.v1') = (model_control_batch='references')),
          CHECK (length(request_fingerprint)=64),
          CHECK (length(fingerprint_key_id)=64),
          CHECK (length(auth_session_generation_hash)=64),
          CHECK (lease_version>=1)
        )
        """

_COMMON_COLUMNS = (
    "team_id", "user_id", "client_submission_id", "workspace_id", "requested_session_id",
)
_IDENTITY_COLUMNS = (
    "submission_kind", "submission_contract_version", "model_control_batch",
    "request_fingerprint", "fingerprint_key_id", "auth_session_generation_hash",
)
_RECEIPT_COLUMNS = _COMMON_COLUMNS + ("actual_session_id", "turn_id") + _IDENTITY_COLUMNS + (
    "outcome", "response_json", "created_at", "expires_at",
)
_INFLIGHT_COLUMNS = _COMMON_COLUMNS + _IDENTITY_COLUMNS + (
    "lease_owner", "lease_version", "lease_expires_at", "created_at",
)


def _copy_table(connection, *, table: str, columns: tuple[str, ...], ddl: str, expiry: str) -> None:
    actual = tuple(str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_xinfo({table})"))
    if actual != columns:
        raise RuntimeError(f"reference submission columns drift: {table}")
    objects = connection.exec_driver_sql(
        "SELECT type,name FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    if objects != [("index", f"idx_{table}_expiry")]:
        raise RuntimeError(f"reference submission index/trigger drift: {table}")
    index_columns = tuple(str(row[2]) for row in connection.exec_driver_sql(f"PRAGMA index_info(idx_{table}_expiry)"))
    if index_columns != (expiry,):
        raise RuntimeError(f"reference submission expiry index drift: {table}")
    new_table = f"{table}_references_new"
    connection.exec_driver_sql(ddl)
    projection = ",".join(columns)
    connection.exec_driver_sql(f"INSERT INTO {new_table} ({projection}) SELECT {projection} FROM {table}")
    old_count = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
    new_count = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {new_table}").scalar_one()
    changed = connection.exec_driver_sql(
        f"SELECT {projection} FROM {table} EXCEPT SELECT {projection} FROM {new_table}"
    ).first()
    if old_count != new_count or changed is not None:
        raise RuntimeError(f"reference submission copy mismatch: {table}")
    connection.exec_driver_sql(f"DROP TABLE {table}")
    connection.exec_driver_sql(f"ALTER TABLE {new_table} RENAME TO {table}")
    connection.exec_driver_sql(f"CREATE INDEX idx_{table}_expiry ON {table}({expiry})")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    # Also rollback the first copy if validating the second table fails.
    connection.exec_driver_sql("SAVEPOINT reference_submission_upgrade")
    try:
        _copy_table(connection, table="chat_submission_receipts", columns=_RECEIPT_COLUMNS, ddl=_RECEIPTS_DDL, expiry="expires_at")
        _copy_table(connection, table="chat_submission_inflight", columns=_INFLIGHT_COLUMNS, ddl=_INFLIGHT_DDL, expiry="lease_expires_at")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_submission_upgrade")
        connection.exec_driver_sql("RELEASE SAVEPOINT reference_submission_upgrade")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT reference_submission_upgrade")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
