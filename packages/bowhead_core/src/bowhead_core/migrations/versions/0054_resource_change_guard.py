"""Add receipts and server-side candidates for protected resource changes.

Revision ID: 0054_resource_change_guard
Revises: 0053_agent_role_preference
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import context

revision = "0054_resource_change_guard"
down_revision = "0053_agent_role_preference"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "resource_change_receipt",
        """
        CREATE TABLE resource_change_receipt (
          team_id          TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          actor_user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          idempotency_key  TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 16 AND 128),
          resource_kind    TEXT NOT NULL CHECK(resource_kind IN ('skill','team_skill','tool_source')),
          resource_id      TEXT NOT NULL CHECK(length(resource_id)>0),
          action           TEXT NOT NULL CHECK(length(action)>0),
          request_digest   TEXT NOT NULL CHECK(request_digest LIKE 'sha256:%'),
          impact_digest    TEXT NOT NULL CHECK(impact_digest LIKE 'sha256:%'),
          response_json    TEXT NOT NULL CHECK(length(response_json)<=16384),
          created_at       TEXT NOT NULL CHECK(length(created_at)>0),
          PRIMARY KEY(team_id,actor_user_id,idempotency_key)
        ) WITHOUT ROWID
        """,
    ),
    (
        "index",
        "idx_resource_change_receipt_created",
        """
        CREATE INDEX idx_resource_change_receipt_created
        ON resource_change_receipt(created_at)
        """,
    ),
    (
        "table",
        "resource_refresh_candidate",
        """
        CREATE TABLE resource_refresh_candidate (
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          actor_user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          resource_id           TEXT NOT NULL REFERENCES team_tool_source(id) ON DELETE CASCADE,
          candidate_digest      TEXT NOT NULL CHECK(candidate_digest LIKE 'sha256:%'),
          resource_state_digest TEXT NOT NULL CHECK(resource_state_digest LIKE 'sha256:%'),
          candidate_json        TEXT NOT NULL CHECK(length(candidate_json)<=2097152),
          expires_at            TEXT NOT NULL CHECK(length(expires_at)>0),
          created_at            TEXT NOT NULL CHECK(length(created_at)>0),
          PRIMARY KEY(team_id,actor_user_id,resource_id)
        ) WITHOUT ROWID
        """,
    ),
    (
        "index",
        "idx_resource_refresh_candidate_expiry",
        """
        CREATE INDEX idx_resource_refresh_candidate_expiry
        ON resource_refresh_candidate(expires_at)
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
            raise RuntimeError(f"Resource change guard schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain resource change receipts.")
