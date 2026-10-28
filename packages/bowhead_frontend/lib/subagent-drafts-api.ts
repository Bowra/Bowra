import { apiFetch, readApiError, type ApiError } from "./api-client";
import { randomUuid } from "./random-id";
import { spaceHeader } from "./space-storage";
import { captureSubagentDraftContext, type SubagentDraftRequestContext } from "./subagent-draft-context";
export type { SubagentDraftRequestContext } from "./subagent-draft-context";

const BASE = "/api/v1/team-subagents/drafts";

export type SubagentDraftStatus =
  | "generating"
  | "needs_clarification"
  | "ready"
  | "blocked"
  | "consumed"
  | "discarded"
  | "expired";


function draftContextHeaders(context: SubagentDraftRequestContext): Record<string, string> {
  if (!context.teamId || !context.workspaceId)
    throw new SubagentDraftApiError(400, "subagent_draft_context_required", "The original Team and Space are required");
  return spaceHeader(context);
}

type ManualDraftRequest = Pick<ManualSubagentDraftDetail, "draft_id" | "etag" | "requestContext">;

function manualRequestContext(draft: ManualDraftRequest): Readonly<SubagentDraftRequestContext> {
  if (!draft.requestContext) {
    throw new SubagentDraftApiError(400, "subagent_draft_context_required", "The original draft request context is required");
  }
  return draft.requestContext;
}

export interface ManualSubagentDraftDetail {
  /** Client-only request scope, captured by the authorized reader; never sent in the JSON body. */
  requestContext?: Readonly<SubagentDraftRequestContext>;
  schema_version: 2;
  kind: "team_subagent_manual_draft_detail";
  draft_id: string;
  draft_status: "editing" | "ready" | "blocked" | "consumed" | "discarded" | "expired";
  source_mode: "manual" | "intent" | "turn";
  entry_mode: "canvas";
  workspace_id: string;
  purpose: string;
  definition: Partial<ManualSubagentDraftDefinition>;
  validation: { status: string; issues: SubagentDraftValidationIssue[] };
  warnings: SubagentDraftWarning[];
  mutation_id: string;
  version: number;
  etag: string;
  /** Last saved time (ISO); older servers may omit it. */
  updated_at?: string | null;
  expires_at: string;
  result: { definition_id: string; consumed_at: string } | null;
  actions: { edit: boolean; validate: boolean; create: boolean; discard: boolean };
}

export interface CanvasDraftSuggestion {
  schema_version: 2;
  kind: "team_subagent_canvas_suggestion";
  suggestion_id: string;
  draft_id: string;
  base_version: number;
  suggestion_kind: "definition" | "clarification";
  status: "pending" | "applied" | "dismissed";
  payload: {
    changed_fields?: string[];
    purpose?: string;
    question?: string;
    definition?: Partial<ManualSubagentDraftDefinition>;
    recommendations?: SubagentDraftTool[];
    assumptions?: string[];
    warnings?: SubagentDraftWarning[];
  };
  created_at: string;
  expires_at: string;
}

export type ManualSubagentDraftDefinition = SubagentDraftDefinition & { output_schema_text?: string };

export interface SubagentDraftDefinition {
  skill_grants?: import("./agents-api").AgentExactSkillGrant[];
  capability_ids?: string[];
  /** Built-in Plugins (`builtin:<name>`), omitted when none are selected. */
  builtin_plugin_ids?: string[];
  name: string;
  display_name: string;
  description: string;
  system_prompt: string;
  model: string | null;
  max_iterations: number;
  output_schema: Record<string, unknown> | null;
  visibility: "creator_only";
  tool_refs?: string[];
  skills?: string[];
}

export interface SubagentDraftTool {
  tool_ref: string;
  display_name: string;
  tier: "required" | "recommended";
  selected: boolean;
  risk: "read" | "compute" | "artifact_write" | "external_write" | "destructive";
  source: {
    kind: string;
    display_name: string | null;
  };
  reason: string;
  evidence: string[];
  confidence: "high" | "medium" | "low";
  locked_reason: string | null;
  approval_required: boolean;
}

