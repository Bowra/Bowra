---
name: canvas-xlsx
description: "Render Bowhead Canvas (ReportSection[] JSON) as Excel workbook via openpyxl. Use for '导出 Excel / 导出 XLSX / export to xlsx'. Output: output.xlsx."
version: 1.1.0
allowed-tools: Bash(python3)
title: "Export report to Excel"
title_zh: "报告导出 Excel"
category: documents
---

# Canvas → XLSX

Maps `ReportSection[]` to multiple worksheets:

- `Summary` — title + eyebrow + deck + meta + kpi_row
- `Data` — one sheet per `data_table` / `attribution` / `trend_chart`
- `SQL` — one row per `sql_block`

## Invocation

The current canvas's sections are injected automatically server-side — do **not**
retype the full `ReportSection[]` into `input` (a large canvas will truncate mid-stream
and silently fail to produce a file). Just call with an empty (or title-only) input:

```json
{ "name": "canvas-xlsx", "script": "render_from_canvas.py", "input": { "title": "Optional" } }
```

(`sections` is still honored if explicitly passed and no active canvas is found —
e.g. test/eval harnesses — but never rely on this in a real turn.)

Output: `workdir/output.xlsx`.

## Style rules (from anthropic/skills/xlsx)

- Fonts: Arial 11pt default
- Financial color code (when applicable):
  - BLUE (0,0,255) — hardcoded inputs
  - BLACK — formulas / calculations
  - GREEN (0,128,0) — intra-workbook refs
  - RED (255,0,0) — external refs
  - YELLOW bg — key assumptions
- Number formats: currency `$#,##0`, percent `0.0%`, multiples `0.0x`, negatives as `(123)` not `-123`
- Years as text (`"2024"`, no thousands separator)
- Zero-formula-errors rule: no `#REF! / #DIV/0! / #VALUE!`
