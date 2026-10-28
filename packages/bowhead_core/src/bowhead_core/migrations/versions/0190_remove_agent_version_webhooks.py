"""Remove Agent version webhooks: drop their subscription, event and delivery tables.

Webhooks were removed from the product (design agent-release-beginner-experience §10). The tables
created by ``0075_agent_version_operations`` are dropped in core with their indexes and triggers;
automation grants and their stored scopes stay (a stored ``webhook:receive`` scope is ignored).
"""

from alembic import context

revision = "0190_remove_agent_version_webhooks"
down_revision = "0189_reference_primary_continuation"
branch_labels = None
depends_on = None

# Children before parents: deliveries reference subscriptions and events.
_TABLES = ("agent_version_webhook_delivery", "agent_version_webhook_event", "agent_version_webhook_subscription")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT remove_agent_version_webhooks")
    try:
        # DROP TABLE removes each table's indexes and triggers; its implicit delete fires no trigger.
        for table in _TABLES:
            db.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        tables = {row[0] for row in db.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "account_deletion_rows" in tables:
            db.exec_driver_sql(
                "DELETE FROM account_deletion_rows WHERE table_name IN (?,?,?)",
                _TABLES,
            )
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT remove_agent_version_webhooks")
        db.exec_driver_sql("RELEASE SAVEPOINT remove_agent_version_webhooks")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT remove_agent_version_webhooks")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
