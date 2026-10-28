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


def test_db_helper_has_no_internal_bowhead_imports() -> None:
    """Invariant from tech-design §2.8: `_db.py` is foundation, must not import
    anything else from `bowhead_core`. Prevents circular import."""
    db_module = _CORE_ROOT / "_db.py"
    src = db_module.read_text(encoding="utf-8")
    # Skip module docstring (everything before the first `from __future__` or
    # `import` line). Pattern: any `from bowhead_core.` import.
    bowhead_imports = [line.strip() for line in src.split("\n") if re.match(r"^\s*(from\s+bowhead_core\.)", line)]
    assert bowhead_imports == [], f"_db.py must have zero internal bowhead_core imports (circular import risk). Found: {bowhead_imports}"
