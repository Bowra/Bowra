"""Track post-commit cleanup for quarantined Skill bundles.

Revision ID: 0055_resource_file_cleanup_receipt
Revises: 0054_resource_change_guard
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import context

revision = "0055_resource_file_cleanup_receipt"
down_revision = "0054_resource_change_guard"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "resource_file_cleanup_receipt",
        """
        CREATE TABLE resource_file_cleanup_receipt (
          cleanup_id        TEXT PRIMARY KEY,
          team_id           TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          actor_user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          idempotency_key   TEXT NOT NULL,
          resource_kind     TEXT NOT NULL CHECK(resource_kind IN ('skill','team_skill')),
          resource_id       TEXT NOT NULL CHECK(length(resource_id)>0),
          original_path     TEXT NOT NULL CHECK(length(original_path)>0),
          quarantine_path   TEXT NOT NULL UNIQUE CHECK(length(quarantine_path)>0),
          state             TEXT NOT NULL CHECK(state IN ('pending','completed','restore_failed')),
          error_code        TEXT,
          created_at        TEXT NOT NULL CHECK(length(created_at)>0),
          updated_at        TEXT NOT NULL CHECK(length(updated_at)>0),
          FOREIGN KEY(team_id,actor_user_id,idempotency_key)
            REFERENCES resource_change_receipt(team_id,actor_user_id,idempotency_key)
            ON DELETE CASCADE
        )
        """,
    ),
    (
        "index",
        "idx_resource_file_cleanup_pending",
        """
        CREATE INDEX idx_resource_file_cleanup_pending
        ON resource_file_cleanup_receipt(state,created_at)
        """,
    ),
)


def _normalized_sql(value: str) -> str:
    return "".join(value.lower().split())


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for kind, name, ddl in _OBJECTS:
        current = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
            (kind, name),
        ).scalar()
        if current is None:
            connection.exec_driver_sql(ddl)
        elif _normalized_sql(str(current)) != _normalized_sql(ddl):
            raise RuntimeError(f"Resource file cleanup schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain resource file cleanup receipts.")
