"""Add the durable legacy-clarification invalidation outbox.

The clarification row remains the source of truth.  This table only stores the
delivery intent that is committed in the same transaction as a legacy
``pending -> dismissed|expired`` transition.

Revision ID: 0017_clarification_invalidation_outbox
Revises: 0016_plan_replan
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import context

revision = "0017_clarification_invalidation_outbox"
down_revision = "0016_plan_replan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS clarification_invalidation_outbox (
          request_id       TEXT PRIMARY KEY,
          turn_id          TEXT NOT NULL,
          event_id         TEXT NOT NULL UNIQUE,
          target_status    TEXT NOT NULL,
          reason_code      TEXT NOT NULL,
          actor_kind       TEXT NOT NULL,
          state            TEXT NOT NULL DEFAULT 'pending',
          lease_owner      TEXT,
          lease_expires_at TEXT,
          attempt_count    INTEGER NOT NULL DEFAULT 0,
          created_at       TEXT NOT NULL,
          updated_at       TEXT NOT NULL,
          delivered_at     TEXT,
          CHECK (target_status IN ('dismissed','expired')),
          CHECK (actor_kind IN ('user','system')),
          CHECK (state IN ('pending','delivered')),
          CHECK (attempt_count >= 0),
          CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
          CHECK (
            (state='pending' AND delivered_at IS NULL)
            OR
            (state='delivered' AND delivered_at IS NOT NULL
              AND lease_owner IS NULL AND lease_expires_at IS NULL)
          )
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_clarification_invalidation_outbox_pending
        ON clarification_invalidation_outbox(state, lease_expires_at, created_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_clarification_invalidation_outbox_turn_claim
        ON clarification_invalidation_outbox(turn_id, state, lease_expires_at)
        """,
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
