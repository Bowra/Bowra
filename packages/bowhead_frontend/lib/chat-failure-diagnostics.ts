import { normalizeErrorDiagnostics, parseErrorDiagnosticsWire, type ErrorDiagnostics } from "./error-presentation";

/** The same scoped diagnostic projection for live events and restored history. */
export function terminalErrorDiagnostics(
  payload: Record<string, unknown>,
  reasonCode: string,
  turnId: string,
  diagnosticCode?: string,
): ErrorDiagnostics | undefined {
  const wire = payload.diagnostics;
  if (wire && typeof wire === "object" && "code" in wire && wire.code !== reasonCode) return undefined;
  const parsed = parseErrorDiagnosticsWire(wire);
  const expectedDiagnosticCode =
    diagnosticCode ??
    (reasonCode.startsWith("llm_") ? "chat_llm_failed" : "chat_turn_failed");
  if (
    parsed?.disclosure !== "authenticated" ||
    parsed.diagnosticCode !== expectedDiagnosticCode ||
    !turnId ||
    parsed.turnId !== turnId
  ) {
    return undefined;
  }
  return normalizeErrorDiagnostics({
    ...parsed,
    code: reasonCode,
  });
}
