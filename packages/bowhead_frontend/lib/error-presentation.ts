import { entitlementErrorMessage } from "./entitlement-error";
import type { Locale } from "./locale";

export type ErrorDisclosure =
  | "public-neutral"
  | "authenticated"
  | "admin"
  | "protocol";

export type ErrorSurface =
  | "login"
  | "authentication"
  | "global"
  | "route"
  | "settings"
  | "connection"
  | "upload"
  | "export"
  | "task"
  | "chat";

export type ErrorActionKind =
  | "retry"
  | "refresh"
  | "sign_in"
  | "open_settings"
  | "contact_admin"
  | "view_task";

export interface AgentStepUsage { used: number; limit: number }

export function readAgentStepUsage(value: unknown): AgentStepUsage | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  const used = candidate.used, limit = candidate.limit;
  if (typeof used !== "number" || typeof limit !== "number" || !Number.isSafeInteger(used) || !Number.isSafeInteger(limit)
    || used < 0 || limit < 1 || limit > 10000 || used > limit) return undefined;
  return { used, limit };
}

export interface ErrorDiagnostics {
  stepUsage?: AgentStepUsage;
  schemaVersion: 1;
  disclosure: ErrorDisclosure;
  status?: number;
  code?: string;
  diagnosticCode?: string;
  requestId?: string;
  occurrenceId?: string;
  digest?: string;
  traceId?: string;
  turnId?: string;
  operationId?: string;
  safeDetail?: string;
  providerMessage?: string;
  retryable?: boolean;
  observedAt?: string;
  /**
   * Client transport projections set this only when fetch failed before an
   * HTTP response existed. It must never be used together with requestId.
   */
  serverRequestIdUnavailable?: true;
}

export interface ErrorPresentation {
  message: string;
  action?: {
    kind: ErrorActionKind;
    label: string;
    href?: string;
  };
  diagnostics?: ErrorDiagnostics;
}

export type ValidationIssueCode =
  | "field_required"
  | "invalid_format"
  | "value_too_short"
  | "value_too_large"
  | "value_out_of_range"
  | "invalid_value";

export interface ValidationIssue {
  path?: string;
  code: ValidationIssueCode;
  params: Readonly<Record<string, number | string>>;
}

export interface TransportFailureContext {
  phase: "connect" | "send" | "receive" | "stream";
  timeoutTriggered: boolean;
  userAborted: boolean;
  onlineHint?: boolean;
}

export interface TransportFailureMessages {
  requestTimedOut: string;
  serviceUnreachable: string;
  retry: string;
}

export interface ClientBoundaryFailureMessages {
  routeRenderFailed: string;
  globalRenderFailed: string;
  chunkLoadFailed: string;
  retry: string;
  reload: string;
}

export interface ErrorPresentationContext {
  locale: Locale;
  surface: ErrorSurface;
  allowedDisclosures: ReadonlyArray<ErrorDisclosure>;
  genericMessage: string;
  messageForCode?: (code: string) => string | null | undefined;
  actionForCode?: (
    code: string,
  ) => ErrorPresentation["action"] | null | undefined;
}

interface ErrorLike {
  status?: unknown;
  code?: unknown;
  body?: unknown;
  message?: unknown;
  diagnostics?: unknown;
  validationIssues?: unknown;
}

interface ResponseDiagnosticMetadata {
  status: number;
  code?: string;
  requestId?: string;
  observedAt?: string;
}

const DISCLOSURES = new Set<ErrorDisclosure>([
  "public-neutral",
  "authenticated",
  "admin",
  "protocol",
]);
const VALIDATION_CODES = new Set<ValidationIssueCode>([
  "field_required",
  "invalid_format",
  "value_too_short",
  "value_too_large",
  "value_out_of_range",
  "invalid_value",
]);
const CODE_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/;
const DIAGNOSTIC_CODE_RE = /^[a-z][a-z0-9_]{0,63}$/;
const REQUEST_OR_OCCURRENCE_ID_RE = /^[0-9a-f]{32}$/;
const OTHER_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const VALIDATION_PATH_RE =
  /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,3}$/;
const SENSITIVE_PATH_RE =
  /(?:^|_|\b)(?:authorization|cookie|credential|password|secret|token)(?:$|_|\b)/i;
