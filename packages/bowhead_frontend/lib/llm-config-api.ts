"use client";

import { type FieldContract } from "./configuration-field-guidance";
import { apiJson } from "./api-client";
import type { ParameterProfile } from "./runtime-parameters";

export interface LlmCatalogProvider {
  id: string;
  display_name: string;
  type?: string;
  models: string[];
  supports?: Record<string, boolean>;
}

export interface LlmCatalog {
  version: number;
  providers: LlmCatalogProvider[];
  default_model?: { provider: string; model: string } | null;
  kb?: {
    active?: Record<string, string>;
    reindex?: { state?: string; pending_embed?: { state?: string } | null };
  };
}

export interface AdminLlmConfigResp {
  field_contracts?: Record<string, FieldContract>;
  version: number;
  config: Record<string, unknown>;
  providers: Array<{
    provider_name: string;
    role?: string;
    type?: string;
    base_url?: string;
    fingerprint?: string;
  }>;
  features?: {
    model_discovery_v2?: boolean;
    bowra_connect?: boolean;
    model_runtime_parameters?: boolean;
    capability_probes?: boolean;
    azure_openai?: boolean;
    custom_http?: boolean;
      /** Outbound address policy: `local` allows http/intranet/bare IPs; `saas` accepts only public https. */
    deployment_mode?: "local" | "saas";
};
  provider_identities?: Record<string, string>;
  parameter_profiles?: ParameterProfile[];
  verification?: {
    as_of: string;
    entries: LlmVerificationEntry[];
  };
  migration?: {
    state: string;
    enrolled_config_version: number;
    baseline_state: string;
  };
  reindex?: unknown;
  apply_snapshot_id?: string | null;
}

export function fetchLlmCatalog(): Promise<LlmCatalog> {
  return apiJson<LlmCatalog>("/api/v1/llm/catalog");
}

export function fetchAdminLlmConfig(): Promise<AdminLlmConfigResp> {
  return apiJson<AdminLlmConfigResp>("/api/v1/admin/llm-config");
}

export function saveAdminLlmConfig(
  config: Record<string, unknown>,
  confirmEmbedReindex = false,
  expectedVersion?: number,
  readiness?: LlmConfigApplyReadiness,
  configurationProposalId?: string,
): Promise<AdminLlmConfigResp> {
  return apiJson<AdminLlmConfigResp>("/api/v1/admin/llm-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      config,
      confirm_embed_reindex: confirmEmbedReindex,
      expected_version: expectedVersion,
      readiness,
      configuration_proposal_id: configurationProposalId,
    }),
  });
}

export function fetchModelConfigurationProposal(proposalId: string): Promise<{ proposal_id: string; version: number; config: Record<string, unknown> }> {
  return apiJson(`/api/v1/config-proposals/${encodeURIComponent(proposalId)}/model-review`);
}

/** Retries after a model call fails before replying; the server treats an absent value as this default. */
export const DEFAULT_LLM_MAX_RETRIES = 3;
export const MIN_LLM_MAX_RETRIES = 0;
export const MAX_LLM_MAX_RETRIES = 5;

export interface LlmRetrySetting {
  max_retries: number;
}

export interface SaveLlmRetryResp {
  ok: boolean;
  version: number;
  retry: LlmRetrySetting;
}

/** Saved team retry count from a loaded configuration, or the default when absent or malformed. */
export function llmMaxRetriesFromConfig(config: Record<string, unknown> | null | undefined): number {
  const retry = config?.retry;
  const value = retry && typeof retry === "object" ? (retry as Record<string, unknown>).max_retries : undefined;
  return typeof value === "number"
    && Number.isInteger(value)
    && value >= MIN_LLM_MAX_RETRIES
    && value <= MAX_LLM_MAX_RETRIES
    ? value
    : DEFAULT_LLM_MAX_RETRIES;
}

/** Save only the retry setting. It changes no model assignment, so no readiness review applies. */
export function saveAdminLlmRetry(
  maxRetries: number,
  expectedVersion: number,
): Promise<SaveLlmRetryResp> {
  return apiJson<SaveLlmRetryResp>("/api/v1/admin/llm-config/retry", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_retries: maxRetries, expected_version: expectedVersion }),
  });
}

export interface LlmConfigValidationIssue {
  path?: string;
  code?: string;
  message: string;
  severity?: "error" | "warning";
}

export interface LlmConfigValidationResp {
  valid: boolean;
  version: number;
  issues: LlmConfigValidationIssue[];
  has_changes: boolean;
  changed_sections: string[];
  impact?: {
    active_files?: number;
    requires_embed_reindex?: boolean;
    [key: string]: unknown;
  };
  readiness?: LlmReadiness;
}

export interface LlmVerificationEntry {
  provider_id: string;
  model_id: string;
  capability: string;
  fact_state: string;
  attempt_state?: string | null;
  evidence_id?: string | null;
  observed_at?: string | null;
  fresh_until?: string | null;
  safe_code?: string | null;
}

export interface LlmReadinessIssue {
  code: string;
  field_path: string;
  role_key: string;
  provider_id: string;
  model_id: string;
  capability: string;
  action_code: string;
}

