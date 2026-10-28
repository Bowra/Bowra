"""Owner-isolated remote MCP connections, exact bindings and call confirmations."""

from alembic import context

revision = "0100_personal_mcp"
down_revision = "0099_artifact_workspace_cutover"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE IF NOT EXISTS personal_mcp_connection (
      id TEXT PRIMARY KEY CHECK(id LIKE 'pcm_%'), owner_user_id TEXT NOT NULL,
      name TEXT NOT NULL, endpoint TEXT NOT NULL,
      auth_type TEXT NOT NULL CHECK(auth_type IN ('none','bearer','api_key')),
      auth_cipher TEXT NOT NULL, status TEXT NOT NULL,
      version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS personal_mcp_owner ON personal_mcp_connection(owner_user_id)",
    """CREATE TABLE IF NOT EXISTS personal_mcp_tool (
      id TEXT PRIMARY KEY CHECK(id LIKE 'pmt_%'), connection_id TEXT NOT NULL
        REFERENCES personal_mcp_connection(id), remote_name TEXT NOT NULL,
      description TEXT NOT NULL, schema_json TEXT NOT NULL, schema_digest TEXT NOT NULL,
      risk TEXT NOT NULL CHECK(risk IN ('unknown','write','read')),
      selected INTEGER NOT NULL CHECK(selected IN (0,1)),
      UNIQUE(connection_id, remote_name)
    )""",
    """CREATE TABLE IF NOT EXISTS personal_mcp_binding (
      agent_id TEXT NOT NULL, revision_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
      tool_ids_json TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
      PRIMARY KEY(agent_id,revision_id)
    )""",
    """CREATE TABLE IF NOT EXISTS personal_mcp_manifest (
      turn_id TEXT NOT NULL, execution_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
      agent_id TEXT NOT NULL, revision_id TEXT NOT NULL, binding_version INTEGER NOT NULL,
      tools_json TEXT NOT NULL, PRIMARY KEY(turn_id,execution_id)
    )""",
    """CREATE TABLE IF NOT EXISTS personal_mcp_invocation (
      id TEXT PRIMARY KEY CHECK(id LIKE 'pmi_%'), owner_user_id TEXT NOT NULL,
      turn_id TEXT NOT NULL, execution_id TEXT NOT NULL, call_id TEXT NOT NULL,
      tool_id TEXT NOT NULL, connection_id TEXT NOT NULL, connection_version INTEGER NOT NULL,
      schema_digest TEXT NOT NULL, args_digest TEXT NOT NULL, args_cipher TEXT,
      status TEXT NOT NULL CHECK(status IN
        ('pending','approved','rejected','expired','dispatching','ok','error','unknown','denied')),
      created_at TEXT NOT NULL, expires_at TEXT NOT NULL, duration_ms INTEGER,
      UNIQUE(turn_id,execution_id,call_id)
    )""",
    "CREATE INDEX IF NOT EXISTS personal_mcp_pending ON personal_mcp_invocation(owner_user_id,status)",
    """CREATE TRIGGER IF NOT EXISTS personal_mcp_owner_immutable BEFORE UPDATE OF owner_user_id
      ON personal_mcp_connection WHEN NEW.owner_user_id IS NOT OLD.owner_user_id
      BEGIN SELECT RAISE(ABORT,'personal_mcp_owner_immutable'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT personal_mcp_schema")
    try:
        for sql in DDL:
            db.exec_driver_sql(sql)
        # Binding is separate from shared definitions. Every publication writer
        # still has to remove the personal binding before widening the audience.
        for table, agent_column, audience_column in (
            ("team_subagent_working_copy", "subagent_id", "audience_json"),
            ("team_subagent_release_candidate", "subagent_id", "target_audience_json"),
            ("team_subagent_release", "subagent_id", "audience_snapshot_json"),
        ):
            for action in ("INSERT", "UPDATE"):
                db.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS personal_mcp_{table}_{action.lower()}
                  BEFORE {action} ON {table}
                  WHEN json_extract(NEW.{audience_column},'$.effective_mode') IS NOT 'private'
                    AND EXISTS(SELECT 1 FROM personal_mcp_binding b
                      WHERE b.agent_id=NEW.{agent_column} AND b.tool_ids_json<>'[]')
                  BEGIN SELECT RAISE(ABORT,'personal_mcp_remove_bindings_before_sharing'); END""")
        db.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS personal_mcp_agent_share BEFORE UPDATE ON team_subagent
          WHEN (NEW.audience_mode IS NOT 'private' OR NEW.visibility IS NOT 'creator_only')
            AND EXISTS(SELECT 1 FROM personal_mcp_binding b WHERE b.agent_id=NEW.id AND b.tool_ids_json<>'[]')
          BEGIN SELECT RAISE(ABORT,'personal_mcp_remove_bindings_before_sharing'); END""")
        db.exec_driver_sql("RELEASE SAVEPOINT personal_mcp_schema")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT personal_mcp_schema")
        db.exec_driver_sql("RELEASE SAVEPOINT personal_mcp_schema")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
