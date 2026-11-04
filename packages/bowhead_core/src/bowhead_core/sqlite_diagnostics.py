"""Bounded, value-free SQLite error diagnostics for sync and async connections.

Only original statement structure is inspected. Expanded SQL, bound arguments,
result rows and exception messages never enter a diagnostic event.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import aiosqlite
import structlog

log = structlog.get_logger(__name__)
_MAX_SQL_BYTES = 16_384
_MAX_TEMPLATE_BYTES = 2_048
_MAX_TABLES = 16
_SQL_WORDS = frozenset(
    "abort action add after all alter analyze and as asc attach autoincrement before begin between by cascade case cast check collate column commit conflict constraint create cross current_date current_time current_timestamp database default deferrable deferred delete desc detach distinct do drop each else end escape except exclude exclusive exists explain fail filter first following for foreign from full generated glob group groups having if ignore immediate in index indexed initially inner insert instead intersect into is isnull join key last left like limit match materialized natural no not nothing notnull null nulls of offset on or order others outer over partition plan pragma preceding primary query raise range recursive references regexp reindex release rename replace restrict right rollback row rows savepoint select set strict table temp temporary then ties to transaction trigger unbounded union unique update using vacuum values view virtual when where window with without".split()  # noqa: SIM905 -- Compact reviewed SQL vocabulary.
)
_SQL_FUNCTIONS = frozenset(
    "abs avg changes char coalesce count date datetime hex ifnull instr json json_array json_array_length json_each json_extract json_group_array json_group_object json_insert json_object json_patch json_remove json_replace json_set json_tree json_type json_valid julianday length lower ltrim max min nullif printf quote random randomblob replace round rtrim strftime substr substring sum total trim typeof unicode upper zeroblob".split()  # noqa: SIM905 -- Compact reviewed SQL vocabulary.
)


def _reviewed_identifiers() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, INFRASTRUCTURE_TABLES, TABLE_DOMAINS

    policy = json.loads(Path(__file__).with_name("account_erasure_policy.json").read_text())
    tables = set(policy["tables"])
    names = set(tables)
    for item in policy["tables"].values():
        names.update(item["columns"])
    for secondary in policy["secondary"].values():
        tables.update(secondary)
        names.update(secondary)
        for columns in secondary.values():
            names.update(columns)
    tables.update(
        {
            "alembic_version",
            "_phase0_probe",
            "sqlite_schema",
            "sqlite_master",
            "sqlite_temp_master",
            "business_rules",
            "mcp_servers",
            "artifact_stdout",
            "pbi_connection_legacy_0022",
        }
    )
    tables.update(TABLE_DOMAINS)
    tables.update(INFRASTRUCTURE_TABLES)
    names.update(tables)
    names.update({"main", "temp", "version_num", "rowid", "oid", "_rowid_", "new", "old", "busy_timeout", "foreign_keys", "synchronous"})
    filenames = set(policy["secondary"])
    filenames.update(DOMAIN_TARGET_FILES.values())
    return frozenset(word.lower() for word in names), frozenset(tables), frozenset(filenames)


_IDENTIFIERS, _TABLES, _DATABASE_FILES = _reviewed_identifiers()
_SAFE_WORDS = _IDENTIFIERS | _SQL_WORDS | _SQL_FUNCTIONS
_SQLITE_ERROR_NAMES = frozenset(name for name in dir(sqlite3) if name.startswith("SQLITE_"))


@dataclass(frozen=True, slots=True)
class StatementSummary:
    template: str | None
    fingerprint: str | None
    tables: tuple[str, ...]
    reason: str | None = None


def summarize_statement(sql: str | None) -> StatementSummary:
    """Lex SQL without retaining literals, comments or unreviewed identifiers."""
    if sql is None or len(sql) > _MAX_SQL_BYTES:
        return StatementSummary(None, None, (), "statement_unavailable_or_too_large")
    tokens: list[str] = []
    i = 0
    while i < len(sql):
        char = sql[i]
        if char.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                return StatementSummary(None, None, (), "unterminated_comment")
            i = end + 2
        elif char in "'\"`[":
            closing = "]" if char == "[" else char
            j = i + 1
            value: list[str] = []
            while j < len(sql):
                if sql[j] == closing:
                    if j + 1 < len(sql) and sql[j + 1] == closing:
                        value.append(closing)
                        j += 2
                        continue
                    break
                value.append(sql[j])
                j += 1
            if j == len(sql):
                return StatementSummary(None, None, (), "unterminated_quoted_token")
            word = "".join(value).lower()
            tokens.append(word if char != "'" and word in _IDENTIFIERS else "?")
            i = j + 1
        elif char in ":@$?":
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            tokens.append("?")
        elif char.isdigit() or (char == "." and i + 1 < len(sql) and sql[i + 1].isdigit()):
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in "._+-"):
                i += 1
            tokens.append("?")
        elif char.isalpha() or char == "_":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j].lower()
            tokens.append(word if word in _SAFE_WORDS else "?")
            i = j
        elif char in "(),.;=<>!+-*/%&|~":
            tokens.append(char)
            i += 1
        else:
            tokens.append("?")
            i += 1
    tables: list[str] = []
    for index, token in enumerate(tokens):
        if token not in {"from", "join", "into", "update", "table"}:
            continue
        following = index + 1
        while following < len(tokens) and tokens[following] in {"if", "not", "exists", "only"}:
            following += 1
        if following + 1 < len(tokens) and tokens[following + 1] == ".":
            following += 2
        if following < len(tokens) and tokens[following] in _TABLES and tokens[following] not in tables:
            tables.append(tokens[following])
    template = " ".join(tokens)
    if len(template) > _MAX_TEMPLATE_BYTES:
        return StatementSummary(None, None, tuple(tables[:_MAX_TABLES]), "template_too_large")
    return StatementSummary(template, hashlib.sha256(template.encode()).hexdigest(), tuple(tables[:_MAX_TABLES]))


@dataclass(frozen=True, slots=True)
class _OperationContext:
    callsite: str
    request_ids: tuple[tuple[str, str], ...]


_CONTEXT: contextvars.ContextVar[_OperationContext | None] = contextvars.ContextVar("sqlite_diagnostic_context", default=None)
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _operation_context() -> _OperationContext:
    frame = sys._getframe(1)
    callsite = "unavailable"
    for _ in range(24):
        if frame is None:
            break
        filename = Path(frame.f_code.co_filename).name
        if filename not in {"sqlite_diagnostics.py", "sqlite_domain_runtime.py", "_db.py", "core.py", "cursor.py"}:
            safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "?", filename)[:80]
            safe_function = re.sub(r"[^A-Za-z0-9_.-]", "?", frame.f_code.co_name)[:80]
            callsite = f"{safe_filename}:{frame.f_lineno}:{safe_function}"
            break
        frame = frame.f_back
    ids = tuple(
        (key, value)
        for key, value in structlog.contextvars.get_contextvars().items()
        if key in {"request_id", "trace_id", "turn_id"} and isinstance(value, str) and _SAFE_ID.fullmatch(value)
    )
    return _OperationContext(callsite, ids)


def _kind(sql: str) -> str:
    remaining = sql.lstrip()
    while remaining.startswith(("--", "/*")):
        if remaining.startswith("--"):
            remaining = remaining.partition("\n")[2].lstrip()
        else:
            _, end, remaining = remaining.partition("*/")
            if not end:
                return "unknown"
            remaining = remaining.lstrip()
    match = re.match(r"[A-Za-z]+", remaining)
    word = match[0].lower() if match else "unknown"
    return word if word in _SQL_WORDS else "unknown"


_T = TypeVar("_T")


class DiagnosticConnection(sqlite3.Connection):
    """SQLite factory retaining bounded statement context, not parameter values."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        database = kwargs.get("database", args[0] if args else "")
        name = Path(str(database).removeprefix("file:").split("?", 1)[0]).name
        self.bowhead_database = name if name in _DATABASE_FILES else "unavailable"
        self.bowhead_connection_id = uuid4().hex
        self.bowhead_transaction_id: str | None = None
        self.bowhead_transaction_started: float | None = None
        self.bowhead_transaction_mode = "unknown"
        self.bowhead_busy_timeout_ms = int(float(kwargs.get("timeout", args[1] if len(args) > 1 else 5.0)) * 1000)
        self.bowhead_recent_sql: str | None = None
        self.bowhead_recent_phase = "unavailable"
        self.bowhead_recent_completed = False

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if not args:
            kwargs.setdefault("factory", DiagnosticCursor)
        return super().cursor(*args, **kwargs)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self.cursor().executescript(sql_script)

    def _operation(self, function: Callable[[], _T], phase: str, sql: str | None = None) -> _T:
        started = time.monotonic()
        if sql is not None:
            self.bowhead_recent_sql = sql if len(sql) <= _MAX_SQL_BYTES else None
            self.bowhead_recent_phase = phase
            self.bowhead_recent_completed = False
        before = self.in_transaction
        if phase == "begin" and not before:
            self.bowhead_transaction_id = uuid4().hex
            self.bowhead_transaction_started = started
            try:
                begin_words = (summarize_statement(sql).template or "").split()
                self.bowhead_transaction_mode = next((mode for mode in ("immediate", "exclusive", "deferred") if mode in begin_words), "deferred")
            except Exception:
                self.bowhead_transaction_mode = "unknown"
        try:
            result = function()
        except sqlite3.Error as error:
            self._report(error, phase, started)
            if not self.in_transaction:
                self.bowhead_transaction_started = None
                self.bowhead_transaction_id = None
                self.bowhead_transaction_mode = "unknown"
            raise
        else:
            self.bowhead_recent_completed = True
            if phase == "begin" and not before and self.in_transaction:
                self.bowhead_transaction_started = time.monotonic()
            if self.in_transaction and not before and self.bowhead_transaction_started is None:
                self.bowhead_transaction_started = started
                self.bowhead_transaction_id = uuid4().hex
                self.bowhead_transaction_mode = "implicit"
            if sql is not None:
                timeout = re.fullmatch(r"\s*PRAGMA\s+busy_timeout\s*=\s*(\d+)\s*;?\s*", sql, re.I)
                if timeout:
                    self.bowhead_busy_timeout_ms = int(timeout[1])
            if not self.in_transaction:
                self.bowhead_transaction_started = None
                self.bowhead_transaction_id = None
                self.bowhead_transaction_mode = "unknown"
            return result

    def _report(self, error: sqlite3.Error, phase: str, started: float) -> None:
        try:
            if isinstance(error, (sqlite3.IntegrityError, sqlite3.ProgrammingError)):
                return
            if getattr(error, "bowhead_diagnostic_id", None) is not None:
                return
            diagnostic_id = uuid4().hex
            error.bowhead_diagnostic_id = diagnostic_id
            code = getattr(error, "sqlite_errorcode", None)
            name = getattr(error, "sqlite_errorname", None)
            try:
                summary = summarize_statement(self.bowhead_recent_sql)
            except Exception:
                summary = StatementSummary(None, None, (), "redaction_failed")
            context = _CONTEXT.get() or _operation_context()
            fields = {
                "diagnostic_id": diagnostic_id,
                "database": self.bowhead_database,
                "connection_id": self.bowhead_connection_id,
                "transaction_id": self.bowhead_transaction_id,
                "transaction_mode": self.bowhead_transaction_mode,
                "transaction_elapsed_ms": None
                if self.bowhead_transaction_started is None
                else round((time.monotonic() - self.bowhead_transaction_started) * 1000, 3),
                "in_transaction": self.in_transaction,
                "phase": phase,
                "operation": _kind(self.bowhead_recent_sql or ""),
                "access": (
                    "read"
                    if _kind(self.bowhead_recent_sql or "") == "select"
                    else "write"
                    if _kind(self.bowhead_recent_sql or "") in {"insert", "update", "delete", "replace", "create", "drop", "alter"}
                    else "unknown"
                ),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "busy_timeout_ms": self.bowhead_busy_timeout_ms,
                "exception_type": type(error).__name__,
                "sqlite_errorcode": code,
                "sqlite_primary_errorcode": code & 0xFF if isinstance(code, int) else None,
                "sqlite_errorname": name if isinstance(name, str) and name in _SQLITE_ERROR_NAMES else None,
                "sql_template": summary.template,
                "sql_fingerprint": summary.fingerprint,
                "tables": summary.tables,
                "tables_source": "reviewed_sql_tokens",
                "sql_unavailable_reason": summary.reason,
                "callsite": context.callsite,
                "statement_completed": self.bowhead_recent_completed,
                "retry_attempt": None,
                **dict(context.request_ids),
            }
            version = os.environ.get("BOWHEAD_VERSION", "")
            fields["app_version"] = version if _SAFE_ID.fullmatch(version) else "unavailable"
            # Do not merge unrelated context variables or include the exception's raw message.
            contextvars.Context().run(log.error, "sqlite.operation_failed", **fields)
        except Exception:  # noqa: S110 -- A failing diagnostic sink must not replace the SQLite error.
            # Diagnostics must preserve the original SQLite error even if the log sink fails.
            pass

    def commit(self) -> None:
        self._operation(super().commit, "commit")

    def rollback(self) -> None:
        self._operation(super().rollback, "rollback")


