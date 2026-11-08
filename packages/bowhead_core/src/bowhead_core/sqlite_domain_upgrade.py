"""Offline, forward-only domain copying and source cleanup at the split revision."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import struct
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from bowhead_core.sqlite_diagnostics import DiagnosticConnection
from bowhead_core.sqlite_domains import (
    DOMAIN_LAYOUT,
    DOMAIN_TARGET_FILES,
    ORIGINAL_TARGET_FILES,
    domain_tables_at,
)
from bowhead_core.sqlite_schema_identity import static_schema_digest
from bowhead_core.sqlite_source_compatibility import (
    canonical_column_order,
    canonical_object,
    canonical_source_rows,
    canonical_table_sql,
    is_retired_object,
    retire_legacy_objects,
    retirement_evidence,
    verify_retirement_record,
)
from bowhead_core.sqlite_upgrade import (
    LAYOUT_FILE,
    SQLiteUpgradeError,
    _assert_integrity,
    _load_state,
    _recover_rollback_journal,
    _sync_directory,
    _verify_checkpoint_backup,
    _write_json,
    backup_before_upgrade,
    database_head,
    migration_lock,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

_split_migration = importlib.import_module("bowhead_core.migrations.versions.0220_sqlite_domains")
SPLIT_REVISION: str = _split_migration.revision
SOURCE_REVISION: str = _split_migration.down_revision
_SPLIT_SCHEMA = json.loads(Path(__file__).with_name("sqlite_domain_schema.json").read_text(encoding="utf-8"))
SPLIT_TABLES: dict[str, tuple[str, ...]] = {name: tuple(tables) for name, tables in _SPLIT_SCHEMA["target_tables"].items()}
SPLIT_FILES: dict[str, str] = _SPLIT_SCHEMA["target_files"]
_SPLIT_TABLE_DOMAINS = {table: domain for domain, tables in SPLIT_TABLES.items() for table in tables}
NEW_DOMAINS = ("execution", "content", "notifications", "observability")
_WAL_DOMAINS = ("core", *NEW_DOMAINS)
_PROBE_DOMAINS = frozenset((*NEW_DOMAINS, "core", "extracted_rows", "per_row_jobs", "matrix"))
_FTS = "canvas_fts"
_FTS_SHADOWS = frozenset(f"canvas_fts_{part}" for part in ("data", "idx", "content", "docsize", "config"))


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
        raise SQLiteUpgradeError("The reviewed database schema contains an unsupported identifier.")
    return f'"{value}"'


def schema_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def schema_digest(connection: sqlite3.Connection) -> str:
    return static_schema_digest(schema_rows(connection))


def _parts(sql: str) -> tuple[str, list[str], str]:
    start = sql.index("(")
    depth = 0
    quote = ""
    beginning = start + 1
    parts: list[str] = []
    index = beginning
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif sql[index : index + 2] == "--":
            newline = sql.find("\n", index)
            index = len(sql) if newline < 0 else newline
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                parts.append(sql[beginning:index])
                return sql[: start + 1], parts, sql[index:]
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(sql[beginning:index])
            beginning = index + 1
        index += 1
    raise SQLiteUpgradeError("The reviewed database table definition is incomplete.")


def domain_table_sql(table: str, sql: str) -> str:
    """Remove only reviewed cross-domain REFERENCES clauses; retain local constraints."""
    prefix, parts, suffix = _parts(sql)
    domain = _SPLIT_TABLE_DOMAINS[table]
    output = []
    for part in parts:
        reference = re.search(r"\bREFERENCES\s+[\"`\[]?([A-Za-z_][A-Za-z_0-9]*)[\"`\]]?\s*\([^)]*\)", part, re.I)
        if reference is None:
            output.append(part)
            continue
        parent = reference.group(1)
        if parent not in _SPLIT_TABLE_DOMAINS:
            raise SQLiteUpgradeError(f"Unregistered foreign-key target in {table}; review the schema before migration.")
        if _SPLIT_TABLE_DOMAINS[parent] == domain:
            output.append(part)
            continue
        if re.match(r"\s*(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\b", part, re.I):
            continue
        end = reference.end()
        actions = re.compile(
            r"\s+(?:ON\s+(?:DELETE|UPDATE)\s+(?:NO\s+ACTION|SET\s+(?:NULL|DEFAULT)|CASCADE|RESTRICT)|(?:NOT\s+)?DEFERRABLE|INITIALLY\s+(?:DEFERRED|IMMEDIATE)|MATCH\s+\w+)",
            re.I,
        )
        while match := actions.match(part, end):
            end = match.end()
        output.append(part[: reference.start()] + part[end:])
    if table == "terminal_auth_scope_epochs":
        output.append(" CHECK(scope_kind='session_visibility')")
    return prefix + ",".join(output) + suffix


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_xinfo({identifier(table)})") if row[6] == 0]


def _rowid_name(connection: sqlite3.Connection, table: str) -> str | None:
    sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name=? AND type='table'", (table,)).fetchone()[0]
    if re.search(r"\bWITHOUT\s+ROWID\b", sql, re.I):
        return None
    columns = {name.lower() for name in _columns(connection, table)}
    for name in ("_rowid_", "rowid", "oid"):
        if name not in columns:
            return name
    raise SQLiteUpgradeError(f"Table {table} hides all SQLite row identities; review its migration explicitly.")


def _select_columns(connection: sqlite3.Connection, table: str, *, canonical_source: bool = False) -> list[str]:
    columns = _columns(connection, table)
    if canonical_source:
        sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name=? AND type='table'", (table,)).fetchone()[0]
        columns = canonical_column_order(table, sql, columns)
    rowid = _rowid_name(connection, table)
    return [rowid, *columns] if rowid else columns


def _rows(connection: sqlite3.Connection, table: str, columns: list[str], predicate: str | None = None) -> sqlite3.Cursor:
    rowid = _rowid_name(connection, table)
    order = (
        [rowid]
        if rowid
        else [row[1] for row in sorted(connection.execute(f"PRAGMA table_info({identifier(table)})"), key=lambda row: row[5]) if row[5]]
    )
    if not order:
        raise SQLiteUpgradeError(f"Table {table} has no stable migration identity.")
    if predicate is None and table == "terminal_auth_scope_epochs":
        predicate = "scope_kind='session_visibility'"
    where = " WHERE " + predicate if predicate else ""
    return connection.execute(
        f"SELECT {','.join(identifier(name) for name in columns)} FROM {identifier(table)}{where} ORDER BY {','.join(identifier(name) for name in order)}"
    )


def _typed(value: object) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, int):
        return b"i" + str(value).encode()
    if isinstance(value, float):
        return b"f" + struct.pack(">d", value)
    if isinstance(value, str):
        return b"s" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"b" + value
    raise SQLiteUpgradeError("A database row contains an unsupported SQLite storage type.")


def table_evidence(
    connection: sqlite3.Connection,
    table: str,
    *,
    witnesses: Mapping[str, str] | None = None,
    predicate: str | None = None,
    canonical_source: bool = False,
) -> dict[str, object]:
    columns = _select_columns(connection, table, canonical_source=canonical_source)
    digest = hashlib.sha256()
    count = 0
    with closing(_rows(connection, table, columns, predicate)) as cursor:
        for raw in cursor:
            row = list(raw)
            if table == "turn_effect_settlements" and witnesses:
                attempt = row[columns.index("attempt_id")]
                if attempt in witnesses:
                    row[columns.index("billing_witness_ciphertext")] = witnesses[attempt]
            for value in row:
                encoded = _typed(value)
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            count += 1
    return {"rows": count, "digest": digest.hexdigest()}


def _copy_table(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    table: str,
    *,
    destination_table: str | None = None,
    witnesses: Mapping[str, str] | None = None,
    predicate: str | None = None,
    canonical_source: bool = False,
) -> None:
    columns = _select_columns(source, table, canonical_source=canonical_source)
    sql = f"INSERT INTO {identifier(destination_table or table)} ({','.join(identifier(name) for name in columns)}) VALUES ({','.join('?' for _ in columns)})"
    with closing(_rows(source, table, columns, predicate)) as cursor:
        while rows := cursor.fetchmany(500):
            if table == "turn_effect_settlements" and witnesses:
                changed = []
                for raw in rows:
                    row = list(raw)
                    attempt = row[columns.index("attempt_id")]
                    if attempt in witnesses:
                        row[columns.index("billing_witness_ciphertext")] = witnesses[attempt]
                    changed.append(row)
                rows = changed
            destination.executemany(sql, rows)


def _sequences(connection: sqlite3.Connection) -> dict[str, int]:
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone() is None:
        return {}
    return dict(connection.execute("SELECT name,seq FROM sqlite_sequence"))


def _restore_sequences(connection: sqlite3.Connection, sequences: Mapping[str, int], tables: Iterable[str]) -> None:
    if not sequences:
        return
    for table in tables:
        if table in sequences:
            connection.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, sequences[table]))


def _install_objects(connection: sqlite3.Connection, objects: Iterable[tuple[str, str, str, str]], domain: str) -> None:
    from bowhead_core.sqlite_domain_runtime import compile_domain_trigger, compile_domain_view

    for row in objects:
        if is_retired_object(row):
            continue
        kind, name, table, sql = canonical_object(row)
        if kind == "view":
            if domain == "execution":
                connection.execute(compile_domain_view(name, sql))
        elif table in SPLIT_TABLES[domain] and kind in {"index", "trigger"}:
            connection.execute(compile_domain_trigger(name, sql) if kind == "trigger" else sql)
    for statement in _SPLIT_SCHEMA["additional_triggers"].get(domain, []):
        connection.execute(statement)


def create_and_copy_domain(
    source: sqlite3.Connection,
    path: Path,
    domain: str,
    *,
    witnesses: Mapping[str, str] | None = None,
    before_commit: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Create and commit one complete domain; never commit intermediate batches."""
    if domain not in NEW_DOMAINS:
        raise SQLiteUpgradeError("Only explicitly registered new domain targets can be imported.")
    objects = schema_rows(source)
    tables = {name: sql for kind, name, _, sql in objects if kind == "table"}
    if set(SPLIT_TABLES[domain]).difference(tables):
        raise SQLiteUpgradeError(f"Source tables for {domain} are missing; repair the source before migration.")
    if path.exists():
        raise SQLiteUpgradeError(f"Unverified target {path.name} already exists; inspect its migration checkpoint.")
    with closing(sqlite3.connect(path, factory=DiagnosticConnection)) as destination:
        path.chmod(0o600)
        destination.execute("PRAGMA synchronous=FULL")
        destination.execute("PRAGMA foreign_keys=OFF")
        destination.execute("BEGIN IMMEDIATE")
        try:
            for table in SPLIT_TABLES[domain]:
                if table not in _FTS_SHADOWS:
                    sql = canonical_table_sql(table, tables[table])
                    destination.execute(domain_table_sql(table, sql) if table != _FTS else sql)
            evidence = {}
            for table in SPLIT_TABLES[domain]:
                if table not in _FTS_SHADOWS:
                    _copy_table(source, destination, table, witnesses=witnesses, canonical_source=True)
                    expected = table_evidence(source, table, witnesses=witnesses, canonical_source=True)
                    if table_evidence(destination, table) != expected:
                        raise SQLiteUpgradeError(f"Imported data verification failed for {table}; source data has not been deleted.")
                    evidence[table] = expected
            _restore_sequences(destination, _sequences(source), SPLIT_TABLES[domain])
            _install_objects(destination, objects, domain)
            if domain == "content":
                destination.execute("INSERT INTO canvas_fts(canvas_fts) VALUES ('integrity-check')")
            if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SQLiteUpgradeError(f"Required local foreign keys failed in {domain}; repair the source data.")
            destination.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)")
            destination.execute("INSERT INTO alembic_version VALUES (?)", (SPLIT_REVISION,))
            destination.execute("CREATE TABLE _phase0_probe (k TEXT PRIMARY KEY, ts REAL NOT NULL)")
            if before_commit:
                before_commit()
            destination.commit()
        except BaseException:
            destination.rollback()
            raise
        if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SQLiteUpgradeError(f"Integrity verification failed for {path.name}; source data has not been deleted.")
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
        return {"schema_digest": schema_digest(destination), "evidence": evidence}


