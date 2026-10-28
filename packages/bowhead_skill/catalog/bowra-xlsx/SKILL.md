---
name: bowra-xlsx
description: "Create editable Excel (.xlsx) sheets with typed values, dates and number formats, or read cell values and formula text. Formulas are not calculated."
version: 1.0.0
title: "Excel workbooks"
title_zh: "Excel 工作簿"
category: documents
kind: tool
audience: all
license: MIT
---

# Excel workbooks

## Choose the operation

Use `create` for a new XLSX deliverable; use `inspect` for supported text/value inspection of an authorized staged file. Reading this Skill grants no Tool, file or network access. Keep the user's requested output language and original values.

Separate source data, assumptions and conclusions across named sheets where useful. Keep units in column headers; use decimal numbers for numeric values and `percent` only for ratios (0.25 displays as 25%). Preserve identifiers with leading zeros as strings. Do not report formula results unless a separate calculation engine has verified them.

## Execute

Read [the input contract](references/format.md), then construct a request matching [input-schema.json](input-schema.json). The only entry script is `scripts/run.py`; it accepts the existing `--input` and `--workdir` CLI protocol.

For a caller explicitly granted this exact package, creation uses:

```json
{
  "name": "bowra-xlsx",
  "script": "run.py",
  "input": {
    "operation": "create",
    "sheets": [
      {
        "name": "Metrics",
        "columns": ["Metric", "Value"],
        "rows": [
          ["Requests", 240],
          ["Median minutes", 12]
        ],
        "formats": ["general", "integer"]
      },
      {
        "name": "Follow-up",
        "columns": ["Action", "Due date"],
        "rows": [
          [
            "Review failures",
            {
              "date": "2026-10-08"
            }
          ]
        ]
      }
    ]
  }
}
```

This is a standard built-in Skill discovered from `catalog/`. Check actual enablement and exact-resource grants before invocation. Source inspection requires the caller to materialize an authorized file within the invocation workdir first; passing an Artifact ID or an arbitrary host path is not supported.

Run the bundled example locally with installed dependencies:

```bash
uv run --no-sync python packages/bowhead_skill/catalog/bowra-xlsx/scripts/run.py \
  --input packages/bowhead_skill/catalog/bowra-xlsx/examples/create.json \
  --workdir /tmp/bowra-xlsx-example
```

Outside the Bowhead workspace, install [requirements.txt](requirements.txt) with Python 3.12 and run the same script. This package makes no network calls and does not install dependencies during execution.

## Verify and deliver

Creation emits `workbook.xlsx`; inspection emits `inspection.json`. Both emit `result.json` and a JSON stdout summary with the relative output path, byte count and SHA-256. Use a fresh workdir: outputs are create-only. Reopen the generated format and check key titles, values, row/slide counts and language. For visual deliverables, use a separate authorized renderer and inspect its preview; these scripts do not render previews.

Return the actual registered Artifact or file link, the supported inspection scope and any relevant limitation. Local file creation does not register or publish a platform Artifact. Follow the platform's existing access, draft-confirmation and publication rules.

## Supported scope

Creation writes value-only worksheets, freezes header rows and adds filters. Strings beginning with `=` remain literal text. Explicit date objects become Excel dates. Inspection labels formula cells with `calculated: false`; it never substitutes stale cached values for a calculation. Charts, macros, external-link evaluation, formula creation and preserving existing templates are unsupported.

See [provenance and licensing](PROVENANCE.md). The MIT license covers this independently authored package, not unrelated catalog packages or runtime images.
