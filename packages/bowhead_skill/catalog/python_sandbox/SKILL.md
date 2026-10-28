---
name: python_sandbox
description: "Run authorized Python calculations in a configured isolated, offline sandbox when the connected BI tools do not provide the required analysis."
version: 1.2.2
allowed-tools: Bash(docker)
audience: peer
title: "Python sandbox"
title_zh: "Python 沙箱"
category: platform
---

# Python sandbox

Use this Skill for statistical tests, forecasting, clustering, regression, correlations and custom calculations beyond the connected BI tools. Use authorized input artifacts and inspect their schemas before calculating.

## Runtime requirements

The operator must configure a compatible executor, isolated runtime and shared writable work directory. The Community source does not include a supported Python sandbox image build. Library availability, fonts, memory limits and optional offline wheels depend on that reviewed runtime; do not assume an unavailable package or helper is installed.

The execution contract uses an isolated container without network access. Do not fetch external data, run shell commands or install packages from the network. If the executor is unavailable, report that limitation and use a supported BI calculation where possible. Changing the analysis code will not fix missing infrastructure.

Native `bowra-docx`, `bowra-pdf`, `bowra-pptx` and `bowra-xlsx` Skills run separately in the application environment. Read their schemas for supported document operations. This Python Skill does not provide the excluded PowerPoint template-refresh helpers. Do not promise to preserve an uploaded presentation through an unavailable helper or silently substitute a new deck.

## When to use a connected BI tool

Prefer `bi_query` for supported aggregation, time series and period comparisons. Use the sandbox when a task needs a calculation the authorized BI tool cannot express. Never recalculate known source figures with invented input values.

## Invocation

```json
{
  "name": "python_sandbox",
  "script": "runner.py",
  "input": {
    "code": "<Python code using the declared inputs>",
    "intent": "Compare sales changes by region",
    "inputs": {
      "sales": { "artifact_id": "art_example" }
    },
    "timeout_sec": 30,
    "purpose": "analysis",
    "deliverable_outputs": []
  }
}
```

- `code` is required and is executed as `/workdir/user.py`.
- Always provide `intent`: one clear sentence explaining this calculation to the user. It appears in source references. Describe the work, rather than copying a code expression. Different calculations need distinguishable descriptions.
- `inputs` maps names of your choice to authorized `artifact_id` values. Use a BI result's actual `_ref_artifact_id` when that result must become an input. Do not supply guessed filenames or paths.
- `timeout_sec` defaults to the effective Team setting (120 seconds by default) and is bounded by the Team maximum and deployment ceiling (600 seconds by default). Running tasks retain their initial limits. Split expensive calculations into smaller tasks; a longer timeout does not repair an unavailable executor.
- `purpose` defaults to `analysis`. Use `deliverable` only when the user has requested a final downloadable file from this execution.
- With `purpose="deliverable"`, list the exact final filenames in `deliverable_outputs`, for example `["results.csv"]`. Write those files at the top level of `/workdir`. Unlisted helper files remain analysis artifacts.

## Files and injected inputs

Each invocation has a fresh work directory and container:

```text
/workdir/
├── user.py
├── input.json
├── inputs/
│   └── sales.csv
└── results.csv
```

The runner injects the `INPUTS` mapping into the Python process. Read the path from that mapping:

```python
import pandas as pd

print(INPUTS["sales"]["columns"])
print(INPUTS["sales"]["sample_first_5"][:2])
print(INPUTS["sales"]["dtypes"])
data = pd.read_csv(INPUTS["sales"]["path"])
```

`INPUTS["sales"]["path"]` is an expression, not a literal filename. Do not put the expression itself inside another quoted string. Only declared inputs are staged; files from a previous invocation do not persist. To reuse a previous output, declare its returned `artifact_id` as an input to the next invocation.

## Input schema (π K)

With schema previews enabled, inspect `INPUTS["<input_name>"]` before using a table. `<input_name>` is the key you chose; it is not necessarily `rows` or `sales`.

