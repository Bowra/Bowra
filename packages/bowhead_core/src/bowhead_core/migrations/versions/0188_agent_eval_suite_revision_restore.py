"""Let an Agent Eval suite return to an earlier definition as a new current revision.

``0037`` made ``agent_eval_suite_revision`` unique on ``(suite_id, definition_hash)``. The current
revision of a suite is its highest ``sequence`` everywhere (runs, release checks, CI evidence, Coach
reads and the database guards), so saving A, then B, then A again must append a third revision.
The constraint forbade that and the store returned revision 1 while revision 2 stayed current.

SQLite can only drop a table constraint by rebuilding the table. The rebuild keeps every row, id,
sequence and hash; ``UNIQUE(suite_id, sequence)`` and ``UNIQUE(suite_id, id)`` stay. The table is
recreated under its own name (no ``RENAME``, which would reject the triggers of other tables that
name it while it is absent), so the foreign keys and triggers of the tables referencing it keep
pointing at it. Its own triggers are recreated from their stored SQL, which keeps the ``0176``
scope trigger. Dropping a referenced table with foreign key enforcement on would delete or reject
the dependent evaluation records, so enforcement is suspended for the rebuild when the connection
has it on (as ``0073`` does) and restored afterwards, on success and on failure.
"""

from alembic import context

revision = "0188_agent_eval_suite_revision_restore"
down_revision = "0187_retire_term_aliases"
branch_labels = None
depends_on = None

_TABLE = "agent_eval_suite_revision"
_BACKUP = "agent_eval_suite_revision_0188_backup"
_SAVEPOINT = "agent_eval_suite_revision_restore"
_DROPPED_CONSTRAINT = "unique(suite_id,definition_hash)"

# Exactly the table DDL created by 0037.
_EXPECTED_DDL = """
CREATE TABLE agent_eval_suite_revision (
  id                 TEXT PRIMARY KEY CHECK(id LIKE 'aev_%'),
  suite_id           TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
  sequence           INTEGER NOT NULL CHECK(sequence >= 1),
  definition_json    TEXT NOT NULL CHECK(json_valid(definition_json))
                      CHECK(json_type(definition_json)='object'),
  definition_hash    TEXT NOT NULL CHECK(definition_hash LIKE 'sha256:%'),
  scorer_bundle_ref  TEXT NOT NULL CHECK(length(scorer_bundle_ref) > 0),
  judge_policy_json  TEXT NOT NULL CHECK(json_valid(judge_policy_json))
                      CHECK(json_type(judge_policy_json)='object'),
  case_count         INTEGER NOT NULL CHECK(case_count BETWEEN 1 AND 200),
  case_set_hash      TEXT NOT NULL CHECK(case_set_hash LIKE 'sha256:%'),
  created_by         TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at         TEXT NOT NULL,
  UNIQUE(suite_id, sequence),
  UNIQUE(suite_id, definition_hash),
  UNIQUE(suite_id, id)
)"""

_NEW_DDL = """
CREATE TABLE agent_eval_suite_revision (
  id                 TEXT PRIMARY KEY CHECK(id LIKE 'aev_%'),
  suite_id           TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
  sequence           INTEGER NOT NULL CHECK(sequence >= 1),
  definition_json    TEXT NOT NULL CHECK(json_valid(definition_json))
                      CHECK(json_type(definition_json)='object'),
  definition_hash    TEXT NOT NULL CHECK(definition_hash LIKE 'sha256:%'),
  scorer_bundle_ref  TEXT NOT NULL CHECK(length(scorer_bundle_ref) > 0),
  judge_policy_json  TEXT NOT NULL CHECK(json_valid(judge_policy_json))
                      CHECK(json_type(judge_policy_json)='object'),
  case_count         INTEGER NOT NULL CHECK(case_count BETWEEN 1 AND 200),
  case_set_hash      TEXT NOT NULL CHECK(case_set_hash LIKE 'sha256:%'),
  created_by         TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at         TEXT NOT NULL,
  UNIQUE(suite_id, sequence),
  UNIQUE(suite_id, id)
)"""

_COLUMNS = (
    "id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,"
    "judge_policy_json,case_count,case_set_hash,created_by,created_at"
)
_TRIGGERS = (
    "trg_agent_eval_suite_revision_immutable_delete",
    "trg_agent_eval_suite_revision_immutable_update",
    "trg_agent_eval_suite_revision_scope",
)


def _canonical(sql: str) -> str:
    return "".join(sql.lower().split())


def _rebuild(db) -> None:
    row = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (_TABLE,)).fetchone()
    if row is None:
        raise RuntimeError(f"{_TABLE} is missing; upgrade through 0037 first")
    if _DROPPED_CONSTRAINT not in _canonical(row[0]):
        return
    if _canonical(row[0]) != _canonical(_EXPECTED_DDL):
        raise RuntimeError(f"{_TABLE} does not match the 0037 schema; refusing to rebuild it")
    dependents = db.exec_driver_sql(
        "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
        (_TABLE,),
    ).fetchall()
    if not set(_TRIGGERS) <= {name for _, name, _ in dependents}:
        raise RuntimeError(f"{_TABLE} is missing one of its 0037 triggers")
    db.exec_driver_sql(f"CREATE TEMP TABLE {_BACKUP} AS SELECT {_COLUMNS} FROM {_TABLE}")  # noqa: S608
    db.exec_driver_sql(f"DROP TABLE {_TABLE}")
    db.exec_driver_sql(_NEW_DDL)
    # Copied before the triggers come back, so the insert scope guard does not judge historical rows.
    db.exec_driver_sql(f"INSERT INTO {_TABLE}({_COLUMNS}) SELECT {_COLUMNS} FROM {_BACKUP} ORDER BY suite_id,sequence")  # noqa: S608
    copied = db.exec_driver_sql(f"SELECT (SELECT COUNT(*) FROM {_TABLE}),(SELECT COUNT(*) FROM {_BACKUP})").fetchone()  # noqa: S608
    if copied[0] != copied[1]:
        raise RuntimeError(f"{_TABLE} rebuild lost rows")
    db.exec_driver_sql(f"DROP TABLE {_BACKUP}")
    for _kind, _name, sql in dependents:
        db.exec_driver_sql(sql)
    broken = [item for item in db.exec_driver_sql("PRAGMA foreign_key_check").fetchall() if item[2] == _TABLE or item[0] == _TABLE]
    if broken:
        raise RuntimeError(f"{_TABLE} foreign keys broken by rebuild")


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
        _rebuild(db)
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
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
