---
name: canvas-pptx
description: "Export a Bowra Canvas to an editable PowerPoint presentation with content-driven pagination. Use for export PPT, create PPTX, or 导出 PPT."
version: 2.1.1
allowed-tools: Bash(python3)
title: "Export report to PowerPoint"
title_zh: "报告导出 PPT"
category: documents
---

# Canvas to PowerPoint

Export the current Canvas as an editable `.pptx`. Standard downloads render supplied sections in order with `scripts/render_content.py`. They paginate long text, nested chapters, KPI cells, recommendations, tables and compatible category charts, with a 500-slide safety limit. No model is called to summarize or shorten supplied paragraphs.

Bar, horizontal bar and line charts use native PowerPoint charts when the data is compatible. Large charts paginate categories. Unsupported chart types, mixed units, ambiguous dimensions and overly long labels become data tables with a visible explanation. Missing images and unsupported interactive blocks receive notices in the deck and `result.json.warnings`. The export includes supplied rows; it does not fetch more data from linked workbooks or promise pixel-identical Canvas styling.

Code, SQL, plain text and table cells preserve literal operators and identifiers; Markdown fields are flattened. Explicit percentage storage scale takes precedence over generic display format: `27.5` with `percent_scale="points"` means `27.5%`, including when `format="percent"` is present.

## Invocation

The active Canvas sections are injected server-side. Pass only small overrides; do not retype the entire report into an invocation.

```json
{
  "name": "canvas-pptx",
  "script": "render_from_canvas.py",
  "input": {
    "title": "Optional deck title",
    "locale": "en-US"
  }
}
```

Use `locale: "zh-CN"` when Chinese is explicitly selected. Community defaults generated labels to English and preserves supplied report text. Explicit `sections` remain supported for test harnesses or when no active Canvas is available.

Output files are `workdir/output.pptx` and `workdir/result.json`.

## Explicit compatible templates

Only an explicit, allowed, existing `template_path` selects the role-template renderer. The resolved file must be inside the temporary directory or invocation work directory. Missing or disallowed paths use standard content-driven export. Arbitrary uploaded slide structures are not automatically adapted.

The packaged neutral template is [`templates/community-neutral.pptx`](templates/community-neutral.pptx), selected by `templates/default.json`. It is independently authored from a fresh public `python-pptx` presentation and synthetic chart data. [Provenance, rebuild instructions and exact slot contract](templates/community-neutral.md) are included. No external fonts or remote assets are required. The template is available for explicit compatible-template use and as the fallback for an explicit empty presentation; standard downloads do not load it.

| Slide           | Layout        | Content                                                                 |
| --------------- | ------------- | ----------------------------------------------------------------------- |
| Cover           | `COVER`       | Eyebrow, title, subtitle and date                                       |
| Summary         | `TITLE_ONLY`  | First insight, narrative, up to three findings and first recommendation |
| Metrics         | `KPI_GRID`    | Up to four KPI cells and a summary                                      |
| Bar insight     | `CHART_SLIDE` | First compatible bar insight and its chart                              |
| Line insight    | `CHART_SLIDE` | First compatible trend insight or standalone line chart                 |
| Recommendations | `TITLE_ONLY`  | Up to three action, rationale, owner and timing blocks                  |
| Closing         | `CLOSING`     | Thank you and date, without vendor contact details                      |

Role templates have fixed slots. The summary hero is limited to 40 characters, body to 60, findings to 35 each, chart title to 18, thesis to 50, chart narrative to 160, recommendation action to 24, rationale to 50, and owner/timing to 20 each. SQL and data tables are omitted on this path. Choose standard content-driven export when all supplied content must be preserved.

Extra unknown layouts, extra `TITLE_ONLY` slides, and visual slides without real chart/image data are removed. The packaged-template renderer checks for ungrounded template text and chart data before succeeding. Image substitution clears the native chart's embedded workbook. Do not reorder role slots when creating a compatible template.
