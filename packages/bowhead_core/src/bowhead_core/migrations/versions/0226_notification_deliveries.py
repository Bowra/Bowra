"""Record each email a notification is due to send (notification convergence S3).

One row per notification, recipient and server channel: the system escalation of an item still
waiting after its delay, or a delivery the owner configured (a scheduled result, an urgent task
alert). The delivery worker leases due rows, rechecks the item, membership and the recipient's
switch at send time, retries transient failures and records the outcome code that run history
shows. Addresses are never stored; they are resolved from the verified member list when sending.

Notifications target only; every other target advances its ledger without changes. A retired
account cannot receive a new row, through the same domain guard as ``notification_recipients``.

Revision ID: 0226_notification_deliveries
Revises: 0225_context_material_recovery
"""

from alembic import context

revision = "0226_notification_deliveries"
down_revision = "0225_context_material_recovery"
branch_labels = None
depends_on = None

TABLE = """CREATE TABLE notification_deliveries (
    id                TEXT PRIMARY KEY CHECK(id LIKE 'ndl_%'),
    notification_id   TEXT NOT NULL REFERENCES notification_items(id) ON DELETE CASCADE,
    user_id           TEXT NOT NULL,
    channel           TEXT NOT NULL CHECK(channel IN ('email_escalation', 'email_delivery')),
    status            TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'skipped', 'failed', 'dead')),
    not_before        TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_owner       TEXT,
    lease_expires_at  TEXT,
    outcome_code      TEXT,
    diagnostic_ref    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(notification_id, user_id, channel)
)"""

INDEX = "CREATE INDEX idx_notification_deliveries_due ON notification_deliveries(status, not_before)"
NOTIFICATION_INDEX = "CREATE INDEX idx_notification_deliveries_item ON notification_deliveries(notification_id)"

# The compiled form of the reviewed guard registered in ``sqlite_domain_guards.json``: the account
# deletion ledger lives in core, so the notifications file asks the runtime's domain predicate.
RETIRED_GUARD = (
    'CREATE TRIGGER trg_account_retired_notification_deliveries_insert BEFORE INSERT ON "notification_deliveries" '
    "WHEN bowhead_domain_predicate_v1('trg_account_retired_notification_deliveries_insert',NEW.\"user_id\")=1 "
    "BEGIN SELECT RAISE(ABORT,'account is deleted'); END"
)

STATEMENTS = (TABLE, INDEX, NOTIFICATION_INDEX, RETIRED_GUARD)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "notifications":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT notification_deliveries")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE notification_deliveries")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO notification_deliveries")
        db.exec_driver_sql("RELEASE notification_deliveries")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