export interface LlmReadinessRow {
  role_key: string;
  provider_id: string;
  provider_display_name: string;
  model_id: string;
  capability: string;
  routing_critical: boolean;
  declared: boolean;
  fact_state: string;
  evidence_id?: string | null;
  observed_at?: string | null;
  fresh_until?: string | null;
  safe_code?: string | null;
  attempt_state?: string | null;
  attempt_safe_code?: string | null;
  /** Provider-reported model id. When different from the configured name, records what the remote actually returned (annotation only, no longer a failure). */
  actual_model_id?: string | null;
  status: string;
  changed: boolean;
  issue_code?: string | null;
}

export interface LlmRiskAcceptance {
  acceptance_token: string;
  risk_code: string;
  provider_id: string;
  model_id: string;
  capability: string;
  role_key: string;
}

export interface LlmReadiness {
  as_of: string;
  draft_semantic_hash: string;
  may_apply: boolean;
  rows: LlmReadinessRow[];
  blocking: LlmReadinessIssue[];
  warnings: LlmReadinessIssue[];
  required_acceptances: LlmRiskAcceptance[];
  migration_acceptance?: {
    acceptance_token: string;
    risk_code: string;
  } | null;
  external_rows: Array<{
    subject_kind: string;
    configured: boolean;
    status: string;
  }>;
  baseline_will_invalidate: boolean;
}

export interface LlmConfigApplyReadiness {
  draft_semantic_hash: string;
  acceptance_tokens: string[];
  migration_acceptance_token?: string | null;
}

export function validateAdminLlmConfig(
  config: Record<string, unknown>,
  expectedVersion?: number,
): Promise<LlmConfigValidationResp> {
  return apiJson<LlmConfigValidationResp>("/api/v1/admin/llm-config/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config, expected_version: expectedVersion }),
  });
}

// WS4 — test-connection by value (before the provider is saved). Probes through the
// gateway's SSRF-guarded client; `code` is a stable machine token → localized string.
export type VerifyProviderCode =
  | "ok"
  | "unauthorized"
  | "unreachable"
  | "blocked"
  | "empty_catalog"
  | "model_required"
  | "model_not_found"
  | "deployment_not_found"
  | "bad_request"
  | "capability_degraded"
  | "capability_failed"
  | "probe_not_supported"
  | "invalid_response"
  | "tool_call_missing"
  | "embedding_missing"
  | "rerank_results_missing"
  | "image_output_missing"
  | "image_generation_failed"
  | "image_generation_timeout"
  | "web_search_output_missing"
  | "rate_limited"
  | "stream_degraded"
  | "error";

export interface VerifyProviderStep {
  step: "connection" | "capability";
  ok: boolean;
  code: VerifyProviderCode;
  status?: "passed" | "degraded" | "failed";
  phase?: "prepare" | "stream" | "tool_selection" | "tool_continuation" | "embed" | null;
  model?: string | null;
  capability?: string | null;
  http_status?: number | null;
  message?: string;
}

export interface VerifyProviderResult {
  ok: boolean;
  code: VerifyProviderCode;
  http_status?: number | null;
  message?: string;
  model_count?: number | null;
  steps?: VerifyProviderStep[];
}

export interface VerifyProviderBody {
  provider_type:
    | "openai_compatible"
    | "anthropic_compatible"
    | "azure_openai"
    | "custom_http";
  base_url: string;
  api_key: string;
  model?: string | null;
  anthropic_version?: string | null;
  messages_path?: string | null;
  capability_protocol?: string | null;
  models?: Array<{
    id: string;
    capabilities: string[];
    capability_protocol?: string | null;
  }>;
}

export function verifyProvider(body: VerifyProviderBody): Promise<VerifyProviderResult> {
  return apiJson<VerifyProviderResult>("/api/v1/admin/llm-config/providers/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function verifyProviderCapability(
  body: VerifyProviderBody & { capability: string },
): Promise<VerifyProviderResult> {
  return apiJson<VerifyProviderResult>("/api/v1/admin/llm-config/providers/verify-capability", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function discoverProviderModels(body: VerifyProviderBody): Promise<VerifyProviderResult & { models: string[] }> {
  return apiJson("/api/v1/admin/llm-config/providers/discover-models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export interface VerifyDraftProviderBody extends Omit<VerifyProviderBody, "api_key"> {
  api_key?: string;
}

export function verifyDraftProvider(
  providerName: string,
  body: VerifyDraftProviderBody,
): Promise<VerifyProviderResult> {
  return apiJson<VerifyProviderResult>(
    "/api/v1/admin/llm-config/providers/verify-draft",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, provider_name: providerName }),
    },
  );
}

export function verifyDraftProviderCapability(
  providerName: string,
  body: VerifyDraftProviderBody & { capability: string },
): Promise<VerifyProviderResult> {
  return apiJson<VerifyProviderResult>(
    "/api/v1/admin/llm-config/providers/verify-draft-capability",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, provider_name: providerName }) },
  );
}

export function discoverDraftProviderModels(providerName: string, body: VerifyDraftProviderBody): Promise<VerifyProviderResult & { models: string[] }> {
  return apiJson("/api/v1/admin/llm-config/providers/discover-draft-models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, provider_name: providerName }) });
}
