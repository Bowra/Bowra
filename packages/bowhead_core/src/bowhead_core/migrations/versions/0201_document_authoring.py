"""Admit versioned private authoring without reinterpreting historical jobs."""

import sqlite3
from importlib import import_module

from alembic import context

revision = "0201_document_authoring"
down_revision = "0200_agent_runtime_budget"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0149_document_planning_attempts")
DISPATCH = import_module("bowhead_core.migrations.versions.0130_document_drafts").DDL[0]
SELECTION = """model_selection_json TEXT
                CHECK(model_selection_json IS NULL OR (
                  json_valid(model_selection_json) AND json_type(model_selection_json)='object'
                  AND coalesce(json_type(model_selection_json,'$.provider')='text'
                    AND length(json_extract(model_selection_json,'$.provider'))>0
                    AND json_type(model_selection_json,'$.model')='text'
                    AND length(json_extract(model_selection_json,'$.model'))>0,0)))"""


def _canonical(sql):
    return "".join(sql.lower().replace('"', '').split())


def _append_columns(sql, *columns):
    # Use SQLite's actual ALTER serialization, including the position of table
    # constraints after appended columns, to compare the exact installed DDL.
    with sqlite3.connect(":memory:") as db:
        db.execute(sql)
        table = sql.split()[2]
        for column in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        return db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]


EXPECTED = {
    "document_dispatches": _append_columns(DISPATCH, SELECTION),
    "document_model_calls": _append_columns(
        PREVIOUS.CALL_TABLE,
        "credit_rates_json TEXT CHECK(credit_rates_json IS NULL OR json_valid(credit_rates_json))", "usage_family TEXT",
    ),
    "document_physical_receipt_links": next(sql for sql in PREVIOUS.DDL if sql.startswith("CREATE TABLE document_physical_receipt_links")),
}
AUTHORING = "pptx-script-worker-v1"


def _rebuild(db, table, expected):
    actual = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name=? AND type='table'", (table,)).scalar()
    if actual is None or _canonical(actual) != _canonical(expected):
        raise RuntimeError(f"Document authoring schema drift: {table}")
    objects = db.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name", (table,)
    ).fetchall()
    replacement = actual.replace("CHECK(call_no IN (1,2))", "CHECK(call_no>=1)").replace(
        "CHECK(profile='ordinary-pptx-worker-v1')", f"CHECK(profile IN ('ordinary-pptx-worker-v1','{AUTHORING}'))"
    )
    if replacement == actual:
        raise RuntimeError(f"Document authoring constraint missing: {table}")
    backup = f"{table}_0198_backup"
    db.exec_driver_sql(f"CREATE TEMP TABLE {backup} AS SELECT * FROM {table}")  # noqa: S608
    db.exec_driver_sql(f"DROP TABLE {table}")  # noqa: S608
    db.exec_driver_sql(replacement)
    db.exec_driver_sql(f"INSERT INTO {table} SELECT * FROM {backup}")  # noqa: S608
    columns = ",".join(row[1] for row in db.exec_driver_sql(f"PRAGMA table_info({table})"))
    if db.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {columns} FROM {table} EXCEPT SELECT {columns} FROM {backup}) "
        f"UNION ALL SELECT 1 FROM (SELECT {columns} FROM {backup} EXCEPT SELECT {columns} FROM {table})"
    ).fetchone() or db.exec_driver_sql(f"SELECT (SELECT count(*) FROM {table})<>(SELECT count(*) FROM {backup})").scalar():  # noqa: S608
        raise RuntimeError(f"Document authoring copy mismatch: {table}")
    db.exec_driver_sql(f"DROP TABLE {backup}")
    for (sql,) in objects:
        db.exec_driver_sql(sql)


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    foreign_keys = int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys:
        db.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one()):
            raise RuntimeError("Document authoring rebuild needs a connection outside an active foreign-key transaction")
    db.exec_driver_sql("SAVEPOINT document_authoring")
    try:
        for table, expected in EXPECTED.items():
            _rebuild(db, table, expected)
        # The enlarged counter belongs only to explicitly admitted, Turn-owned
        # authoring. Existing planner and background rows retain their old bound.
        for table in ("document_model_calls", "document_physical_receipt_links"):
            db.exec_driver_sql(f"""CREATE TRIGGER trg_{table}_authoring_call BEFORE INSERT ON {table}
              WHEN (NEW.call_no>2 OR EXISTS(SELECT 1 FROM document_dispatches WHERE job_id=NEW.job_id AND profile='{AUTHORING}'))
              AND NOT EXISTS(SELECT 1 FROM document_dispatches d
                JOIN document_turn_dispatches t ON t.job_id=d.job_id WHERE d.job_id=NEW.job_id
                  AND d.profile='{AUTHORING}' AND json_extract(t.runtime_snapshot_json,'$.schema_version')=2
                  AND json_extract(t.runtime_snapshot_json,'$.planner_profile')='{AUTHORING}')
              BEGIN SELECT RAISE(ABORT,'document authoring call unavailable'); END""")
        db.exec_driver_sql(f"""CREATE TRIGGER trg_document_authoring_dispatch BEFORE INSERT ON document_turn_dispatches
          WHEN NOT EXISTS(SELECT 1 FROM document_dispatches d WHERE d.job_id=NEW.job_id AND (
            (d.profile='{AUTHORING}' AND json_extract(NEW.runtime_snapshot_json,'$.schema_version')=2
              AND json_extract(NEW.runtime_snapshot_json,'$.planner_profile')='{AUTHORING}') OR
            (d.profile='ordinary-pptx-worker-v1' AND json_extract(NEW.runtime_snapshot_json,'$.schema_version')=1
              AND json_extract(NEW.runtime_snapshot_json,'$.planner_profile') IN ('pptx-grounded-plan-v4','pptx-grounded-plan-v5'))))
          BEGIN SELECT RAISE(ABORT,'document runtime profile mismatch'); END""")
        if any(row[0].startswith("document_") or row[2].startswith("document_") for row in db.exec_driver_sql("PRAGMA foreign_key_check")):
            raise RuntimeError("Document authoring foreign key mismatch")
        db.exec_driver_sql("RELEASE SAVEPOINT document_authoring")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_authoring")
        db.exec_driver_sql("RELEASE SAVEPOINT document_authoring")
        raise
    finally:
        if foreign_keys:
            db.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade():
    raise NotImplementedError("Restore the matching database archive to roll back.")
