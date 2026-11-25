"""Let a personal MCP connection sign in with the member's own account (OAuth).

``0100`` limited ``personal_mcp_connection.auth_type`` to ``none``, ``bearer`` and ``api_key``.
A signed-in connection stores its sealed OAuth grant in the same ``auth_cipher`` and needs
``auth_type='oauth'`` (docs/sprints/personal-mcp-oauth-2026-11-24/tech-design.md §4.1).

SQLite can only change a CHECK by rebuilding the table. Every row and column is kept and the row
count is checked. The table is recreated under its own name (no ``RENAME``), so
``personal_mcp_tool``'s foreign key keeps pointing at it; its own indexes and triggers (the owner
guard, the account-retirement insert guard and any domain mutation guard) are recreated from their
stored SQL after the copy. Foreign key enforcement is suspended for the rebuild when the connection
has it on, as ``0188`` does, and restored afterwards. A savepoint rolls back an interrupted rebuild;
a repeated upgrade finds the widened CHECK and does nothing. Only core receives DDL.
"""

from collections.abc import Callable
from typing import Any

from alembic import context

revision = "0233_personal_mcp_oauth"
down_revision = "0232_user_result_pins"
branch_labels = None
depends_on = None

_TABLE = "personal_mcp_connection"
_BACKUP = "personal_mcp_connection_0233_backup"
_SAVEPOINT = "personal_mcp_oauth"

# Exactly the table DDL created by 0100.
_EXPECTED_DDL = """CREATE TABLE personal_mcp_connection (
      id TEXT PRIMARY KEY CHECK(id LIKE 'pcm_%'), owner_user_id TEXT NOT NULL,
      name TEXT NOT NULL, endpoint TEXT NOT NULL,
      auth_type TEXT NOT NULL CHECK(auth_type IN ('none','bearer','api_key')),
      auth_cipher TEXT NOT NULL, status TEXT NOT NULL,
      version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )"""

_NEW_DDL = """CREATE TABLE personal_mcp_connection (
      id TEXT PRIMARY KEY CHECK(id LIKE 'pcm_%'), owner_user_id TEXT NOT NULL,
      name TEXT NOT NULL, endpoint TEXT NOT NULL,
      auth_type TEXT NOT NULL CHECK(auth_type IN ('none','bearer','api_key','oauth')),
      auth_cipher TEXT NOT NULL, status TEXT NOT NULL,
      version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )"""

_COLUMNS = "id,owner_user_id,name,endpoint,auth_type,auth_cipher,status,version,created_at,updated_at"


def _canonical(sql: str) -> str:
    return "".join(sql.lower().split())


def widen_auth_type(execute: Callable[..., Any]) -> bool:
    """Rebuild the table with the widened CHECK; False when it is already widened.

    ``execute(sql, params=())`` is ``exec_driver_sql`` of the migration connection, or ``execute``
    of a plain ``sqlite3`` connection (tests apply this revision's table change to pre-split fixtures).
    """
    row = execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (_TABLE,)).fetchone()
    if row is None:
        raise RuntimeError(f"{_TABLE} is missing; upgrade through 0100 first")
    if _canonical(row[0]) == _canonical(_NEW_DDL):
        return False
    if _canonical(row[0]) != _canonical(_EXPECTED_DDL):
        raise RuntimeError(f"{_TABLE} does not match the 0100 schema; refusing to rebuild it")
    dependents = execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
        (_TABLE,),
    ).fetchall()
    if "personal_mcp_owner_immutable" not in {name for _, name, _ in dependents}:
        raise RuntimeError(f"{_TABLE} is missing its 0100 owner guard")
    execute(f"CREATE TEMP TABLE {_BACKUP} AS SELECT {_COLUMNS} FROM {_TABLE}")
    execute(f"DROP TABLE {_TABLE}")
    execute(_NEW_DDL)
    # Copied before the triggers come back, so the account-retirement guard does not judge historical rows.
    execute(f"INSERT INTO {_TABLE}({_COLUMNS}) SELECT {_COLUMNS} FROM {_BACKUP} ORDER BY id")  # noqa: S608
    copied = execute(f"SELECT (SELECT COUNT(*) FROM {_TABLE}),(SELECT COUNT(*) FROM {_BACKUP})").fetchone()  # noqa: S608
    if copied[0] != copied[1]:
        raise RuntimeError(f"{_TABLE} rebuild lost rows")
    execute(f"DROP TABLE {_BACKUP}")
    for _kind, _name, sql in dependents:
        execute(sql)
    broken = [item for item in execute("PRAGMA foreign_key_check").fetchall() if item[2] == _TABLE or item[0] == _TABLE]
    if broken:
        raise RuntimeError(f"{_TABLE} foreign keys broken by rebuild")
    return True


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    foreign_keys = int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys:
        db.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one()):
            # SQLite ignores the pragma inside an open transaction.
            raise RuntimeError(
                f"{_TABLE} rebuild could not suspend foreign key enforcement; run this upgrade on a connection "
                "with foreign keys off or outside an open transaction"
            )
    db.exec_driver_sql(f"SAVEPOINT {_SAVEPOINT}")
    try:
        widen_auth_type(db.exec_driver_sql)
    except BaseException:
        db.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
        db.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        if foreign_keys:
            db.exec_driver_sql("PRAGMA foreign_keys=ON")
        raise
    db.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    if foreign_keys:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