class DiagnosticCursor(sqlite3.Cursor):
    def _run(self, function: Callable[[], _T], phase: str, sql: str | None = None) -> _T:
        if sql is not None:
            self.bowhead_sql = sql if len(sql) <= _MAX_SQL_BYTES else None
        elif phase == "fetch":
            sql = getattr(self, "bowhead_sql", None)
        connection = self.connection
        if isinstance(connection, DiagnosticConnection):
            return connection._operation(function, phase, sql)
        return function()

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        phase = _kind(sql)
        return self._run(
            lambda: super(DiagnosticCursor, self).execute(sql, parameters), phase if phase in {"begin", "commit", "rollback"} else "execute", sql
        )

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return self._run(lambda: super(DiagnosticCursor, self).executemany(sql, parameters), "execute_many", sql)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self._run(lambda: super(DiagnosticCursor, self).executescript(sql_script), "script", sql_script)

    def fetchone(self) -> Any:
        return self._run(super().fetchone, "fetch")

    def fetchmany(self, size: int | None = None) -> list[Any]:
        return self._run(super().fetchmany if size is None else lambda: super(DiagnosticCursor, self).fetchmany(size), "fetch")

    def fetchall(self) -> list[Any]:
        return self._run(super().fetchall, "fetch")

    def __next__(self) -> Any:
        return self._run(super().__next__, "fetch")


