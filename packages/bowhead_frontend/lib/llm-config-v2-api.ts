"use client";

import { apiJson } from "./api-client";
import type { ContextPolicy } from "./context-policy";
import type { OutboundParams } from "./outbound-params";
import type { ParameterPreview, RuntimeParameters } from "./runtime-parameters";

export type ModelCapability = "chat" | "tool" | "vision" | "embed" | "rerank";

export interface V2ModelDeclaration {
  id: string;
  capabilities: ModelCapability[];
  declaration_origin?: string;
  public_name?: string;
  deployment?: string;
  /** API surface (Responses API): persist only "responses"; default = chat/completions. */
  api_surface?: "chat_completions" | "responses";
  capability_transports?: Record<string, string>;
  request_body_overrides?: Record<string, Record<string, unknown>>;
  outbound_params?: OutboundParams;
  context_window?: number;
  max_output_tokens?: number;
  runtime_parameters?: RuntimeParameters;
  context_policy?: ContextPolicy;
}

export interface V2ProviderDraft {
  provider_id?: string;
  runtime_key: string;
  display_name?: string;
  type:
    | "openai_compatible"
    | "anthropic_compatible"
    | "azure_openai"
    | "custom_http";
  base_url: string;
  endpoint?: string | null;
  api_version?: string | null;
  api_key?: string;
  auth?: Record<string, unknown>;
  transports?: Record<string, unknown>;
  models: V2ModelDeclaration[];
  default_headers?: Record<string, string>;
  timeout_sec?: number;
  messages_path?: string | null;
  anthropic_version?: string | null;
  outbound_params?: OutboundParams;
  context_window?: number;
  max_output_tokens?: number;
}

export function previewModelParameters(providerDraft: V2ProviderDraft, modelId: string, capability: string, baseConfigVersion: number): Promise<ParameterPreview> {
  return apiJson<ParameterPreview>("/api/v1/admin/llm-config/parameter-preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider_draft: providerDraft, model_id: modelId, capability, base_config_version: baseConfigVersion }),
  });
}

export interface ProviderPrecheckResult {
  ok: true;
  provider_id: string;
  discovery: { supported: boolean };
  request_previews: Array<{
    purpose: string;
    method: string;
    safe_url: string;
  }>;
  warnings: string[];
}

export interface DiscoveryCandidate {
  model_id: string;
  upstream_metadata?: Record<string, string | number | boolean | null>;
  already_added: boolean;
  capability_state: "unknown";
}

export interface DiscoveryResult {
  ok: true;
  provider_id: string;
  discovered_at: string;
  candidates: DiscoveryCandidate[];
  returned_count: number;
  truncated: boolean;
  limit: number;
}

export interface VerificationPlanItem {
  provider_id: string;
  provider_display_name: string;
  model_id: string;
  capability: ModelCapability;
  testability: "testable" | "not_testable";
  safe_code?: string | null;
  provider_request_count: number;
  testability_contract_version: string;
  probe_contract_version?: string | null;
  probe_execution_policy_version: string;
}

export interface VerificationPlan {
  plan_id: string;
  confirmation_token: string;
  expires_at: string;
  item_count: number;
  provider_request_count: number;
  max_concurrency: number;
  items: VerificationPlanItem[];
  notices: string[];
}

export type VerificationItemState =
  | "queued"
  | "verifying"
  | "verified"
  | "failed"
  | "not_testable"
  | "errored"
  | "cancelled"
  | "interrupted";

export interface VerificationBatchItem {
  item_id: string;
  ordinal: number;
  provider_id: string;
  model_id: string;
  capability: ModelCapability;
  state: VerificationItemState;
  revision: number;
  safe_code?: string | null;
  safe_http_status?: number | null;
  safe_summary_code?: string | null;
  duration_ms?: number | null;
}

export interface VerificationBatch {
  batch_id: string;
  plan_id: string;
  state: "queued" | "running" | "cancelling" | "completed" | "cancelled" | "interrupted";
  revision: number;
  base_config_version: number;
  counts: Partial<Record<VerificationItemState, number>>;
  items: VerificationBatchItem[];
}