const CONTROL_OR_MARKUP_RE = /[\u0000-\u001f\u007f<>]/;
const URL_RE = /\b(?:https?|file):\/\//i;
const ABSOLUTE_PATH_RE =
  /(?:^|[ =])\/(?:[A-Za-z0-9_.-]+\/)+[A-Za-z0-9_.-]*/;
const INTERNAL_TEXT_RE =
  /\b(?:authorization|bearer|cookie|credential|password|secret|token|traceback|select\s+.+\s+from|insert\s+into|update\s+.+\s+set|delete\s+from)\b/i;
// Mirror backend `DiagnosticPolicy.max_length`: must be ≥ the policy table's maximum
// (`chat_llm_failed` uses 420 for upstream_type/code/param).
//
// ⚠ Exceeding this does **not merely drop one field**: `optionalValue` converts invalid values to null,
// `normalizeErrorDiagnostics` sees null and returns undefined for everything—the entire diagnostics block disappears,
// hiding even phase / http_status, worse than omitting this field.
// Gateway `test_error_diagnostics_detail_budget_parity` binds the two implementations; never change one side alone.
const MAX_SAFE_DETAIL_LENGTH = 420;
const MAX_VALIDATION_ISSUES = 20;
const MAX_VALIDATION_PARAM = 1_000_000;

export function responseRequestId(response: Response): string | undefined {
  const candidate = response.headers.get("x-request-id")?.trim();
  return candidate && REQUEST_OR_OCCURRENCE_ID_RE.test(candidate)
    ? candidate
    : undefined;
}

export function parsePublishedErrorDiagnostics(
  body: unknown,
  metadata: ResponseDiagnosticMetadata,
): ErrorDiagnostics | undefined {
  const envelope = errorEnvelope(body);
  if (!envelope || !isRecord(envelope.diagnostics)) return undefined;

  const parsed = diagnosticsFromWire(envelope.diagnostics);
  if (!parsed) return undefined;

  const code = validCode(metadata.code);
  const requestId =
    validRequestOrOccurrenceId(metadata.requestId) ?? parsed.requestId;
  const observedAt =
    validObservedAt(metadata.observedAt) ??
    parsed.observedAt ??
    new Date().toISOString();

  return {
    ...parsed,
    status: validHttpStatus(metadata.status),
    ...(code ? { code } : {}),
    ...(requestId ? { requestId } : {}),
    observedAt,
  };
}

/** Parse a diagnostics v1 object from a non-HTTP transport such as Chat SSE. */
export function parseErrorDiagnosticsWire(
  value: unknown,
): ErrorDiagnostics | undefined {
  return isRecord(value) ? diagnosticsFromWire(value) : undefined;
}

export function parseValidationIssues(
  body: unknown,
): ReadonlyArray<ValidationIssue> | undefined {
  const envelope = errorEnvelope(body);
  if (!envelope || !Array.isArray(envelope.field_errors)) return undefined;

  const issues: ValidationIssue[] = [];
  for (const candidate of envelope.field_errors.slice(
    0,
    MAX_VALIDATION_ISSUES,
  )) {
    const issue = validationIssue(candidate);
    if (issue) issues.push(issue);
  }
  return issues.length ? issues : undefined;
}

export function toFieldValidationIssues(
  error: ErrorLike,
  formFields: ReadonlyArray<string>,
): ReadonlyArray<ValidationIssue> {
  if (!Array.isArray(error.validationIssues)) return [];
  const allowed = new Set(formFields);
  return error.validationIssues.filter(
    (candidate): candidate is ValidationIssue =>
      isValidationIssue(candidate) &&
      candidate.path !== undefined &&
      allowed.has(candidate.path),
  );
}

export function toErrorPresentation(
  error: ErrorLike,
  context: ErrorPresentationContext,
): ErrorPresentation {
  const code = validCode(error.code);
  const mappedMessage = code
    ? nonEmptyString(context.messageForCode?.(code))
      ?? entitlementErrorMessage(error, context.locale)
    : undefined;
  const diagnostics = normalizeErrorDiagnostics(error.diagnostics);
  const allowedDiagnostics =
    diagnostics &&
    context.allowedDisclosures.includes(diagnostics.disclosure)
      ? diagnostics
      : undefined;
  const action = code ? context.actionForCode?.(code) ?? undefined : undefined;

  return {
    message: mappedMessage ?? context.genericMessage,
    ...(action ? { action } : {}),
    ...(allowedDiagnostics ? { diagnostics: allowedDiagnostics } : {}),
  };
}

