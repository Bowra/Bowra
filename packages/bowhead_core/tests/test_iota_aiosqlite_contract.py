"""Sprint ι r2 P1 #1: assert artifact_store.py routes ALL aiosqlite use through _open_db().

Direct `aiosqlite.connect(` outside `_open_db()` definition = FK pragma drift risk.
This contract lint test prevents regression where someone adds a new
aiosqlite.connect() call site that bypasses the helper.
"""

from __future__ import annotations

import re
from pathlib import Path

ARTIFACT_STORE = Path(__file__).resolve().parents[1] / "src/bowhead_core/artifact_store.py"


def test_aiosqlite_connect_only_in_open_db_helper() -> None:
    src = ARTIFACT_STORE.read_text(encoding="utf-8")
    # Match real call sites only — exclude string-literal mentions in docstrings
    # (which appear as `aiosqlite.connect(self.db_path)` inside backticks/quotes).
    # Strip out lines that have leading whitespace + leading text containing
    # only a backtick before the match — keep only true code call sites.
    matches = list(re.finditer(r"aiosqlite\.connect\(", src))
    # Filter out matches that are inside a docstring backtick context.
    # Heuristic: walk lines, skip any line where match starts after a backtick.
    lines_with_real_call = []
    for m in matches:
        # Find the line containing match start.
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_end = src.find("\n", m.start())
        if line_end == -1:
            line_end = len(src)
        line = src[line_start:line_end]
        # Real call: not inside a code-fence-style backtick comment.
        # Docstring lines mention `aiosqlite.connect(self.db_path)` inside backticks.
        # The actual call is `async with aiosqlite.connect(...)`.
        if "async with aiosqlite.connect(" in line:
            lines_with_real_call.append((m.start(), line.strip()))

    assert len(lines_with_real_call) == 1, (
        f"Expected exactly 1 real `aiosqlite.connect(...)` call (inside _open_db()), "
        f"got {len(lines_with_real_call)}. "
        f"New direct call sites bypass FK pragma. Sites: {lines_with_real_call}"
    )
    # Confirm the one hit is preceded by `async def _open_db` within ~400 chars upstream.
    pos = lines_with_real_call[0][0]
    upstream = src[max(0, pos - 600) : pos]
    assert "_open_db" in upstream, "aiosqlite.connect() found outside _open_db() helper — FK pragma will not be set; CASCADE will silently no-op."
