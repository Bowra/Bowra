"""Sprint ω Phase 0 contract test: 3 hot writers must route all SQLite use
through `bowhead_core._db.open_shared_ctx`.

After Phase 0 migration:
- turn_log_store.py: 4 sites migrated
- canvas_store.py: 18 sites migrated
- session_store.py: 33 sites migrated

Any new `aiosqlite.connect(...)` call site in these files = busy_timeout=0
regression (Spike Q9 finding — SQLITE_BUSY on contention).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_CORE_ROOT = Path(__file__).resolve().parents[1] / "src/bowhead_core"

# (filename, expected_open_shared_ctx_count_lower_bound)
HOT_WRITERS = [
    ("turn_log_store.py", 4),
    ("canvas_store.py", 18),
    ("session_store.py", 33),
]


@pytest.mark.parametrize("filename,expected_min", HOT_WRITERS)
def test_hot_writer_uses_open_shared_ctx(filename: str, expected_min: int) -> None:
    """Each migrated hot writer must use `open_shared_ctx` at every connect site."""
    src = (_CORE_ROOT / filename).read_text(encoding="utf-8")

    # Real call sites: `open_shared_ctx(` calls (imports lack parentheses and are naturally excluded).
    # 2026-07-14: the old check recognized only single-line `async with open_shared_ctx(`; repository-wide ruff format
    # rearranged two sites into `async with (\n    open_shared_ctx(...) as db,`, falsely reporting regressions (all
    # 5 calls remained). Count call parentheses instead, regardless of layout.
    open_shared_count = len(re.findall(r"open_shared_ctx\(", src))
    assert open_shared_count >= expected_min, (
        f"{filename}: expected ≥{expected_min} `open_shared_ctx(` call sites, got {open_shared_count}. Spike Q9 — busy_timeout=0 regression risk."
    )

    # No raw `async with aiosqlite.connect(` call site should remain in the
    # 3 hot writers (artifact_store has its own `_open_db`, separate contract).
    raw_connect_lines = [line.strip() for line in src.split("\n") if "async with aiosqlite.connect(" in line]
    assert raw_connect_lines == [], (
        f"{filename}: found raw `async with aiosqlite.connect(...)` sites: {raw_connect_lines}. All must route through `open_shared_ctx`."
    )


def test_db_helper_module_exists() -> None:
    """Sanity: the new helper module is importable."""
    from bowhead_core._db import (  # noqa: F401
        bootstrap_shared_db,
        open_shared_ctx,
        phase0_self_test,
    )


# aea6191e3 (SQLite domain split) and ebdb52379 (connection capacity) placed the SQLite runtime
# beneath `_db.py`: connection capacity, lock diagnostics, domain routing and the domain map.
# These modules form the foundation layer `_db.py` builds on; none of them may import `_db.py`
# or any Store at module import time.
_SQLITE_FOUNDATION = frozenset({"sqlite_capacity", "sqlite_diagnostics", "sqlite_domain_runtime", "sqlite_domains"})


def _module_level_internal_imports(module: str) -> set[str]:
    """`bowhead_core` modules imported while ``module`` itself is imported (not lazily, not for typing)."""
    tree = ast.parse((_CORE_ROOT / f"{module.replace('.', '/')}.py").read_text(encoding="utf-8"))
    found: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module == "bowhead_core":
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bowhead_core."):
                found.add(node.module.removeprefix("bowhead_core."))
            elif isinstance(node, ast.Import):
                found.update(alias.name.removeprefix("bowhead_core.") for alias in node.names if alias.name.startswith("bowhead_core."))
            elif isinstance(node, ast.If) and not (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(tree.body)
    return found


def test_db_helper_imports_only_the_sqlite_foundation() -> None:
    """Invariant from tech-design §2.8: `_db.py` is foundation and must not import the rest of
    `bowhead_core`, which prevents circular imports. Its only internal dependencies are the SQLite
    foundation modules, and their import closure never comes back to `_db.py` or reaches a Store."""
    direct = _module_level_internal_imports("_db")
    assert direct, "the scanner found no internal import in _db.py; it no longer parses the module"
    assert direct <= _SQLITE_FOUNDATION, f"_db.py imports outside the SQLite foundation (circular import risk): {sorted(direct - _SQLITE_FOUNDATION)}"

    closure: set[str] = set()
    pending = list(direct)
    while pending:
        module = pending.pop()
        if module in closure:
            continue
        closure.add(module)
        pending.extend(_module_level_internal_imports(module))
    assert "_db" not in closure, "a SQLite foundation module imports _db.py at import time (circular import)"
    assert closure <= _SQLITE_FOUNDATION, (
        f"the SQLite foundation imports other bowhead_core modules at import time: {sorted(closure - _SQLITE_FOUNDATION)}"
    )
