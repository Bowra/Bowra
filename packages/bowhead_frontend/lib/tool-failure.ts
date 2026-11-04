/** Finite Tool failure codes published by the shared invocation boundary. */
export const TOOL_FAILURE_CODES: ReadonlySet<string> = new Set([
  "tool_arguments_invalid", "tool_resource_unavailable", "tool_code_failed",
  "tool_execution_timeout", "tool_execution_unknown", "tool_execution_failed",
  "tool_delivery_review_required",
]);
