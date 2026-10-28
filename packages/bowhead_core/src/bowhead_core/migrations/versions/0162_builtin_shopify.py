"""Admit Shopify connections and fence in-flight reads by connection revision."""

from __future__ import annotations

from alembic import context

revision = "0162_builtin_shopify"
down_revision = "0161_ordinary_shared_outcome"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT builtin_shopify")
    try:
        db.exec_driver_sql(
            """CREATE TABLE team_builtin_tool_shopify (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
              provider TEXT NOT NULL CHECK(provider IN ('tavily','datarc','google_workspace','powerbi','shopify')),
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
              connection_revision INTEGER NOT NULL DEFAULT 1 CHECK(connection_revision > 0),
              UNIQUE(team_id, provider, display_name)
            )"""
        )
        columns = (
            "id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,"
            "scope_state,scope_json,scope_fingerprint,scope_checked_at,config_json,created_by,created_at,updated_at"
        )
        db.exec_driver_sql(f"INSERT INTO team_builtin_tool_shopify({columns}) SELECT {columns} FROM team_builtin_tool")  # noqa: S608
        old_count = db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool").scalar_one()
        if db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool_shopify").scalar_one() != old_count:
            raise RuntimeError("Built-in connection copy mismatch")
        children = ("builtin_tool_user_grant", "builtin_tool_oauth_state", "builtin_pbi_template")
        for table in children:
            db.exec_driver_sql(f"CREATE TEMP TABLE shopify_backup_{table} AS SELECT * FROM {table}")  # noqa: S608
        db.exec_driver_sql("DROP TABLE team_builtin_tool")
        db.exec_driver_sql("ALTER TABLE team_builtin_tool_shopify RENAME TO team_builtin_tool")
        db.exec_driver_sql("CREATE INDEX idx_team_builtin_tool_team ON team_builtin_tool(team_id,status)")
        for table in children:
            db.exec_driver_sql(f"INSERT OR IGNORE INTO {table} SELECT * FROM shopify_backup_{table}")  # noqa: S608
            if db.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one() != db.exec_driver_sql(  # noqa: S608
                f"SELECT COUNT(*) FROM shopify_backup_{table}"  # noqa: S608
            ).scalar_one():
                raise RuntimeError("Built-in connection child copy mismatch")
            db.exec_driver_sql(f"DROP TABLE shopify_backup_{table}")  # noqa: S608
        db.exec_driver_sql(
            """CREATE TRIGGER trg_builtin_connection_revision AFTER UPDATE ON team_builtin_tool
               WHEN NEW.connection_revision = OLD.connection_revision
               BEGIN UPDATE team_builtin_tool SET connection_revision=OLD.connection_revision+1 WHERE id=NEW.id; END"""
        )
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_shopify")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT builtin_shopify")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_shopify")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore matching snapshots.")