export interface SubagentDraftWarning {
  code: string;
  message: string;
  risk_registry_version?: string;
  tool_count?: number;
}

export interface SubagentDraftValidationIssue {
  code: string;
  severity: "error" | "warning";
  field: string | null;
  tool_ref: string | null;
  message_key: string;
  /** Server-rendered, user-readable explanation in the draft locale, when available. */
  message?: string;
  details: Record<string, unknown>;
}

export interface SubagentDraftActions {
  clarify: boolean;
  edit: boolean;
  validate: boolean;
  create: boolean;
  discard: boolean;
  regenerate: boolean;
}

export interface SubagentDraftDetail {
  schema_version: 1;
  kind: "team_subagent_draft_detail";
  draft_id: string;
  draft_status: SubagentDraftStatus;
  authoring_state: string;
  source_mode: "intent" | "turn";
  entry_mode: "conversational";
  purpose: string | null;
  goal_summary: string | null;
  definition: SubagentDraftDefinition | null;
  tools: SubagentDraftTool[];
  skills: [];
  assumptions: string[];
  warnings: SubagentDraftWarning[];
  clarification: {
    schema_version: 1;
    question: string;
    answered: boolean;
    answer?: string;
  } | null;
  validation: {
    status: "valid" | "warning" | "invalid" | "unknown";
    issues: SubagentDraftValidationIssue[];
    catalog_fingerprint: string | null;
  };
  mutation_id: string;
  version: number;
  etag: string;
  expires_at: string;
  result: {
    definition_id: string;
    consumed_at: string | null;
  } | null;
  actions: SubagentDraftActions;
}

export interface SubagentDraftListItem {
  draft_id: string;
  draft_status: "generating" | "needs_clarification" | "editing" | "ready" | "blocked";
  source_mode: "intent" | "turn" | "manual";
  title: string;
  updated_at: string;
  expires_at: string;
}

export async function listSubagentDrafts(): Promise<SubagentDraftListItem[]> {
  const response = await check(await apiFetch(BASE, { cache: "no-store" }));
  const body: unknown = await response.json();
  if (!isRecord(body) || body.kind !== "team_subagent_draft_list" || !Array.isArray(body.items)) {
    throw new SubagentDraftApiError(502, "subagent_draft_response_invalid", "Unsupported Subagent draft list");
  }
  return body.items as SubagentDraftListItem[];
}

/** Manual drafts are saved in a Space: callers pass the resolved Space instead of reading tab storage. */
export async function listManualSubagentDrafts(context: SubagentDraftRequestContext): Promise<SubagentDraftListItem[]> {
  const response = await check(await apiFetch(`${BASE}?schema_version=2`, {
    cache: "no-store", headers: draftContextHeaders(context),
  }));
  const body: unknown = await response.json();
  if (!isRecord(body) || body.schema_version !== 2 || body.kind !== "team_subagent_draft_list" || !Array.isArray(body.items)) {
    throw new SubagentDraftApiError(502, "subagent_draft_response_invalid", "Unsupported manual draft list");
  }
  return body.items as SubagentDraftListItem[];
}

export interface CreatedSubagentFromDraft {
  schema_version: 1;
  kind: "team_subagent_created_from_draft";
  replayed: boolean;
  draft_id: string;
  definition_id: string;
  visibility?: "creator_only";
  status?: "enabled";
  lifecycle_state?: "active";
}

interface DraftRequestInProgress {
  schema_version: 1;
  kind: "subagent_draft_request_in_progress";
  retry_after_ms: number;
  expires_at: string;
  draft_id: null;
}

export class SubagentDraftApiError extends Error {
  constructor(
    public status: number,
    public code: string | undefined,
    message: string,
    public details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "SubagentDraftApiError";
  }
}

