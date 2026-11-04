"""Admit only reviewed pre-baseline SQLite shapes at the frozen split source."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from bowhead_core.sqlite_upgrade import SQLiteUpgradeError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SchemaRow = tuple[str, str, str, str]
_METADATA = json.loads(Path(__file__).with_name("sqlite_source_0219_compatibility.json").read_text(encoding="utf-8"))
_FROZEN_SOURCE = json.loads(Path(__file__).with_name("sqlite_domain_schema.json").read_text(encoding="utf-8"))["source_revision"]
if _METADATA.get("format") != 1 or _METADATA.get("source_revision") != _FROZEN_SOURCE:
    raise RuntimeError("The reviewed historical SQLite source contract is invalid")
_VARIANTS = {tuple(entry["source"]): entry for entry in _METADATA["variants"]}
_RETIRED = frozenset(tuple(row) for row in _METADATA["retired_objects"])
_RETIRED_TABLES = {row[1]: row for row in _RETIRED if row[0] == "table"}
_EMPTY_ONLY = frozenset(_METADATA["empty_only_tables"])
_ARCHIVED = frozenset(_METADATA["archived_tables"])
if _EMPTY_ONLY & _ARCHIVED or set(_RETIRED_TABLES) != _EMPTY_ONLY | _ARCHIVED:
    raise RuntimeError("The reviewed historical SQLite retirement policy is incomplete")


def canonical_object(row: SchemaRow) -> SchemaRow:
    entry = _VARIANTS.get(tuple(row))
    return tuple(entry["canonical"]) if entry else row


def canonical_source_rows(rows: Iterable[SchemaRow]) -> list[SchemaRow]:
    """Unknown bytes remain in the digest and cannot acquire source admission."""
    return [canonical_object(row) for row in rows if tuple(row) not in _RETIRED]


def canonical_table_sql(table: str, sql: str) -> str:
    return canonical_object(("table", table, table, sql))[3]


def canonical_column_order(table: str, sql: str, columns: list[str]) -> list[str]:
    entry = _VARIANTS.get(("table", table, table, sql))
    return list(entry["columns"]) if entry else columns


def is_retired_object(row: SchemaRow) -> bool:
    return tuple(row) in _RETIRED


def _verify_archived_table(archive: sqlite3.Connection, table: str, expected: Mapping[str, object]) -> None:
    from bowhead_core.sqlite_domain_upgrade import table_evidence

    row = archive.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
    if row is None or tuple(row) != _RETIRED_TABLES[table] or table_evidence(archive, table) != expected:
        raise SQLiteUpgradeError(
            f"The verified archive does not preserve historical table {table}; recover its matching complete backup before continuing."
        )


def verify_retirement_record(record: object, backup: str | None) -> None:
    """Keep archived maintenance payloads attested until the split becomes ready."""
    if record is None:
        return
    if not isinstance(record, dict) or set(record) - set(_RETIRED_TABLES):
        raise SQLiteUpgradeError("The historical table retirement checkpoint is invalid; inspect its migration record.")
    if not record:
        return
    if backup is None:
        raise SQLiteUpgradeError("Historical table retirement requires its verified complete backup; recover the matching archive.")
    for table, evidence in record.items():
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"rows", "digest"}
            or type(evidence["rows"]) is not int
            or evidence["rows"] < 0
            or table in _EMPTY_ONLY
            and evidence["rows"] != 0
            or not isinstance(evidence["digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["digest"]) is None
        ):
            raise SQLiteUpgradeError("The historical table retirement evidence is invalid; inspect its migration record.")
    try:
        with closing(sqlite3.connect((Path(backup) / "sqlite/bowhead.db").resolve().as_uri() + "?mode=ro", uri=True)) as archive:
            for table, evidence in record.items():
                _verify_archived_table(archive, table, evidence)
    except sqlite3.DatabaseError as exc:
        raise SQLiteUpgradeError(
            "Cannot read the matching historical table archive; keep the application stopped and recover its complete backup."
        ) from exc


def retirement_evidence(source: sqlite3.Connection, backup: str | None) -> dict[str, dict[str, object]]:
    """Only empty dead tables and exactly archived maintenance sidecars may retire."""
    from bowhead_core.sqlite_domain_upgrade import schema_rows, table_evidence

    tables = sorted(row[1] for row in schema_rows(source) if row[0] == "table" and row in _RETIRED)
    evidence = {}
    for table in tables:
        if table in _EMPTY_ONLY and source.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None:
            raise SQLiteUpgradeError(
                f"Historical table {table} contains retained data; keep the application stopped and obtain its feature owner's preservation decision."
            )
        evidence[table] = table_evidence(source, table)
    verify_retirement_record(evidence, backup)
    return evidence


def retire_legacy_objects(source: sqlite3.Connection) -> None:
    """Drop reviewed objects inside the original cleanup transaction after proof."""
    from bowhead_core.sqlite_domain_upgrade import schema_rows
    from bowhead_core.sqlite_upgrade import _load_state

    path = next(Path(row[2]) for row in source.execute("PRAGMA database_list") if row[1] == "main")
    state = _load_state(path.parent)
    if state is None or state.get("phase") != "cleanup":
        raise SQLiteUpgradeError("Historical object retirement requires the verified offline cleanup checkpoint.")
    actual = retirement_evidence(source, state.get("backup"))
    if actual != state.get("legacy_retirement_evidence", {}):
        raise SQLiteUpgradeError("Historical table evidence changed before cleanup; preserve the source and inspect its complete backup.")
    for row in schema_rows(source):
        if row in _RETIRED:
            source.execute(f'DROP {row[0].upper()} IF EXISTS "{row[1]}"')
    for table in actual:
        source.execute("DELETE FROM account_deletion_rows WHERE table_name=?", (table,))
