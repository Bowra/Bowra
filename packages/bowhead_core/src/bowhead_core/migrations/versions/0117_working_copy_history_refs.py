"""Let a published draft be deleted while history rows still name it.

Activation deletes the working copy when it equals what went live — the normal case, where the
Owner prepared a candidate and did not edit further. Two immutable history tables pinned that row
with ``ON DELETE RESTRICT``: ``agent_eval_candidate_control.source_working_copy_id`` (every Owner-eval
candidate) and ``agent_taxonomy_suggestion.working_copy_id``. Neither history row can be deleted,
so the draft could never be removed either: every private activation and shared approval of such
a candidate raised ``FOREIGN KEY constraint failed`` and rolled back — a candidate could not go live.

Both rows already carry what they need without the parent: the control row keeps the working
copy's id, version and hash; the suggestion keeps its id and source hash. Their insert-scope triggers
still require the working copy to exist, belong to the Agent and (for control) match the version at
insert time. This revision only drops the two parent references; every other column, constraint,
index and trigger is recreated byte-for-byte.

Copy-migrate per table inside one SAVEPOINT: exact preflight of table, indexes and triggers against
the revisions that created them (0073, 0044), shadow copy, two-way row verification, rename, rebuild,
``PRAGMA foreign_key_check``. Core only; the other seven targets are no-op.

Revision ID: 0117_working_copy_history_refs
Revises: 0116_builtin_tool_user_grants
Create Date: 2026-09-25
"""

from __future__ import annotations

import re
from importlib import import_module
from typing import Any

from alembic import context

revision = "0117_working_copy_history_refs"
down_revision = "0116_builtin_tool_user_grants"
branch_labels = None
depends_on = None

_OWNER_EVAL = import_module("bowhead_core.migrations.versions.0073_agent_owner_eval_foundation")
_TAXONOMY = import_module("bowhead_core.migrations.versions.0044_agent_taxonomy")

CONTROL = "agent_eval_candidate_control"
SUGGESTION = "agent_taxonomy_suggestion"


def _canonical(sql: str) -> str:
    # SQLite quotes a table name it rewrote on RENAME; compare the statement, not its quoting.
    return "".join(sql.lower().replace("if not exists", "").replace('"', "").split())


def _object_name(sql: str) -> str:
    match = re.search(r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)\s+(\w+)", sql)
    if match is None:
        raise RuntimeError("working copy history refs: unparseable DDL")
    return match.group(1)


def _control_objects() -> tuple[str, dict[str, str]]:
    table = next(sql for sql in _OWNER_EVAL._TABLES if _object_name(sql) == CONTROL)
    dependents = {
        _object_name(sql): sql
        for sql in (*_OWNER_EVAL._INDEXES, *_OWNER_EVAL._SPECIAL_TRIGGERS)
        if re.search(rf"\bON\s+{CONTROL}\b", sql)
    }
    return table, dependents


def _suggestion_objects() -> tuple[str, dict[str, str]]:
    table = next(ddl for kind, name, ddl in _TAXONOMY._OBJECTS if kind == "table" and name == SUGGESTION)
    dependents = {
        name: ddl
        for kind, name, ddl in _TAXONOMY._OBJECTS
        if kind in {"index", "trigger"} and re.search(rf"\bON\s+{SUGGESTION}\b", ddl)
    }
    return table, dependents


def _without(table_ddl: str, reference: str, replacement: str) -> str:
    if table_ddl.count(reference) != 1:
        raise RuntimeError("working copy history refs: source DDL drift")
    return table_ddl.replace(reference, replacement)


_CONTROL_TABLE, _CONTROL_DEPENDENTS = _control_objects()
_SUGGESTION_TABLE, _SUGGESTION_DEPENDENTS = _suggestion_objects()

