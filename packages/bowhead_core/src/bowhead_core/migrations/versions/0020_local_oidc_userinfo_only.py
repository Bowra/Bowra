"""Use UserInfo-only local OAuth identity resolution.

Revision ID: 0020_local_oidc_userinfo_only
Revises: 0019_local_oidc_sso
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import context

revision = "0020_local_oidc_userinfo_only"
down_revision = "0019_local_oidc_sso"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()

    # A configuration without UserInfo cannot complete the new login flow.
    # Preserve its remaining values for the owner to amend, but never leave it
    # active after this contract change.
    connection.exec_driver_sql(
        "UPDATE local_oidc_configs SET status='disabled' WHERE userinfo_endpoint IS NULL OR trim(userinfo_endpoint)=''",
    )
    for column in ("issuer", "jwks_uri", "allowed_algs_json", "last_tested_at", "last_test_result"):
        connection.exec_driver_sql(f"ALTER TABLE local_oidc_configs DROP COLUMN {column}")
    for column in ("nonce_hash", "nonce_encrypted"):
        connection.exec_driver_sql(f"ALTER TABLE oidc_login_transactions DROP COLUMN {column}")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