class DiagnosticAsyncConnection(aiosqlite.Connection):
    def __init__(self, connector: Callable[[], sqlite3.Connection], iter_chunk_size: int, *, database: str = "unavailable") -> None:
        super().__init__(connector, iter_chunk_size)
        self._diagnostic_database = database

    async def _connect(self) -> DiagnosticAsyncConnection:
        started = time.monotonic()
        try:
            return await super()._connect()
        except sqlite3.Error as error:
            try:
                identifier = uuid4().hex
                error.bowhead_diagnostic_id = identifier
                context = _operation_context()
                code = getattr(error, "sqlite_errorcode", None)
                fields = {
                    "diagnostic_id": identifier,
                    "database": self._diagnostic_database,
                    "phase": "connect",
                    "operation": "connect",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "sqlite_errorcode": code,
                    "sqlite_primary_errorcode": code & 0xFF if isinstance(code, int) else None,
                    "sqlite_errorname": getattr(error, "sqlite_errorname", None)
                    if getattr(error, "sqlite_errorname", None) in _SQLITE_ERROR_NAMES
                    else None,
                    "exception_type": type(error).__name__,
                    "callsite": context.callsite,
                    "tables": (),
                    "sql_template": None,
                    "sql_unavailable_reason": "connection_not_open",
                    **dict(context.request_ids),
                }
                contextvars.Context().run(log.error, "sqlite.operation_failed", **fields)
            except Exception:  # noqa: S110 -- Preserve the connection error when diagnostics fail.
                pass
            raise

    async def _execute(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        context = contextvars.Context()
        context.run(_CONTEXT.set, _operation_context())
        return await super()._execute(context.run, fn, *args, **kwargs)


def connect(database: str | Path, *, iter_chunk_size: int = 64, **kwargs: Any) -> DiagnosticAsyncConnection:
    """Create an async diagnostic connection without changing sqlite3 or aiosqlite globally."""
    kwargs.setdefault("factory", DiagnosticConnection)
    name = Path(str(database).removeprefix("file:").split("?", 1)[0]).name
    return DiagnosticAsyncConnection(
        lambda: sqlite3.connect(str(database), **kwargs), iter_chunk_size, database=name if name in _DATABASE_FILES else "unavailable"
    )


def clear_diagnostics(connection: sqlite3.Connection) -> None:
    """Clear borrowed-connection statement context before returning it to a pool."""
    if isinstance(connection, DiagnosticConnection):
        connection.bowhead_recent_sql = None
        connection.bowhead_recent_phase = "unavailable"
        connection.bowhead_recent_completed = False
        if not connection.in_transaction:
            connection.bowhead_transaction_started = None
            connection.bowhead_transaction_id = None
            connection.bowhead_transaction_mode = "unknown"


def diagnostic_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    """Safe metadata for the existing slow-open-transaction warning."""
    if not isinstance(connection, DiagnosticConnection):
        return {}
    try:
        summary = summarize_statement(connection.bowhead_recent_sql)
    except Exception:
        summary = StatementSummary(None, None, (), "redaction_failed")
    return {
        "database": connection.bowhead_database,
        "connection_id": connection.bowhead_connection_id,
        "transaction_id": connection.bowhead_transaction_id,
        "transaction_mode": connection.bowhead_transaction_mode,
        "transaction_elapsed_ms": None
        if connection.bowhead_transaction_started is None
        else round((time.monotonic() - connection.bowhead_transaction_started) * 1000, 3),
        "sql_template": summary.template,
        "tables": summary.tables,
        "tables_source": "reviewed_sql_tokens",
        "statement_completed": connection.bowhead_recent_completed,
    }
