"""Permit answer-free success only for an explicitly admitted maintenance Turn."""

from importlib import import_module

from alembic import context

revision = "0207_context_maintenance_terminal"
down_revision = "0206_model_context"
branch_labels = None
depends_on = None

TABLES = ("turn_terminal_candidate_variants", "turn_terminal_receipts")


def _canonical(sql):
    return "".join(sql.lower().replace("if not exists", "").replace('"', "").split())


class _Statements:
    def __init__(self):
        self.values = []

    def exec_driver_sql(self, sql):
        self.values.append(sql)


def _rebuild(db, table):
    recorded = _Statements()
    import_module("bowhead_core.migrations.versions.0008_terminal_protocol_v1")._create_protocol_tables(recorded)
    expected = next(sql for sql in recorded.values if f"CREATE TABLE IF NOT EXISTS {table} (" in sql)
    actual = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).scalar()
    if actual is None or _canonical(actual) != _canonical(expected):
        raise RuntimeError(f"Context maintenance terminal schema drift: {table}")
    objects = db.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name", (table,),
    ).fetchall()
    columns = ",".join(row[1] for row in db.exec_driver_sql(f"PRAGMA table_info({table})"))
    column = "operation_kind TEXT CHECK(operation_kind IS NULL OR operation_kind='context_maintenance'),"
    replacement = actual.replace("(\n", f"(\n{column}\n", 1).replace(
        "(status = 'completed' AND final_answer_mode != 'none')",
        "(status = 'completed' AND ((operation_kind IS NULL AND final_answer_mode != 'none') "
        "OR (operation_kind IS 'context_maintenance' AND final_answer_mode = 'none')))",
    )
    if replacement == actual or "operation_kind IS 'context_maintenance'" not in replacement:
        raise RuntimeError(f"Context maintenance terminal constraint missing: {table}")
    backup = f"{table}_context_backup"
    db.exec_driver_sql(f"CREATE TEMP TABLE {backup} AS SELECT * FROM {table}")  # noqa: S608
    db.exec_driver_sql(f"DROP TABLE {table}")
    db.exec_driver_sql(replacement)
    db.exec_driver_sql(f"INSERT INTO {table}({columns}) SELECT {columns} FROM {backup}")  # noqa: S608
    if db.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {columns} FROM {table} EXCEPT SELECT {columns} FROM {backup}) "
        f"UNION ALL SELECT 1 FROM (SELECT {columns} FROM {backup} EXCEPT SELECT {columns} FROM {table})"
    ).fetchone() or db.exec_driver_sql(f"SELECT (SELECT count(*) FROM {table})<>(SELECT count(*) FROM {backup})").scalar():  # noqa: S608
        raise RuntimeError(f"Context maintenance terminal copy mismatch: {table}")
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
            raise RuntimeError("Context maintenance upgrade needs a connection outside an active foreign-key transaction")
    db.exec_driver_sql("SAVEPOINT context_maintenance_terminal")
    try:
        for table in TABLES:
            _rebuild(db, table)
        db.exec_driver_sql("""CREATE TRIGGER trg_context_maintenance_candidate BEFORE INSERT ON turn_terminal_candidate_variants
          WHEN NEW.operation_kind IS NOT (SELECT t.operation_kind FROM turns t JOIN turn_terminal_candidates c ON c.turn_id=t.id WHERE c.candidate_id=NEW.candidate_id)
            OR (NEW.operation_kind IS 'context_maintenance' AND NEW.final_answer_mode IS NOT 'none')
          BEGIN SELECT RAISE(ABORT,'context maintenance candidate source mismatch'); END""")
        db.exec_driver_sql("""CREATE TRIGGER trg_context_maintenance_receipt BEFORE INSERT ON turn_terminal_receipts
          WHEN NEW.operation_kind IS NOT (SELECT v.operation_kind FROM turn_terminal_candidate_variants v WHERE v.candidate_id=NEW.candidate_id AND v.variant_key=NEW.selected_variant_key)
            OR NEW.operation_kind IS NOT (SELECT operation_kind FROM turns WHERE id=NEW.turn_id)
            OR (NEW.operation_kind IS 'context_maintenance' AND NEW.final_answer_mode IS NOT 'none')
          BEGIN SELECT RAISE(ABORT,'context maintenance receipt source mismatch'); END""")
        if db.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Context maintenance terminal foreign key mismatch")
        db.exec_driver_sql("RELEASE SAVEPOINT context_maintenance_terminal")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT context_maintenance_terminal")
        db.exec_driver_sql("RELEASE SAVEPOINT context_maintenance_terminal")
        raise
    finally:
        if foreign_keys:
            db.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade():
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
