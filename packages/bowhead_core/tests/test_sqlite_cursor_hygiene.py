"""No row cursor on an async connection may be left open: close it, or read it with execute_fetchall."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
# Setter PRAGMAs that answer with a row. A bare ``await db.execute(...)`` drops that cursor, and until
# the event loop closes it the connection counts an open read: the next statement may not wait for a
# busy authority lease and fails at once with "database is locked" (116, 1.7.47 and 1.7.49).
DROPPED = re.compile(r"^\s*await [A-Za-z_][\w\.]*\.execute\(\s*f?[\"'][^\"']*PRAGMA [\w\.]*(?:busy_timeout|secure_delete|journal_mode)\s*=")


def test_row_returning_pragmas_close_their_cursors() -> None:
    sources = [*ROOT.glob("packages/*/src/**/*.py"), *ROOT.glob("enterprise/python/*/src/**/*.py")]
    assert sources
    dropped = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if DROPPED.match(line)
    ]
    assert dropped == [], "Use bowhead_core._db.execute_and_close (or close the cursor) for: " + ", ".join(dropped)


# A row cursor that is dropped instead of closed stays an open read until the connection's worker takes its
# next job: the next statement may not wait for a busy authority lease, and an unexhausted read of core holds
# its shared lease against another connection's protected write
# (docs/sprints/dropped-cursor-lease-2026-11-17/discovery.md). A cursor counts as closed through
# ``async with db.execute(...)``, ``execute_fetchall``, ``_db.execute_and_close``, ``async with cursor`` or an
# explicit ``cursor.close()``. DML cursors (no rows) never count as an open read and are not checked.
_DB_RECEIVERS = {"db", "conn", "connection", "core", "probe"}
_FETCHES = {"fetchone", "fetchall", "fetchmany"}
_ROW_SQL = re.compile(r"^\s*(?:select|with|pragma\s+\w+\s*(?:$|\()|values)", re.IGNORECASE)
_RETURNING = re.compile(r"\breturning\b", re.IGNORECASE)
_CTE_DML = re.compile(r"^\s*with\b.*\)\s*(?:update|insert|delete|replace)\b", re.IGNORECASE | re.DOTALL)


def _sql_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else " x " for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_literal(node.left), _sql_literal(node.right)
        if left is not None or right is not None:
            return (left or " x ") + (right or " x ")
    return None


def _database_execute(node: ast.AST) -> ast.Call | None:
    """An ``X.execute(...)`` call on an aiosqlite connection: literal SQL, or a connection-named receiver."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"):
        return None
    receiver = node.func.value
    named_connection = isinstance(receiver, ast.Name) and (receiver.id in _DB_RECEIVERS or receiver.id.endswith("_db"))
    literal = _sql_literal(node.args[0]) if node.args else None
    return node if named_connection or literal is not None else None


def _returns_rows(call: ast.Call) -> bool | None:
    literal = _sql_literal(call.args[0]) if call.args else None
    if literal is None:
        return None
    if _RETURNING.search(literal):
        return True
    if _CTE_DML.search(literal):
        return False  # WITH ... UPDATE/INSERT/DELETE has no result columns
    return bool(_ROW_SQL.match(literal))


def _unclosed_cursors(function: ast.AsyncFunctionDef) -> list[int]:
    parents = {id(child): parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)}
    managed = {id(item.context_expr) for node in ast.walk(function) if isinstance(node, ast.AsyncWith) for item in node.items}
    closed_names = {
        node.func.value.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "close" and isinstance(node.func.value, ast.Name)
    }
    closed_names |= {
        item.context_expr.id
        for node in ast.walk(function)
        if isinstance(node, ast.AsyncWith)
        for item in node.items
        if isinstance(item.context_expr, ast.Name)
    }
    found: list[int] = []
    for node in ast.walk(function):
        call = _database_execute(node)
        if call is None or id(call) in managed:
            continue
        awaited = parents.get(id(call))
        if not isinstance(awaited, ast.Await):
            continue
        holder = parents.get(id(awaited))
        if isinstance(holder, ast.Attribute) and holder.attr in _FETCHES:
            found.append(call.lineno)  # (await db.execute(...)).fetchone(): never closed
        elif isinstance(holder, ast.Assign) and len(holder.targets) == 1 and isinstance(holder.targets[0], ast.Name):
            name = holder.targets[0].id
            if name in closed_names or _returns_rows(call) is False:
                continue
            uses = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)]
            dml_only = all(isinstance(parents.get(id(n)), ast.Attribute) and parents[id(n)].attr in {"rowcount", "lastrowid"} for n in uses)
            if not dml_only:
                found.append(call.lineno)
        elif isinstance(holder, ast.Expr):
            if _returns_rows(call):
                found.append(call.lineno)  # a row-returning statement whose cursor is dropped
        elif _returns_rows(call) is not False:
            found.append(call.lineno)  # returned, passed on or otherwise kept: its owner must close it
    return found


def test_async_row_cursors_are_closed() -> None:
    sources = [*ROOT.glob("packages/*/src/**/*.py"), *ROOT.glob("enterprise/python/*/src/**/*.py")]
    unclosed = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sources
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(function, ast.AsyncFunctionDef)
        for line in _unclosed_cursors(function)
    ]
    assert unclosed == [], "Close these async row cursors (async with db.execute(...) as cursor, or execute_fetchall): " + ", ".join(
        sorted(set(unclosed))
    )