export interface VerificationBatchCreated {
  batch_id: string;
  plan_id: string;
  state: VerificationBatch["state"];
  revision: number;
  replayed: boolean;
}

export function precheckV2Provider(
  draft: V2ProviderDraft,
): Promise<ProviderPrecheckResult> {
  return apiJson("/api/v1/admin/llm-config/providers/precheck", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...draft, reserve_provider_id: true }),
  });
}

export function discoverV2Models(
  providerId: string,
  draft: V2ProviderDraft,
): Promise<DiscoveryResult> {
  return apiJson(
    `/api/v1/admin/llm-config/providers/${encodeURIComponent(providerId)}/discover-models`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...draft, provider_id: providerId }),
    },
  );
}

export function createVerificationPlan(
  body: {
    base_config_version: number;
    provider_drafts: V2ProviderDraft[];
    items: Array<{
      provider_id: string;
      model_id: string;
      capability: ModelCapability;
    }>;
  },
): Promise<VerificationPlan> {
  return apiJson("/api/v1/admin/llm-config/verification-plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function createVerificationBatch(
  body: {
    plan_id: string;
    confirmation_token: string;
    provider_drafts: V2ProviderDraft[];
  },
  idempotencyKey: string,
): Promise<VerificationBatchCreated> {
  return apiJson("/api/v1/admin/llm-config/verification-batches", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(body),
  });
}

export function fetchVerificationBatch(
  batchId: string,
  afterRevision?: number,
): Promise<VerificationBatch> {
  const query = afterRevision === undefined
    ? ""
    : `?after_revision=${encodeURIComponent(String(afterRevision))}`;
  return apiJson(
    `/api/v1/admin/llm-config/verification-batches/${encodeURIComponent(batchId)}${query}`,
  );
}

export function fetchActiveVerificationBatches(): Promise<{
  batches: Array<Pick<
    VerificationBatch,
    "batch_id" | "plan_id" | "state" | "revision" | "base_config_version"
  >>;
}> {
  return apiJson("/api/v1/admin/llm-config/verification-batches?state=active&limit=10");
}

export function cancelVerificationBatch(
  batchId: string,
): Promise<{
  batch_id: string;
  state: VerificationBatch["state"];
  revision: number;
  request_may_have_reached_provider: boolean;
}> {
  return apiJson(
    `/api/v1/admin/llm-config/verification-batches/${encodeURIComponent(batchId)}/cancel`,
    { method: "POST" },
  );
}

/**
 * Outbound parameters **automatically adapted away** at runtime (read by settings' Automatically Adapted state).
 *
 * Observation, not configuration: the model service rejected a parameter; runtime dropped it, retried and remembered. Normally an empty array,
 * keeping the page quiet by default. Callers treat fetch failures as empty, never an error banner: this UI provides
 * supplementary explanation, not required information.
 */
/** Reviewed context windows the model form prefills; `default` applies to models the catalog does not know. */
export interface OutputBudgetDefaults {
  version: string;
  automatic_ceiling: number;
  unknown_capacity: number;
  models: Record<string, number>;
}

export interface ContextWindowCatalog {
  default: number;
  models: Record<string, number>;
  output_budget_defaults?: OutputBudgetDefaults;
}

export function fetchContextWindowCatalog(): Promise<ContextWindowCatalog> {
  return apiJson("/api/v1/admin/llm-config/context-window-catalog");
}

/** The prefilled window for a model id and where it comes from. */
export function suggestedContextWindow(catalog: ContextWindowCatalog | null, modelId: string, inherited?: number): { value: number; source: "provider" | "catalog" | "default" } | null {
  if (inherited !== undefined && Number.isSafeInteger(inherited) && inherited > 0) return { value: inherited, source: "provider" };
  if (!catalog) return null;
  const known = catalog.models[modelId.trim().toLowerCase()];
  return known ? { value: known, source: "catalog" } : { value: catalog.default, source: "default" };
}

export function fetchOutboundAdaptations(): Promise<{
  items: Array<{ provider: string; model_id: string; param: string; since: string }>;
}> {
  return apiJson("/api/v1/admin/llm-config/outbound-adaptations");
}
