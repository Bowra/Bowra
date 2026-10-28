/**
 * kb-matrix — shared typed API error (M3 §4.8).
 *
 * In M2, MatrixApiError existed only in run-api; snapshot-api threw plain Error without status,
 * so use-matrix-poll's catch could not identify 404/403 → endless polling (FE-P2-11 root cause = untyped error).
 * Move the type to a shared module; snapshot / run clients both throw MatrixApiError (carrying status).
 */

/** Structured API error (402/409/429 copy branches by code; 404/403 stop polling by status). */
export class MatrixApiError extends Error {
  code: string;
  status: number;
  payload: Record<string, unknown>;

  constructor(status: number, code: string, message: string, payload: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

/** Non-2xx → MatrixApiError (handle both gateway error envelopes and plain detail). */
export async function matrixJsonOrThrow<T>(r: Response, fn: string): Promise<T> {
  if (!r.ok) {
    const body = (await r.json().catch(() => ({}))) as { detail?: unknown };
    const detail = body.detail;
    if (detail && typeof detail === "object" && "error" in detail) {
      const err = (detail as { error: { code?: string; message?: string } & Record<string, unknown> }).error;
      throw new MatrixApiError(r.status, err.code ?? "unknown", err.message ?? `${fn}: ${r.status}`, err);
    }
    const msg = typeof detail === "string" ? detail : detail != null ? JSON.stringify(detail) : `${fn}: ${r.status}`;
    throw new MatrixApiError(r.status, "unknown", msg, {});
  }
  return (await r.json()) as T;
}
