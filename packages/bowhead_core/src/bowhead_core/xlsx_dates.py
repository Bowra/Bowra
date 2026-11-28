"""Read Excel's East Asian built-in date formats as dates.

Number formats 27–36 and 50–58 are built-in East Asian date and time formats (for example
``yyyy"年"m"月"`` and ``m"月"d"日"`` in zh-CN). A workbook stores only the format's ID, and the
application that opens it supplies the pattern for its locale (ECMA-376 Part 1, 18.8.30). openpyxl
knows only the IDs 0–22 and 37–49, so it reads these cells as plain numbers: a zh-CN workbook's
「2026年11月」 arrives as 46327. Measured on 14 real workbooks, 582 date cells in 5 of them were read as
numbers, so attachment previews, knowledge-base text and the Results sample showed serial numbers
(docs/sprints/office-inline-edit-2026-11-26/spike.md, section 3.5).

``install()`` makes openpyxl's reader treat cells styled with these IDs as dates. Only the reader's
index of date styles changes; how openpyxl writes formats is untouched, and a workbook that defines
its own format under one of these IDs keeps that definition. The sandbox applies the same rule in
``python_sandbox/scripts/runner.py``, which cannot import this package.
"""

from __future__ import annotations

import threading

#: Built-in East Asian date and time format IDs, which openpyxl does not know.
EAST_ASIAN_DATE_FORMAT_IDS = frozenset((*range(27, 37), *range(50, 59)))

_lock = threading.Lock()
_installed = False


def install() -> None:
    """Patch openpyxl's stylesheet reader once per process; later calls do nothing."""
    global _installed
    if _installed:
        return
    with _lock:
        if _installed:
            return
        from openpyxl.styles.stylesheet import Stylesheet

        original = Stylesheet._normalise_numbers

        def _normalise_numbers(self: Stylesheet) -> None:
            original(self)
            # Custom formats were rebased to IDs from 164 by the original, so only built-in IDs match.
            for index, style in enumerate(self.cell_styles):
                if style.numFmtId in EAST_ASIAN_DATE_FORMAT_IDS:
                    self.date_formats.add(index)

        Stylesheet._normalise_numbers = _normalise_numbers  # type: ignore[method-assign]
        _installed = True


__all__ = ["EAST_ASIAN_DATE_FORMAT_IDS", "install"]