/** Only an exact draft read establishes loss of access; a rejected field/action does not. */
export function isSubagentDraftReadUnavailable(cause: unknown): boolean {
  return cause instanceof SubagentDraftApiError && [401, 403, 404].includes(cause.status);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function errorDetails(error: ApiError): Record<string, unknown> | undefined {
  if (!isRecord(error.body)) return undefined;
  const detail = error.body.detail;
  if (!isRecord(detail) || !isRecord(detail.error)) return undefined;
  return isRecord(detail.error.details) ? detail.error.details : undefined;
}

async function check(response: Response): Promise<Response> {
  if (response.ok) return response;
  const error = await readApiError(response);
  throw new SubagentDraftApiError(
    error.status,
    error.code,
    error.message,
    errorDetails(error),
  );
}

function expectDraft(value: unknown): SubagentDraftDetail {
  if (
    !isRecord(value)
    || value.schema_version !== 1
    || value.kind !== "team_subagent_draft_detail"
    || typeof value.draft_id !== "string"
    || typeof value.etag !== "string"
  ) {
    throw new SubagentDraftApiError(
      502,
      "subagent_draft_response_invalid",
      "Unsupported Subagent draft response",
    );
  }
  return value as unknown as SubagentDraftDetail;
}

function uuid(): string {
  return randomUuid();
}

function abortableDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timeout = window.setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

async function postDraftAttempt(
  purpose: string,
  idempotencyKey: string,
  locale: string,
  signal?: AbortSignal,
): Promise<SubagentDraftDetail | DraftRequestInProgress> {
  const response = await apiFetch(BASE, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({
      schema_version: 1,
      source_mode: "intent",
      purpose,
      source_turn_id: null,
      locale,
    }),
    signal,
  });
  await check(response);
  const value = await response.json() as unknown;
  if (
    isRecord(value)
    && value.schema_version === 1
    && value.kind === "subagent_draft_request_in_progress"
  ) {
    return value as unknown as DraftRequestInProgress;
  }
  return expectDraft(value);
}

/**
 * Generates a draft and replays the same idempotent POST while another request
 * still owns the short assessing lease. The total client-side replay wait is
 * bounded to ten seconds.
 */
export async function generateSubagentDraft(
  purpose: string,
  options: {
    locale?: string;
    idempotencyKey?: string;
    signal?: AbortSignal;
  } = {},
): Promise<SubagentDraftDetail> {
  const idempotencyKey = options.idempotencyKey ?? uuid();
  const startedAt = Date.now();
  const delays = [1000, 2000, 4000, 5000];
  let attempt = 0;
  for (;;) {
    const result = await postDraftAttempt(
      purpose,
      idempotencyKey,
      options.locale ?? "en-US",
      options.signal,
    );
    if (result.kind === "team_subagent_draft_detail") return result;
    const elapsed = Date.now() - startedAt;
    if (elapsed >= 10_000) {
      throw new SubagentDraftApiError(
        408,
        "subagent_draft_generation_timeout",
        "Draft generation is taking longer than expected. Try again.",
      );
    }
    const serverDelay = Math.max(250, result.retry_after_ms || 0);
    const delay = Math.min(
      Math.max(
        delays[Math.min(attempt, delays.length - 1)],
        serverDelay,
      ),
      10_000 - elapsed,
    );
    attempt += 1;
    await abortableDelay(delay, options.signal);
  }
}

export async function getSubagentDraft(
  draftId: string,
  signal?: AbortSignal,
  context?: SubagentDraftRequestContext,
): Promise<SubagentDraftDetail> {
  const response = await apiFetch(`${BASE}/${encodeURIComponent(draftId)}`, {
    signal,
    ...(context ? { headers: spaceHeader(context) } : {}),
  });
  await check(response);
  return expectDraft(await response.json() as unknown);
}

