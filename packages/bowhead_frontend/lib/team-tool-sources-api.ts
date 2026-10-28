/**
 * CCA P2 — team tool sources API wrapper.
 *
 * Backend: /api/v1/team-tool-sources/* (preview / propose / create / validate / CRUD).
 * All calls are cookie-authed (`credentials: "include"`); team-admin + licensing are
 * enforced server-side (the FE never re-checks authz). Mirrors lib/memory-api.ts.
 */

import type { ConnectorPreset } from "@/components/canvas/sections/preset-directory";

import { apiFetch, readApiError } from "./api-client";
import type { ResourceUsageResponse, ResourceUsageSummary } from "./resource-usage";

// ── types (match the gateway response shapes) ──────────────────────────────

export interface OpSummary {
  operation_id: string;
  method: string;
  path: string;
  summary: string;
  is_write: boolean;
  /** null = approval not applicable; false/true = exact write-tool execution gate result. */
  approval_executable?: boolean | null;
  tags: string[];
}

export interface QueryAuthWarning {
  operation_id: string;
  param: string;
}

/** preview returns EITHER a B-type route-away OR the parsed-OpenAPI shortlist. */
export type PreviewResult =
  | { route_away: true; kind: "mcp_server" | "repo"; hint: string }
  | {
      route_away: false;
      kind?: "openapi";
      /** Server-minted identity that create preserves as the final source id. */
      draft_source_id?: string;
      /** Server attestation binding the reserved identity to this actor and source kind. */
      draft_source_token?: string;
      operations: OpSummary[];
      count: number;
      tags: string[];
      query_auth_warnings: QueryAuthWarning[];
      openapi_spec_json: string;
    }
  | {
      // WP2: a remote MCP server preview — the enumerated tools (shaped as ops) + the cache
      // (tools_schema) that createSource stores. No openapi_spec_json.
      route_away: false;
      kind: "mcp";
      /** Server-minted identity that create preserves as the final source id. */
      draft_source_id?: string;
      /** Server attestation binding the reserved identity to this actor and source kind. */
      draft_source_token?: string;
      operations: OpSummary[];
      count: number;
      tags: string[];
      tools_schema: Array<Record<string, unknown>>;
      /** Metadata-only prompts/list cache. Prompt expansion is gated separately. */
      prompts_schema: Array<Record<string, unknown>>;
      prompts_count: number;
    }
  | {
      // WP2b-3c: the MCP server REQUIRES OAuth — enumerate refused unauthenticated, so we can't list
      // tools yet. The FE routes into the create-then-authorize flow (create empty oauth source → authorize).
      route_away: false;
      kind: "mcp";
      needs_oauth: true;
      mcp_url: string;
      provider_hint: string | null;
    };

export interface ProposeResult {
  /** operationId → LLM-drafted description (the admin edits these before create). */
  proposals: Record<string, string>;
}

// "oauth" (WP2b) is MCP-only: no creds are sent at create — the token is established via the
// authorizeSource() → browser redirect → /oauth/callback flow. A source may be created oauth with
// NO tools; the callback re-enumerates post-auth.
export type AuthType = "none" | "bearer" | "api_key" | "basic" | "oauth";

export interface TeamToolSourcePublic {
  id: string;
  team_id: string;
  name: string;
  base_url: string;
  kind: string;
  selected_op_ids: string[];
  auth_type: string;
  has_auth: boolean;
  visibility: string;
  status: string;
  tools_count: number;
  prompts_count?: number;
  /** The real N imported operations (= selected_op_ids.length) — the success-screen count. */
  op_count: number;
  /** Present on create: operations currently advertised to Agents. */
  available_op_count?: number;
  /** Present on create: imported writes hidden by the approval execution gate. */
  approval_unavailable_count?: number;
  /** Server-owned canary state. False means mutations must keep the legacy flow. */
  impact_guard_enabled?: boolean;
  created_at: string;
  updated_at: string;
  query_auth_warnings?: QueryAuthWarning[];
  usage_summary?: ResourceUsageSummary | null;
  /** Present on Impact Guard patches: schedules paused until their dependencies are confirmed again. */
  paused_schedule_ids?: string[];
}

