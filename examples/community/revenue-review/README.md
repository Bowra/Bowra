# Revenue review: growth with a margin warning

English | [简体中文](README.zh-CN.md) | [All examples](../README.md)

An eight-row synthetic dataset makes it easy to check whether a report's headline, table and recommendation agree.

## Run the task

1. Attach [revenue.csv](revenue.csv) and [metric-definitions.md](metric-definitions.md), or paste their contents if needed.
2. Paste [task.md](task.md) into your configured Bowra chat.
3. Check the answer against [expected-results.md](expected-results.md).
4. Optionally request a new basic document using an available `bowra-*` Skill. Inspect the actual download and its values before sharing it.

The task does not assume that a model, calculation runtime or document Skill has already been configured. If a required resource is unavailable, the Agent should report that limitation. Standard report downloads use a separate Canvas export flow; see [report help](../../../docs/help/revise-report.en-US.md).

## Inspect and reproduce the reference

Net revenue grows from **$127,000 to $138,000**, while contribution margin falls from **39.45% to 38.91%**. South / Direct is the segment to investigate; the data does not establish the cause.

![Cropped first-page preview of the independent synthetic revenue PDF](references/report-preview.png)

[Download the DOCX, PDF, XLSX and PPTX references](references/README.md). These files were generated directly from checked JSON with the independent document CLIs. They are not an application/LLM transcript, a customer example or proof of an end-to-end deployment.

Recalculate locally with Python 3.12, without external dependencies:

```bash
python3 examples/community/revenue-review/calculate.py
```

The calculator reads the adjacent CSV and prints totals and segment changes. The reference workbook is a **value snapshot**, not a spreadsheet formula model; recalculate from the CSV when changing inputs.
