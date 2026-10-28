"""Access-request lifecycle columns (withdraw / expire / shadow rows / decision note).

Adds the persisted fields the access-request expansion needs on the 1.0.0
``access_requests`` table: the approver's note, the pending-request expiry, the
granted permission and optional grant expiry, the requester cooldown after a
denial, the 72h escalation marker, the withdrawal time, the Agent release
candidate an approval is waiting on (``release_candidate_id``), the reserved
``requested_role`` / ``target`` fields for later role kinds, and the ``shadow``
marker for requests whose resource could not be resolved (anti-enumeration).
Plus ``idx_acr_pending_expiry`` for the hourly expiry sweep.

``access_requests`` is also created by ``AccessRequestStore.initialize()`` (1.0.0
startup DDL), so this revision follows the 0011/0016 dual-ownership exception: the
Store SCHEMA guard and this revision add the same columns in the same order and
both sides are idempotent (PRAGMA-guarded). Existing rows keep ``shadow=0`` and
NULL for every new column, which is exactly the pre-expansion behavior.

Revision ID: 0126_access_request_lifecycle
Revises: 0125_reference_lead_capability
"""

from __future__ import annotations

from alembic import context

revision = "0126_access_request_lifecycle"
down_revision = "0125_reference_lead_capability"
branch_labels = None
depends_on = None

# Frozen at this revision; keep in the same order as AccessRequestStore._LIFECYCLE_COLUMNS.
_COLUMNS = (
    ("decision_note", "TEXT"),
    ("expires_at", "TEXT"),
    ("requested_role", "TEXT"),
    ("granted_permission", "TEXT"),
    ("withdrawn_at", "TEXT"),
    ("target", "TEXT"),
    ("shadow", "INTEGER NOT NULL DEFAULT 0"),
    ("grant_expires_at", "TEXT"),
    ("cooldown_until", "TEXT"),
    ("escalated_at", "TEXT"),
    ("release_candidate_id", "TEXT"),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    columns = {str(row[1]) for row in db.exec_driver_sql("PRAGMA table_info(access_requests)").fetchall()}
    if not columns:
        return
    for name, ddl in _COLUMNS:
        if name not in columns:
            db.exec_driver_sql(f"ALTER TABLE access_requests ADD COLUMN {name} {ddl}")
    db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_acr_pending_expiry ON access_requests(status, expires_at)")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