export function projectTransportFailure(
  context: TransportFailureContext,
  messages: TransportFailureMessages,
): ErrorPresentation | null {
  if (context.userAborted) return null;

  const timedOut = context.timeoutTriggered;
  return {
    message: timedOut
      ? messages.requestTimedOut
      : messages.serviceUnreachable,
    action: {
      kind: "retry",
      label: messages.retry,
    },
    diagnostics: {
      schemaVersion: 1,
      disclosure: "public-neutral",
      code: timedOut ? "request_timed_out" : "service_unreachable",
      occurrenceId: createClientOccurrenceId(),
      safeDetail: `phase=${context.phase}`,
      retryable: true,
      observedAt: new Date().toISOString(),
      serverRequestIdUnavailable: true,
    },
  };
}

export function projectClientBoundaryFailure(
  error: unknown,
  surface: "route" | "global",
  messages: ClientBoundaryFailureMessages,
): ErrorPresentation {
  const candidate = isRecord(error) ? error : {};
  const chunkLoad =
    surface === "route" &&
    safeRecordProperty(candidate, "name") === "ChunkLoadError";
  const digest = validOtherId(safeRecordProperty(candidate, "digest"));
  const code = chunkLoad
    ? "client_chunk_load_failed"
    : surface === "global"
      ? "global_render_failed"
      : "route_render_failed";

  return {
    message: chunkLoad
      ? messages.chunkLoadFailed
      : surface === "global"
        ? messages.globalRenderFailed
        : messages.routeRenderFailed,
    action: {
      kind: chunkLoad ? "refresh" : "retry",
      label: chunkLoad ? messages.reload : messages.retry,
    },
    diagnostics: {
      schemaVersion: 1,
      disclosure:
        surface === "global" ? "public-neutral" : "authenticated",
      code,
      diagnosticCode: chunkLoad
        ? "client_chunk_load_failed"
        : surface === "global"
          ? "global_error_boundary_caught"
          : "route_error_boundary_caught",
      occurrenceId: createClientOccurrenceId(),
      ...(digest ? { digest } : {}),
      safeDetail: chunkLoad ? "phase=chunk_load" : "phase=render",
      retryable: true,
      observedAt: new Date().toISOString(),
    },
  };
}

/**
 * Extract failure diagnostics for support troubleshooting (HTTP status, request ID).
 *
 * Status codes appear only in collapsed diagnostics/copied content, never user-facing explanation.
 * Prefer published API diagnostics; otherwise assemble minimal diagnostics from error `status` / `requestId`
 * (`ApiError`, `HttpStatusError`). Return undefined when unrecognized.
 */
export function errorDiagnosticsFrom(
  error: unknown,
): ErrorDiagnostics | undefined {
  if (!isRecord(error)) return undefined;
  const published = normalizeErrorDiagnostics(error.diagnostics);
  if (published) return published;
  if (typeof error.status !== "number") return undefined;
  const minimal = {
    schemaVersion: 1,
    disclosure: "authenticated",
    status: error.status,
    ...(typeof error.requestId === "string" && error.requestId
      ? { requestId: error.requestId }
      : {}),
    observedAt: new Date().toISOString(),
  };
  // Invalid error-code format invalidates all diagnostics; fall back without code, retaining status.
  return (
    (typeof error.code === "string" && error.code
      ? normalizeErrorDiagnostics({ ...minimal, code: error.code })
      : undefined) ?? normalizeErrorDiagnostics(minimal)
  );
}

