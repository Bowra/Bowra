"""Retire the billing role; existing memberships and invitations become members.

Keep the immutable baseline CHECK and narrow writes with triggers, avoiding a
rebuild of team_members referenced by cross-table triggers. Audit history stays
unchanged. No live account or Space membership is removed.
"""

from __future__ import annotations

from alembic import context

revision = "0097_retire_billing_admin"
down_revision = "0096_artifact_workspace_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT retire_billing_role")
    try:
        # Invalidate existing authorization snapshots before changing membership.
        connection.exec_driver_sql("""
            UPDATE terminal_auth_scope_epochs SET epoch=epoch+1, row_version=row_version+1
            WHERE scope_kind='team_membership' AND team_id IN (
                SELECT team_id FROM team_members WHERE role='billing_admin'
            )
        """)
        connection.exec_driver_sql("UPDATE team_members SET role='member' WHERE role='billing_admin'")
        connection.exec_driver_sql("UPDATE team_invite_tokens SET team_role='member' WHERE team_role='billing_admin'")
        # Tokens are not currently accepted for gateway authentication, but must
        # not retain elevated snapshots if that entry is enabled in the future.
        connection.exec_driver_sql("UPDATE api_tokens SET role_snapshot='member' WHERE role_snapshot='billing_admin'")
        for table, column in (("team_members", "role"), ("team_invite_tokens", "team_role")):
            for operation in ("INSERT", "UPDATE"):
                connection.exec_driver_sql(f"""
                    CREATE TRIGGER IF NOT EXISTS trg_{table}_supported_role_{operation.lower()}
                    BEFORE {operation} ON {table}
                    WHEN NEW.{column} NOT IN ('team_admin','audit_admin','member')
                    BEGIN SELECT RAISE(ABORT, 'unsupported_team_role'); END
                """)
        for operation in ("INSERT", "UPDATE"):
            connection.exec_driver_sql(f"""
                CREATE TRIGGER IF NOT EXISTS trg_api_tokens_retired_role_{operation.lower()}
                BEFORE {operation} ON api_tokens WHEN NEW.role_snapshot='billing_admin'
                BEGIN SELECT RAISE(ABORT, 'unsupported_team_role'); END
            """)
        connection.exec_driver_sql("RELEASE SAVEPOINT retire_billing_role")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT retire_billing_role")
        connection.exec_driver_sql("RELEASE SAVEPOINT retire_billing_role")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
