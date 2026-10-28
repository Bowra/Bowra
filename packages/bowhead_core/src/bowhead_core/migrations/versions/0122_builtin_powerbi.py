"""Move Service Principal Power BI connections into the built-in tool model.

Older databases can still hold the incompatible delegated-token registry at
``pbi_connection``. Those rows cannot be used as Service Principal credentials;
discard them and require administrators to configure a new connection.
"""

from __future__ import annotations

from alembic import context

revision = "0122_builtin_powerbi"
down_revision = "0121_kb_maintenance"
branch_labels = None
depends_on = None

_SERVICE_PRINCIPAL_COLUMNS = {
    "id", "team_id", "name", "auth_ref", "secret_hint", "status", "created_at", "updated_at"
}
_DELEGATED_COLUMNS = {
    "id", "team_id", "name", "workspace_id", "dataset_id", "token_hint", "status", "created_at", "updated_at"
}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT builtin_powerbi")
    try:
        db.exec_driver_sql(
            """CREATE TABLE team_builtin_tool_new (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
              provider TEXT NOT NULL CHECK(provider IN ('tavily','datarc','google_workspace','powerbi')),
              display_name TEXT NOT NULL COLLATE NOCASE,
              endpoint TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'disabled' CHECK(status IN ('enabled','disabled')),
              auth_ref TEXT NOT NULL,
              key_hint TEXT,
              scope_state TEXT NOT NULL DEFAULT 'unknown' CHECK(scope_state IN ('unknown','verified','invalid')),
              scope_json TEXT,
              scope_fingerprint TEXT,
              scope_checked_at TEXT,
              config_json TEXT,
              created_by TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(team_id, provider, display_name)
            )"""
        )
        columns = (
            "id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,"
            "scope_state,scope_json,scope_fingerprint,scope_checked_at,config_json,created_by,created_at,updated_at"
        )
        db.exec_driver_sql(f"INSERT INTO team_builtin_tool_new({columns}) SELECT {columns} FROM team_builtin_tool")  # noqa: S608
        old_count = db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool").scalar_one()
        if db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool_new").scalar_one() != old_count:
            raise RuntimeError("team_builtin_tool copy mismatch")
        pbi_columns = {str(row[1]) for row in db.exec_driver_sql("PRAGMA table_info(pbi_connection)").fetchall()}
        service_principal = _SERVICE_PRINCIPAL_COLUMNS <= pbi_columns and "token_hint" not in pbi_columns
        delegated = _DELEGATED_COLUMNS <= pbi_columns and "secret_hint" not in pbi_columns
        if not service_principal and not delegated:
            raise RuntimeError("unsupported pbi_connection schema at revision 0122")
        if service_principal:
            seen: set[tuple[str, str]] = set()
            connections = db.exec_driver_sql(
                "SELECT id,team_id,name,auth_ref,secret_hint,status,created_at,updated_at FROM pbi_connection ORDER BY team_id,created_at,id"
            ).mappings().all()
            for row in connections:
                name = row["name"]
                suffix = 0
                while (row["team_id"], name.casefold()) in seen:
                    suffix += 1
                    addition = row["id"] if suffix == 1 else f"{row['id']}-{suffix}"
                    name = f"{row['name']} ({addition})"
                seen.add((row["team_id"], name.casefold()))
                db.exec_driver_sql(
                    """INSERT INTO team_builtin_tool_new
                       (id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,created_at,updated_at)
                       VALUES (?,?, 'powerbi',?, '',?,?,?,?,?)""",
                    (
                        row["id"], row["team_id"], name,
                        "enabled" if row["status"] == "active" else "disabled",
                        row["auth_ref"], row["secret_hint"], row["created_at"], row["updated_at"],
                    ),
                )
        # SQLite may have foreign-key enforcement enabled during an upgrade.
        # Preserve Google Workspace grants and in-flight OAuth state when the
        # old parent table is replaced (DROP TABLE can cascade those rows).
        for table in ("builtin_tool_user_grant", "builtin_tool_oauth_state"):
            db.exec_driver_sql(f"CREATE TEMP TABLE backup_{table} AS SELECT * FROM {table}")  # noqa: S608
        db.exec_driver_sql("DROP TABLE team_builtin_tool")
        db.exec_driver_sql("ALTER TABLE team_builtin_tool_new RENAME TO team_builtin_tool")
        db.exec_driver_sql("CREATE INDEX idx_team_builtin_tool_team ON team_builtin_tool(team_id,status)")
        for table in ("builtin_tool_user_grant", "builtin_tool_oauth_state"):
            db.exec_driver_sql(f"INSERT OR IGNORE INTO {table} SELECT * FROM backup_{table}")  # noqa: S608
            db.exec_driver_sql(f"DROP TABLE backup_{table}")  # noqa: S608
        db.exec_driver_sql(
            """CREATE TABLE builtin_pbi_template (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL,
              tool_id TEXT NOT NULL REFERENCES team_builtin_tool(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              dax_template TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(tool_id,name)
            )"""
        )
        db.exec_driver_sql("CREATE INDEX idx_builtin_pbi_template_tool ON builtin_pbi_template(team_id,tool_id)")
        if service_principal:
            db.exec_driver_sql(
                """INSERT INTO builtin_pbi_template(id,team_id,tool_id,name,dax_template,created_at,updated_at)
                   SELECT id,team_id,connection_id,name,dax_template,created_at,updated_at FROM pbi_dax_template"""
            )
            if db.exec_driver_sql("SELECT COUNT(*) FROM builtin_pbi_template").scalar_one() != db.exec_driver_sql(
                "SELECT COUNT(*) FROM pbi_dax_template"
            ).scalar_one():
                raise RuntimeError("Power BI template copy mismatch")
        db.exec_driver_sql("DROP TABLE pbi_dax_template")
        db.exec_driver_sql("DROP TABLE pbi_connection")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_powerbi")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT builtin_powerbi")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_powerbi")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore matching snapshots.")
