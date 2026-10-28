"""Agent Marketplace GA：可恢复回填与 reader/writer 兼容门。

0025 保持不可变。本 revision 只追加运行租约、固定快照、run-scoped expected
staging，以及 Marketplace 自己的最小 reader/writer 版本。部署候选必须先停旧
writer，再由候选镜像升级本 revision、完成一次性回填与对账，最后才启动服务。

Revision ID: 0026_agent_marketplace_safety
Revises: 0025_agent_marketplace_assignments
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0026_agent_marketplace_safety"
down_revision = "0025_agent_marketplace_assignments"
branch_labels = None
depends_on = None


_ADDED_COLUMNS: tuple[tuple[str, str, str, int, str | None], ...] = (
    (
        "agent_marketplace_schema_meta",
        "minimum_reader_version",
        "TEXT NOT NULL DEFAULT '0002'",
        1,
        "'0002'",
    ),
    (
        "agent_marketplace_schema_meta",
        "minimum_writer_version",
        "TEXT NOT NULL DEFAULT '0002'",
        1,
        "'0002'",
    ),
    ("agent_assignment_backfill_run", "snapshot_at", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "snapshot_completed_at", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "stage_cursor_team_id", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "stage_cursor_user_id", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "cursor_agent_id", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "expected_digest", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "owner_token", "TEXT", 0, None),
    ("agent_assignment_backfill_run", "lease_expires_at", "TEXT", 0, None),
)

_CREATE_EXPECTED_TABLE = """
    CREATE TABLE agent_assignment_backfill_expected (
      run_id    TEXT NOT NULL,
      team_id   TEXT NOT NULL,
      user_id   TEXT NOT NULL,
      agent_id  TEXT NOT NULL,
      PRIMARY KEY(run_id, team_id, user_id, agent_id),
      FOREIGN KEY(run_id) REFERENCES agent_assignment_backfill_run(id) ON DELETE CASCADE
    ) WITHOUT ROWID
    """

_CREATE_SINGLE_RUNNING_INDEX = """
    CREATE UNIQUE INDEX uq_agent_assignment_backfill_single_running
    ON agent_assignment_backfill_run((1))
    WHERE status='running'
    """


def _table_info(connection: Any, table_name: str) -> list[Any]:
    return list(connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall())


def _ensure_column(
    connection: Any,
    *,
    table_name: str,
    column_name: str,
    definition: str,
    expected_not_null: int,
    expected_default: str | None,
) -> None:
    columns = {str(row[1]): row for row in _table_info(connection, table_name)}
    if column_name not in columns:
        connection.exec_driver_sql(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}",
        )
        columns = {str(row[1]): row for row in _table_info(connection, table_name)}
    column = columns.get(column_name)
    if column is None or (
        str(column[2]).upper(),
        int(column[3]),
        column[4],
        int(column[5]),
    ) != ("TEXT", expected_not_null, expected_default, 0):
        raise RuntimeError(f"0026 found incompatible column semantics: {table_name}.{column_name}")


def _ensure_expected_table(connection: Any) -> None:
    table_name = "agent_assignment_backfill_expected"
    if not _table_info(connection, table_name):
        connection.exec_driver_sql(_CREATE_EXPECTED_TABLE)
    expected_columns = (
        ("run_id", "TEXT", 1, None, 1),
        ("team_id", "TEXT", 1, None, 2),
        ("user_id", "TEXT", 1, None, 3),
        ("agent_id", "TEXT", 1, None, 4),
    )
    actual_columns = tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in _table_info(connection, table_name)
    )
    if actual_columns != expected_columns:
        raise RuntimeError("0026 found incompatible agent_assignment_backfill_expected columns")
    table_list = connection.exec_driver_sql(
        "PRAGMA table_list('agent_assignment_backfill_expected')",
    ).fetchall()
    if len(table_list) != 1 or str(table_list[0][2]) != "table" or int(table_list[0][4]) != 1:
        raise RuntimeError("0026 requires agent_assignment_backfill_expected WITHOUT ROWID")
    foreign_keys = connection.exec_driver_sql(
        "PRAGMA foreign_key_list(agent_assignment_backfill_expected)",
    ).fetchall()
    foreign_key_semantics = tuple(
        (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]))
        for row in foreign_keys
    )
    if foreign_key_semantics != (
        ("agent_assignment_backfill_run", "run_id", "id", "NO ACTION", "CASCADE"),
    ):
        raise RuntimeError("0026 found incompatible agent_assignment_backfill_expected foreign key")


def _ensure_single_running_index(connection: Any) -> None:
    index_name = "uq_agent_assignment_backfill_single_running"
    index_rows = connection.exec_driver_sql(
        "PRAGMA index_list(agent_assignment_backfill_run)",
    ).fetchall()
    matching = [row for row in index_rows if str(row[1]) == index_name]
    if not matching:
        connection.exec_driver_sql(_CREATE_SINGLE_RUNNING_INDEX)
        index_rows = connection.exec_driver_sql(
            "PRAGMA index_list(agent_assignment_backfill_run)",
        ).fetchall()
        matching = [row for row in index_rows if str(row[1]) == index_name]
    if len(matching) != 1 or (int(matching[0][2]), str(matching[0][3]), int(matching[0][4])) != (1, "c", 1):
        raise RuntimeError("0026 found incompatible single-running-backfill index flags")
    key_rows = tuple(
        (int(row[1]), row[2], int(row[3]), str(row[4]))
        for row in connection.exec_driver_sql(f"PRAGMA index_xinfo({index_name})").fetchall()
        if int(row[5]) == 1
    )
    if key_rows != ((-2, None, 0, "BINARY"),):
        raise RuntimeError("0026 found incompatible single-running-backfill index key")
    sql_row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    normalized_sql = "".join(str(sql_row[0]).lower().split()) if sql_row and sql_row[0] else ""
    expected_sql = "".join(_CREATE_SINGLE_RUNNING_INDEX.lower().split())
    if normalized_sql != expected_sql:
        raise RuntimeError("0026 found incompatible single-running-backfill index predicate")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for table_name, column_name, definition, expected_not_null, expected_default in _ADDED_COLUMNS:
        _ensure_column(
            connection,
            table_name=table_name,
            column_name=column_name,
            definition=definition,
            expected_not_null=expected_not_null,
            expected_default=expected_default,
        )
    _ensure_expected_table(connection)
    _ensure_single_running_index(connection)
    # A pre-0026 process could have left an in-progress row whose expected set
    # was only held in memory. It cannot be resumed safely, so retain it as a
    # failed audit record and let 0026 start a fresh fixed-snapshot run.
    connection.exec_driver_sql(
        """
        UPDATE agent_assignment_backfill_run
        SET status='failed', error_count=error_count+1,
            finished_at=COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE status='running' AND snapshot_at IS NULL
        """,
    )
    connection.exec_driver_sql(
        """
        UPDATE agent_marketplace_schema_meta
        SET schema_version=2,
            minimum_reader_version='0002',
            minimum_writer_version='0002',
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE singleton=1
        """,
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
