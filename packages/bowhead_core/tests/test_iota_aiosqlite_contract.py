"""Sprint ι r2 P1 #1: assert artifact_store.py routes ALL aiosqlite use through _open_db().

Direct `aiosqlite.connect(` outside `_open_db()` definition = FK pragma drift risk.
This contract lint test prevents regression where someone adds a new
aiosqlite.connect() call site that bypasses the helper.
"""

from __future__ import annotations

import ast
from pathlib import Path

ARTIFACT_STORE = Path(__file__).resolve().parents[1] / "src/bowhead_core/artifact_store.py"


def test_aiosqlite_connect_only_in_open_db_helper() -> None:
    src = ARTIFACT_STORE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    helper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_open_db")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "aiosqlite"
        and node.func.attr == "connect"
    ]
    assert len(calls) == 1, f"Expected one connection factory in _open_db; found calls on lines {[node.lineno for node in calls]}"
    assert calls[0] in set(ast.walk(helper)), "aiosqlite.connect() must remain inside _open_db() so new connections enable foreign keys."
