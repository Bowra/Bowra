# Canvas report protocol

Use the admitted artifact operations only for the report target attached to this execution.

1. Read before changing an existing report. Re-read after a revision conflict.
2. Apply the smallest patch that satisfies the task. Append a new owned section only when no existing owned section is the right target.
3. Use only real asset references returned by an admitted artifact operation. Never invent chart, image, section, or report identifiers.
4. Add an asset next to the claim it supports, then finalize only after checking that the report is coherent and the requested output exists.
5. If an operation is denied, the target changed, or the allowed output cannot be produced, stop and report the limitation. Do not switch to an undeclared tool or output.

This module describes model behavior. Ownership, revision checks, grant scope, limits, approvals, persistence, events, and replay consistency are enforced by the runtime.