export function normalizeErrorDiagnostics(
  value: unknown,
): ErrorDiagnostics | undefined {
  if (!isRecord(value) || value.schemaVersion !== 1) return undefined;
  const disclosure = validDisclosure(value.disclosure);
  if (!disclosure) return undefined;

  const status = optionalHttpStatus(value, "status");
  const code = optionalCode(value, "code");
  const diagnosticCode = optionalDiagnosticCode(value, "diagnosticCode");
  const requestId = optionalRequestOrOccurrenceId(value, "requestId");
  const occurrenceId = optionalRequestOrOccurrenceId(value, "occurrenceId");
  const digest = optionalOtherId(value, "digest");
  const traceId = optionalOtherId(value, "traceId");
  const turnId = optionalOtherId(value, "turnId");
  const operationId = optionalOtherId(value, "operationId");
  const safeDetail = optionalSafeDetail(value, "safeDetail");
  const stepUsage = readAgentStepUsage(value.stepUsage);
  const providerMessage = optionalProviderMessage(value, "providerMessage");
  const retryable = optionalBoolean(value, "retryable");
  const observedAt = optionalObservedAt(value, "observedAt");
  const requestIdUnavailable = optionalTrue(
    value,
    "serverRequestIdUnavailable",
  );

  if (
    status === null ||
    code === null ||
    diagnosticCode === null ||
    requestId === null ||
    occurrenceId === null ||
    digest === null ||
    traceId === null ||
    turnId === null ||
    operationId === null ||
    safeDetail === null ||
    retryable === null ||
    observedAt === null ||
    requestIdUnavailable === null ||
    (requestId !== undefined && requestIdUnavailable === true)
  ) {
    return undefined;
  }

  return {
    schemaVersion: 1,
    disclosure,
    ...(status !== undefined ? { status } : {}),
    ...(code ? { code } : {}),
    ...(diagnosticCode ? { diagnosticCode } : {}),
    ...(requestId ? { requestId } : {}),
    ...(occurrenceId ? { occurrenceId } : {}),
    ...(digest ? { digest } : {}),
    ...(traceId ? { traceId } : {}),
    ...(turnId ? { turnId } : {}),
    ...(operationId ? { operationId } : {}),
    ...(safeDetail ? { safeDetail } : {}),
    ...(disclosure === "authenticated" && stepUsage ? { stepUsage } : {}),
    ...(disclosure === "authenticated" && diagnosticCode === "chat_llm_failed" && providerMessage ? { providerMessage } : {}),
    ...(retryable !== undefined ? { retryable } : {}),
    ...(observedAt ? { observedAt } : {}),
    ...(requestIdUnavailable ? { serverRequestIdUnavailable: true } : {}),
  };
}

export function buildDiagnosticCopyBundle({
  diagnostics,
  surface,
  version,
}: {
  diagnostics: ErrorDiagnostics;
  surface: ErrorSurface;
  version: string;
}): string | null {
  const safe = normalizeErrorDiagnostics(diagnostics);
  const safeVersion = validVersion(version);
  if (!safe) return null;

  const rows: Array<[string, string]> = [
    ["surface", surface],
    ...(safeVersion ? ([["version", safeVersion]] as Array<[string, string]>) : []),
    ...(safe.observedAt
      ? ([["observed_at", safe.observedAt]] as Array<[string, string]>)
      : []),
    ...(safe.status !== undefined
      ? ([["http_status", String(safe.status)]] as Array<[string, string]>)
      : []),
    ...(safe.code
      ? ([["error_code", safe.code]] as Array<[string, string]>)
      : []),
    ...(safe.diagnosticCode
      ? ([
          ["diagnostic_code", safe.diagnosticCode],
        ] as Array<[string, string]>)
      : []),
    ...(safe.requestId
      ? ([["request_id", safe.requestId]] as Array<[string, string]>)
      : []),
    ...(safe.serverRequestIdUnavailable
      ? ([
          ["server_request_id", "unavailable (no HTTP response)"],
        ] as Array<[string, string]>)
      : []),
    ...(safe.occurrenceId
      ? ([["occurrence_id", safe.occurrenceId]] as Array<[string, string]>)
      : []),
    ...(safe.digest
      ? ([["digest", safe.digest]] as Array<[string, string]>)
      : []),
    ...(safe.traceId
      ? ([["trace_id", safe.traceId]] as Array<[string, string]>)
      : []),
    ...(safe.turnId
      ? ([["turn_id", safe.turnId]] as Array<[string, string]>)
      : []),
    ...(safe.operationId
      ? ([["operation_id", safe.operationId]] as Array<[string, string]>)
      : []),
    ...(safe.safeDetail
      ? ([["safe_detail", safe.safeDetail]] as Array<[string, string]>)
      : []),
    ...(safe.providerMessage
      ? ([["provider_message", safe.providerMessage]] as Array<[string, string]>)
      : []),
    ...(safe.retryable !== undefined
      ? ([["retryable", String(safe.retryable)]] as Array<[string, string]>)
      : []),
  ];
  return [
    "Bowhead error diagnostic v1",
    ...rows.map(([key, value]) => `${key}: ${value}`),
  ].join("\n");
}

export function errorDiagnosticBuildVersion(): string {
  return (
    validVersion(process.env.NEXT_PUBLIC_BOWHEAD_VERSION ?? "") ?? "dev"
  );
}

