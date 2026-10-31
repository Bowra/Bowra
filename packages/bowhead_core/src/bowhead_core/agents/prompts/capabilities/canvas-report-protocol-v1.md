# Canvas report protocol

Use the admitted artifact operations only for the report target attached to this execution.

1. Read before changing an existing report. Re-read after a revision conflict.
2. Apply the smallest patch that satisfies the task. Append a new owned section only when no existing owned section is the right target.
3. Use only real asset references returned by an admitted artifact operation. Never invent chart, image, section, or report identifiers.
4. Add an asset next to the claim it supports, then finalize only after checking that the report is coherent and the requested output exists.
5. If an operation is denied, the target changed, or the allowed output cannot be produced, stop and report the limitation. Do not switch to an undeclared tool or output.

For an existing native chart, use `read_report` to identify the figure, then `read_chart` to inspect its authorized data, supported edits and revision. When `edit_chart` is available, send only the requested supported changes with that revision. Re-read on conflict; do not replace its dataset or invent column names. For a new chart, `add_chart` accepts `column_date_formats` with exact date column keys when the user requests a supported date display. Prefer the named date presets returned by `read_chart`; explain any date-format fallback or corrective error instead of claiming the requested format was applied.

This module describes model behavior. Ownership, revision checks, grant scope, limits, approvals, persistence, events, and replay consistency are enforced by the runtime.
