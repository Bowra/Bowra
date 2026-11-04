"""Retire unused legacy tables and finish their frozen erasure items.

Revision ID: 0219_retire_unused_tables
Revises: 0218_billing_witness
"""

from alembic import context

revision = "0219_retire_unused_tables"
down_revision = "0218_billing_witness"
branch_labels = None
depends_on = None

_RETIRED_TABLES = ("business_rules", "mcp_servers", "artifact_stdout", "pbi_connection_legacy_0022")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    _normalize_runtime_columns(connection)
    _formalize_custom_subagents(connection)
    for table in _RETIRED_TABLES:
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS main."{table}"')
    if connection.exec_driver_sql("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='account_deletion_rows'").first():
        # The same committed revision proves these tables and all their rows are gone.
        for table in _RETIRED_TABLES:
            connection.exec_driver_sql("DELETE FROM account_deletion_rows WHERE table_name=?", (table,))


_RUNTIME_COLUMNS = {
    "skill_instances": (("import_status", "TEXT", 0, None), ("import_report_json", "TEXT", 0, None)),
    "memory_candidates": (
        ("memory_type", "TEXT", 0, None),
        ("supersedes_json", "TEXT", 0, None),
        ("origin", "TEXT NOT NULL DEFAULT 'dreaming_light'", 1, "'dreaming_light'"),
    ),
    "memory_pack_snapshots": (("executor_block", "TEXT", 0, None),),
}


def _normalize_runtime_columns(connection) -> None:
    """Adopt the six columns already added by released runtime initializers."""
    for table, fields in _RUNTIME_COLUMNS.items():
        info = list(connection.exec_driver_sql(f'PRAGMA main.table_xinfo("{table}")'))
        columns = {row[1]: row for row in info}
        for name, _ddl, required, default in fields:
            if name in columns:
                row = columns[name]
                if str(row[2]).upper() != "TEXT" or row[3] != required or row[4] != default or row[5] != 0 or row[6] != 0:
                    raise RuntimeError(f"Known runtime column drift in {table}.{name}; inspect its type, nullability and default before upgrading")
        present = [row[1] for row in info if row[1] in {field[0] for field in fields}]
        if present != [field[0] for field in fields[: len(present)]]:
            # Recover an independently committed subset of old ALTER statements
            # without changing values, row identities, indexes or business guards.
            sql = connection.exec_driver_sql("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?", (table,)).scalar_one()
            for name, ddl, _required, _default in fields:
                if name in columns:
                    marker = f", {name} {ddl}"
                    if sql.count(marker) != 1:
                        raise RuntimeError(f"Unreviewed partial runtime schema for {table}; inspect its table definition")
                    sql = sql.replace(marker, "", 1)
            objects = list(
                connection.exec_driver_sql(
                    "SELECT sql FROM main.sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,)
                )
            )
            for (child,) in connection.exec_driver_sql("SELECT name FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                if any(row[2] == table for row in connection.exec_driver_sql(f'PRAGMA main.foreign_key_list("{child}")')):
                    raise RuntimeError(f"Unreviewed foreign key references a partial runtime table: {table}")
            selected = ",".join('"' + row[1] + '"' for row in info)
            temporary = "_runtime_columns_0219"
            connection.exec_driver_sql(f'CREATE TEMP TABLE "{temporary}" AS SELECT rowid AS preserved_rowid,{selected} FROM main."{table}"')
            connection.exec_driver_sql(f'DROP TABLE main."{table}"')
            connection.exec_driver_sql(sql)
            for name, ddl, _required, _default in fields:
                connection.exec_driver_sql(f'ALTER TABLE main."{table}" ADD COLUMN {name} {ddl}')
            connection.exec_driver_sql(f'INSERT INTO main."{table}"(rowid,{selected}) SELECT preserved_rowid,{selected} FROM temp."{temporary}"')
            connection.exec_driver_sql(f'DROP TABLE temp."{temporary}"')
            for (ddl,) in objects:
                connection.exec_driver_sql(ddl)
            continue
        for name, ddl, _required, _default in fields:
            if name not in columns:
                connection.exec_driver_sql(f'ALTER TABLE main."{table}" ADD COLUMN {name} {ddl}')


_CUSTOM_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS custom_subagents (\n    id                  TEXT PRIMARY KEY,\n    user_id             TEXT NOT NULL,\n    name                TEXT NOT NULL,\n    display_name        TEXT NOT NULL,\n    description         TEXT NOT NULL,\n    system_prompt       TEXT NOT NULL,\n    allowed_tools       TEXT NOT NULL DEFAULT '[]',\n    auto_read_skills    TEXT NOT NULL DEFAULT '[]',\n    max_iterations      INTEGER NOT NULL DEFAULT 10,\n    status              TEXT NOT NULL DEFAULT 'active',\n    created_at          TEXT NOT NULL,\n    updated_at          TEXT NOT NULL,\n    UNIQUE (user_id, name)\n)",
    "CREATE INDEX IF NOT EXISTS idx_custom_subagents_user\n    ON custom_subagents(user_id, status)",
)


def _formalize_custom_subagents(connection) -> None:
    """Retain the existing opt-in private Agent contract without enabling it."""
    for ddl in _CUSTOM_SCHEMA:
        connection.exec_driver_sql(ddl)
    expected = [
        ("id", "TEXT", 0, None, 1),
        ("user_id", "TEXT", 1, None, 0),
        ("name", "TEXT", 1, None, 0),
        ("display_name", "TEXT", 1, None, 0),
        ("description", "TEXT", 1, None, 0),
        ("system_prompt", "TEXT", 1, None, 0),
        ("allowed_tools", "TEXT", 1, "'[]'", 0),
        ("auto_read_skills", "TEXT", 1, "'[]'", 0),
        ("max_iterations", "INTEGER", 1, "10", 0),
        ("status", "TEXT", 1, "'active'", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ]
    actual = [(row[1], str(row[2]).upper(), row[3], row[4], row[5]) for row in connection.exec_driver_sql("PRAGMA main.table_info(custom_subagents)")]
    if actual != expected:
        raise RuntimeError("The optional private Agent schema has unreviewed columns or defaults; inspect it before upgrading")
    connection.exec_driver_sql(
        'CREATE TRIGGER IF NOT EXISTS trg_account_retired_custom_subagents_insert BEFORE INSERT ON "custom_subagents" '
        'WHEN EXISTS(SELECT 1 FROM account_deletions WHERE user_id=NEW."user_id") '
        "BEGIN SELECT RAISE(ABORT,'account is deleted'); END"
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
