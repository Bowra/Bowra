// @vitest-environment node
import { describe, expect, it } from "vitest";
import { normalizeStepError } from "./sse-events";

describe("shared Tool failures", () => {
  it("preserves approved script diagnostics independently of inline output", () => {
    const error = normalizeStepError({
      type: "tool_returned_error", code: "tool_code_failed", message_zh: "tool_code_failed", is_transient: false,
      diagnostics: { schema_version: 1, disclosure: "authenticated", diagnostic_code: "tool_failure", safe_detail: "exception=KeyError; missing_key=profit; exit_code=1", retryable: false },
    });
    expect(error?.kind).toBe("permanent");
    expect(error?.retryable).toBe(false);
    expect(error?.diagnostics?.safeDetail).toContain("missing_key=profit");
  });

  it("rejects a mismatched diagnostic family and parent code", () => {
    expect(normalizeStepError({ type: "tool_exception", code: "unregistered", message_zh: "private", is_transient: false, diagnostics: { schema_version: 1, disclosure: "authenticated", diagnostic_code: "tool_failure", safe_detail: "field=column_units" } })?.diagnostics).toBeUndefined();
  });

});