function diagnosticsFromWire(
  value: Record<string, unknown>,
): ErrorDiagnostics | undefined {
  if (value.schema_version !== 1) return undefined;
  const disclosure = validDisclosure(value.disclosure);
  if (!disclosure) return undefined;

  const diagnosticCode = optionalDiagnosticCode(value, "diagnostic_code");
  const requestId = optionalRequestOrOccurrenceId(value, "request_id");
  const occurrenceId = optionalRequestOrOccurrenceId(value, "occurrence_id");
  const traceId = optionalOtherId(value, "trace_id");
  const turnId = optionalOtherId(value, "turn_id");
  const operationId = optionalOtherId(value, "operation_id");
  const safeDetail = optionalSafeDetail(value, "safe_detail");
  const stepUsage = readAgentStepUsage(value.step_usage);
  const providerMessage = optionalProviderMessage(value, "provider_message");
  const retryable = optionalBoolean(value, "retryable");
  const observedAt = optionalObservedAt(value, "observed_at");

  if (
    diagnosticCode === null ||
    requestId === null ||
    occurrenceId === null ||
    traceId === null ||
    turnId === null ||
    operationId === null ||
    safeDetail === null ||
    retryable === null ||
    observedAt === null
  ) {
    return undefined;
  }

  return {
    schemaVersion: 1,
    disclosure,
    ...(diagnosticCode ? { diagnosticCode } : {}),
    ...(requestId ? { requestId } : {}),
    ...(occurrenceId ? { occurrenceId } : {}),
    ...(traceId ? { traceId } : {}),
    ...(turnId ? { turnId } : {}),
    ...(operationId ? { operationId } : {}),
    ...(safeDetail ? { safeDetail } : {}),
    ...(disclosure === "authenticated" && stepUsage ? { stepUsage } : {}),
    ...(disclosure === "authenticated" && diagnosticCode === "chat_llm_failed" && providerMessage ? { providerMessage } : {}),
    ...(retryable !== undefined ? { retryable } : {}),
    ...(observedAt ? { observedAt } : {}),
  };
}

function errorEnvelope(body: unknown): Record<string, unknown> | undefined {
  if (!isRecord(body)) return undefined;
  if (isRecord(body.detail)) {
    if (isRecord(body.detail.error)) return body.detail.error;
    return body.detail;
  }
  if (isRecord(body.error)) return body.error;
  return undefined;
}

function validationIssue(value: unknown): ValidationIssue | undefined {
  if (!isRecord(value)) return undefined;
  const code = value.code;
  if (typeof code !== "string" || !VALIDATION_CODES.has(code as ValidationIssueCode)) {
    return undefined;
  }
  const path =
    value.path === undefined
      ? undefined
      : validValidationPath(value.path);
  if (value.path !== undefined && !path) return undefined;
  if (!isRecord(value.params)) return undefined;

  const params = validationParams(code as ValidationIssueCode, value.params);
  if (!params) return undefined;
  return {
    ...(path ? { path } : {}),
    code: code as ValidationIssueCode,
    params,
  };
}

function validationParams(
  code: ValidationIssueCode,
  params: Record<string, unknown>,
): Readonly<Record<string, number | string>> | undefined {
  const keys = Object.keys(params);
  if (code === "value_too_short") {
    return boundedSingleParam(params, keys, "min_length");
  }
  if (code === "value_too_large") {
    return boundedSingleParam(params, keys, "max_length");
  }
  return keys.length === 0 ? {} : undefined;
}

function boundedSingleParam(
  params: Record<string, unknown>,
  keys: string[],
  key: "min_length" | "max_length",
): Readonly<Record<string, number>> | undefined {
  const value = params[key];
  if (
    keys.length !== 1 ||
    !Number.isInteger(value) ||
    (value as number) < 0 ||
    (value as number) > MAX_VALIDATION_PARAM
  ) {
    return undefined;
  }
  return { [key]: value as number };
}

function isValidationIssue(value: unknown): value is ValidationIssue {
  return Boolean(validationIssue(value));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function safeRecordProperty(
  record: Record<string, unknown>,
  key: string,
): unknown {
  try {
    return record[key];
  } catch {
    return undefined;
  }
}

function nonEmptyString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const candidate = value.trim();
  return candidate ? candidate : undefined;
}

