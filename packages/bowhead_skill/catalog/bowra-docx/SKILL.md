---
name: bowra-docx
description: "Create editable Word (.docx) reports with headings, bullets and tables, or read DOCX body text. Not for tracked changes or PDF layout."
version: 1.0.0
title: "Word documents"
title_zh: "Word 文档"
category: documents
kind: tool
audience: all
license: MIT
---

# Word documents

## Choose the operation

Use `create` for a new DOCX deliverable; use `inspect` for supported text/value inspection of an authorized staged file. Reading this Skill grants no Tool, file or network access. Keep the user's requested output language and original values.

Use a short title, meaningful heading levels and compact tables. Put decisions and supporting numbers in separate paragraphs. Do not insert fake citations, change source values or reconstruct a customer template by guessing. Generation uses editable Word objects.

## Execute

Read [the input contract](references/format.md), then construct a request matching [input-schema.json](input-schema.json). The only entry script is `scripts/run.py`; it accepts the existing `--input` and `--workdir` CLI protocol.

For a caller explicitly granted this exact package, creation uses:

```json
{
  "name": "bowra-docx",
  "script": "run.py",
  "input": {
    "operation": "create",
    "title": "Monthly delivery review",
    "blocks": [
      {
        "type": "heading",
        "text": "Summary",
        "level": 1
      },
      {
        "type": "paragraph",
        "text": "The pilot processed 240 requests. Median turnaround was 12 minutes."
      },
      {
        "type": "table",
        "columns": ["Metric", "Value"],
        "rows": [
          ["Requests", "240"],
          ["Median turnaround", "12 minutes"]
        ]
      },
      {
        "type": "bullets",
        "items": ["Expand the pilot to the support team.", "Review failed requests before expansion."]
      }
    ]
  }
}
```

This is a standard built-in Skill discovered from `catalog/`. Check actual enablement and exact-resource grants before invocation. Source inspection requires the caller to materialize an authorized file within the invocation workdir first; passing an Artifact ID or an arbitrary host path is not supported.

Run the bundled example locally with installed dependencies:

```bash
uv run --no-sync python packages/bowhead_skill/catalog/bowra-docx/scripts/run.py \
  --input packages/bowhead_skill/catalog/bowra-docx/examples/create.json \
  --workdir /tmp/bowra-docx-example
```

Outside the Bowhead workspace, install [requirements.txt](requirements.txt) with Python 3.12 and run the same script. This package makes no network calls and does not install dependencies during execution.

## Verify and deliver

Creation emits `document.docx`; inspection emits `inspection.json`. Both emit `result.json` and a JSON stdout summary with the relative output path, byte count and SHA-256. Use a fresh workdir: outputs are create-only. Reopen the generated format and check key titles, values, row/slide counts and language. For visual deliverables, use a separate authorized renderer and inspect its preview; these scripts do not render previews.

Return the actual registered Artifact or file link, the supported inspection scope and any relevant limitation. Local file creation does not register or publish a platform Artifact. Follow the platform's existing access, draft-confirmation and publication rules.

## Supported scope

Creation supports a new document only. Inspection covers main-body paragraphs and top-level table text in order. Headers, footers, nested-table structure, tracked revisions, comments and drawings are outside this inventory; do not describe it as a complete document review. Use a separate authorized renderer for page previews. Template-preserving edits are not supported.

See [provenance and licensing](PROVENANCE.md). The MIT license covers this independently authored package, not unrelated catalog packages or runtime images.
