"""Render ReportSection[] → XLSX using openpyxl.

Invocation:
    python3 render_from_canvas.py --input <dir>/input.json --workdir <dir>

Output: workdir/output.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit(3)


# Financial-model color conventions from anthropic/skills/xlsx.
BLUE_INPUT = Font(name="Arial", size=11, color="0000FF")
BLACK_FORMULA = Font(name="Arial", size=11, color="000000")
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="2E5D4F")
MUTED_FONT = Font(name="Arial", size=10, color="6B7A6F")
THIN_BORDER = Border(
    left=Side(style="thin", color="D0D0CC"),
    right=Side(style="thin", color="D0D0CC"),
    top=Side(style="thin", color="D0D0CC"),
    bottom=Side(style="thin", color="D0D0CC"),
)


def _write_kv(ws, row: int, key: str, val: str, *, bold_val: bool = False) -> int:
    ws.cell(row=row, column=1, value=key).font = MUTED_FONT
    cell = ws.cell(row=row, column=2, value=val)
    cell.font = Font(name="Arial", size=11, bold=bold_val)
    return row + 1


# Excel sheet-name constraints: ≤31 characters, no []:*?/\, and no duplicates (openpyxl raises directly for duplicate names).
_SHEET_BAD = set(r"[]:*?/\\")


def _safe_sheet_name(wb, base: str, fallback: str) -> str:
    name = "".join(" " if c in _SHEET_BAD else c for c in str(base or "")).strip()[:31].strip() or fallback
    if name not in wb.sheetnames:
        return name
    for i in range(2, 100):
        suffix = f" ({i})"
        cand = name[: 31 - len(suffix)] + suffix
        if cand not in wb.sheetnames:
            return cand
    return fallback


def _write_table(ws, start_row: int, cols: list[dict], rows: list[dict]) -> int:
    r = start_row
    for j, c in enumerate(cols, start=1):
        cell = ws.cell(row=r, column=j, value=c.get("header", c.get("key", "")))
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center")
        cell.border = THIN_BORDER
    r += 1
    for row in rows:
        for j, c in enumerate(cols, start=1):
            v = row.get(c["key"])
            if isinstance(v, dict) and "delta" in v:
                val = v["delta"]
            elif isinstance(v, dict) and "bar" in v:
                val = float(v["bar"]) / 100.0
            else:
                val = v
            cell = ws.cell(row=r, column=j, value=val)
            cell.font = BLACK_FORMULA
            cell.border = THIN_BORDER
            # Numeric detection must recognize both column shapes: `data_table` uses `numeric: true`,
            # while chart columns use `type: "numeric"` (observed on 116 production 1.0.90).
            # Recognizing only the former leaves numbers in chart sheets with General formatting, displaying
            # `3268829891.0369` in Excel while the same report exported to Word shows `3,268,829,891.04`.
            if (c.get("numeric") or c.get("type") == "numeric") and isinstance(val, (int, float)):
                cell.alignment = Alignment(horizontal="right")
                cell.number_format = "#,##0.00"
            if isinstance(val, float) and c.get("kind") == "bar":
                cell.number_format = "0.0%"
        r += 1
    # Adaptive column width
    for j, c in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(j)].width = max(
            12,
            min(40, len(str(c.get("header", ""))) + 4),
        )
    return r + 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--workdir", required=True)
    args, _ = ap.parse_known_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    sections = data.get("sections") or []
    title = data.get("title") or "Bowhead Canvas"
    out_path = Path(args.workdir) / "output.xlsx"

    wb = Workbook()

    # ─── Summary ─────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"
    r = 1
    ws.cell(row=r, column=1, value=title).font = Font(name="Arial", size=18, bold=True)
    r += 2
    for sec in sections:
        k = sec.get("kind")
        if k == "eyebrow":
            r = _write_kv(ws, r, "Eyebrow", sec.get("text", ""))
        elif k == "h1":
            r = _write_kv(ws, r, "Title", sec.get("text", ""), bold_val=True)
        elif k == "deck":
            r = _write_kv(ws, r, "Summary", sec.get("markdown", ""))
        elif k == "meta":
            r = _write_kv(ws, r, "Tags", " · ".join(sec.get("pills") or []))
    # kpi_row
    kpi = next((s for s in sections if s.get("kind") == "kpi_row"), None)
    if kpi and kpi.get("cells"):
        r += 1
        ws.cell(row=r, column=1, value="Key Metrics").font = Font(name="Arial", size=12, bold=True)
        r += 1
        for j, c in enumerate(kpi["cells"], start=1):
            ws.cell(row=r, column=j, value=c.get("lbl", "")).font = MUTED_FONT
            cell = ws.cell(row=r + 1, column=j, value=c.get("val", ""))
            cell.font = Font(name="Arial", size=14, bold=True)
        r += 3
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 80

    # ─── Data sheets ─────────────────────────────────────
    table_counter = 0
    for sec in sections:
        k = sec.get("kind")
        if k == "chart":
            # 116 production 1.0.89 dogfood P1: **this branch previously did not exist**.
            # The `chart` produced by `add_chart` is a primary source of tabular data in BI reports, but this recognized
            # only data_table / attribution / trend_chart. A report with two charts exported to
            # Excel had only four rows (title + Tags), **5217 bytes and not a single number**;
            # the same canvas exported to Word had 39084 bytes and two complete data tables.
            # docx/pdf already degraded charts to data tables; xlsx omitted this, although Excel is the format most expected to contain data.
            spec = sec.get("spec") or sec
            cols = spec.get("columns") or []
            rows = spec.get("rows") or []
            if cols and rows:
                table_counter += 1
                sheet = wb.create_sheet(_safe_sheet_name(wb, spec.get("title"), f"Chart {table_counter}"))
                rr = 1
                if spec.get("title"):
                    sheet.cell(row=rr, column=1, value=spec["title"]).font = Font(name="Arial", size=12, bold=True)
                    rr += 2
                _write_table(sheet, rr, cols, rows)
        elif k == "data_table":
            table_counter += 1
            title_sheet = _safe_sheet_name(wb, sec.get("caption"), f"Table {table_counter}")
            sheet = wb.create_sheet(title_sheet)
            rr = 1
            if sec.get("caption"):
                sheet.cell(row=rr, column=1, value=sec["caption"]).font = Font(name="Arial", size=12, bold=True)
                rr += 2
            _write_table(sheet, rr, sec.get("columns") or [], sec.get("rows") or [])
        elif k == "attribution":
            table_counter += 1
            sheet = wb.create_sheet(_safe_sheet_name(wb, f"Attribution {table_counter}", f"Attribution {table_counter}"))
            rr = 1
            summary = sec.get("summary") or {}
            sheet.cell(row=rr, column=1, value=summary.get("label", "")).font = MUTED_FONT
            sheet.cell(row=rr, column=2, value=summary.get("value", "")).font = Font(
                name="Arial",
                size=12,
                bold=True,
            )
            sheet.cell(row=rr, column=3, value=summary.get("pct", ""))
            rr += 2
            cols = [
                {"key": "rank", "header": "#", "numeric": True},
                {"key": "dim", "header": "Dimension"},
                {"key": "name", "header": "Attribute"},
                {"key": "from", "header": "Prev"},
                {"key": "to", "header": "Curr"},
                {"key": "pct", "header": "% share"},
            ]
            _write_table(sheet, rr, cols, sec.get("rows") or [])
        elif k == "trend_chart":
            table_counter += 1
            sheet = wb.create_sheet(f"Trend {table_counter}"[:31])
            pts = sec.get("points") or []
            cols = [{"key": "label", "header": "Period"}, {"key": "value", "header": "Value", "numeric": True}]
            _write_table(sheet, 1, cols, pts)

    # ─── SQL sheet ───────────────────────────────────────
    sqls = [s for s in sections if s.get("kind") == "sql_block" and s.get("sql")]
    if sqls:
        sheet = wb.create_sheet("SQL")
        sheet.cell(row=1, column=1, value="#").font = HEADER_FONT
        sheet.cell(row=1, column=1).fill = HEADER_FILL
        sheet.cell(row=1, column=2, value="SQL").font = HEADER_FONT
        sheet.cell(row=1, column=2).fill = HEADER_FILL
        for i, s in enumerate(sqls, start=2):
            sheet.cell(row=i, column=1, value=i - 1)
            cell = sheet.cell(row=i, column=2, value=s["sql"])
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.font = Font(name="Consolas", size=10)
        sheet.column_dimensions["A"].width = 6
        sheet.column_dimensions["B"].width = 100

    wb.save(str(out_path))

    result = {
        "path": str(out_path),
        "sheets": wb.sheetnames,
        "sections": len(sections),
    }
    (Path(args.workdir) / "result.json").write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
