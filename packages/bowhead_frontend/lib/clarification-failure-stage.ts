import type { ErrorDiagnostics } from "./error-presentation";

/** Native stage evidence never establishes whether an answer was saved. */
export function clarificationFailureStage(
  code: string,
  diagnostics: ErrorDiagnostics | undefined,
): "clarification_capture" | "clarification_resume_preflight" | undefined {
  if (code !== "clarification_resume_unavailable" || diagnostics?.code !== code
    || diagnostics.schemaVersion !== 1 || diagnostics.disclosure !== "authenticated") return undefined;
  const condition = clarificationFailureCondition(code, diagnostics);
  const detail = condition ? diagnostics.safeDetail?.slice(0, -`; condition=${condition}`.length) : diagnostics.safeDetail;
  if (diagnostics.diagnosticCode === "chat_turn_failed"
    && detail === "phase=clarification_capture; failure_class=configuration") return "clarification_capture";
  if (diagnostics.diagnosticCode === "rule_clarification_failure"
    && detail === "phase=clarification_resume_preflight; failure_class=state") return "clarification_resume_preflight";
  return undefined;
}

/** Only native finite facts select input recovery copy; they grant no retry. */
export function clarificationFailureCondition(
  code: string,
  diagnostics: ErrorDiagnostics | undefined,
): "retained_input" | "directory_access" | undefined {
  if (code !== "clarification_resume_unavailable" || diagnostics?.code !== code
    || diagnostics.schemaVersion !== 1 || diagnostics.disclosure !== "authenticated") return undefined;
  const bases = diagnostics.diagnosticCode === "chat_turn_failed"
    ? ["phase=chat; failure_class=configuration", "phase=clarification_capture; failure_class=configuration"]
    : diagnostics.diagnosticCode === "rule_clarification_failure"
      ? ["phase=clarification_resume_preflight; failure_class=state"] : [];
  for (const condition of ["retained_input", "directory_access"] as const) {
    if (bases.some((base) => diagnostics.safeDetail === `${base}; condition=${condition}`)) return condition;
  }
  return undefined;
}
