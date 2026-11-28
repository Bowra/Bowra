"""Scope Teams identities and persist personal chat delivery progress in core."""

from alembic import context

revision = "0235_teams_personal_chat"
down_revision = "0234_owner_draft_trial"
branch_labels = depends_on = None


def upgrade_personal_chat(execute):
    execute("SAVEPOINT teams_personal_chat")
    try:
        # Preserve the installed table's checks, foreign keys, indexes and retirement guards.
        for table, old, new in (
            ("teams_connections", "UNIQUE(microsoft_app_id, entra_tenant_id),", ""),
            ("teams_identity_bindings", "PRIMARY KEY(entra_tenant_id, entra_oid)", "PRIMARY KEY(entra_tenant_id, entra_oid, team_id)"),
            ("teams_workspace_bindings", "PRIMARY KEY(entra_tenant_id, conversation_id, entra_oid)",
             "team_id TEXT NOT NULL, PRIMARY KEY(entra_tenant_id, conversation_id, entra_oid, team_id)"),
        ):
            ddl = execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
            if ddl.count(old) != 1:
                raise RuntimeError(f"Unexpected existing constraint for {table}")
            replacement = ddl.replace(old, new)
            columns = [r[1] for r in execute(f"PRAGMA table_info({table})").fetchall()]
            names = ",".join(columns)
            triggers = execute("SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,)).fetchall()
            execute(f"CREATE TEMP TABLE previous_teams_rows AS SELECT * FROM {table}")  # noqa: S608
            execute(f"DROP TABLE {table}")
            execute(replacement)
            select = names
            if table == "teams_workspace_bindings":
                names += ",team_id"
                select += ",(SELECT team_id FROM workspaces WHERE id=previous_teams_rows.workspace_id)"
            execute(f"INSERT INTO {table}({names}) SELECT {select} FROM previous_teams_rows")  # noqa: S608
            execute("DROP TABLE previous_teams_rows")
            for (statement,) in triggers:
                execute(statement)
        execute("CREATE UNIQUE INDEX idx_teams_registered_ids ON teams_connections(microsoft_app_id,entra_tenant_id) "
                "WHERE microsoft_app_id!='' AND entra_tenant_id!=''")
        execute("ALTER TABLE teams_identity_bindings ADD COLUMN confirmed_at TEXT")
        for column in (
            "claim_owner TEXT", "claim_until INTEGER", "delivery TEXT", "delivery_user_id TEXT",
            "delivery_seq INTEGER NOT NULL DEFAULT -1", "delivery_due INTEGER", "delivery_owner TEXT",
        ):
            execute(f"ALTER TABLE teams_activity_dedup ADD COLUMN {column}")
        execute("CREATE INDEX idx_teams_delivery_due ON teams_activity_dedup(delivery_due) WHERE delivery_due IS NOT NULL")
        execute("RELEASE teams_personal_chat")
    except BaseException:
        execute("ROLLBACK TO teams_personal_chat")
        execute("RELEASE teams_personal_chat")
        raise


def upgrade():
    if context.config.attributes["bowhead_target"] == "core":
        upgrade_personal_chat(context.get_bind().exec_driver_sql)


def downgrade():
    pass
