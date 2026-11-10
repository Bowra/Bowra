import { describe, expect, it } from "vitest";
import { clarificationFailureCondition, clarificationFailureStage } from "./clarification-failure-stage";
import type { ErrorDiagnostics } from "./error-presentation";

const code = "clarification_resume_unavailable";
function proof(safeDetail: string, diagnosticCode = "rule_clarification_failure"): ErrorDiagnostics {
  return { code, schemaVersion: 1, disclosure: "authenticated", safeDetail, diagnosticCode, retryable: false };
}

describe("native continuation input conditions", () => {
  it.each(["retained_input", "directory_access"] as const)("keeps the real phase separate from %s copy", (condition) => {
    const preflight = proof(`phase=clarification_resume_preflight; failure_class=state; condition=${condition}`);
    expect(clarificationFailureCondition(code, preflight)).toBe(condition);
    expect(clarificationFailureStage(code, preflight)).toBe("clarification_resume_preflight");
    const worker = proof(`phase=chat; failure_class=configuration; condition=${condition}`, "chat_turn_failed");
    expect(clarificationFailureCondition(code, worker)).toBe(condition);
    expect(clarificationFailureStage(code, worker)).toBeUndefined();
  });
  it("retains legacy copy and refuses extra or mismatched source facts", () => {
    const legacy = proof("phase=clarification_resume_preflight; failure_class=state");
    expect(clarificationFailureCondition(code, legacy)).toBeUndefined();
    expect(clarificationFailureStage(code, legacy)).toBe("clarification_resume_preflight");
    for (const invalid of [
      proof("phase=clarification_resume_preflight; failure_class=state; condition=untrusted"),
      proof("phase=clarification_resume_preflight; failure_class=state; condition=retained_input; extra=untrusted"),
      proof("phase=clarification_resume_preflight; failure_class=state; condition=retained_input", "tool_failure"),
    ]) expect(clarificationFailureCondition(code, invalid)).toBeUndefined();
  });
});
