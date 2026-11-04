"""Add a sealed core-ledger witness for independently restored execution settlements.

Revision ID: 0218_billing_witness
Revises: 0217_probe_diagnostics
"""

from alembic import context

revision = "0218_billing_witness"
down_revision = "0217_probe_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(turn_effect_settlements)")}
    if "billing_witness_ciphertext" not in columns:
        connection.exec_driver_sql("ALTER TABLE turn_effect_settlements ADD COLUMN billing_witness_ciphertext TEXT")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