async function postRegenerateAttempt(
  draftId: string,
  etag: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<SubagentDraftDetail | DraftRequestInProgress> {
  const response = await apiFetch(
    `${BASE}/${encodeURIComponent(draftId)}/regenerate`,
    {
      method: "POST",
      headers: {
        "If-Match": etag,
        "Idempotency-Key": idempotencyKey,
      },
      signal,
    },
  );
  await check(response);
  const value = await response.json() as unknown;
  if (
    isRecord(value)
    && value.schema_version === 1
    && value.kind === "subagent_draft_request_in_progress"
  ) {
    return value as unknown as DraftRequestInProgress;
  }
  return expectDraft(value);
}

export async function regenerateSubagentDraft(
  draftId: string,
  etag: string,
  options: {
    idempotencyKey?: string;
    signal?: AbortSignal;
  } = {},
): Promise<SubagentDraftDetail> {
  const idempotencyKey = options.idempotencyKey ?? uuid();
  const startedAt = Date.now();
  const delays = [1000, 2000, 4000, 5000];
  let attempt = 0;
  for (;;) {
    const result = await postRegenerateAttempt(
      draftId,
      etag,
      idempotencyKey,
      options.signal,
    );
    if (result.kind === "team_subagent_draft_detail") return result;
    const elapsed = Date.now() - startedAt;
    if (elapsed >= 10_000) {
      throw new SubagentDraftApiError(
        408,
        "subagent_draft_generation_timeout",
        "Draft regeneration is taking longer than expected. Try again.",
      );
    }
    const delay = Math.min(
      Math.max(
        delays[Math.min(attempt, delays.length - 1)],
        Math.max(250, result.retry_after_ms || 0),
      ),
      10_000 - elapsed,
    );
    attempt += 1;
    await abortableDelay(delay, options.signal);
  }
}

export async function clarifySubagentDraft(
  draftId: string,
  answer: string,
  etag: string,
): Promise<SubagentDraftDetail> {
  const response = await apiFetch(
    `${BASE}/${encodeURIComponent(draftId)}/clarify`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "If-Match": etag,
      },
      body: JSON.stringify({ schema_version: 1, answer }),
    },
  );
  await check(response);
  return expectDraft(await response.json() as unknown);
}

export interface UpdateSubagentDraftBody {
  schema_version: 1;
  definition: Omit<SubagentDraftDefinition, "tool_refs" | "skills">;
  selected_tool_refs: string[];
  selected_skills: [];
}

