"""Excel's East Asian built-in date formats are read as dates (spike: office-inline-edit-2026-11-26 §3.5).

A zh-CN workbook stores 「2026年11月」 as the number 46327 with the built-in format ID 57 and no pattern.
openpyxl did not know the ID, so previews and knowledge-base text showed 46327.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

import openpyxl
from bowhead_core.attachments_injector import _extract_xlsx
from bowhead_core.kb.xlsx_utils import read_xlsx_rows_from_bytes


def _workbook(format_id: int, *, define_custom: str | None = None) -> bytes:
    """One sheet: a header and 46327 styled with `format_id` (a built-in ID unless `define_custom` gives a pattern)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "月份"
    ws["A2"] = 46327
    ws["A2"].number_format = "0.000"  # a custom format openpyxl writes under ID 164, rewritten below
    raw = io.BytesIO()
    wb.save(raw)
    source = zipfile.ZipFile(io.BytesIO(raw.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/styles.xml":
                text = data.decode()
                if define_custom is None:
                    text = re.sub(r'<numFmts count="1"><numFmt numFmtId="164" formatCode="[^"]*"\s*/></numFmts>', "", text)
                else:
                    text = text.replace('numFmtId="164" formatCode="0.000"', f'numFmtId="{format_id}" formatCode="{define_custom}"')
                text = text.replace('numFmtId="164"', f'numFmtId="{format_id}"')
                data = text.encode()
            target.writestr(item, data)
    return out.getvalue()


def test_a_built_in_east_asian_date_format_reads_as_a_date() -> None:
    sheets = read_xlsx_rows_from_bytes(_workbook(57))
    assert sheets[0][1][1][0] == dt.datetime(2026, 11, 1)


def test_the_attachment_preview_shows_the_date_not_the_serial() -> None:
    text, _ = _extract_xlsx(_workbook(58))
    assert "2026-11-01" in text and "46327" not in text


def test_a_workbook_that_defines_its_own_format_under_the_id_keeps_it() -> None:
    sheets = read_xlsx_rows_from_bytes(_workbook(57, define_custom="0.00"))
    assert sheets[0][1][1][0] == 46327


def test_other_built_in_number_formats_stay_numbers() -> None:
    for format_id in (3, 37, 49):
        assert read_xlsx_rows_from_bytes(_workbook(format_id))[0][1][1][0] == 46327