export interface CreateSourceBody {
  /** Optional identity returned by preview; old direct-create clients may omit it. */
  draft_source_id?: string;
  /** Must accompany draft_source_id; prevents clients from choosing an allowlisted identity. */
  draft_source_token?: string;
  kind?: "openapi" | "mcp";
  name: string;
  base_url: string;
  openapi_spec_json?: string; // OpenAPI only
  tools_schema?: Array<Record<string, unknown>>; // MCP: the enumerate cache from preview
  prompts_schema?: Array<Record<string, unknown>>; // MCP: metadata-only prompts/list cache
  selected_op_ids: string[];
  /** operationId/toolName → { description } — per-op overrides (registered NAME is not editable). */
  tool_overrides?: Record<string, { description: string }>;
  auth_type?: AuthType;
  auth?: Record<string, string> | null;
  visibility?: "team_wide" | "creator_only";
}

export interface PatchSourceBody {
  name?: string;
  selected_op_ids?: string[];
  status?: "enabled" | "disabled";
  auth_type?: AuthType;
  auth?: Record<string, string> | null;
}

/** One tool of a source's latest saved snapshot, marked with whether the source imports it. */
export interface SourceToolEntry extends OpSummary {
  imported: boolean;
}

/** GET /{id}/tools — the latest snapshot used to re-choose an existing source's imported tools. */
export interface SourceToolCatalog {
  kind: string;
  /** The legacy empty selection: the source imports every tool it lists. */
  imports_all: boolean;
  items: SourceToolEntry[];
  updated_at: string;
}

export interface ResourceChangeImpact {
  kind: "resource_change_impact";
  schema_version: 1;
  action: "patch" | "delete";
  blocking: boolean;
  summary: ResourceUsageSummary;
  new_admission: "blocked_for_affected_agents";
  running_execution: "future_dispatch_blocked";
  already_sent_effect: "not_reversible";
  required_follow_up: Array<{ kind: "schedule_reconfirm"; job_id: string }>;
  impact_token: string | null;
  expires_in_seconds: number;
  command: { action: string; proposed_change: Record<string, unknown> };
  reason_codes: string[];
}

export interface ResourceChangeGuard {
  impactToken: string;
  idempotencyKey: string;
}

/** WP2b — begin an OAuth authorization: the server discovers/registers + mints PKCE/state and
 *  returns the URL to send the browser to (completing at /oauth/callback → ?oauth=success|error). */
export interface AuthorizeResult {
  authorization_url: string;
}

export interface ValidateResult {
  operation_id: string;
  reachable: boolean | null;
  // reachable=null carries WHY in error_kind: "skipped_write" (write-only source),
  // "needs_params"/"tool_error"/"protocol" (probe couldn't touch the wire → inconclusive).
  result: { ok?: boolean; error_kind?: string; error?: string } | null;
}

// ── error + fetch helpers ──────────────────────────────────────────────────

export class TeamToolApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** Error codes and request IDs go only into Copy Diagnostics. */
    public code?: string,
    public requestId?: string,
  ) {
    super(message);
    this.name = "TeamToolApiError";
  }
}

async function _check(r: Response): Promise<Response> {
  if (r.ok) return r;
  const error = await readApiError(r);
  throw new TeamToolApiError(error.status, error.message, error.code, error.requestId);
}

const BASE = "/api/v1/team-tool-sources";

async function _post<T>(path: string, body: unknown): Promise<T> {
  const r = await apiFetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await _check(r);
  return (await r.json()) as T;
}

// ── calls ──────────────────────────────────────────────────────────────────

/** Preview a source: OpenAPI (fetch/parse a spec → ops) OR MCP (kind:"mcp" → connect the remote
 *  server and list its tools). Returns the op shortlist, or a B-type route-away. */