def verify_copied_domain(
    source: sqlite3.Connection, path: Path, domain: str, expected: Mapping[str, object], *, witnesses: Mapping[str, str] | None = None
) -> None:
    """Verify committed import evidence before any source cleanup; not on live restarts."""
    if not path.is_file():
        raise SQLiteUpgradeError(f"Committed target {path.name} is missing; restore it before continuing.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as target:
        if schema_digest(target) != expected["schema_digest"]:
            raise SQLiteUpgradeError(f"Target schema changed for {path.name}; inspect its migration checkpoint.")
        for table, evidence in expected["evidence"].items():
            if table_evidence(source, table, witnesses=witnesses, canonical_source=True) != evidence or table_evidence(target, table) != evidence:
                raise SQLiteUpgradeError(f"Source or imported data changed for {table}; source data has not been deleted.")


def cleanup_core(connection: sqlite3.Connection) -> None:
    """Replace cross-file FKs and remove migrated objects within the core revision transaction."""
    if _source_identity(connection) != _source_schema_digest():
        raise SQLiteUpgradeError("Source schema changed before cleanup; preserve the source and inspect its migration checkpoint.")
    objects = schema_rows(connection)
    tables = {name: sql for kind, name, _, sql in objects if kind == "table"}
    retained = SPLIT_TABLES["core"]
    sequences = _sequences(connection)
    authority_table = "terminal_authority_scope_epochs"
    before = {table: table_evidence(connection, table, canonical_source=True) for table in retained if table != authority_table}
    retiring = sorted(row[1] for row in objects if row[0] == "table" and is_retired_object(row))
    if retiring:
        # Physical table retirement completes only its matching frozen row keys.
        predicate = "table_name NOT IN (" + ",".join("'" + table + "'" for table in retiring) + ")"
        before["account_deletion_rows"] = table_evidence(connection, "account_deletion_rows", predicate=predicate, canonical_source=True)
    if authority_table in retained:
        before[authority_table] = table_evidence(
            connection, "terminal_auth_scope_epochs", predicate="scope_kind<>'session_visibility'", canonical_source=True
        )
        original_scope_sql = tables["terminal_auth_scope_epochs"]
        _, scope_parts, scope_suffix = _parts(original_scope_sql)
        scope_parts.append(" CHECK(scope_kind<>'session_visibility')")
        connection.execute("CREATE TABLE " + identifier(authority_table) + " (" + ",".join(scope_parts) + scope_suffix)
        _copy_table(
            connection,
            connection,
            "terminal_auth_scope_epochs",
            destination_table=authority_table,
            predicate="scope_kind<>'session_visibility'",
            canonical_source=True,
        )
    for kind, name, _, _ in objects:
        if kind in {"trigger", "view"}:
            connection.execute(f"DROP {kind.upper()} IF EXISTS {identifier(name)}")
    retire_legacy_objects(connection)
    for table in retained:
        if table == authority_table:
            continue
        original = tables[table]
        compiled = domain_table_sql(table, canonical_table_sql(table, original))
        if compiled != original:
            temporary = "split_" + table
            prefix, _, _ = _parts(compiled)
            opening = prefix.index("(")
            renamed = "CREATE TABLE " + identifier(temporary) + " " + compiled[opening:]
            connection.execute(renamed)
            _copy_table(connection, connection, table, destination_table=temporary, canonical_source=True)
            connection.execute(f"DROP TABLE {identifier(table)}")
            connection.execute(f"ALTER TABLE {identifier(temporary)} RENAME TO {identifier(table)}")
    for domain in NEW_DOMAINS:
        for table in SPLIT_TABLES[domain]:
            if table not in _FTS_SHADOWS:
                connection.execute(f"DROP TABLE IF EXISTS {identifier(table)}")
    if authority_table in retained:
        original_epoch_guard = next(sql for kind, name, _, sql in objects if kind == "trigger" and name == "terminal_auth_epoch_monotonic")
        authority_epoch_guard = original_epoch_guard.replace("terminal_auth_epoch_monotonic", "terminal_authority_epoch_monotonic").replace(
            "terminal_auth_scope_epochs", authority_table
        )
        connection.execute(authority_epoch_guard)
    _restore_sequences(connection, sequences, retained)
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
    _install_objects(connection, (row for row in objects if row[0] != "index" or row[1] not in existing), "core")
    if any(table_evidence(connection, table) != before[table] for table in retained):
        raise SQLiteUpgradeError("Source cleanup changed retained core data; the cleanup transaction must be rolled back.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SQLiteUpgradeError("Required retained core foreign keys failed; the cleanup transaction must be rolled back.")


def _source_schema_digest() -> str:
    metadata = json.loads(Path(__file__).with_name("sqlite_domain_schema.json").read_text(encoding="utf-8"))
    if metadata.get("source_revision") != SOURCE_REVISION:
        raise SQLiteUpgradeError("The reviewed split schema revision is invalid; use a matching image.")
    return metadata["source_schema_digest"]


def _source_identity(connection: sqlite3.Connection) -> str:
    return static_schema_digest(canonical_source_rows(schema_rows(connection)))


def _expected_schema(source: sqlite3.Connection, domain: str) -> str:
    objects = schema_rows(source)
    with closing(sqlite3.connect(":memory:", factory=DiagnosticConnection)) as target:
        for kind, name, _, sql in objects:
            if kind == "table" and name in SPLIT_TABLES[domain] and name not in _FTS_SHADOWS:
                sql = canonical_table_sql(name, sql)
                target.execute(domain_table_sql(name, sql) if name != _FTS else sql)
        _install_objects(target, objects, domain)
        target.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)")
        target.execute("CREATE TABLE _phase0_probe (k TEXT PRIMARY KEY, ts REAL NOT NULL)")
        return schema_digest(target)


def _recover_domain(source: sqlite3.Connection, path: Path, domain: str, witnesses: Mapping[str, str]) -> dict[str, object] | None:
    if not path.exists():
        return None
    if database_head(path) is None:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as target:
            if target.execute("SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchone() is not None:
                raise SQLiteUpgradeError(f"Uncommitted target {path.name} contains an unknown schema; inspect it before continuing.")
        path.unlink()
        _sync_directory(path.parent)
        return None
    if database_head(path) != SPLIT_REVISION:
        raise SQLiteUpgradeError(f"Target {path.name} has an unexpected revision; use a matching image.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as target:
        expected_schema = _expected_schema(source, domain)
        if schema_digest(target) != expected_schema:
            raise SQLiteUpgradeError(f"Unrecorded target {path.name} differs from the reviewed schema; inspect its migration state.")
        evidence = {
            table: table_evidence(source, table, witnesses=witnesses, canonical_source=True)
            for table in SPLIT_TABLES[domain]
            if table not in _FTS_SHADOWS
        }
        expected = {"schema_digest": expected_schema, "evidence": evidence}
        verify_copied_domain(source, path, domain, expected, witnesses=witnesses)
        return expected


def _checkpoint(directory: Path, state: dict[str, object]) -> None:
    _write_json(directory / LAYOUT_FILE, state)


def _validate_ready(directory: Path, state: Mapping[str, object], *, require_wal: bool = True, expected_head: str | None = None) -> None:
    recorded_head = state.get("target_head")
    if not isinstance(recorded_head, str) or expected_head is not None and recorded_head != expected_head:
        raise SQLiteUpgradeError("The domain layout revision differs from this image; use a matching migration image.")
    for domain, filename in DOMAIN_TARGET_FILES.items():
        target = state["targets"][domain]
        path = directory / filename
        if database_head(path) != recorded_head or target.get("head") != recorded_head:
            raise SQLiteUpgradeError(f"Required database {filename} is not at its verified revision; inspect its migration state.")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as connection:
            if require_wal and domain in _WAL_DOMAINS and connection.execute("PRAGMA main.journal_mode").fetchone() != ("wal",):
                raise SQLiteUpgradeError(
                    f"Database {filename} has lost its required WAL mode; stop external database tools and inspect the layout before restarting."
                )
            if schema_digest(connection) != target.get("schema_digest"):
                raise SQLiteUpgradeError(f"Persistent schema verification failed for {filename}; inspect its migration state.")


def _enable_wal(directory: Path) -> None:
    for domain in _WAL_DOMAINS:
        path = directory / DOMAIN_TARGET_FILES[domain]
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, factory=DiagnosticConnection)) as target:
            target.execute("PRAGMA main.synchronous=FULL")
            if target.execute("PRAGMA main.journal_mode").fetchone() != ("wal",):
                if target.execute("PRAGMA main.journal_mode=WAL").fetchone() != ("wal",):
                    raise SQLiteUpgradeError(f"Required WAL setup failed for {path.name}; keep the application stopped and inspect its storage.")
        _sync_directory(directory)


def run_domain_upgrade(
    directory: Path,
    upgrade_one: Callable[[str, Path, bool], None],
    *,
    target_head: str | None = None,
    known_revisions: frozenset[str] | None = None,
    upgrade_append: Callable[[str, Path], None] | None = None,
    revision_order: tuple[str, ...] | None = None,
    fault: Callable[[str], None] = lambda phase: None,
) -> tuple[str, ...]:
    """Resume whole-domain transactions, then atomically clean core and compact once."""
    import anyio

    from bowhead_core.terminal_protocol_store import prepare_all_billing_witnesses_for_migration

    if target_head is None or known_revisions is None or revision_order is None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from bowhead_core import migrations

        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).parent))
        scripts = ScriptDirectory.from_config(config)
        target_head = target_head or scripts.get_current_head()
        known_revisions = known_revisions or frozenset(revision.revision for revision in scripts.walk_revisions())
        revision_order = revision_order or tuple(revision.revision for revision in reversed(list(scripts.iterate_revisions(target_head, "base"))))
    if (
        target_head not in known_revisions
        or target_head not in revision_order
        or SPLIT_REVISION not in revision_order
        or len(set(revision_order)) != len(revision_order)
        or not set(revision_order) <= known_revisions
    ):
        raise SQLiteUpgradeError("The requested domain revision is unknown; use a compatible application image.")
    with migration_lock(directory):
        for filename in DOMAIN_TARGET_FILES.values():
            _recover_rollback_journal(directory / filename)
        state = _load_state(directory)
        if state is None:
            raise SQLiteUpgradeError("The legacy upgrade checkpoint is missing; complete the startup baseline upgrade first.")
        if state["layout"] == DOMAIN_LAYOUT and (state["status"] == "ready" or state.get("phase") == "append"):
            return _run_append_upgrade(directory, state, target_head, known_revisions, upgrade_append, fault, revision_order)
        if state["layout"] != DOMAIN_LAYOUT:
            if state["status"] != "ready" or any(
                database_head(directory / filename) != SOURCE_REVISION for filename in ORIGINAL_TARGET_FILES.values()
            ):
                raise SQLiteUpgradeError("Every original target must complete the reviewed source revision before domain copying.")
            if any((directory / DOMAIN_TARGET_FILES[domain]).exists() for domain in NEW_DOMAINS):
                raise SQLiteUpgradeError("Unregistered domain files exist without a migration checkpoint; inspect the data directory.")
            with closing(sqlite3.connect(directory / ORIGINAL_TARGET_FILES["core"], factory=DiagnosticConnection)) as source:
                if _source_identity(source) != _source_schema_digest():
                    raise SQLiteUpgradeError("The source schema differs from the reviewed split revision; review schema drift before migration.")
            migration_id = state["migration_id"]
            backup = state.get("backup")
            if backup is not None:
                _verify_checkpoint_backup(directory, backup, migration_id)
            else:
                backup_path = backup_before_upgrade(directory, migration_id, dict.fromkeys(ORIGINAL_TARGET_FILES, SOURCE_REVISION))
                backup = str(backup_path) if backup_path is not None else None
            source_files = [directory / filename for filename in ORIGINAL_TARGET_FILES.values()]
            required = max(
                sum(
                    path.stat().st_size + (path.with_name(path.name + "-wal").stat().st_size if path.with_name(path.name + "-wal").exists() else 0)
                    for path in source_files
                )
                * 4,
                1024 * 1024,
            )
            available = shutil.disk_usage(directory).free
            if available < required:
                raise SQLiteUpgradeError(
                    f"Insufficient space for domain copies and VACUUM (required at least {required} bytes, available {available} bytes); free storage before restarting."
                )
            state = {
                "format": 1,
                "layout": DOMAIN_LAYOUT,
                "status": "in_progress",
                "phase": "copy",
                "migration_id": migration_id,
                "target_head": SPLIT_REVISION,
                "backup": backup,
                "targets": {
                    name: {"file": filename, "head": SOURCE_REVISION if name in ORIGINAL_TARGET_FILES else None}
                    for name, filename in DOMAIN_TARGET_FILES.items()
                },
            }
            _checkpoint(directory, state)
        if state.get("backup") is not None:
            _verify_checkpoint_backup(directory, state["backup"], state["migration_id"])
        verify_retirement_record(state.get("legacy_retirement_evidence"), state.get("backup"))
        core_path = directory / ORIGINAL_TARGET_FILES["core"]
        if state["phase"] == "copy":
            witnesses = anyio.run(lambda: prepare_all_billing_witnesses_for_migration(core_path, existing=state.get("billing_witnesses")))
            state["billing_witnesses"] = witnesses
            _checkpoint(directory, state)
            with closing(sqlite3.connect(core_path, factory=DiagnosticConnection)) as source:
                if database_head(core_path) != SOURCE_REVISION or _source_identity(source) != _source_schema_digest():
                    raise SQLiteUpgradeError("Source schema changed before domain copying; inspect the migration checkpoint.")
                retired = retirement_evidence(source, state.get("backup"))
                if state.get("legacy_retirement_evidence", retired) != retired:
                    raise SQLiteUpgradeError("Historical source data changed before copying; inspect its matching complete backup.")
                state["legacy_retirement_evidence"] = retired
                _checkpoint(directory, state)
                for domain in NEW_DOMAINS:
                    path = directory / DOMAIN_TARGET_FILES[domain]
                    target = state["targets"][domain]
                    if target.get("evidence") is None:
                        evidence = _recover_domain(source, path, domain, witnesses)
                        if evidence is None:
                            evidence = create_and_copy_domain(
                                source, path, domain, witnesses=witnesses, before_commit=lambda domain=domain: fault("before_commit:" + domain)
                            )
                        fault("after_commit:" + domain)
                        target.update(evidence)
                        target["head"] = SPLIT_REVISION
                        _checkpoint(directory, state)
                    verify_copied_domain(source, path, domain, target, witnesses=witnesses)
                # Every target must pass again before source cleanup can be admitted.
                for domain in NEW_DOMAINS:
                    verify_copied_domain(source, directory / DOMAIN_TARGET_FILES[domain], domain, state["targets"][domain], witnesses=witnesses)
            state["phase"] = "cleanup"
            _checkpoint(directory, state)
            fault("all_domains_verified")
        if state["phase"] in {"cleanup", "vacuum", "verify"}:
            for domain in NEW_DOMAINS:
                target_record = state["targets"][domain]
                with closing(
                    sqlite3.connect((directory / DOMAIN_TARGET_FILES[domain]).resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)
                ) as imported:
                    if schema_digest(imported) != target_record.get("schema_digest") or any(
                        table_evidence(imported, table) != expected for table, expected in target_record.get("evidence", {}).items()
                    ):
                        raise SQLiteUpgradeError(
                            f"Committed import evidence changed for {domain}; keep the application stopped and inspect the migration state."
                        )
        if state["phase"] == "cleanup":
            # The revision and cleanup share the original core database transaction.
            for name, filename in ORIGINAL_TARGET_FILES.items():
                current = database_head(directory / filename)
                if current == SOURCE_REVISION:
                    upgrade_one(name, directory / filename, name == "core")
                    fault("after_cleanup:" + name)
                elif current != SPLIT_REVISION:
                    raise SQLiteUpgradeError(f"Database {filename} has an unexpected cleanup revision; inspect it before restarting.")
                state["targets"][name]["head"] = SPLIT_REVISION
            state["phase"] = "vacuum"
            _checkpoint(directory, state)
        if state["phase"] == "vacuum":
            compact = directory / (".bowhead-compact-" + state["migration_id"] + ".db")
            compact.unlink(missing_ok=True)
            with closing(sqlite3.connect(core_path, factory=DiagnosticConnection)) as source:
                source.execute("PRAGMA synchronous=FULL")
                if source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise SQLiteUpgradeError("The core WAL is busy during offline compaction; stop all database writers before continuing.")
                source.execute("PRAGMA journal_mode=DELETE")
                before = {table: table_evidence(source, table) for table in SPLIT_TABLES["core"]}
                source_schema = schema_digest(source)
                source.execute("VACUUM INTO ?", (str(compact),))
            with closing(sqlite3.connect(compact, factory=DiagnosticConnection)) as candidate:
                if schema_digest(candidate) != source_schema or any(
                    table_evidence(candidate, table) != expected for table, expected in before.items()
                ):
                    raise SQLiteUpgradeError(
                        "Compaction would change retained identities or schema; the original core database is preserved for inspection."
                    )
                _assert_integrity(compact)
            with compact.open("rb") as stream:
                os.fsync(stream.fileno())
            compact.chmod(0o600)
            compact.replace(core_path)
            _sync_directory(directory)
            fault("after_vacuum")
            state["phase"] = "verify"
            _checkpoint(directory, state)
        if state["phase"] == "verify":
            _enable_wal(directory)
            for name, filename in DOMAIN_TARGET_FILES.items():
                path = directory / filename
                _assert_integrity(path)
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as target:
                    actual = {row[0] for row in target.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                    if actual != set(SPLIT_TABLES[name]) | {"alembic_version"} | ({"_phase0_probe"} if name in _PROBE_DOMAINS else set()):
                        raise SQLiteUpgradeError(f"Final table ownership verification failed for {filename}; inspect the migration state.")
                    state["targets"][name]["schema_digest"] = schema_digest(target)
                    state["targets"][name]["head"] = database_head(path)
            state["status"] = "ready"
            state["phase"] = "complete"
            state.pop("billing_witnesses", None)
            state.pop("legacy_retirement_evidence", None)
            _checkpoint(directory, state)
        _validate_ready(directory, state, expected_head=SPLIT_REVISION)
        return _run_append_upgrade(directory, state, target_head, known_revisions, upgrade_append, fault, revision_order)


def _run_append_upgrade(
    directory: Path,
    state: dict[str, object],
    head: str,
    known_revisions: frozenset[str],
    upgrade: Callable[[str, Path], None] | None,
    fault: Callable[[str], None],
    revision_order: tuple[str, ...],
) -> tuple[str, ...]:
    """Apply later revisions under the caller's exclusive whole-instance lease."""
    actual_heads = {name: database_head(directory / filename) for name, filename in DOMAIN_TARGET_FILES.items()}
    if state.get("target_head") not in revision_order or any(revision not in revision_order for revision in actual_heads.values()):
        raise SQLiteUpgradeError("The domain layout has unknown migration revisions; use a compatible application image.")
    positions = {revision: index for index, revision in enumerate(revision_order)}
    if positions[state["target_head"]] > positions[head] or any(
        positions[revision] > positions[head] or positions[revision] < positions[SPLIT_REVISION] for revision in actual_heads.values()
    ):
        raise SQLiteUpgradeError("The domain append would downgrade a database; use a compatible forward migration image.")
    if state["status"] == "ready":
        _validate_ready(directory, state, require_wal=False)
        if all(revision == head for revision in actual_heads.values()):
            _enable_wal(directory)
            _validate_ready(directory, state, expected_head=head)
            return tuple(DOMAIN_TARGET_FILES)
        if upgrade is None:
            raise SQLiteUpgradeError("Later domain revisions require the complete startup migration callback.")
        # A ready archive is captured before its record changes to in-progress.
        migration_id = uuid4().hex
        backup = backup_before_upgrade(directory, migration_id, actual_heads)
        state = {
            **state,
            "migration_id": migration_id,
            "backup": str(backup) if backup else None,
            "status": "in_progress",
            "phase": "append",
            "target_head": head,
        }
        state["targets"] = {name: dict(target) for name, target in state["targets"].items()}
        _checkpoint(directory, state)
    else:
        if state.get("phase") != "append" or state.get("target_head") != head:
            raise SQLiteUpgradeError("An interrupted domain append belongs to another image; restart its matching image first.")
        if state.get("backup") is None:
            raise SQLiteUpgradeError("The domain append backup is missing; inspect its checkpoint before continuing.")
        _verify_checkpoint_backup(directory, state["backup"], state["migration_id"])
        if upgrade is None:
            raise SQLiteUpgradeError("Interrupted domain revisions require the startup migration callback.")
    # Each committed target's schema digest is published before moving onward.
    # A database already at the requested head after a crash is reattested without
    # replaying its migration; unknown/intermediate mismatches stop before writes.
    for name, filename in DOMAIN_TARGET_FILES.items():
        path = directory / filename
        actual = actual_heads[name]
        recorded = state["targets"][name].get("head")
        if actual == recorded:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as connection:
                if schema_digest(connection) != state["targets"][name].get("schema_digest"):
                    raise SQLiteUpgradeError(f"Persistent schema drift before append for {filename}; inspect its migration record.")
        elif recorded not in positions or not positions[recorded] <= positions[actual] <= positions[head]:
            raise SQLiteUpgradeError(f"Interrupted target {filename} has an invalid revision path; inspect its migration checkpoint.")
        if actual != head:
            assert upgrade is not None
            upgrade(name, path)
            fault("append_after_commit:" + name)
        if database_head(path) != head:
            raise SQLiteUpgradeError(f"Domain target {filename} did not reach the requested image revision.")
        _assert_integrity(path)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, factory=DiagnosticConnection)) as connection:
            actual_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            reached = lambda revision: revision in positions and positions[revision] <= positions[head]  # noqa: E731
            expected_tables = set(domain_tables_at(name, reached)) | {"alembic_version"} | ({"_phase0_probe"} if name in _PROBE_DOMAINS else set())
            if actual_tables != expected_tables:
                raise SQLiteUpgradeError(f"Post-append table ownership verification failed for {filename}; inspect its migration revision.")
            state["targets"][name]["schema_digest"] = schema_digest(connection)
        state["targets"][name]["head"] = head
        _checkpoint(directory, state)
    _enable_wal(directory)
    _validate_ready(directory, state, expected_head=head)
    state["status"] = "ready"
    state["phase"] = "complete"
    _checkpoint(directory, state)
    return tuple(DOMAIN_TARGET_FILES)