PLANS: tuple[tuple[str, str, str, dict[str, str]], ...] = (
    (
        CONTROL,
        _CONTROL_TABLE,
        _without(
            _CONTROL_TABLE,
            "source_working_copy_id       TEXT NOT NULL REFERENCES team_subagent_working_copy(id) ON DELETE RESTRICT,",
            "source_working_copy_id       TEXT NOT NULL,",
        ),
        _CONTROL_DEPENDENTS,
    ),
    (
        SUGGESTION,
        _SUGGESTION_TABLE,
        _without(
            _SUGGESTION_TABLE,
            """,
          FOREIGN KEY(working_copy_id)
            REFERENCES team_subagent_working_copy(id) ON DELETE RESTRICT""",
            "",
        ),
        _SUGGESTION_DEPENDENTS,
    ),
)


def _sql(db: Any, name: str) -> str | None:
    row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _preflight(db: Any, table: str, old_ddl: str, new_ddl: str, dependents: dict[str, str]) -> bool:
    """Return True when the table still needs rebuilding; raise on anything unexpected."""
    current = _sql(db, table)
    if current is None:
        raise RuntimeError(f"working copy history refs: {table} missing")
    attached = {
        str(name): str(sql)
        for name, sql in db.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
            (table,),
        ).fetchall()
    }
    if set(attached) != set(dependents) or any(_canonical(attached[n]) != _canonical(dependents[n]) for n in dependents):
        raise RuntimeError(f"working copy history refs: {table} dependents drift")
    # Nothing outside the table may reference it: a rename would rewrite or break it unseen.
    outside = db.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE tbl_name<>? AND sql LIKE ?",
        (table, f"%{table}%"),
    ).fetchall()
    if outside:
        raise RuntimeError(f"working copy history refs: {table} referenced by {outside[0][0]}")
    if _canonical(current) == _canonical(new_ddl):
        return False
    if _canonical(current) != _canonical(old_ddl):
        raise RuntimeError(f"working copy history refs: {table} schema drift")
    if _sql(db, f"{table}_0117") is not None:
        raise RuntimeError(f"working copy history refs: {table} shadow already exists")
    return True


def _rebuild(db: Any, table: str, new_ddl: str, dependents: dict[str, str]) -> None:
    shadow = f"{table}_0117"
    db.exec_driver_sql(re.sub(rf"CREATE TABLE {table}\b", f"CREATE TABLE {shadow}", new_ddl, count=1))
    db.exec_driver_sql(f"INSERT INTO {shadow} SELECT * FROM {table}")
    if (
        db.exec_driver_sql(f"SELECT COUNT(*) FROM {shadow}").scalar() != db.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
        or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {shadow} EXCEPT SELECT * FROM {table}) LIMIT 1").fetchone()
        or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {table} EXCEPT SELECT * FROM {shadow}) LIMIT 1").fetchone()
    ):
        raise RuntimeError(f"working copy history refs: {table} copy mismatch")
    for name, sql in dependents.items():
        kind = "INDEX" if re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX", sql) else "TRIGGER"
        db.exec_driver_sql(f"DROP {kind} {name}")
    db.exec_driver_sql(f"DROP TABLE {table}")
    db.exec_driver_sql(f"ALTER TABLE {shadow} RENAME TO {table}")
    for sql in dependents.values():
        db.exec_driver_sql(sql)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    pending = [(table, new, deps) for table, old, new, deps in PLANS if _preflight(db, table, old, new, deps)]
    if not pending:
        return
    foreign_keys = int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys:
        db.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one()):
            raise RuntimeError("working copy history refs could not suspend foreign keys")
    db.exec_driver_sql("SAVEPOINT working_copy_history_refs")
    try:
        for table, new_ddl, dependents in pending:
            _rebuild(db, table, new_ddl, dependents)
        violations = db.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"working copy history refs foreign key check failed: {violations[0]}")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT working_copy_history_refs")
        db.exec_driver_sql("RELEASE SAVEPOINT working_copy_history_refs")
        if foreign_keys:
            db.exec_driver_sql("PRAGMA foreign_keys=ON")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT working_copy_history_refs")
    if foreign_keys:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
