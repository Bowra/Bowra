import type { ErrorDiagnostics } from "./error-presentation";

/** Native stage evidence never establishes whether an answer was saved. */
export function clarificationFailureStage(
  code: string,
  diagnostics: ErrorDiagnostics | undefined,
): "clarification_capture" | "clarification_resume_preflight" | undefined {
  if (code !== "clarification_resume_unavailable" || diagnostics?.code !== code
    || diagnostics.schemaVersion !== 1 || diagnostics.disclosure !== "authenticated") return undefined;
  if (diagnostics.diagnosticCode === "chat_turn_failed"
    && diagnostics.safeDetail === "phase=clarification_capture; failure_class=configuration") return "clarification_capture";
  if (diagnostics.diagnosticCode === "rule_clarification_failure"
    && diagnostics.safeDetail === "phase=clarification_resume_preflight; failure_class=state") return "clarification_resume_preflight";
  return undefined;
}