function validDisclosure(value: unknown): ErrorDisclosure | undefined {
  return typeof value === "string" &&
    DISCLOSURES.has(value as ErrorDisclosure)
    ? (value as ErrorDisclosure)
    : undefined;
}

function validHttpStatus(value: unknown): number | undefined {
  return Number.isInteger(value) && (value as number) >= 100 && (value as number) <= 599
    ? (value as number)
    : undefined;
}

function validCode(value: unknown): string | undefined {
  return typeof value === "string" && CODE_RE.test(value)
    ? value
    : undefined;
}

function validDiagnosticCode(value: unknown): string | undefined {
  return typeof value === "string" && DIAGNOSTIC_CODE_RE.test(value)
    ? value
    : undefined;
}

function validRequestOrOccurrenceId(value: unknown): string | undefined {
  return typeof value === "string" &&
    REQUEST_OR_OCCURRENCE_ID_RE.test(value)
    ? value
    : undefined;
}

function validOtherId(value: unknown): string | undefined {
  return typeof value === "string" && OTHER_ID_RE.test(value)
    ? value
    : undefined;
}

function validValidationPath(value: unknown): string | undefined {
  return typeof value === "string" &&
    VALIDATION_PATH_RE.test(value) &&
    !SENSITIVE_PATH_RE.test(value)
    ? value
    : undefined;
}

function validSafeDetail(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  if (
    !value ||
    value !== value.trim() ||
    value.length > MAX_SAFE_DETAIL_LENGTH ||
    CONTROL_OR_MARKUP_RE.test(value) ||
    URL_RE.test(value) ||
    ABSOLUTE_PATH_RE.test(value) ||
    INTERNAL_TEXT_RE.test(value)
  ) {
    return undefined;
  }
  return value;
}

function validProviderMessage(value: unknown): string | undefined {
  if (typeof value !== "string" || !value || value.length > 500 || value !== value.trim()
    || CONTROL_OR_MARKUP_RE.test(value) || URL_RE.test(value) || ABSOLUTE_PATH_RE.test(value)
    || INTERNAL_TEXT_RE.test(value) || /sk-[A-Za-z0-9][A-Za-z0-9._-]{6,}/.test(value)) return undefined;
  return value;
}

function validObservedAt(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return undefined;
  return new Date(timestamp).toISOString() === value ? value : undefined;
}

function validVersion(value: unknown): string | undefined {
  return typeof value === "string" &&
    /^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$/.test(value)
    ? value
    : undefined;
}

function optionalHttpStatus(
  record: Record<string, unknown>,
  key: string,
): number | undefined | null {
  return optionalValue(record, key, validHttpStatus);
}

function optionalCode(
  record: Record<string, unknown>,
  key: string,
): string | undefined | null {
  return optionalValue(record, key, validCode);
}

function optionalDiagnosticCode(
  record: Record<string, unknown>,
  key: string,
): string | undefined | null {
  return optionalValue(record, key, validDiagnosticCode);
}

function optionalRequestOrOccurrenceId(
  record: Record<string, unknown>,
  key: string,
): string | undefined | null {
  return optionalValue(record, key, validRequestOrOccurrenceId);
}

function optionalOtherId(
  record: Record<string, unknown>,
  key: string,
): string | undefined | null {
  return optionalValue(record, key, validOtherId);
}

function optionalSafeDetail(
  record: Record<string, unknown>,
  key: string,
): string | undefined | null {
  return optionalValue(record, key, validSafeDetail);
}

function optionalProviderMessage(
  record: Record<string, unknown>,
  key: string,
): string | undefined {
  return validProviderMessage(record[key]);
}

function optionalObservedAt(
  record: Record<string, unknown>,
  key: string,
): string | undefined | null {
  return optionalValue(record, key, validObservedAt);
}

function optionalBoolean(
  record: Record<string, unknown>,
  key: string,
): boolean | undefined | null {
  if (!(key in record)) return undefined;
  return typeof record[key] === "boolean" ? record[key] : null;
}

function optionalTrue(
  record: Record<string, unknown>,
  key: string,
): true | undefined | null {
  if (!(key in record)) return undefined;
  return record[key] === true ? true : null;
}

function optionalValue<T>(
  record: Record<string, unknown>,
  key: string,
  validator: (value: unknown) => T | undefined,
): T | undefined | null {
  if (!(key in record)) return undefined;
  return validator(record[key]) ?? null;
}

function createClientOccurrenceId(): string {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}
