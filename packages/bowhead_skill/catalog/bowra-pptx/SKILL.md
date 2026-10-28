---
name: bowra-pptx
description: "Create a basic 16:9 PowerPoint (.pptx) deck from titles, bullets and tables, or read slide text. Does not edit customer templates."
version: 1.0.0
title: "PowerPoint documents"
title_zh: "PowerPoint 演示文稿"
category: documents
kind: tool
audience: all
license: MIT
---

# PowerPoint documents

## Choose the operation

Use `create` for a new PPTX deliverable; use `inspect` for supported text/value inspection of an authorized staged file. Reading this Skill grants no Tool, file or network access. Keep the user's requested output language and original values.

Make each slide communicate one point. Keep titles short, with at most six concise bullets, or one table with at most six columns and seven data rows. If content does not fit, divide it into more slides; do not claim automatic font fitting or preview validation. Use the authorized platform draft workflow for template updates and source-linked publication.

## Execute

Read [the input contract](references/format.md), then construct a request matching [input-schema.json](input-schema.json). The only entry script is `scripts/run.py`; it accepts the existing `--input` and `--workdir` CLI protocol.

For a caller explicitly granted this exact package, creation uses:

```json
{
  "name": "bowra-pptx",
  "script": "run.py",
  "input": {
    "operation": "create",
    "title": "Monthly delivery review",
    "slides": [
      {
        "title": "Pilot results",
        "bullets": ["240 requests processed", "12-minute median turnaround", "Review failures before expanding"]
      },
      {
        "title": "Delivery metrics",
        "columns": ["Metric", "Value"],
        "rows": [
          ["Requests", "240"],
          ["Median turnaround", "12 minutes"]
        ]
      }
    ]
  }
}
```

This is a standard built-in Skill discovered from `catalog/`. Check actual enablement and exact-resource grants before invocation. Source inspection requires the caller to materialize an authorized file within the invocation workdir first; passing an Artifact ID or an arbitrary host path is not supported.

Run the bundled example locally with installed dependencies:

```bash
uv run --no-sync python packages/bowhead_skill/catalog/bowra-pptx/scripts/run.py \
  --input packages/bowhead_skill/catalog/bowra-pptx/examples/create.json \
  --workdir /tmp/bowra-pptx-example
```

Outside the Bowhead workspace, install [requirements.txt](requirements.txt) with Python 3.12 and run the same script. This package makes no network calls and does not install dependencies during execution.

## Verify and deliver

Creation emits `deck.pptx`; inspection emits `inspection.json`. Both emit `result.json` and a JSON stdout summary with the relative output path, byte count and SHA-256. Use a fresh workdir: outputs are create-only. Reopen the generated format and check key titles, values, row/slide counts and language. For visual deliverables, use a separate authorized renderer and inspect its preview; these scripts do not render previews.

Return the actual registered Artifact or file link, the supported inspection scope and any relevant limitation. Local file creation does not register or publish a platform Artifact. Follow the platform's existing access, draft-confirmation and publication rules.

## Supported scope

Creation uses a fresh presentation with editable text and tables. It does not patch or preserve an uploaded template. Inspection reads top-level text and tables, and identifies other shapes without interpreting them. Masters, grouped text, notes, charts, images, animations and rendering are outside scope. This CLI does not replace a platform's review-and-confirm publication workflow.

See [provenance and licensing](PROVENANCE.md). The MIT license covers this independently authored package, not unrelated catalog packages or runtime images.
