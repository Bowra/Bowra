---
name: canvas-pdf
description: "Render Bowhead Canvas (ReportSection[] JSON) as production PDF via reportlab. Use for '导出 PDF / export PDF / 把报告下载成 PDF'. Output: output.pdf."
version: 1.1.1
allowed-tools: Bash(python3)
title: "Export report to PDF"
title_zh: "报告导出 PDF"
category: documents
---

# Canvas → PDF

This skill converts Bowhead's `ReportSection[]` schema to a PDF via `reportlab.platypus`.

## Invocation

The current canvas's sections are injected automatically server-side — do **not**
retype the full `ReportSection[]` into `input` (a large canvas will truncate mid-stream
and silently fail to produce a file). Just pass the small optional title override:

```json
{
  "name": "canvas-pdf",
  "script": "render_from_canvas.py",
  "input": { "title": "Optional document title" }
}
```

(`sections` is still honored if explicitly passed and no active canvas is found —
e.g. test/eval harnesses — but never rely on this in a real turn.)

Output file: `workdir/output.pdf`. Returns `result.json` with `{"path": "output.pdf", "pages": N}`.

## Style rules (inherited from anthropic/skills/pdf)

- ReportLab builtin fonts do **not** include unicode subscripts / superscripts (₀₁₂ / ⁰¹² render as black boxes). Use `<sub>...</sub>` / `<super>...</super>` XML in `Paragraph` instead.
- Font stack: Helvetica by default. Use `getSampleStyleSheet()` base + minor tweaks.
- Page size: A4; 1 inch margins.
