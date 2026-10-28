"""Mark access requests written by the legacy (flag-off) intake.

``legacy_origin`` lets the access-request expansion hide those rows from the requester's
own views (``scope=mine`` and ``/status``): a legacy row exists only for resources that
resolved, so showing it would reveal that the resource existed. Approvers still see and
decide them.

Existing rows without ``expires_at`` that are not shadow placeholders were written by the
legacy intake, so they are marked here (deterministic, no time or external reads).

Assumption: ``FEATURE_ACCESS_REQUEST_EXPANSION`` was never enabled between 0126 and 0127.
The 0126-era sweeper gave legacy pending rows an ``expires_at`` on its first run, which would
make them indistinguishable here; the rollout register forbids enabling the flag before the
frontend exists, and no deployment has. From 0127 on legacy rows never get an expiry, so the
expansion sweeper re-applies the same rule (``AccessRequestStore.mark_legacy_rows``) and also
covers rows written by an image that predates this column.

``access_requests`` stays dual-owned (0011/0016 exception, see 0126): the Store guard adds
the same column; both sides are idempotent.

Revision ID: 0127_access_request_legacy_origin
Revises: 0126_access_request_lifecycle
"""

from __future__ import annotations

from alembic import context

revision = "0127_access_request_legacy_origin"
down_revision = "0126_access_request_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    columns = {str(row[1]) for row in db.exec_driver_sql("PRAGMA table_info(access_requests)").fetchall()}
    if not columns:
        return
    if "legacy_origin" not in columns:
        db.exec_driver_sql("ALTER TABLE access_requests ADD COLUMN legacy_origin INTEGER NOT NULL DEFAULT 0")
    db.exec_driver_sql("UPDATE access_requests SET legacy_origin=1 WHERE expires_at IS NULL AND shadow=0 AND legacy_origin=0")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
