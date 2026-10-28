import { clearInvalidSessionAndRedirect, fetchWithRefresh } from "./auth-api";
import {
  genericUserFacingErrorMessage,
  safeUserFacingErrorMessage,
} from "./user-facing-error";
import {
  parsePublishedErrorDiagnostics,
  parseValidationIssues,
  responseRequestId,
} from "./error-presentation";
import type {
  ErrorDiagnostics,
  ValidationIssue,
} from "./error-presentation";

export interface ApiErrorOptions {
  body?: unknown;
  code?: string;
  requestId?: string;
  diagnostics?: ErrorDiagnostics;
  validationIssues?: ReadonlyArray<ValidationIssue>;
  /** Structured `details` published next to the error code (for example `blocked_items`). */
  details?: Record<string, unknown>;
  /** Response is not from this application (HTML error page)—see `ApiError.nonApiResponse`. */
  nonApiResponse?: boolean;
}

/**
 * Handwritten-fetch failure: retain original label: status-code message shape (callers classify 404/503 from it);
 * attach status/request ID fields for `errorDiagnosticsFrom` in collapsed diagnostics.
 * Message is for logging/classification only, never direct user display.
 */
export class HttpStatusError extends Error {
  status: number;
  requestId?: string;

  constructor(label: string, response: Response) {
    super(`${label}: ${response.status}`);
    this.name = "HttpStatusError";
    this.status = response.status;
    this.requestId = responseRequestId(response);
  }
}

export class ApiError extends Error {
  status: number;
  body?: unknown;
  code?: string;
  requestId?: string;
  diagnostics?: ErrorDiagnostics;
  validationIssues?: ReadonlyArray<ValidationIssue>;
  /** Structured `details` from `detail.error.details`, `error.details` or `detail.details`; data, not UI copy. */
  details?: Record<string, unknown>;
  /**
   * Failure answered by **intermediate infrastructure**, not this application: HTML error body instead of our JSON.
   *
   * Typical case: enterprise WAF / reverse proxy (Azure Application Gateway, Nginx, Cloudflare) intercepts by rule
   * and returns its own 403/502 page—request **never reached the application**. Business-error-code fallbacks
   * wrongly direct admins toward the application (Refresh/Contact Admin), while gateway rules need inspection.
   *
   * A reverse proxy can return an HTML 403 when a request body contains a URL, for example when a WAF
   * treats a nested provider URL as a remote-file inclusion attempt. Preserve the HTML classification
   * from `looksLikeHtml` so administrators can distinguish proxy rejection from application errors.
   */
  nonApiResponse?: boolean;

  constructor(status: number, message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = options.body;
    this.code = options.code;
    this.requestId = options.requestId;
    this.diagnostics = options.diagnostics;
    this.validationIssues = options.validationIssues;
    this.details = options.details;
    this.nonApiResponse = options.nonApiResponse;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readDetail(detail: unknown): { message: string | null; code?: string } {
  if (typeof detail === "string") return { message: stringValue(detail) };
  // FastAPI/Pydantic detail[] contains framework text and may echo user input.
  // New Gateway responses publish finite field_errors next to a localized parent
  // message; legacy arrays fail closed to the generic main message.
  if (Array.isArray(detail)) return { message: null };
  if (!isRecord(detail)) return { message: null };

  const nestedError = detail.error;
  if (isRecord(nestedError)) {
    const nestedMessage = stringValue(nestedError.message);
    const nestedCode = stringValue(nestedError.code)
      ?? stringValue(detail.code)
      ?? undefined;
    // Prefer inner {error:{code}} (nested HTTPException detail, e.g. approval 409
    // E_SKILL_CANDIDATE_STALE), with outer detail.code as fallback.
    if (nestedMessage) return { message: nestedMessage, code: nestedCode };
    if (nestedCode) return { message: null, code: nestedCode };
  }

  const message = stringValue(detail.message);
  const code = stringValue(detail.code) ?? undefined;
  if (message) return { message, code };
  return { message: null, code };
}

/** Structured error details, read from the same envelopes as the error code. */
export function readApiErrorDetails(body: unknown): Record<string, unknown> | undefined {
  if (!isRecord(body)) return undefined;
  const detail = isRecord(body.detail) ? body.detail : null;
  const candidates = [
    detail && isRecord(detail.error) ? detail.error.details : undefined,
    detail?.details,
    isRecord(body.error) ? body.error.details : undefined,
  ];
  for (const candidate of candidates) {
    if (isRecord(candidate)) return candidate;
  }
  return undefined;
}

export function readApiErrorBody(
  body: unknown,
  fallback: string,
): { message: string; code?: string } {
  // Plain text and malformed JSON are not a publication boundary. Keep the raw
  // body on ApiError for compatibility, but never promote it to UI copy.
  if (!isRecord(body)) return { message: fallback };

  const detail = readDetail(body.detail);
  if (detail.message) return { message: detail.message, code: detail.code };
  if (detail.code) {
    return {
      message: safeUserFacingErrorMessage(null, detail.code),
      code: detail.code,
    };
  }

  // Top-level {error: {code, message}}—gateway error_handler.py's standardized CodedError shape.
  if (isRecord(body.error)) {
    const topError = readDetail(body.error);
    if (topError.message) return { message: topError.message, code: topError.code };
    if (topError.code) {
      return {
        message: safeUserFacingErrorMessage(null, topError.code),
        code: topError.code,
      };
    }
  }

  const message = stringValue(body.message) ?? stringValue(body.error);
  if (message) return { message };

  const directCode = stringValue(body.code) ?? undefined;
  if (directCode) {
    return {
      message: safeUserFacingErrorMessage(null, directCode),
      code: directCode,
    };
  }

  return { message: safeUserFacingErrorMessage(fallback) };
}

export async function readApiError(response: Response): Promise<ApiError> {
  const fallback = genericUserFacingErrorMessage();
  const observedAt = new Date().toISOString();
  const requestId = responseRequestId(response);
  let body: unknown = null;
  // HTML response = not answered by this application (WAF / reverse-proxy error page). Propagate this finding,
  // or callers see only an unrecognized error and direct users toward the application.
  let nonApiResponse = false;
  const raw = await response.text().catch(() => "");
  if (raw) {
    try {
      body = JSON.parse(raw) as unknown;
    } catch {
      const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
      const looksLikeHtml =
        contentType.includes("text/html") ||
        /^\s*<!doctype\s+html\b/i.test(raw) ||
        /^\s*<html\b/i.test(raw);
      body = looksLikeHtml ? null : raw;
      nonApiResponse = looksLikeHtml;
    }
  }
  const { message, code } = readApiErrorBody(body, fallback);
  const diagnostics = parsePublishedErrorDiagnostics(body, {
    status: response.status,
    code,
    requestId,
    observedAt,
  });
  const validationIssues = parseValidationIssues(body);
  return new ApiError(response.status, message, {
    body,
    code,
    requestId,
    diagnostics,
    validationIssues,
    details: readApiErrorDetails(body),
    nonApiResponse,
  });
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetchWithRefresh(input, init);
  if (response.status === 401) {
    await clearInvalidSessionAndRedirect();
  }
  return response;
}

export async function apiJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await apiFetch(input, init);
  if (!response.ok) throw await readApiError(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function apiVoid(input: RequestInfo | URL, init?: RequestInit): Promise<void> {
  const response = await apiFetch(input, init);
  if (!response.ok) throw await readApiError(response);
}