- `path`: the staged file path.
- `columns`: available column names; a capped preview can also contain `_more_columns_count`.
- `dtypes`: column types.
- `sample_first_5`: sample rows, with detected sensitive values masked.
- `row_count`: total rows when known; CSV/JSON can return `None`.
- `preview_truncated`: whether a preview limit was reached.

Use previews to understand shape, never as the complete calculation input. Read the actual authorized file and use its real column names. Preserve source language and spelling in column references. If previews are unavailable, inspect the actual file schema before computing.

## Optional retained calculation helpers

A compatible runtime may expose the retained `period_growth` and `dirty_table` modules. Use them only when that runtime includes them.

For period comparisons, `compare_growth` aligns comparable periods, removes unfinished periods and describes whether the result is year-over-year or recent versus previous. Inspect its summary, exclusions and result table before describing a ranking:

```python
import pandas as pd
from period_growth import compare_growth

current = pd.read_csv(INPUTS["current"]["path"])
previous = pd.read_csv(INPUTS["previous"]["path"])
result = compare_growth([current, previous], value="<value_column>", period="<period_column>", group="<group_column>", top_n=3)
print(result.summary)
result.table.to_csv("/workdir/growth.csv", index=False)
```

Use `read_dirty_table` for spreadsheets with title rows, merged cells, unit rows or multiple header levels. Inspect the returned columns and rows. For a clean single-header sheet, ordinary `pandas.read_excel` is sufficient:

```python
from dirty_table import read_dirty_table

data = read_dirty_table(INPUTS["<input_name>"]["path"])
print(data.columns.tolist())
print(data.head(3))
```

## Results and reporting

Both `python_exec` and `skill_invoke(name="python_sandbox", ...)` return an envelope. Read calculated values from `result_json.stdout` and script errors from `result_json.stderr` or `error`. The outer `stdout` belongs to the runner wrapper. Output may be truncated; print final facts concisely.

`outputs` is a recursive filename listing, not proof of upload or successful execution. The ordinary collector registers new, nonempty regular files at the top level of `/workdir`; write final files there instead of a nested directory. Use the actual `output_artifacts[].artifact_id`, also present in `result_json.outputs`, matched by filename. A listing-only file has no registered artifact to reuse.

Check `ok` separately: failed scripts may retain partial analysis artifacts without completing the task. Only explicitly requested, successfully produced filenames listed in `deliverable_outputs` become final deliverables. Use returned artifact identifiers; never invent a download link. To reuse a registered output in another invocation, declare it under `inputs`; the previous path does not persist.

For native charts, write row or series data to CSV and pass the resulting artifact to `add_chart`. Prefer native interactive charts where supported. When computing a ratio, declare `percent_fraction` for values such as `0.83` meaning 83%, and `percent_points` for already scaled values such as `13.5` meaning 13.5%. Do not make the renderer guess the scale.

Use an image only when the required visual is outside the native chart types. A supported scatter chart can label points, so a request for point labels alone does not require a PNG. If the configured runtime supplies plotting libraries, save an image rather than opening an interactive display.

## Failures and optional dependencies

- Missing executor or shared storage: stop retrying the same execution and report the unavailable capability accurately.
- Syntax or calculation error: correct the code using the actual error and rerun a bounded task.
- Missing package: choose an available supported approach or explain the missing runtime dependency. Do not invent an installation command that needs network access inside the sandbox.
- Out of memory: read fewer columns, choose appropriate data types or process chunks. Do not assume unlimited memory.
- Timeout: simplify the algorithm, split the task or request a timeout within the supported limit.

The runtime supports a separately configured offline wheelhouse for declared Skill `sandbox_requirements`. Packages must already be present in the operator's reviewed lock, installed without an index and verified by hashes. Missing or unreviewed wheels fail closed; this is not permission to install arbitrary network packages. Keep runtime package inventory and redistribution review with the operator's runtime deployment.
