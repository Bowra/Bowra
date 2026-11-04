"""Reviewed cross-domain SQLite guards and managed connection lifecycle.

Persistent guards call fixed predicates shipped with the application. Attached databases
are read-only. A process-shared authority gate prevents revocation races while allowing
independent domain writers to hold shared leases concurrently.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import re
import sqlite3
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import structlog

from bowhead_core.sqlite_diagnostics import DiagnosticAsyncConnection, DiagnosticConnection, DiagnosticCursor
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, TABLE_DOMAINS

_REGISTRY = json.loads(Path(__file__).with_name("sqlite_domain_guards.json").read_text(encoding="utf-8"))
_TRIGGERS: dict[str, dict[str, Any]] = _REGISTRY["triggers"]
_VIEWS: dict[str, dict[str, Any]] = _REGISTRY["views"]
_AUTHORITY: dict[str, dict[str, list[str]]] = _REGISTRY["authority_columns"]
_LOG = structlog.get_logger(__name__)


def compile_domain_trigger(name: str, sql: str) -> str:
    """Rewrite only frozen, reviewed cross-domain trigger definitions."""
    spec = _TRIGGERS.get(name)
    if spec is None:
        clean = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
        table = re.search(r'\bON\s+["`\[]?([A-Za-z_]\w*)', clean, re.I)
        owner = TABLE_DOMAINS.get(table[1]) if table else None
        if owner and any(word in TABLE_DOMAINS and TABLE_DOMAINS[word] != owner for word in re.findall(r"\b\w+\b", clean)):
            raise ValueError(f"Unreviewed cross-domain trigger: {name}")
        return sql
    if sql.strip().rstrip(";") != spec["source"].strip().rstrip(";"):
        raise ValueError(f"Unreviewed cross-domain trigger definition: {name}")
    return str(spec["compiled"])


def compile_domain_view(name: str, sql: str) -> str:
    """Preserve the three reviewed retention/scope view contracts."""
    spec = _VIEWS.get(name)
    if spec is None:
        raise ValueError(f"Unreviewed cross-domain view: {name}")
    if sql.strip().rstrip(";") != spec["source"].strip().rstrip(";"):
        raise ValueError(f"Unreviewed cross-domain view definition: {name}")
    return str(spec["compiled"])


def domain_authority_triggers(domain: str) -> tuple[str, ...]:
    """Persistent admission fences for every reviewed cross-file authority write.

    Ordinary balance fields are excluded. The generated definitions are part of
    the migration-owned schema, so an unmanaged writer fails closed rather than
    silently bypassing the process-shared lease protocol.
    """
    statements = []
    for table, columns in sorted(_AUTHORITY.get(domain, {}).items()):
        operations = (("insert", "INSERT"), ("delete", "DELETE"))
        update_columns = ",".join(f'"{column}"' for column in columns if column)
        if update_columns:
            operations += (("update", f"UPDATE OF {update_columns}"),)
        for suffix, operation in operations:
            statements.append(
                f'CREATE TRIGGER "trg_domain_authority_{table}_{suffix}" BEFORE {operation} ON "{table}" '
                f"BEGIN SELECT bowhead_domain_authority_v1('{table}'); END"
            )
    return tuple(statements)


def domain_additional_triggers(domain: str) -> tuple[str, ...]:
    """New split-only diagnostic privacy admission; no plaintext payload reads."""
    if domain != "observability":
        return ()
    return (
        "CREATE TRIGGER trg_domain_llm_request_records_scope BEFORE INSERT ON llm_request_records "
        "WHEN bowhead_domain_request_scope_v1(NEW.turn_id,NEW.session_id,NEW.user_id,NEW.workspace_id,NEW.team_id)<>1 "
        "BEGIN SELECT RAISE(ROLLBACK,'request diagnostic source is no longer available'); END",
        "CREATE TRIGGER trg_domain_llm_request_chunks_scope BEFORE INSERT ON llm_request_chunks "
        "WHEN bowhead_domain_request_turn_scope_v1(NEW.turn_id)<>1 "
        "BEGIN SELECT RAISE(ROLLBACK,'request diagnostic source is no longer available'); END",
        "CREATE TRIGGER trg_domain_llm_request_changes_scope BEFORE INSERT ON llm_request_changes "
        "WHEN bowhead_domain_request_record_scope_v1(NEW.record_id)<>1 "
        "BEGIN SELECT RAISE(ROLLBACK,'request diagnostic source is no longer available'); END",
    )


def _busy() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked: SQLite authority lease is busy; retry the complete transaction")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = "SQLITE_BUSY"
    return error


class DomainConnection(DiagnosticConnection):
    """Diagnostic SQLite factory; domain behavior is enabled by guard_connection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._domain: str | None = None
        self._core_path: Path | None = None
        self._gates: dict[str, Any] = {}
        self._leases: dict[str, int] = {}
        self._external_authorizer: Any = None
        self._profiles: dict[str, dict[str, int]] = {}
        self._preparing: list[str] = []
        self._active_readers: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()
        self._domain_cursors: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()
        self._operation_depth = 0
        self._gate_failed = False
        self._callback_busy: sqlite3.Error | None = None
        self._lost_lease = False
        self._cleanup_events: set[tuple[str, str]] = set()
        self._draining_cleanup = False

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if not args:
            kwargs.setdefault("factory", DomainCursor)
        factory = args[0] if args else kwargs["factory"]
        if self._domain is not None and (not isinstance(factory, type) or not issubclass(factory, DomainCursor)):
            raise sqlite3.ProgrammingError("Split SQLite cursors require DomainCursor")
        cursor = super().cursor(*args, **kwargs)
        self._domain_cursors.add(cursor)
        return cursor

    def set_authorizer(self, authorizer_callback: Any) -> None:
        self._external_authorizer = authorizer_callback
        super().set_authorizer(self._authorize if self._domain else authorizer_callback)
        # SQLite expires prepared statements when its authorizer is replaced. Only
        # at this boundary can cached security profiles be discarded safely.
        self._profiles.clear()

    def _acquire(self, domain: str, mode: int) -> None:
        if mode <= self._leases.get(domain, 0):
            return
        gate = self._gates.get(domain)
        if gate is None:
            assert self._core_path is not None
            gate = self._core_path.with_name(f".sqlite-authority-{domain}.lock").open("a+b")
            self._gates[domain] = gate
        previous = self._leases.get(domain, 0)
        try:
            # No waiting while retaining a database writer or snapshot. Competing
            # cross-domain upgrades fail and require rollback of the whole operation.
            fcntl.flock(gate.fileno(), (fcntl.LOCK_EX if mode == 2 else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            if previous == 1:
                self._lost_lease = True
                try:
                    fcntl.flock(gate.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Do not allow any subsequent SQL to use an unprotected old
                    # snapshot; rollback/close remain available to the caller.
                    self._leases.pop(domain, None)
                    self._lost_lease = True
            raise _busy() from None
        self._leases[domain] = mode

    def _release(self) -> None:
        for domain in list(self._leases):
            fcntl.flock(self._gates[domain].fileno(), fcntl.LOCK_UN)
        self._leases.clear()
        self._lost_lease = False

    def _settle(self) -> None:
        if self._operation_depth or self.in_transaction or self._active_readers:
            return
        self._release()
        if self._cleanup_events and not self._draining_cleanup:
            self._drain_cleanup()

    def _authorize(self, action: int, first: str | None, second: str | None, database: str | None, source: str | None) -> int:
        required_domain = None
        required = 0
        if action == sqlite3.SQLITE_PRAGMA and second and first in {"table_info", "table_xinfo", "foreign_key_list", "index_list"}:
            owner = TABLE_DOMAINS.get(second.strip('"`[]'))
            if owner and owner != self._domain:
                required_domain, required = owner, 1
        if action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_PRAGMA} and database and database.startswith("domain_"):
            # Any read pins that file's snapshot. Fence before its first read,
            # including reads preceding a later security predicate in the same tx.
            required_domain = database.removeprefix("domain_")
            required = 1
        elif database == "main" and first in _AUTHORITY.get(self._domain or "", {}):
            columns = _AUTHORITY[self._domain][first]
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE} or action == sqlite3.SQLITE_UPDATE and second in columns:
                required_domain = self._domain
                required = 2
        if required_domain is not None:
            if self._preparing:
                profile = self._profiles.setdefault(self._preparing[-1], {})
                profile[required_domain] = max(required, profile.get(required_domain, 0))
            try:
                self._acquire(required_domain, required)
            except sqlite3.OperationalError:
                self._gate_failed = True
                return sqlite3.SQLITE_DENY
        if self._external_authorizer is not None:
            return int(self._external_authorizer(action, first, second, database, source))
        return sqlite3.SQLITE_OK

    def _operation(self, function: Any, phase: str, sql: str | None = None) -> Any:
        if self._domain is None:
            return super()._operation(function, phase, sql)
        self._operation_depth += 1
        if sql is not None:
            self._preparing.append(sql)
        try:

            def guarded() -> Any:
                if self._lost_lease and phase != "rollback":
                    raise _busy()
                for domain, mode in self._profiles.get(sql or "", {}).items():
                    self._acquire(domain, mode)
                try:
                    return function()
                except sqlite3.DatabaseError:
                    if self._callback_busy is not None:
                        error, self._callback_busy = self._callback_busy, None
                        raise error from None
                    if self._gate_failed:
                        self._gate_failed = False
                        raise _busy() from None
                    raise

            result = super()._operation(guarded, phase, sql)
            if phase == "rollback" and not self.in_transaction:
                self._cleanup_events.clear()
            return result
        finally:
            if sql is not None:
                self._preparing.pop()
            self._operation_depth -= 1
            self._settle()

    def _request_scope(self, turn_id: str, session_id: str, user_id: str, workspace_id: str, team_id: str) -> int:
        if self._domain != "observability":
            raise sqlite3.OperationalError("Unknown SQLite request diagnostic authority")
        return int(
            self._scalar(
                """SELECT EXISTS(SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
            JOIN workspaces w ON w.id=s.workspace_id JOIN users u ON u.id=s.user_id
            WHERE t.id=? AND s.id=? AND s.user_id=? AND s.workspace_id=? AND w.team_id=?
              AND u.status='active' AND s.deleted_at IS NULL
              AND NOT EXISTS(SELECT 1 FROM account_deletions d WHERE d.user_id=u.id))""",
                (turn_id, session_id, user_id, workspace_id, team_id),
            )
        )

    def _request_child_scope(self, key: str, *, record: bool) -> int:
        if self._domain != "observability":
            raise sqlite3.OperationalError("Unknown SQLite request diagnostic authority")
        selector = "r.id=?" if record else "r.turn_id=?"
        return int(
            self._scalar(
                "SELECT EXISTS(SELECT 1 FROM llm_request_records r JOIN turns t ON t.id=r.turn_id "
                "JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
                "JOIN users u ON u.id=s.user_id WHERE "
                + selector
                + " AND r.session_id=s.id AND r.user_id=s.user_id AND r.workspace_id=s.workspace_id AND r.team_id=w.team_id "
                "AND u.status='active' AND s.deleted_at IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM account_deletions d WHERE d.user_id=u.id))",
                (key,),
            )
        )

    def _authority(self, table: str) -> int:
        if table not in _AUTHORITY.get(self._domain or "", {}):
            raise sqlite3.OperationalError("Unknown SQLite domain authority")
        try:
            assert self._domain is not None
            self._acquire(self._domain, 2)
        except sqlite3.Error as error:
            self._callback_busy = error
            raise
        return 1

    def _scalar(self, sql: str, values: Any) -> Any:
        try:
            with self.execute(sql, values) as cursor:
                row = cursor.fetchone()
                return row[0] if row else None
        except sqlite3.Error as error:
            # sqlite3 replaces exceptions crossing a Python UDF boundary with a
            # generic SQLITE_ERROR. Carry contention back to the outer operation
            # so callers receive the original BUSY/LOCKED code and diagnostic ID.
            code = getattr(error, "sqlite_errorcode", 0)
            if code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                self._callback_busy = error
            raise

    def _predicate(self, name: str, *values: Any) -> int:
        spec = _TRIGGERS.get(name)
        if spec is None or spec.get("cleanup") or spec["domain"] != self._domain or len(values) != len(spec["refs"]):
            raise sqlite3.OperationalError("Unknown SQLite domain guard")
        return int(self._scalar(spec["predicate"], values))

    def _workspace_team(self, workspace_id: str) -> str | None:
        return self._scalar("SELECT team_id FROM workspaces WHERE id=?", (workspace_id,))

    def _retention_days(self, team_id: str, version: int) -> int | None:
        return self._scalar("SELECT MIN(days) FROM team_reference_retention_policies WHERE team_id=? AND version>=?", (team_id, version))

    def _enqueue_cleanup(self, name: str, key: str) -> int:
        if name not in _TRIGGERS or not _TRIGGERS[name].get("cleanup") or self._domain != "execution":
            raise sqlite3.OperationalError("Unknown SQLite domain cleanup")
        self._cleanup_events.add((name, key))
        return 0

    def _drain_cleanup(self) -> None:
        """Ordinary cross-file cleanup runs after the source writer is released.

        A crash can leave tolerated orphan rows; privacy erasure independently
        enumerates and verifies every target. No distributed atomicity is claimed.
        Final source state also filters events undone by ROLLBACK TO SAVEPOINT.
        """
        events, self._cleanup_events = self._cleanup_events, set()
        self._draining_cleanup = True
        try:
            for name, key in events:
                target = "core" if name == "trg_memory_turn_requests_turn_delete" else "observability"
                try:
                    self._cleanup_event(name, key, target)
                except Exception as error:
                    # Continue independent ordinary cleanup after a failed target.
                    # Neither row keys nor raw exception messages enter diagnostics.
                    with contextlib.suppress(Exception):
                        contextvars.Context().run(
                            _LOG.warning,
                            "sqlite.domain_cleanup_deferred",
                            target=target,
                            exception_type=type(error).__name__,
                            diagnostic_id=getattr(error, "bowhead_diagnostic_id", None),
                        )
        finally:
            self._draining_cleanup = False
            self._settle()

    def _cleanup_event(self, name: str, key: str, target: str) -> None:
        session = name == "trg_llm_request_session_delete"
        sql = "SELECT deleted_at FROM sessions WHERE id=?" if session else "SELECT 1 FROM turns WHERE id=?"
        with self.execute(sql, (key,)) as cursor:
            row = cursor.fetchone()
        if (session and (row is None or row[0] is None)) or (not session and row is not None):
            return
        assert self._core_path is not None
        path = self._core_path.with_name(DOMAIN_TARGET_FILES[target])
        other = sqlite3.connect(path.as_uri() + "?mode=rw", factory=DomainConnection, uri=True)
        try:
            guard_connection(other, self._core_path, target)
            if target == "core":
                other.execute("DELETE FROM memory_turn_requests WHERE turn_id=?", (key,)).close()
            elif session:
                other.execute("BEGIN IMMEDIATE").close()
                # A previous source-delete crash can leave request rows after the
                # execution Turn is gone. Preserve their ownership evidence until
                # every associated chunk has been deleted in this same obs tx.
                with other.execute("SELECT DISTINCT turn_id FROM llm_request_records WHERE session_id=?", (key,)) as records:
                    turn_ids = {row[0] for row in records.fetchall()}
                with self.execute("SELECT id FROM turns WHERE session_id=?", (key,)) as turns:
                    turn_ids.update(row[0] for row in turns.fetchall())
                other.execute("DELETE FROM llm_request_records WHERE session_id=?", (key,)).close()
                other.executemany("DELETE FROM llm_request_chunks WHERE turn_id=?", [(turn_id,) for turn_id in turn_ids]).close()
            else:
                other.execute("DELETE FROM llm_request_records WHERE turn_id=?", (key,)).close()
                other.execute("DELETE FROM llm_request_chunks WHERE turn_id=?", (key,)).close()
            other.commit()
        finally:
            other.close()

    def __enter__(self) -> DomainConnection:
        return self

    def __exit__(self, exception_type: Any, _exception: Any, _traceback: Any) -> bool:
        if exception_type is None:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        else:
            self.rollback()
        return False

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._release()
            for gate in self._gates.values():
                gate.close()
            self._gates.clear()


class DomainCursor(DiagnosticCursor):
    """Keep authority leases until autocommit read cursors finish or close."""

    def _run(self, function: Any, phase: str, sql: str | None = None) -> Any:
        connection = self.connection
        if not isinstance(connection, DomainConnection) or connection._domain is None:
            return super()._run(function, phase, sql)
        connection._operation_depth += 1
        try:
            result = super()._run(function, phase, sql)
            if phase in {"execute", "execute_many", "script"} and self.description is not None:
                connection._active_readers.add(self)
            if phase == "fetch" and (result is None or result == []):
                connection._active_readers.discard(self)
            return result
        finally:
            connection._operation_depth -= 1
            connection._settle()

    def __next__(self) -> Any:
        try:
            return super().__next__()
        except StopIteration:
            connection = self.connection
            if isinstance(connection, DomainConnection):
                connection._active_readers.discard(self)
                connection._settle()
            raise

    def fetchall(self) -> list[Any]:
        result = super().fetchall()
        connection = self.connection
        if isinstance(connection, DomainConnection):
            connection._active_readers.discard(self)
            connection._settle()
        return result

    def close(self) -> None:
        try:
            super().close()
        finally:
            connection = self.connection
            if isinstance(connection, DomainConnection):
                connection._active_readers.discard(self)
                connection._settle()

    def __enter__(self) -> DomainCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def guard_connection(connection: DomainConnection, core_path: Path, domain: str) -> None:
    """Initialize an admitted split connection on its SQLite worker thread.

    The migration coordinator calls this explicitly after all target files exist;
    ordinary factories admit the layout only through the ready-state registry.
    """
    if connection._domain is not None:
        if connection._domain != domain or connection._core_path != core_path.resolve():
            raise ValueError("SQLite domain connection is already initialized")
        return
    if domain not in DOMAIN_TARGET_FILES:
        raise ValueError("Unknown SQLite domain")
    if connection.in_transaction:
        raise sqlite3.ProgrammingError("SQLite domain initialization requires an idle connection")
    core_path = core_path.resolve()
    with connection.execute("PRAGMA database_list") as cursor:
        actual = next(Path(row[2]).resolve() for row in cursor.fetchall() if row[1] == "main")
    if actual != core_path.with_name(DOMAIN_TARGET_FILES[domain]):
        raise ValueError("SQLite connection does not match the admitted domain path")
    # URI mode=ro is an independent SQLite write prohibition, not merely a convention.
    for other, filename in DOMAIN_TARGET_FILES.items():
        if other == domain or other not in {"core", "execution", "content", "notifications", "observability"}:
            continue
        path = core_path.with_name(filename)
        if not path.is_file():
            raise FileNotFoundError(f"Required SQLite domain file is absent: {filename}")
        connection.execute(f'ATTACH DATABASE ? AS "domain_{other}"', (path.as_uri() + "?mode=ro",)).close()
    connection._core_path = core_path
    connection._domain = domain
    connection.create_function("bowhead_domain_request_scope_v1", 5, connection._request_scope)
    connection.create_function("bowhead_domain_request_turn_scope_v1", 1, lambda key: connection._request_child_scope(key, record=False))
    connection.create_function("bowhead_domain_request_record_scope_v1", 1, lambda key: connection._request_child_scope(key, record=True))
    connection.create_function("bowhead_domain_authority_v1", 1, connection._authority)
    connection.create_function("bowhead_domain_predicate_v1", -1, connection._predicate)
    connection.create_function("bowhead_workspace_team_v1", 1, connection._workspace_team)
    connection.create_function("bowhead_retention_days_v1", 2, connection._retention_days)
    connection.create_function("bowhead_domain_cleanup_v1", 2, connection._enqueue_cleanup)
    connection.set_authorizer(connection._external_authorizer)


def _connection_connector(database: str | Path, kwargs: dict[str, Any]) -> tuple[Path, Callable[[], sqlite3.Connection]]:
    """Resolve admission once; opening itself runs on the caller or async worker."""
    from bowhead_core import sqlite_domains

    value = str(database)
    if value.startswith("file:"):
        uri = urlsplit(value)
        if uri.netloc not in {"", "localhost"}:
            raise ValueError("SQLite file URIs require a local path")
        path = Path(unquote(uri.path))
    else:
        path = Path(value)
    active = sqlite_domains.domain_layout_active(path)
    factory = kwargs.pop("factory", DomainConnection if active else DiagnosticConnection)
    if active:
        kwargs["uri"] = True
        parameters = parse_qsl(uri.query, keep_blank_values=True) if value.startswith("file:") else []
        modes = [mode for key, mode in parameters if key == "mode"]
        if len(modes) > 1 or modes and modes[0] not in {"ro", "rw"}:
            raise ValueError("Admitted SQLite domain URIs require mode=ro or mode=rw")
        if not modes:
            parameters.append(("mode", "rw"))
        # OPEN_READWRITE without OPEN_CREATE closes the ready-state/open race;
        # a deleted domain can never be replaced by an empty live business DB.
        value = path.resolve().as_uri() + "?" + urlencode(parameters)

    def connector() -> sqlite3.Connection:
        connection = sqlite3.connect(value, factory=factory, **kwargs)
        if active:
            if not isinstance(connection, DomainConnection):
                connection.close()
                raise TypeError("Split SQLite connections require DomainConnection")
            domain = next((domain for domain, filename in DOMAIN_TARGET_FILES.items() if filename == path.name), None)
            if domain is None:
                connection.close()
                raise ValueError("Unregistered SQLite domain file")
            try:
                guard_connection(connection, path.with_name(DOMAIN_TARGET_FILES["core"]), domain)
            except BaseException:
                connection.close()
                raise
        return connection

    return path, connector


def connect_sync(database: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """Open a sync business connection with the same admission and guards as async."""
    _path, connector = _connection_connector(database, kwargs)
    return connector()


def connect(database: str | Path, *, iter_chunk_size: int = 64, **kwargs: Any) -> DiagnosticAsyncConnection:
    """Async factory with legacy compatibility and ready-state domain admission."""
    path, connector = _connection_connector(database, kwargs)
    return DiagnosticAsyncConnection(connector, iter_chunk_size, database=path.name if path.name in DOMAIN_TARGET_FILES.values() else "unavailable")


def release_domain_snapshots(connection: sqlite3.Connection) -> None:
    """Finish idle read cursors before an explicitly sequential domain followup.

    Callers must have consumed or intentionally discarded all query results. This
    changes neither the borrowed connection's callbacks nor its diagnostic context.
    """
    if not isinstance(connection, DomainConnection) or connection._domain is None:
        return
    if connection.in_transaction:
        raise sqlite3.ProgrammingError("SQLite domain snapshots cannot be released inside a transaction")
    for cursor in list(connection._domain_cursors):
        cursor.close()
    if connection._active_readers:
        raise sqlite3.ProgrammingError("SQLite domain connection still has a read cursor")
    connection._settle()


def reset_domain_connection(connection: sqlite3.Connection) -> None:
    """Verify idle domain state after the pool has closed every borrowed cursor."""
    if not isinstance(connection, DomainConnection) or connection._domain is None:
        return
    release_domain_snapshots(connection)
    connection._cleanup_events.clear()
    connection._gate_failed = False
    connection._callback_busy = None
    connection.set_authorizer(None)
    connection._release()