export function previewSource(body: {
  kind?: "openapi" | "mcp";
  spec_url?: string;
  openapi_spec_json?: string;
  mcp_url?: string;
  auth_type?: AuthType;
  auth?: Record<string, string> | null;
}): Promise<PreviewResult> {
  return _post<PreviewResult>("/preview", body);
}

/** Draft a per-op description via the team LLM (zero-egress: only the pasted spec). */
export function proposeDescriptions(body: {
  openapi_spec_json: string;
  selected_op_ids: string[];
}): Promise<ProposeResult> {
  return _post<ProposeResult>("/propose", body);
}

/** Create the source (auth in the same POST → server-side Fernet; secret never echoed back). */
export function createSource(
  body: CreateSourceBody,
): Promise<TeamToolSourcePublic> {
  return _post<TeamToolSourcePublic>("", body);
}

/** Dry-run reachability: OpenAPI fires a read op with empty args; MCP connects + lists tools. */
export function validateSource(id: string): Promise<ValidateResult> {
  return _post<ValidateResult>(`/${encodeURIComponent(id)}/validate`, {});
}

/** WP2b — begin OAuth authorization for a remote MCP source; returns the URL to redirect the browser to. */
export function authorizeSource(id: string): Promise<AuthorizeResult> {
  return _post<AuthorizeResult>(`/${encodeURIComponent(id)}/oauth/authorize`, {});
}

export async function listSources(): Promise<TeamToolSourcePublic[]> {
  const r = await apiFetch(BASE);
  await _check(r);
  const data = (await r.json()) as { items?: TeamToolSourcePublic[] };
  return data.items ?? [];
}

// external-capability-onboarding §3.1 — curated one-click connector catalog (entitlement-filtered
// server-side; empty for teams that can't create sources). Feeds CreateToolCard's preset picker.
export async function listConnectorPresets(): Promise<ConnectorPreset[]> {
  const r = await apiFetch(`${BASE}/presets`);
  await _check(r);
  const data = (await r.json()) as { items?: ConnectorPreset[] };
  return data.items ?? [];
}

export async function getSourceUsage(
  id: string,
  operationId?: string,
): Promise<ResourceUsageResponse> {
  const params = new URLSearchParams();
  if (operationId) params.set("operation_id", operationId);
  const query = params.toString();
  const r = await apiFetch(
    `${BASE}/${encodeURIComponent(id)}/usage${query ? `?${query}` : ""}`,
  );
  await _check(r);
  return (await r.json()) as ResourceUsageResponse;
}

/** Every tool in the source's latest saved snapshot; save a new selection with patchSource. */
export async function listSourceTools(id: string): Promise<SourceToolCatalog> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}/tools`);
  await _check(r);
  return (await r.json()) as SourceToolCatalog;
}

export function previewSourceImpact(
  id: string,
  body: {
    action: "patch" | "delete";
    proposed_change?: Record<string, unknown>;
  },
): Promise<ResourceChangeImpact> {
  return _post<ResourceChangeImpact>(
    `/${encodeURIComponent(id)}/impact-preview`,
    { ...body, proposed_change: body.proposed_change ?? {} },
  );
}

function _changeGuardHeaders(guard?: ResourceChangeGuard): Record<string, string> {
  if (!guard) return {};
  return {
    "X-Bowhead-Impact-Token": guard.impactToken,
    "Idempotency-Key": guard.idempotencyKey,
  };
}

export async function patchSource(
  id: string,
  body: PatchSourceBody,
  guard?: ResourceChangeGuard,
): Promise<TeamToolSourcePublic> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ..._changeGuardHeaders(guard),
    },
    body: JSON.stringify(body),
  });
  await _check(r);
  return (await r.json()) as TeamToolSourcePublic;
}

export async function deleteSource(
  id: string,
  guard?: ResourceChangeGuard,
): Promise<void> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: _changeGuardHeaders(guard),
  });
  await _check(r);
}
