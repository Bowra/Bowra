# DOCX input and output contract

[Skill entry](../SKILL.md)

The schema is closed: reject unknown fields, unsupported layouts, mismatched table widths, nonfinite numbers and excess budgets. Input JSON is at most 2 MiB. Create a new output; never overwrite a source or silently drop fields.

## Creation

Every block has one type: `heading` (`text`, `level` 1–6), `paragraph` (`text`), `bullets` (`items`), or `table` (`columns`, `rows`). Table cells are strings and each row has exactly the declared column count. Limits: 100 blocks, 4,000 total table cells; inspection: 2,000 blocks and 200,000 characters.

[create.json](../examples/create.json) is a synthetic example containing no customer data.

## Inspection

```json
{ "operation": "inspect", "source": "inputs/source.docx" }
```

`source` is a canonical relative path inside `--workdir`: no absolute paths, parent segments or symlinks. Stage the file through the caller's existing authorization and immutable-input lifecycle before running. Source size is at most 16 MiB; Office ZIP inputs additionally allow at most 4,096 entries and 64 MiB total declared expanded bytes. These limits are not a substitute for the host worker's process/time/memory isolation.

The report includes `source_sha256` for the exact bytes parsed, scope, content and limitations. No script, macro, formula, embedded content or remote resource is executed. Text/value inspection does not certify a complete visual or security review.

## Outputs and failure

A successful operation creates the format-specific output or `inspection.json`, then writes `result.json`. The summary binds output identity through byte count and SHA-256. Existing outputs are never overwritten. Invalid inputs exit nonzero; a partially failed workdir must not be advertised as a deliverable or reused for a retry.

Dependency failures require fixing the installed environment. Do not improvise missing source text or values, substitute a different format or claim a parser is a renderer.

## Public API references

- [Official format-library documentation](https://python-docx.readthedocs.io/en/latest/api/document.html)
