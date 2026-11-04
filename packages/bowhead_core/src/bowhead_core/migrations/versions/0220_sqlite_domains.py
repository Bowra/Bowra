"""Complete the admitted offline domain split and advance every target ledger.

Revision ID: 0220_sqlite_domains
Revises: 0219_retire_unused_tables
"""

from alembic import context

revision = "0220_sqlite_domains"
down_revision = "0219_retire_unused_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    if context.config.attributes.get("bowhead_split_cleanup") is not True:
        raise RuntimeError("SQLite domain cleanup requires the offline coordinator's verified complete-copy barrier")
    from bowhead_core.sqlite_domain_upgrade import cleanup_core

    connection = context.get_bind()
    raw = connection.connection.driver_connection
    if raw.in_transaction:
        raise RuntimeError("SQLite domain cleanup must disable foreign-key actions before its transaction begins")
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("PRAGMA legacy_alter_table=ON")
    raw.execute("BEGIN IMMEDIATE")
    cleanup_core(raw)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