export async function updateSubagentDraft(
  draftId: string,
  body: UpdateSubagentDraftBody,
  etag: string,
): Promise<SubagentDraftDetail> {
  const response = await apiFetch(`${BASE}/${encodeURIComponent(draftId)}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      "If-Match": etag,
    },
    body: JSON.stringify(body),
  });
  await check(response);
  return expectDraft(await response.json() as unknown);
}

export async function createSubagentFromDraft(
  draft: Pick<SubagentDraftDetail, "draft_id" | "mutation_id" | "etag">,
  acknowledgedWarningCodes: string[],
): Promise<CreatedSubagentFromDraft> {
  const response = await apiFetch(
    `${BASE}/${encodeURIComponent(draft.draft_id)}/create`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "If-Match": draft.etag,
      },
      body: JSON.stringify({
        schema_version: 1,
        mutation_id: draft.mutation_id,
        acknowledged_warning_codes: acknowledgedWarningCodes,
      }),
    },
  );
  await check(response);
  const value = await response.json() as unknown;
  if (
    !isRecord(value)
    || value.schema_version !== 1
    || value.kind !== "team_subagent_created_from_draft"
    || typeof value.definition_id !== "string"
  ) {
    throw new SubagentDraftApiError(
      502,
      "subagent_draft_create_response_invalid",
      "Unsupported created Subagent response",
    );
  }
  return value as unknown as CreatedSubagentFromDraft;
}

export async function discardSubagentDraft(
  draftId: string,
  etag: string,
): Promise<void> {
  const response = await apiFetch(`${BASE}/${encodeURIComponent(draftId)}`, {
    method: "DELETE",
    headers: { "If-Match": etag },
  });
  await check(response);
}

function expectManualDraft(value: unknown, context: SubagentDraftRequestContext): ManualSubagentDraftDetail {
  if (!isRecord(value) || value.schema_version !== 2
    || value.kind !== "team_subagent_manual_draft_detail"
    || typeof value.draft_id !== "string" || typeof value.etag !== "string") {
    throw new SubagentDraftApiError(502, "subagent_draft_response_invalid", "Unsupported manual draft response");
  }
  if (value.workspace_id !== context.workspaceId) {
    throw new SubagentDraftApiError(502, "subagent_draft_response_invalid", "The draft response does not match its requested Space");
  }
  return { ...value, requestContext: Object.freeze({ ...context }) } as unknown as ManualSubagentDraftDetail;
}

export async function createManualSubagentDraft(
  purpose: string,
  idempotencyKey: string,
  locale: string,
  sourceMode: "manual" | "intent" = "manual",
  context: SubagentDraftRequestContext = captureSubagentDraftContext(),
): Promise<ManualSubagentDraftDetail> {
  const response = await check(await apiFetch(BASE, {
    method: "POST",
    headers: { ...draftContextHeaders(context), "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ schema_version: 2, source_mode: sourceMode, purpose, locale }),
  }));
  return expectManualDraft(await response.json() as unknown, context);
}

function expectCanvasSuggestion(value: unknown): CanvasDraftSuggestion {
  if (!isRecord(value) || value.schema_version !== 2
    || value.kind !== "team_subagent_canvas_suggestion"
    || typeof value.suggestion_id !== "string" || !isRecord(value.payload)) {
    throw new SubagentDraftApiError(502, "subagent_draft_response_invalid", "Unsupported Canvas suggestion");
  }
  return value as unknown as CanvasDraftSuggestion;
}

export async function listCanvasDraftSuggestions(draftId: string, context: SubagentDraftRequestContext): Promise<CanvasDraftSuggestion[]> {
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draftId)}/suggestions`, {
    cache: "no-store", headers: draftContextHeaders(context),
  }));
  const value: unknown = await response.json();
  if (!isRecord(value) || value.schema_version !== 2 || value.kind !== "team_subagent_canvas_suggestions"
    || !Array.isArray(value.items)) {
    throw new SubagentDraftApiError(502, "subagent_draft_response_invalid", "Unsupported Canvas suggestions");
  }
  return value.items.map(expectCanvasSuggestion);
}

export async function generateCanvasDraftSuggestion(
  draft: ManualDraftRequest,
  purpose: string,
  options: { idempotencyKey: string; locale: string; signal?: AbortSignal },
): Promise<CanvasDraftSuggestion> {
  const context = manualRequestContext(draft);
  for (;;) {
    if (options.signal?.aborted) throw new DOMException("Stopped waiting", "AbortError");
    const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draft.draft_id)}/suggestions`, {
      method: "POST",
      headers: { ...draftContextHeaders(context), "Content-Type": "application/json", "Idempotency-Key": options.idempotencyKey, "If-Match": draft.etag },
      body: JSON.stringify({ schema_version: 2, purpose, locale: options.locale }),
    }));
    const value: unknown = await response.json();
    if (isRecord(value) && value.kind === "subagent_canvas_suggestion_in_progress") {
      await abortableDelay(Math.max(500, Math.min(Number(value.retry_after_ms) || 1000, 5000)), options.signal);
      continue;
    }
    return expectCanvasSuggestion(value);
  }
}

export async function applyCanvasDraftSuggestion(
  draft: ManualDraftRequest,
  suggestionId: string,
  selectedFields: string[],
  mutationId: string,
): Promise<ManualSubagentDraftDetail> {
  const context = manualRequestContext(draft);
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draft.draft_id)}/suggestions/${encodeURIComponent(suggestionId)}/apply`, {
    method: "POST",
    headers: { ...draftContextHeaders(context), "Content-Type": "application/json", "If-Match": draft.etag },
    body: JSON.stringify({ schema_version: 2, selected_fields: selectedFields, mutation_id: mutationId }),
  }));
  return expectManualDraft(await response.json() as unknown, context);
}

export async function dismissCanvasDraftSuggestion(draftId: string, suggestionId: string, context: SubagentDraftRequestContext): Promise<void> {
  await check(await apiFetch(`${BASE}/${encodeURIComponent(draftId)}/suggestions/${encodeURIComponent(suggestionId)}/dismiss`, {
    method: "POST", headers: draftContextHeaders(context),
  }));
}

export async function convertSubagentDraftToManual(
  draft: Pick<SubagentDraftDetail, "draft_id" | "etag">,
  purpose: string,
  definition: ManualSubagentDraftDefinition,
  textOnlyConfirmed: boolean,
  context: SubagentDraftRequestContext = captureSubagentDraftContext(),
): Promise<ManualSubagentDraftDetail> {
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draft.draft_id)}/manualize`, {
    method: "POST",
    headers: { ...draftContextHeaders(context), "Content-Type": "application/json", "If-Match": draft.etag },
    body: JSON.stringify({
      schema_version: 2, purpose, definition, text_only_confirmed: textOnlyConfirmed,
    }),
  }));
  return expectManualDraft(await response.json() as unknown, context);
}

export async function getManualSubagentDraft(draftId: string, context: SubagentDraftRequestContext = captureSubagentDraftContext()): Promise<ManualSubagentDraftDetail> {
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draftId)}?schema_version=2`, {
    cache: "no-store", headers: draftContextHeaders(context),
  }));
  return expectManualDraft(await response.json() as unknown, context);
}

export async function saveManualSubagentDraft(
  draft: ManualDraftRequest,
  purpose: string,
  definition: ManualSubagentDraftDefinition,
  textOnlyConfirmed: boolean,
): Promise<ManualSubagentDraftDetail> {
  const context = manualRequestContext(draft);
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draft.draft_id)}`, {
    method: "PATCH",
    headers: { ...draftContextHeaders(context), "Content-Type": "application/json", "If-Match": draft.etag },
    body: JSON.stringify({ schema_version: 2, purpose, definition, text_only_confirmed: textOnlyConfirmed }),
  }));
  return expectManualDraft(await response.json() as unknown, context);
}

export async function validateManualSubagentDraft(
  draft: ManualDraftRequest,
): Promise<ManualSubagentDraftDetail> {
  const context = manualRequestContext(draft);
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draft.draft_id)}/validate`, {
    method: "POST", headers: { ...draftContextHeaders(context), "If-Match": draft.etag },
  }));
  return expectManualDraft(await response.json() as unknown, context);
}

export async function createSubagentFromManualDraft(
  draft: ManualDraftRequest & Pick<ManualSubagentDraftDetail, "mutation_id">,
  acknowledgedWarningCodes: string[],
): Promise<{ definition_id: string; replayed: boolean }> {
  const context = manualRequestContext(draft);
  const response = await check(await apiFetch(`${BASE}/${encodeURIComponent(draft.draft_id)}/create`, {
    method: "POST",
    headers: { ...draftContextHeaders(context), "Content-Type": "application/json", "If-Match": draft.etag },
    body: JSON.stringify({
      schema_version: 2, mutation_id: draft.mutation_id,
      acknowledged_warning_codes: acknowledgedWarningCodes,
    }),
  }));
  const value: unknown = await response.json();
  if (!isRecord(value) || value.schema_version !== 2
    || value.kind !== "team_subagent_created_from_draft"
    || typeof value.definition_id !== "string") {
    throw new SubagentDraftApiError(502, "subagent_draft_create_response_invalid", "Unsupported created Subagent response");
  }
  return { definition_id: value.definition_id, replayed: value.replayed === true };
}
