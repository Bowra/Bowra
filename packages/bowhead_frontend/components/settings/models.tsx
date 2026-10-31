"use client";


import { ConfigurationFieldHelp, ConfigurationFieldProvider } from "@/components/settings/configuration-field-help";
import { modelExternalCapabilitiesPath } from "@/lib/team-settings-paths";

/**
 * Team model settings: overview and scoped connection, assignment, and external pages.
 * Each editor starts from the saved config and changes only its own configuration.
 * Production Review / Apply keeps the existing server-authoritative evidence contract.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { claimBowraImport, startBowraConnection, type BowraImport } from "@/lib/bowra-connect-api";
import { ApiError } from "@/lib/api-client";
import { fetchModelConfigurationProposal } from "@/lib/llm-config-api";
import { refreshLiteState } from "@/lib/config-copilot-lite";

import {
  serializeOutboundParams,
  parseOutboundParams,
  outboundParamsValid,
  type OutboundAdaptation,
  type OutboundParams,
} from "@/lib/outbound-params";
import { OutboundParamsSection } from "@/components/settings/models/outbound-params-section";
import { ContextPolicyFields } from "@/components/settings/models/context-policy-fields";
import { contextPolicyValid, type ContextPolicy } from "@/lib/context-policy";
import { OutputBudgetField } from "@/components/settings/models/output-budget-field";
import { RuntimeParametersSection } from "@/components/settings/models/runtime-parameters-section";
import { RetrySettingSection } from "@/components/settings/models/retry-setting-section";
import { runtimeParametersValid, type ParameterPreview, type ParameterProfile, type RuntimeParameters } from "@/lib/runtime-parameters";

import {
  AlertTriangle, Bolt, Bot, Check, ChevronDown, ChevronRight, Cpu, Database, Edit, Info,
  Layers, Lock, Palette, Plug, Plus, RefreshCw, Search, Server, Slash, Spinner, Trash,
  Users, XSm,
} from "@/components/icons";
import { AiFillPanel, type AiFillApplyReceipt } from "@/components/config-copilot/ai-fill-panel";
import { DiscoveryPicker } from "@/components/settings/models/discovery-picker";
import { DialogShell } from "@/components/ui/dialog";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import {
  ModelSettingsOverview,
  type ModelSettingsOverviewProvider,
} from "@/components/settings/models/model-settings-overview";
import { ReadinessReview } from "@/components/settings/models/readiness-review";
import { evidenceItemKey } from "@/lib/readiness-items";
import { autoJudgeModel } from "@/lib/judge-auto";
import { VerificationPanel } from "@/components/settings/models/verification-panel";
import {
  VerificationSelector,
  type VerificationCandidate,
} from "@/components/settings/models/verification-selector";
import {
  discoverDraftProviderModels, discoverProviderModels, fetchAdminLlmConfig, llmMaxRetriesFromConfig, saveAdminLlmConfig, validateAdminLlmConfig, verifyDraftProviderCapability, verifyProviderCapability,
  type AdminLlmConfigResp, type LlmConfigValidationIssue, type LlmReadinessRow, type LlmConfigValidationResp, type LlmVerificationEntry, type VerifyProviderCode, type VerifyProviderStep,
} from "@/lib/llm-config-api";
import {
  cancelVerificationBatch,
  createVerificationBatch,
  createVerificationPlan,
  discoverV2Models,
  fetchActiveVerificationBatches,
  fetchContextWindowCatalog,
  fetchOutboundAdaptations,
  fetchVerificationBatch,
  precheckV2Provider,
  previewModelParameters,
  suggestedContextWindow,
  type ContextWindowCatalog,
  type DiscoveryResult,
  type ModelCapability,
  type V2ProviderDraft,
  type VerificationBatch,
  type VerificationPlan,
} from "@/lib/llm-config-v2-api";
import {
  modelSettingsErrorKey,
  modelSettingsValidationErrorKey,
} from "@/lib/model-settings-error";
import { type ConfigDraftResult } from "@/lib/config-drafts-api";
import { mapProviderDraftToConnect, pickSecretForApiKey } from "@/lib/provider-draft-map";
import { ExternalCapabilityEditor } from "@/components/settings/models/external-capability-editor";
import { BEFORE_APP_NAVIGATION_EVENT, useAppRouter, type AppNavigationDetail } from "@/lib/use-app-router";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { useCopilotFields } from "@/lib/use-copilot-fields";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { SaveBar } from "@/components/ui/save-bar";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { randomUuid } from "@/lib/random-id";

const LLM_SETTINGS_V4_ENABLED = true;
const TERMINAL_VERIFICATION_BATCH_STATES = new Set([
  "completed",
  "cancelled",
  "interrupted",
]);

// ─── config blob types (v2) ───────────────────────────────────────
type ProviderType =
  | "openai_compatible"
  | "anthropic_compatible"
  | "azure_openai"
  | "custom_http";
type CreditRates = { input: string; cached_input: string; output: string };
const DEFAULT_CREDIT_RATES: CreditRates = { input: "1", cached_input: "1", output: "1" };
function creditRatesValid(rates: CreditRates | undefined): boolean {
  const valueByKind = rates || DEFAULT_CREDIT_RATES;
  if (Object.keys(valueByKind).length !== 3) return false;
  return (["input", "cached_input", "output"] as const).every((kind) => {
    const value = valueByKind[kind];
    if (!/^(?:\d+)(?:\.\d{1,6})?$/.test(value)) return false;
    return Number(value) <= 1000;
  });
}
type ModelObj = {
  id: string;
  capabilities?: string[];
  source?: string;
  protocol?: string;
  deployment?: string;
  api_surface?: string;
  capability_transports?: Record<string, string>;
  request_body_overrides?: Record<string, Record<string, unknown>>;
  outbound_params?: OutboundParams;
  runtime_parameters?: RuntimeParameters;
  context_policy?: ContextPolicy;
  credit_rates?: CreditRates;
  /** Model-declared context window (tokens); omit when unknown. */
  context_window?: number;
  max_output_tokens?: number;
};
interface ProviderSpec {
  max_output_tokens?: number;
  type?: ProviderType;
  display_name?: string;
  base_url?: string;
  endpoint?: string;
  api_version?: string;
  auth?: {
    type?: "bearer" | "api_key" | "secret_header" | "none";
    header_name?: string;
    prefix?: string;
  };
  transports?: Record<string, {
    contract?: string;
    request?: {
      method?: string;
      url_template?: string;
      query?: Record<string, string>;
      headers?: Record<string, string>;
      body_template?: Record<string, unknown>;
    };
  }>;
  api_key?: string;
  fingerprint?: string;
  anthropic_version?: string;
  messages_path?: string;
  supports?: Record<string, boolean>;
  models?: Array<string | ModelObj>;
  [k: string]: unknown;
}
interface Pair { provider?: string; model?: string }
// Tier configuration: primary is explicit; fast/judge may store follow/auto sentinels (intent, not copies).
type TierRef = Pair | { follow: "primary" } | { auto: true };
interface TiersConfig { primary?: Pair; primary_fallback?: Pair[]; fast?: TierRef; judge?: TierRef }
interface KbModel { provider?: string; real_id?: string; capability?: string; protocol?: string; dim?: number; [k: string]: unknown }
interface KbConfig {
  retrieval_top_k?: number;
  models: Record<string, KbModel>;
  active: Record<string, string>;
  pending_embed?: {
    state?: string;
    error_code?: string;
    [k: string]: unknown;
  } | null;
  index_version?: string;
  [k: string]: unknown;
}
// media (P2 team-level image_gen): image models have a separate section, outside provider.models (cap vocabulary excludes image_gen,
// and media endpoints use their own protocol calls). The provider only needs an openai_compatible declaration in config.providers.
interface MediaModel { provider?: string; real_id?: string; capability?: string; protocol?: string; [k: string]: unknown }
interface MediaConfig { models: Record<string, MediaModel>; active: Record<string, string>; [k: string]: unknown }
// search (team-level web_search): external web-search models have a separate section like media (mirror image_gen overall).
interface SearchModel { provider?: string; real_id?: string; capability?: string; protocol?: string; [k: string]: unknown }
interface SearchConfig { models: Record<string, SearchModel>; active: Record<string, string>; [k: string]: unknown }
interface TeamConfig {
  schema_version: 2;
  providers: Record<string, ProviderSpec>;
  tiers: TiersConfig;
  kb: KbConfig;
  media?: MediaConfig;
  search?: SearchConfig;
  [k: string]: unknown;
}

// Tier reference checks
const isFollow = (r?: TierRef): r is { follow: "primary" } => Boolean(r && (r as { follow?: string }).follow === "primary");
const isAuto = (r?: TierRef): r is { auto: true } => Boolean(r && (r as { auto?: boolean }).auto);
const refPair = (r?: TierRef): Pair | null => (r && !isFollow(r) && !isAuto(r) && (r as Pair).provider && (r as Pair).model ? (r as Pair) : null);

// ─── capability vocab (matches backend team_llm_config._CAPABILITY_VOCAB) ───
// image_gen is a UI-only pseudo-capability: selecting it routes this model row to config.media.models (backend provider
// cap vocabulary **excludes** image_gen), so omit it from CAP_VOCAB — provider.models round trips leave no residue either.
const CAP_VOCAB = new Set(["chat", "tool", "vision", "embed", "rerank", "think"]);
const IMAGE_CAP = "image_gen";
// Like image_gen, web_search is a UI-only pseudo-capability: selecting it routes this model row to config.search.models,
// outside CAP_VOCAB (backend provider cap vocabulary **excludes** web_search).
const SEARCH_CAP = "web_search";
const UI_CAPS = ["chat", "tool", "vision", "embed", "rerank", "image_gen", "web_search"] as const;
// image_gen / web_search are both UI-only pseudo-capabilities, routed to separate sections rather than provider.models.
// image_gen is an exclusive role; web_search can share one model row with chat/tool/vision,
// then split into provider.models + search.models on save.
const SPECIAL_CAPS = [IMAGE_CAP, SEARCH_CAP] as const;
const CONVERSATIONAL_CAPS = new Set(["chat", "tool", "vision", "think"]);
const isPureSpecialCap = (caps: string[]): boolean =>
  caps.length > 0
  && caps.every((capability) => SPECIAL_CAPS.includes(capability as never));
// Anthropic-compat providers can't do embedding/rerank/image generation/web search → hide those toggles.
const ANTHROPIC_CAPS = ["chat", "tool", "vision"] as const;
const DECLARATIVE_HTTP_CAPS = ["chat", "tool", "vision", "embed"] as const;
// media call protocols (matches config_loader.MEDIA_PROTOCOLS): dashscope asynchronous polling / ark synchronous.
const MEDIA_PROTOCOLS = ["dashscope_async", "ark_sync"] as const;
const DEFAULT_MEDIA_PROTOCOL = "dashscope_async";
// search call protocols (matches search_config.SEARCH_PROTOCOLS): OpenAI-compatible / native generation.
const SEARCH_PROTOCOLS = ["dashscope_compat", "dashscope_native"] as const;
const DEFAULT_SEARCH_PROTOCOL = "dashscope_compat";
const RERANK_PROTOCOLS = ["openai_compat", "dashscope_native"] as const;
const DEFAULT_RERANK_PROTOCOL = "openai_compat";
const DEFAULT_ANTHROPIC_VERSION = "2023-06-01";
// Tier-based: stop flattening into each agent; configuration stores 3 tiers (primary/fast/judge), and backend AGENT_TIER_MAP
// expands them into individual agent bindings at build time. fast/judge default to follow/auto.
const FOLLOW_VALUE = "__follow__";
const AUTO_VALUE = "__auto__";

const EMPTY_CONFIG: TeamConfig = {
  schema_version: 2, providers: {}, tiers: {}, kb: { models: {}, active: {}, pending_embed: null },
};

// ─── normalized view types ─────────────────────────────────────────
interface NModel {
  id: string;
  caps: string[];
  source: "preset" | "manual";
  protocol?: string;
  deployment?: string;
  /** Protocol surface (Responses API batch 2): only "responses" is meaningful; default = chat/completions. */
  apiSurface?: string;
  requestBodyOverrides?: Record<string, Record<string, unknown>>;
  outboundParams?: OutboundParams;
  runtimeParameters?: RuntimeParameters;
  contextPolicy?: ContextPolicy;
  creditRates?: CreditRates;
  /** Declared capacities only; inferred defaults are never persisted by an unrelated save. */
  contextWindow?: number;
  maxOutputTokens?: number;
}
interface FlatModel { providerKey: string; providerType: ProviderType; id: string; caps: string[]; protocol?: string }
interface DraftModel {
  id: string;
  caps: string[];
  source: "preset" | "manual";
  protocol?: string;
  deployment?: string;
  apiSurface?: string;
  requestBodyOverrides?: string;
  /** Typed outbound-parameter declarations (the raw-JSON escape hatch remains the string field above). */
  outboundParams?: OutboundParams;
  runtimeParameters?: RuntimeParameters;
  contextPolicy?: ContextPolicy;
  creditRates?: CreditRates;
  parameterError?: string;
  /** Declared capacities only; inherited values remain unset in the draft. */
  contextWindow?: number;
  maxOutputTokens?: number;
}
interface ConnectDraft {
  mode: "add" | "edit";
  providerId?: string;
  origKey?: string;
  protocol: ProviderType;
  name: string;
  baseUrl: string;
  apiKey: string;
  messagesPath: string;   // Anthropic-compatible custom endpoint (e.g. DeepSeek /messages); empty = adapter default.
  apiVersion: string;
  providerOutboundParams?: OutboundParams;
  providerContextWindow?: number;
  providerMaxOutputTokens?: number;
  authType: "bearer" | "api_key" | "secret_header" | "none";
  authHeaderName: string;
  authPrefix: string;
  customChatUrl: string;
  customEmbedUrl: string;
  customChatContract: "openai_chat_completions" | "openai_chat_completions_sse";
  customChatQuery: string;
  customChatHeaders: string;
  customChatBody: string;
  customEmbedQuery: string;
  customEmbedHeaders: string;
  customEmbedBody: string;
  models: DraftModel[];
  testing: boolean;
  test?: { ok: boolean; code: VerifyProviderCode; model_count?: number | null; steps?: VerifyProviderStep[]; signature: string };
}
interface ProviderTestEvidence {
  ok: boolean;
  code: VerifyProviderCode;
  status: "passed" | "degraded" | "failed";
  signature: string;
  testedAt: string;
}

const VERIFIABLE_CAPABILITIES = new Set<ModelCapability>([
  "chat",
  "tool",
  "vision",
  "embed",
  "rerank",
]);
const LEGACY_CONNECTION_TEST_CAPABILITIES = new Set([
  ...VERIFIABLE_CAPABILITIES,
  IMAGE_CAP,
  SEARCH_CAP,
]);

export function verifiableModelCapabilities(
  capabilities: string[],
): ModelCapability[] {
  return capabilities.filter(
    (capability): capability is ModelCapability =>
      VERIFIABLE_CAPABILITIES.has(capability as ModelCapability),
  );
}

export function legacyConnectionTestCapabilities(
  capabilities: string[],
): string[] {
  return capabilities.filter((capability) =>
    LEGACY_CONNECTION_TEST_CAPABILITIES.has(capability));
}

const V2_RUNTIME_KEY = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const DEFAULT_AZURE_API_VERSION = "2024-10-21";
const DEFAULT_CUSTOM_CHAT_BODY = JSON.stringify({
  model: { $bowhead: "model" },
  messages: { $bowhead: "messages" },
  tools: { $bowhead: "tools", omit_if_empty: true },
  tool_choice: { $bowhead: "tool_choice", omit_if_empty: true },
  stream: { $bowhead: "stream" },
  max_tokens: { $bowhead: "max_output_tokens" },
}, null, 2);
const DEFAULT_CUSTOM_EMBED_BODY = JSON.stringify({
  model: { $bowhead: "model" },
  input: { $bowhead: "input" },
}, null, 2);

function parseJsonObject(value: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function parseStringRecord(value: string): Record<string, string> | null {
  const parsed = parseJsonObject(value);
  if (!parsed || Object.values(parsed).some((item) => typeof item !== "string")) {
    return null;
  }
  return parsed as Record<string, string>;
}

export function parseModelRequestBodyOverrides(
  value: string,
): Record<string, Record<string, unknown>> | null {
  const parsed = parseJsonObject(value);
  if (
    !parsed
    || Object.values(parsed).some((item) =>
      !item || typeof item !== "object" || Array.isArray(item))
  ) {
    return null;
  }
  return parsed as Record<string, Record<string, unknown>>;
}

export function modelRequestBodyOverridesValid(
  value: string | undefined,
  capabilities: string[],
): boolean {
  if (!value) return true;
  const overrides = parseModelRequestBodyOverrides(value);
  return Boolean(
    overrides
    && Object.keys(overrides).every((capability) =>
      capabilities.includes(capability)),
  );
}

export function draftApiKeyForEdit(
  spec: { api_key?: unknown },
  isPersisted: boolean,
): string {
  if (isPersisted) return "";
  return typeof spec.api_key === "string" ? spec.api_key : "";
}

export function v2ProviderDraftFromConnect(draft: ConnectDraft): V2ProviderDraft {
  const runtimeKey = draft.mode === "edit" && draft.origKey
    ? draft.origKey
    : draft.name.trim();
  const models = draft.models.flatMap((model) => {
    const capabilities = verifiableModelCapabilities(model.caps);
    if (!model.id.trim() || capabilities.length === 0) return [];
    return [{
      id: model.id.trim(),
      capabilities,
      declaration_origin: model.source === "preset"
        ? "discovered_then_declared"
        : "manual",
      ...(model.deployment?.trim()
        ? { deployment: model.deployment.trim() }
        : {}),
      ...(draft.protocol === "openai_compatible" && model.apiSurface === "responses"
        && model.caps.some((capability) => ["chat", "tool", "vision"].includes(capability))
        ? { api_surface: "responses" as const }
        : {}),
      ...(serializeOutboundParams(model.outboundParams) ? { outbound_params: serializeOutboundParams(model.outboundParams) } : {}),
      ...(model.contextWindow ? { context_window: model.contextWindow } : {}),
      ...(model.maxOutputTokens ? { max_output_tokens: model.maxOutputTokens } : {}),
      ...(model.runtimeParameters ? { runtime_parameters: model.runtimeParameters } : {}),
      ...(model.contextPolicy !== undefined ? { context_policy: model.contextPolicy } : {}),
      ...(providerConsumesBodyOverrides(draft.protocol) && model.requestBodyOverrides
        ? { request_body_overrides: parseModelRequestBodyOverrides(model.requestBodyOverrides) || {} }
        : {}),
      ...(draft.protocol === "custom_http"
        ? {
            capability_transports: Object.fromEntries(
              capabilities.map((capability) => [
                capability,
                capability === "embed" ? "embed" : "chat",
              ]),
            ),
            ...(model.requestBodyOverrides
              && Object.keys(
                parseModelRequestBodyOverrides(
                  model.requestBodyOverrides,
                ) || {},
              ).length
              ? {
                  request_body_overrides:
                    parseModelRequestBodyOverrides(
                      model.requestBodyOverrides,
                    ) || {},
                }
              : {}),
          }
        : {}),
    }];
  });
  const chatBody = parseJsonObject(draft.customChatBody);
  const embedBody = parseJsonObject(draft.customEmbedBody);
  const chatQuery = parseStringRecord(draft.customChatQuery);
  const chatHeaders = parseStringRecord(draft.customChatHeaders);
  const embedQuery = parseStringRecord(draft.customEmbedQuery);
  const embedHeaders = parseStringRecord(draft.customEmbedHeaders);
  return {
    ...(draft.providerId ? { provider_id: draft.providerId } : {}),
    runtime_key: runtimeKey,
    display_name: draft.name.trim() || runtimeKey,
    type: draft.protocol,
    base_url: draft.baseUrl.trim(),
    ...(draft.protocol === "azure_openai"
      ? {
          endpoint: draft.baseUrl.trim(),
          api_version: draft.apiVersion.trim(),
          auth: { type: draft.authType },
        }
      : {}),
    ...(draft.protocol === "custom_http"
      ? {
          auth: {
            type: draft.authType,
            ...(draft.authType === "secret_header"
              ? {
                  header_name: draft.authHeaderName.trim(),
                  prefix: draft.authPrefix,
                }
              : {}),
          },
          transports: {
            ...(models.some((model) =>
              model.capabilities.some((capability) =>
                ["chat", "tool", "vision"].includes(capability)))
              ? {
                  chat: {
                    contract: draft.customChatContract,
                    request: {
                      method: "POST",
                      url_template: draft.customChatUrl.trim(),
                      query: chatQuery || {},
                      headers: chatHeaders || {},
                      body_template: chatBody || {},
                    },
                  },
                }
              : {}),
            ...(models.some((model) => model.capabilities.includes("embed"))
              ? {
                  embed: {
                    contract: "openai_embeddings",
                    request: {
                      method: "POST",
                      url_template: draft.customEmbedUrl.trim(),
                      query: embedQuery || {},
                      headers: embedHeaders || {},
                      body_template: embedBody || {},
                    },
                  },
                }
              : {}),
          },
        }
      : {}),
    ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
    models,
    ...(draft.providerOutboundParams ? { outbound_params: draft.providerOutboundParams } : {}),
    ...(draft.providerContextWindow ? { context_window: draft.providerContextWindow } : {}),
    ...(draft.providerMaxOutputTokens ? { max_output_tokens: draft.providerMaxOutputTokens } : {}),
    ...(draft.protocol === "anthropic_compatible"
      ? {
          messages_path: draft.messagesPath.trim() || null,
          anthropic_version: DEFAULT_ANTHROPIC_VERSION,
        }
      : {}),
  };
}

export function v2ProviderDraftFromSavedConfig(
  runtimeKey: string,
  providerId: string,
  spec: ProviderSpec,
): V2ProviderDraft {
  const type = spec.type || "openai_compatible";
  const models = normModels(spec).flatMap((model) => {
    const capabilities = verifiableModelCapabilities(model.caps);
    if (!model.id.trim() || capabilities.length === 0) return [];
    const original = (spec.models || []).find((candidate) =>
      typeof candidate === "string"
        ? candidate === model.id
        : candidate.id === model.id) as ModelObj | string | undefined;
    return [{
      id: model.id,
      capabilities,
      declaration_origin: model.source === "preset"
        ? "discovered_then_declared"
        : "manual",
      ...(model.deployment ? { deployment: model.deployment } : {}),
      ...(type === "openai_compatible" && model.apiSurface === "responses"
        && model.caps.some((capability) => ["chat", "tool", "vision"].includes(capability))
        ? { api_surface: "responses" as const }
        : {}),
      ...(typeof original === "object" && original.capability_transports
        ? { capability_transports: original.capability_transports }
        : {}),
      ...(model.requestBodyOverrides
        ? { request_body_overrides: model.requestBodyOverrides }
        : {}),
      ...(serializeOutboundParams(model.outboundParams) ? { outbound_params: serializeOutboundParams(model.outboundParams) } : {}),
      ...(model.contextWindow ? { context_window: model.contextWindow } : {}),
      ...(model.maxOutputTokens ? { max_output_tokens: model.maxOutputTokens } : {}),
      ...(model.runtimeParameters ? { runtime_parameters: model.runtimeParameters } : {}),
      ...(model.contextPolicy !== undefined ? { context_policy: model.contextPolicy } : {}),
    }];
  });
  const baseUrl = spec.endpoint || spec.base_url || "";
  return {
    provider_id: providerId,
    runtime_key: runtimeKey,
    display_name: spec.display_name || runtimeKey,
    type,
    base_url: baseUrl,
    ...(spec.outbound_params ? { outbound_params: spec.outbound_params } : {}),
    ...(typeof spec.context_window === "number" ? { context_window: spec.context_window } : {}),
    ...(typeof spec.max_output_tokens === "number" ? { max_output_tokens: spec.max_output_tokens } : {}),
    ...(type === "azure_openai"
      ? {
          endpoint: baseUrl,
          api_version: spec.api_version || null,
          auth: spec.auth,
        }
      : {}),
    ...(type === "custom_http"
      ? {
          auth: spec.auth,
          transports: spec.transports,
        }
      : {}),
    models,
    ...(type === "anthropic_compatible"
      ? {
          messages_path: spec.messages_path || null,
          anthropic_version: spec.anthropic_version || null,
        }
      : {}),
  };
}

function mergeVerificationBatch(
  current: VerificationBatch,
  update: VerificationBatch,
): VerificationBatch {
  const items = new Map(current.items.map((item) => [item.item_id, item]));
  for (const item of update.items) items.set(item.item_id, item);
  return {
    ...current,
    ...update,
    items: [...items.values()].sort((left, right) => left.ordinal - right.ordinal),
  };
}

export type CapabilityTestState =
  "off" | "testing" | "unverified" | "verified" | "degraded" | "failed";

export function uniqueCapabilitySteps(steps: VerifyProviderStep[] | undefined): VerifyProviderStep[] {
  const byCapability = new Map<string, VerifyProviderStep>();
  for (const step of steps || []) {
    if (step.step === "capability") {
      byCapability.set(`${step.model || ""}:${step.capability || ""}`, step);
    }
  }
  return [...byCapability.values()];
}

function capabilityStepFailed(step: VerifyProviderStep): boolean {
  return step.status === "failed" || (step.status !== "degraded" && !step.ok);
}

export function modelCapabilityTestState(
  modelId: string,
  capabilities: string[],
  capability: string,
  testing: boolean,
  steps?: VerifyProviderStep[],
): CapabilityTestState {
  if (!capabilities.includes(capability)) return "off";
  const step = uniqueCapabilitySteps(steps).find((item) => item.model === modelId && item.capability === capability);
  if (!step) return testing ? "testing" : "unverified";
  if (step.status === "degraded") return "degraded";
  return step.ok ? "verified" : "failed";
}

export function projectedVerificationSteps(
  entries: LlmVerificationEntry[],
  providerId: string | undefined,
): VerifyProviderStep[] {
  if (!providerId) return [];
  const byCapability = new Map<string, VerifyProviderStep>();
  for (const entry of entries) {
    if (
      entry.provider_id !== providerId
      || !VERIFIABLE_CAPABILITIES.has(entry.capability as ModelCapability)
      || (entry.fact_state !== "verified" && entry.fact_state !== "failed")
    ) {
      continue;
    }
    const verified = entry.fact_state === "verified";
    byCapability.set(`${entry.model_id}:${entry.capability}`, {
      step: "capability",
      ok: verified,
      code: verified ? "ok" : "capability_failed",
      status: verified ? "passed" : "failed",
      model: entry.model_id,
      capability: entry.capability,
    });
  }
  return [...byCapability.values()];
}

function providerSpecForEvidence(spec: ProviderSpec): Record<string, unknown> {
  const {
    api_key: _apiKey,
    secret_key: _secretKey,
    fingerprint: _fingerprint,
    has_secret: _hasSecret,
    auth,
    ...publicSpec
  } = spec;
  void _apiKey;
  void _secretKey;
  void _fingerprint;
  void _hasSecret;
  if (!auth) return publicSpec;
  const {
    secret: _authSecret,
    fingerprint: _authFingerprint,
    secret_configured: _secretConfigured,
    ...publicAuth
  } = auth as Record<string, unknown>;
  void _authSecret;
  void _authFingerprint;
  void _secretConfigured;
  return { ...publicSpec, auth: publicAuth };
}

function providerScopedModels(
  models: Record<string, { provider?: string }>,
  providerKey: string,
): Record<string, { provider?: string }> {
  return Object.fromEntries(
    Object.entries(models).filter(([, model]) => model.provider === providerKey),
  );
}

export function providerDraftMatchesApplied(
  draft: TeamConfig,
  applied: TeamConfig,
  providerKey: string,
): boolean {
  const draftProvider = draft.providers[providerKey];
  const appliedProvider = applied.providers[providerKey];
  if (!draftProvider || !appliedProvider) return false;
  const draftAuth = draftProvider.auth as Record<string, unknown> | undefined;
  const hasNewSecret = [
    draftProvider.api_key,
    draftProvider.secret_key,
    draftAuth?.secret,
  ].some((value) => typeof value === "string" && value.length > 0);
  if (hasNewSecret) return false;
  const scope = (config: TeamConfig, provider: ProviderSpec) => ({
    provider: providerSpecForEvidence(provider),
    kb: providerScopedModels(config.kb.models, providerKey),
    media: providerScopedModels(config.media?.models || {}, providerKey),
    search: providerScopedModels(config.search?.models || {}, providerKey),
  });
  return stableJson(scope(draft, draftProvider))
    === stableJson(scope(applied, appliedProvider));
}

export function uniqueFallbackPairs(
  chain: Array<{ provider?: string; model?: string }>,
  primary?: { provider?: string; model?: string } | null,
): Pair[] {
  const primaryKey = primary?.provider && primary?.model
    ? `${primary.provider}::${primary.model}`
    : null;
  const seen = new Set<string>();
  return chain.filter((pair) => {
    if (!pair.provider || !pair.model) return true;
    const key = `${pair.provider}::${pair.model}`;
    if (key === primaryKey || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function modelCapabilityTestSummary(result: {
  ok: boolean;
  code: VerifyProviderCode;
  steps?: VerifyProviderStep[];
} | undefined): {
  state: "connection_failed" | "connection_only" | "capability_complete" | "capability_degraded" | "capability_failed";
  failed: number;
  degraded: number;
  total: number;
} | null {
  if (!result) return null;
  const capabilities = uniqueCapabilitySteps(result.steps);
  const connection = result.steps?.find((step) => step.step === "connection");
  if ((connection && !connection.ok) || (!connection && !result.ok && !capabilities.length)) {
    return { state: "connection_failed", failed: 0, degraded: 0, total: 0 };
  }
  if (!capabilities.length) return { state: "connection_only", failed: 0, degraded: 0, total: 0 };
  const failed = capabilities.filter(capabilityStepFailed).length;
  const degraded = capabilities.filter((step) => step.status === "degraded").length;
  return {
    state: failed ? "capability_failed" : degraded ? "capability_degraded" : "capability_complete",
    failed,
    degraded,
    total: capabilities.length,
  };
}

/** Persisted shape of a model row (extracted from saveConnect — batch 1-d WP-3 needs it testable).
 *
 * ★`request_body_overrides` **no longer lives inside the custom_http branch**: the editor rendering gate and this persistence gate
 * must open together, or the affordance is false (editable, silently discarded on save). qwen's enable_thinking
 * is precisely an openai_compatible case (design review P1-2).
 */
/** Whether this protocol's router adapter **actually** consumes `request_body_overrides`.
 *
 * `openai_compatible` / `azure_openai` / `custom_http` all construct `OpenAICompatibleAdapter` and pass
 * `models` into `provider_options` (`bowhead_router/registry.py:101-132`); the adapter merges by
 * capability in `openai_compat.py:556-570`. In contrast, `anthropic_compatible` uses
 * `AnthropicCompatibleAdapter`, with **zero override consumption in the entire file**.
 *
 * Enabling the editor indiscriminately in batch 1-d would add a false affordance for Anthropic — editable,
 * persistable, never effective at runtime (implementation review).
 */
/** Copilot fields → backend `LlmProviderDraftDTO` field names.
 *
 * Any misspelling **silently prevents filling** (`field_meta[backendKey]` missing → `admit` returns false,
 * neither error nor value). Call sites must look up key names, never write literals (R3 review P1-2: guards added in r2
 * only locked the constants themselves; replacing a call site with a misspelled literal still passed everything).
 * Backend DTO fields are locked by `test_config_copilot_llm_provider.py::test_generation_schema_field_names`.
 */
export const PROVIDER_DRAFT_BACKEND_KEYS = {
  protocol: "protocol",
  name: "name",
  baseUrl: "base_url",
  apiVersion: "api_version",
  messagesPath: "messages_path",
  auth: "auth_type",
  models: "models",
} as const;

const BODY_OVERRIDE_CONSUMERS: ReadonlySet<string> = new Set(["openai_compatible", "azure_openai", "custom_http"]);

/** Raw-JSON escape hatch (`request_body_overrides`) — only OpenAI-side adapters read it. */
export function providerConsumesBodyOverrides(protocol: string): boolean {
  // Positive allowlist: listing exclusions would default future router adapter types to the Consumes side,
  // precisely the false affordance this batch must eliminate (R3 review P2-3).
  return BODY_OVERRIDE_CONSUMERS.has(protocol);
}

const OUTBOUND_PARAM_CONSUMERS: ReadonlySet<string> = new Set([
  "openai_compatible",
  "azure_openai",
  "custom_http",
  "anthropic_compatible",
]);

/**
 * Declarative outbound parameters (`outbound_params`) — **both adapters read them**; distinct from raw JSON.
 *
 * ★ Found while dogfooding on machine 116: this entire section previously depended on `providerConsumesBodyOverrides`, so
 * Anthropic-compatible sources could neither render it nor preserve declarations via `buildProviderModelSpec` on save —
 * even though `anthropic_compat.py` applies them in `_build_body` (apply_outbound_params
 * family="anthropic"), self-heals on rejection, and backend save validation also accepts the Anthropic family.
 * `deepseek_anthropic`, used for production conversations on 116, uses this protocol: capability shipped, entry closed.
 *
 * Separate the two gates: the raw-JSON gate's rationale still holds (Anthropic adapter indeed ignores overrides).
 */
export function providerConsumesOutboundParams(protocol: string): boolean {
  return OUTBOUND_PARAM_CONSUMERS.has(protocol);
}

/**
 * Why Save is disabled — return the **first** unmet condition, or null when all pass.
 *
 * ★ Origin (user testing): manually adding a model row and filling its ID disabled Save immediately. The condition itself is correct
 * (v2 requires explicitly selecting at least one capability per model; do not automatically add "对话"), but the explanation
 * only said "**发现**模型不会自动标记为对话" — manual-entry users had discovered nothing and
 * felt the message did not apply to them. Worse, an empty name, missing URL, missing Azure version, or invalid source identifier **had
 * no explanation at all**, disabling the primary action and leaving users without guidance.
 *
 * Replace a 20-line saveability boolean with **enumerable reasons**, rather than adding one message:
 * one implementation per condition (`saveEnabled` is now `saveBlockReason(...) === null`),
 * each independently verifiable and directly renderable next to the button.
 *
 * ⚠ Each branch copies the original expression exactly, **preserving every configuration's saveability** — this only explains it.
 */
export type SaveBlockReason =
  | "name_missing"
  | "name_format"
  | "address_missing"
  | "api_version_missing"
  | "no_model"
  | "duplicate_ids"
  | "capability_missing"
  | "outbound_value_missing"
  | "overrides_invalid"
  | "runtime_parameters_invalid"
  | "credit_rates_invalid"
  | "test_required";

export interface SaveGateInput {
  name: string;
  protocol: string;
  mode: "add" | "edit";
  models: Array<{
    id: string;
    caps: string[];
    requestBodyOverrides?: string;
    outboundParams?: OutboundParams;
    runtimeParameters?: RuntimeParameters;
  contextPolicy?: ContextPolicy;
    creditRates?: CreditRates;
    maxOutputTokens?: number;
    parameterError?: string;
  }>;
}

/**
 * Required-field markers.
 *
 * ★ User testing: leaving "来源标识" blank for a new connection disabled Save — the page **said nothing**.
 * The asterisk tells users the field is required before filling it; `saveBlockReason` explains where they are blocked afterward.
 * Both are needed: an asterisk alone does not explain the disabled button; a reason alone requires hitting the block first.
 *
 * Mark **only fields genuinely required by the save gate** — marking without blocking, or blocking without marking, misleads users:
 * do not mark the key (save does not require it; only the "验证计划" does).
 */
function RequiredMark() {
  return <span className="star" aria-hidden="true">*</span>;
}

/**
 * A batch created by single-item verification must be **actively cleared** at a terminal state.
 *
 * ★ P1 found on machine 116: `verificationBatch` also determines whether the verification workflow is open
 * (`verificationWorkflowOpen` → `configurationActionsDisabled`). Full-batch verification has a closable panel;
 * single-item verification has none — without clearing, "编辑配置", "添加连接", and similar actions
 * **stay disabled forever without explanation** after one verification, until page refresh.
 *
 * Claim ownership with batch_id rather than a boolean: the terminal-state effect is shared with full-batch verification;
 * without ownership, it would also close the full batch's results panel before users read it.
 *
 * Extract the condition here because unit tests cannot lock component-internal state.
 */
export function shouldReleaseSingleRowBatch(
  batch: { batch_id: string; state: string } | null | undefined,
  singleRowBatchId: string | null,
): boolean {
  if (!batch || !singleRowBatchId) return false;
  if (batch.batch_id !== singleRowBatchId) return false;
  return TERMINAL_VERIFICATION_BATCH_STATES.has(batch.state);
}

export function saveBlockReason(
  draft: SaveGateInput | null,
  opts: {
    v2Enabled: boolean;
    modelsOptional?: boolean;
    /** Protocols differ (custom_http uses a template URL); callers calculate and supply it. */
    addressValid: boolean;
    protocolFieldsValid: boolean;
    /** v1 only: add mode must first pass connectivity testing, except drafts with only special capabilities. */
    v1TestSatisfied: boolean;
  },
): SaveBlockReason | null {
  if (!draft) return "name_missing";
  const name = draft.name.trim();
  if (!name) return "name_missing";
  if (opts.v2Enabled && !V2_RUNTIME_KEY.test(name)) return "name_format";
  if (!opts.addressValid) return "address_missing";
  if (!opts.protocolFieldsValid) return "api_version_missing";
  if (!opts.modelsOptional && !connectDraftHasModel(draft.models)) return "no_model";
  if (!connectDraftModelIdsUnique(draft.models)) return "duplicate_ids";
  if (
    opts.v2Enabled
    && !draft.models.every((model) =>
      !model.id.trim()
      || isPureSpecialCap(model.caps)
      || model.caps.some((capability) =>
        VERIFIABLE_CAPABILITIES.has(capability as ModelCapability)))
  ) {
    return "capability_missing";
  }
  if (!draft.models.every((model) => outboundParamsValid(model.outboundParams))) {
    return "outbound_value_missing";
  }
  if (!draft.models.every((model) => !model.parameterError && (model.maxOutputTokens === undefined || (Number.isSafeInteger(model.maxOutputTokens) && model.maxOutputTokens > 0)) && runtimeParametersValid(model.runtimeParameters) && contextPolicyValid(model.contextPolicy))) return "runtime_parameters_invalid";
  if (!draft.models.every((model) => creditRatesValid(model.creditRates))) return "credit_rates_invalid";
  if (
    !draft.models.every((model) =>
      modelRequestBodyOverridesValid(model.requestBodyOverrides || "{}", model.caps))
  ) {
    return "overrides_invalid";
  }
  if (!opts.v2Enabled && !opts.v1TestSatisfied) return "test_required";
  return null;
}

export function buildProviderModelSpec(
  protocol: string,
  model: { id: string; caps: string[]; source: "preset" | "manual"; deployment?: string; protocol?: string; apiSurface?: string; requestBodyOverrides?: Record<string, Record<string, unknown>>; outboundParams?: OutboundParams; runtimeParameters?: RuntimeParameters; contextPolicy?: ContextPolicy; creditRates?: CreditRates; contextWindow?: number; maxOutputTokens?: number },
): ModelObj {
  return {
    id: model.id,
    capabilities: model.caps,
    source: model.source,
    ...(model.deployment ? { deployment: model.deployment } : {}),
    ...(protocol === "openai_compatible" && model.apiSurface === "responses"
      && model.caps.some((capability) => ["chat", "tool", "vision"].includes(capability))
      ? { api_surface: "responses" }
      : {}),
    ...(protocol === "custom_http"
      ? {
          capability_transports: Object.fromEntries(
            model.caps.map((capability) => [capability, capability === "embed" ? "embed" : "chat"]),
          ),
        }
      : {}),
    ...(providerConsumesBodyOverrides(protocol) && model.requestBodyOverrides && Object.keys(model.requestBodyOverrides).length
      ? { request_body_overrides: model.requestBodyOverrides }
      : {}),
    // When all follow, serialize returns undefined ⇒ the key is entirely absent from configuration:
    // Not Configured and Configured But All Follow must be equivalent in the evidence signature.
    ...(providerConsumesOutboundParams(protocol) && serializeOutboundParams(model.outboundParams)
      ? { outbound_params: serializeOutboundParams(model.outboundParams) }
      : {}),
    ...(model.runtimeParameters ? { runtime_parameters: model.runtimeParameters } : {}),
      ...(model.contextPolicy !== undefined ? { context_policy: model.contextPolicy } : {}),
    credit_rates: model.creditRates || DEFAULT_CREDIT_RATES,
    ...(model.caps.includes("rerank") ? { protocol: model.protocol || DEFAULT_RERANK_PROTOCOL } : {}),
    ...(model.maxOutputTokens !== undefined ? { max_output_tokens: model.maxOutputTokens } : {}),
    // Pass-through: backend validates a positive integer; this layer only returns whatever it read.
    ...(Number.isInteger(model.contextWindow) && (model.contextWindow as number) > 0
      ? { context_window: model.contextWindow }
      : {}),
  };
}

/** Whether copilot "智能填写" is offered for this connection draft (batch 1-d §1-6).
 *
 * **Add mode only**: an existing provider's runtime_key necessarily collides with itself during editing; the materializer produces
 * a repairable `runtime_key_taken` issue, and the repair round renames the user's provider (design review P1-4).
 */
export function shouldOfferProviderCopilot(
  enabled: boolean,
  hasConfiguredModel: boolean,
  draft: { mode: "add" | "edit" } | null,
): boolean {
  return Boolean(enabled && hasConfiguredModel && draft && draft.mode === "add");
}

export function connectDraftHasModel(models: Array<{ id: string }>): boolean {
  return models.some((model) => Boolean(model.id.trim()));
}

export function connectDraftModelIdsUnique(models: Array<{ id: string }>): boolean {
  const ids = models
    .map((model) => model.id.trim())
    .filter(Boolean);
  return new Set(ids).size === ids.length;
}

export function suggestedCapabilitiesForModelId(modelId: string): string[] {
  const normalized = modelId.trim().toLowerCase();
  if (
    normalized.startsWith("text-embedding-")
    || normalized.includes("/embedding")
    || normalized.includes("-embed-")
  ) {
    return ["embed"];
  }
  return [];
}

export function nextDraftModelCapability(
  model: { caps: string[]; protocol?: string },
  capability: string,
): { caps: string[]; protocol?: string } {
  const enabled = model.caps.includes(capability);
  let caps: string[];
  if (enabled) {
    caps = model.caps.filter((item) => item !== capability);
  } else if (capability === "embed") {
    caps = [capability];
  } else if (capability === IMAGE_CAP) {
    caps = [capability];
  } else if (capability === SEARCH_CAP) {
    caps = [
      ...model.caps.filter((item) => CONVERSATIONAL_CAPS.has(item)),
      capability,
    ];
  } else {
    caps = [
      ...model.caps.filter((item) =>
        item !== IMAGE_CAP
        && (capability !== "rerank" || item !== SEARCH_CAP)
        && item !== "embed"
      ),
      capability,
    ];
  }
  const protocol = caps.includes(IMAGE_CAP)
    ? (MEDIA_PROTOCOLS.includes(model.protocol as never) ? model.protocol : DEFAULT_MEDIA_PROTOCOL)
    : caps.includes(SEARCH_CAP)
      ? (SEARCH_PROTOCOLS.includes(model.protocol as never) ? model.protocol : DEFAULT_SEARCH_PROTOCOL)
      : caps.includes("rerank")
        ? (RERANK_PROTOCOLS.includes(model.protocol as never) ? model.protocol : DEFAULT_RERANK_PROTOCOL)
        : undefined;
  return { caps, protocol };
}

export function regularProviderRows<T extends { caps: string[] }>(
  rows: T[],
): T[] {
  return rows
    .map((row) => ({
      ...row,
      caps: row.caps.filter((capability) => !SPECIAL_CAPS.includes(capability as never)),
    }))
    .filter((row) => row.caps.length > 0);
}

export function hasBillableImageProbe(
  models: Array<{ id: string; caps: string[] }>,
): boolean {
  return models.some((model) => Boolean(model.id.trim()) && model.caps.includes(IMAGE_CAP));
}

export function providerTestAllowsAdd(
  test: { ok: boolean; code: VerifyProviderCode } | undefined,
): boolean {
  return Boolean(test?.ok || test?.code === "capability_degraded");
}

function capsFromSupports(supports: Record<string, boolean> | undefined): string[] {
  const s = supports || {};
  const caps = ["chat"];
  if (s.tool_use || s.tools) caps.push("tool");
  if (s.vision) caps.push("vision");
  if (s.extended_thinking || s.thinking) caps.push("think");
  return caps;
}

function normModels(spec: ProviderSpec): NModel[] {
  const inferred = capsFromSupports(spec.supports);
  const out: NModel[] = [];
  for (const m of spec.models || []) {
    if (typeof m === "string") {
      if (m) out.push({ id: m, caps: [...inferred], source: "preset" });
    } else if (m && m.id) {
      const caps = (m.capabilities || []).filter((c) => CAP_VOCAB.has(c));
      out.push({
        id: String(m.id),
        caps: caps.length ? caps : [...inferred],
        source: m.source === "manual" ? "manual" : "preset",
        protocol: typeof m.protocol === "string" ? m.protocol : undefined,
        deployment: typeof m.deployment === "string"
          ? m.deployment
          : undefined,
        apiSurface: m.api_surface === "responses" ? "responses" : undefined,
        requestBodyOverrides:
          m.request_body_overrides
          && typeof m.request_body_overrides === "object"
            ? m.request_body_overrides
            : undefined,
        outboundParams: parseOutboundParams(m.outbound_params),
        runtimeParameters: m.runtime_parameters,
        contextPolicy: m.context_policy,
        creditRates: m.credit_rates || DEFAULT_CREDIT_RATES,
        maxOutputTokens: m.max_output_tokens,
        contextWindow: Number.isInteger(m.context_window) && (m.context_window as number) > 0
          ? m.context_window
          : undefined,
      });
    }
  }
  return out;
}

function normConfig(raw: Record<string, unknown> | undefined): TeamConfig {
  const c = (raw || {}) as Partial<TeamConfig>;
  const kb = (c.kb || {}) as Partial<KbConfig>;
  const media = (c.media || {}) as Partial<MediaConfig>;
  const search = (c.search || {}) as Partial<SearchConfig>;
  // Read tier-shaped configuration; omit the `agents` section from legacy agents-shaped blobs → the user's first save writes pure tier
  // config (backend is_tier_config recognizes tiers), self-healing into the tier shape.
  return {
    schema_version: 2,
    providers: (c.providers as Record<string, ProviderSpec>) || {},
    tiers: (c.tiers as TiersConfig) || {},
    kb: { models: kb.models || {}, active: kb.active || {}, pending_embed: kb.pending_embed ?? null, index_version: kb.index_version, ...kb },
    media: { models: media.models || {}, active: media.active || {}, ...media },
    search: { models: search.models || {}, active: search.active || {}, ...search },
  };
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, child]) => [key, stableValue(child)]),
    );
  }
  return value;
}

function stableJson(value: unknown): string {
  return JSON.stringify(stableValue(value));
}

function configForWrite(cfg: TeamConfig): TeamConfig {
  const providers: Record<string, ProviderSpec> = {};
  for (const [key, spec] of Object.entries(cfg.providers)) {
    const { fingerprint: _fingerprint, has_secret: _hasSecret, ...writeSpec } = spec;
    void _fingerprint;
    void _hasSecret;
    providers[key] = writeSpec;
  }
  return { ...cfg, providers };
}

function comparableConfig(cfg: TeamConfig): TeamConfig {
  const write = configForWrite(cfg);
  const providers: Record<string, ProviderSpec> = {};
  for (const [key, spec] of Object.entries(write.providers)) {
    const { api_key: _apiKey, secret_key: _secretKey, ...safeSpec } = spec;
    void _apiKey;
    void _secretKey;
    providers[key] = safeSpec;
  }
  const kb = { ...write.kb };
  const pending = kb.pending_embed as { model?: string } | null | undefined;
  if (pending?.model) kb.active = { ...kb.active, embed: pending.model };
  delete kb.pending_embed;
  delete kb.index_version;
  delete kb.last_embed_reindex_completed_at;
  return { ...write, providers, kb };
}

/**
 * The URL a chat request will actually be sent to.
 *
 * Mirrors the adapters: openai-compat posts to `<base_url>/chat/completions`;
 * anthropic-compat posts to `<base_url><messages_path>`, defaulting to
 * `/v1/messages`. Keep in sync with `adapters/openai_compat.py` and
 * `adapters/anthropic_compat.py`.
 */
export function effectiveEndpoint(
  protocol: ProviderType,
  baseUrl: string,
  messagesPath: string,
  modelOrDeployment?: string,
  apiVersion?: string,
  customChatUrl?: string,
): string {
  const root = baseUrl.trim().replace(/\/+$/, "");
  if (protocol === "azure_openai") {
    const deployment = modelOrDeployment
      ? encodeURIComponent(modelOrDeployment)
      : "{deployment}";
    const version = encodeURIComponent(
      apiVersion?.trim() || DEFAULT_AZURE_API_VERSION,
    );
    return `${root}/openai/deployments/${deployment}/chat/completions?api-version=${version}`;
  }
  if (protocol === "custom_http") {
    return (customChatUrl || "{base_url}/chat/completions")
      .replace("{base_url}", root)
      .replace(
        "{deployment}",
        modelOrDeployment ? encodeURIComponent(modelOrDeployment) : "{deployment}",
      )
      .replace(
        "{model}",
        modelOrDeployment ? encodeURIComponent(modelOrDeployment) : "{model}",
      );
  }
  if (protocol !== "anthropic_compatible") return `${root}/chat/completions`;
  const path = messagesPath.trim() || "/v1/messages";
  return `${root}${path.startsWith("/") ? path : `/${path}`}`;
}

export function customHttpBaseUrlSatisfied(
  baseUrl: string,
  urlTemplates: string[],
): boolean {
  return Boolean(baseUrl.trim())
    || urlTemplates.every((template) => !template.includes("{base_url}"));
}

export function providerSignature(
  protocol: ProviderType,
  baseUrl: string,
  messagesPath: string,
  models: Array<{ id: string; caps?: string[]; protocol?: string; apiSurface?: string; outboundParams?: OutboundParams; runtimeParameters?: RuntimeParameters; contextPolicy?: ContextPolicy; requestBodyOverrides?: unknown; contextWindow?: number; maxOutputTokens?: number }>,
  secretToken: string,
  transportOptions?: unknown,
): string {
  return stableJson({
    protocol,
    baseUrl: baseUrl.trim(),
    messagesPath: messagesPath.trim(),
    models: models
      .map((model) => ({
        id: model.id.trim(),
        caps: [...(model.caps || [])].sort(),
        protocol: model.protocol || null,
        // Switching surfaces must invalidate the local "测试通过" badge (review R4: legacy test signatures omit the surface).
        apiSurface: model.apiSurface === "responses" ? "responses" : null,
        outboundParams: model.outboundParams,
        runtimeParameters: model.runtimeParameters,
        contextPolicy: model.contextPolicy,
        requestBodyOverrides: model.requestBodyOverrides,
        contextWindow: model.contextWindow,
        maxOutputTokens: model.maxOutputTokens,
      }))
      .filter((model) => Boolean(model.id)),
    secretToken,
    transportOptions: stableValue(transportOptions),
  });
}

interface DraftIssue {
  path: string;
  message: string;
}

/**
 * Inline issue strip on the edit page — display `validateDraftLocally` results **where users can fix them**.
 *
 * A configuration without a primary model needs an inline hint on the edit page; otherwise users click
 * "审阅并应用" and only on the next page saw "请选择一个仍然存在的主模型". Yet `localIssues` was
 * computed live with `useMemo` (recalculated on config changes), left in memory and only incorporated into the review dialog.
 * Checks already existed but never spoke in the right place — like other cases in this sprint, code knew the answer but stayed silent.
 */
export function DraftIssueList({
  issues,
  prefixes,
}: {
  issues: DraftIssue[];
  /** Show only issues whose paths match these prefixes, keeping each hint beside its corresponding control. */
  prefixes: string[];
}) {
  const mine = issues.filter((issue) => prefixes.some((prefix) => issue.path === prefix || issue.path.startsWith(`${prefix}.`)));
  if (mine.length === 0) return null;
  return (
    <div className="draft-issues" role="alert">
      {mine.map((issue) => (
        <div key={`${issue.path}:${issue.message}`}>
          <AlertTriangle size={14} />
          <span>{issue.message}</span>
        </div>
      ))}
    </div>
  );
}

function validateDraftLocally(
  cfg: TeamConfig,
  t: ReturnType<typeof useTranslations>,
): DraftIssue[] {
  const issues: DraftIssue[] = [];
  const models = new Map<string, Set<string>>();
  for (const [provider, spec] of Object.entries(cfg.providers)) {
    models.set(provider, new Set(normModels(spec).map((model) => model.id)));
  }
  const pairExists = (pair?: Pair): boolean =>
    Boolean(pair?.provider && pair?.model && models.get(pair.provider)?.has(pair.model));
  if (models.size === 0) {
    issues.push({ path: "providers", message: t("v4.invalid_providers") });
  }
  if (!pairExists(cfg.tiers.primary)) {
    issues.push({ path: "tiers.primary", message: t("v4.invalid_primary") });
  }
  const primaryKey = cfg.tiers.primary?.provider && cfg.tiers.primary?.model
    ? `${cfg.tiers.primary.provider}::${cfg.tiers.primary.model}`
    : null;
  const fallbackKeys = new Set<string>();
  for (const [index, pair] of (cfg.tiers.primary_fallback || []).entries()) {
    if (!pairExists(pair)) {
      issues.push({
        path: `tiers.primary_fallback.${index}`,
        message: t("v4.invalid_fallback", { index: index + 1 }),
      });
    }
    const key = pair.provider && pair.model
      ? `${pair.provider}::${pair.model}`
      : null;
    if (key && key === primaryKey) {
      issues.push({
        path: `tiers.primary_fallback.${index}`,
        message: t("v4.primary_in_fallback", { index: index + 1 }),
      });
    } else if (key && fallbackKeys.has(key)) {
      issues.push({
        path: `tiers.primary_fallback.${index}`,
        message: t("v4.duplicate_fallback", { index: index + 1 }),
      });
    }
    if (key) fallbackKeys.add(key);
  }
  for (const [name, ref] of [["fast", cfg.tiers.fast], ["judge", cfg.tiers.judge]] as const) {
    const pair = refPair(ref);
    if (pair && !pairExists(pair)) {
      issues.push({
        path: `tiers.${name}`,
        message: t("v4.invalid_tier_ref", {
          tier: name === "fast" ? t("tier.fast_label") : t("tier.judge_label"),
        }),
      });
    }
  }
  const validateModelSection = (
    section: string,
    sectionModels: Record<string, { provider?: string; real_id?: string }>,
    active: Record<string, string>,
  ) => {
    for (const [name, model] of Object.entries(sectionModels)) {
      if ((section === "media" || section === "search") && !model.real_id?.trim()) issues.push({ path: `${section}.models.${name}.real_id`, message: t("sections.external_model_required", { name }) });
      if (!model.provider || !models.has(model.provider)) {
        issues.push({
          path: `${section}.models.${name}`,
          message: t("v4.invalid_section_provider", {
            section: t(`v4.section_${section}`),
            name,
          }),
        });
      }
    }
    for (const [capability, name] of Object.entries(active)) {
      if (!sectionModels[name]) {
        issues.push({
          path: `${section}.active.${capability}`,
          message: t("v4.invalid_active_ref", {
            section: t(`v4.section_${section}`),
            capability,
            name,
          }),
        });
      }
    }
  };
  validateModelSection("kb", cfg.kb.models, cfg.kb.active);
  validateModelSection("media", cfg.media?.models || {}, cfg.media?.active || {});
  validateModelSection("search", cfg.search?.models || {}, cfg.search?.active || {});
  return issues;
}

// Legacy flag-off path only: preserve its immediate-save behavior by repairing a complete
// v2 config before PUT. v4 deliberately never calls this helper; it surfaces dangling
// references in Review instead of silently changing the draft:
//   - tiers.primary does not resolve → seed from a real model (fresh team: empty tiers → seed; deletion/edit: discard dangling references);
//   - filter out unresolved primary_fallback entries;
//   - broken pinned fast/judge pair → restore follow/auto sentinel (preserve intent);
//   - drop kb.models on deleted providers + kb.active pointing to a vanished model;
//   - strip redacted `fingerprint` from GET config (never write it back).
function sanitizeConfig(cfg: TeamConfig): TeamConfig {
  const providers = cfg.providers || {};
  const providerKeys = new Set(Object.keys(providers));
  const modelsByProvider: Record<string, Set<string>> = {};
  const flat: Array<{ provider: string; id: string; caps: string[] }> = [];
  for (const [k, spec] of Object.entries(providers)) {
    const ms = normModels(spec);
    modelsByProvider[k] = new Set(ms.map((m) => m.id));
    for (const m of ms) flat.push({ provider: k, id: m.id, caps: m.caps });
  }
  const pairOk = (p?: Pair): boolean =>
    Boolean(p?.provider && p?.model && providerKeys.has(p.provider) && modelsByProvider[p.provider]?.has(p.model));
  const pick = flat.find((m) => m.caps.includes("tool")) ?? flat.find((m) => m.caps.includes("chat")) ?? flat[0];
  const fallbackDefault: Pair | null = pick ? { provider: pick.provider, model: pick.id } : null;

  // strip fingerprint from provider specs
  const cleanProviders: Record<string, ProviderSpec> = {};
  for (const [k, spec] of Object.entries(providers)) {
    const { fingerprint: _fp, ...rest } = spec;
    void _fp;
    cleanProviders[k] = rest;
  }

  // ── Tier repair ──
  const srcTiers = cfg.tiers || {};
  const nextTiers: TiersConfig = {};
  const primary: Pair | null = pairOk(srcTiers.primary) ? (srcTiers.primary as Pair) : fallbackDefault;
  if (primary) nextTiers.primary = primary;
  const fb = (srcTiers.primary_fallback || []).filter((f) => pairOk(f));
  if (fb.length) nextTiers.primary_fallback = fb;
  // fast: follow sentinel or pinned pair; broken pair → follow
  if (srcTiers.fast) nextTiers.fast = isFollow(srcTiers.fast) ? srcTiers.fast : (pairOk(refPair(srcTiers.fast) ?? undefined) ? srcTiers.fast : { follow: "primary" });
  // judge: auto sentinel or pinned pair; broken pair → auto
  if (srcTiers.judge) nextTiers.judge = isAuto(srcTiers.judge) ? srcTiers.judge : (pairOk(refPair(srcTiers.judge) ?? undefined) ? srcTiers.judge : { auto: true });

  const kbModels: Record<string, KbModel> = {};
  for (const [name, km] of Object.entries(cfg.kb.models)) {
    if (km.provider && providerKeys.has(km.provider)) kbModels[name] = km;
  }
  const kbActive: Record<string, string> = {};
  for (const [cap, name] of Object.entries(cfg.kb.active)) {
    if (kbModels[name]) kbActive[cap] = name;
  }

  // media (image_gen): like kb — drop models on deleted providers + active pointing to a vanished model.
  const srcMedia = cfg.media || { models: {}, active: {} };
  const mediaModels: Record<string, MediaModel> = {};
  for (const [name, mm] of Object.entries(srcMedia.models || {})) {
    if (mm.provider && providerKeys.has(mm.provider)) mediaModels[name] = mm;
  }
  const mediaActive: Record<string, string> = {};
  for (const [cap, name] of Object.entries(srcMedia.active || {})) {
    if (mediaModels[name]) mediaActive[cap] = name;
  }
  const media: MediaConfig = { ...srcMedia, models: mediaModels, active: mediaActive };

  // search (web_search): like media — drop models on deleted providers + active pointing to a vanished model.
  const srcSearch = cfg.search || { models: {}, active: {} };
  const searchModels: Record<string, SearchModel> = {};
  for (const [name, sm] of Object.entries(srcSearch.models || {})) {
    if (sm.provider && providerKeys.has(sm.provider)) searchModels[name] = sm;
  }
  const searchActive: Record<string, string> = {};
  for (const [cap, name] of Object.entries(srcSearch.active || {})) {
    if (searchModels[name]) searchActive[cap] = name;
  }
  const search: SearchConfig = { ...srcSearch, models: searchModels, active: searchActive };

  return { ...cfg, providers: cleanProviders, tiers: nextTiers, kb: { ...cfg.kb, models: kbModels, active: kbActive }, media, search };
}

export type ModelSettingsSection = "overview" | "connection" | "assignments" | "index" | "external" | "image" | "search";

/** Connection-card actions: "编辑" and "删除" live in the ⋯ menu (appearance batch 4); deletion still asks for confirmation. */
function ConnectionActions({
  editLabel,
  deleteLabel,
  menuLabel,
  disabled,
  disabledReason,
  onEdit,
  onDelete,
}: {
  editLabel: string;
  deleteLabel: string;
  menuLabel: string;
  disabled: boolean;
  /** Disabled reason (always visible on the menu's second line). */
  disabledReason?: string;
  onEdit: () => void;
  onDelete: (trigger: HTMLElement | null) => void;
}) {
  return <RowActionsMenu
    label={menuLabel}
    actions={[{ id: "edit", label: editLabel, disabled, disabledReason: disabled ? disabledReason : undefined, onSelect: onEdit }, {
      id: "delete",
      label: deleteLabel,
      danger: true,
      disabled,
      disabledReason: disabled ? disabledReason : undefined,
      // Selecting a menu item has already returned focus to ⋯; return there after confirmation closes.
      onSelect: () => {
        const trigger = document.activeElement;
        onDelete(trigger instanceof HTMLElement ? trigger : null);
      },
    }]}
  />;
}

export function SettingsModels({ section = "overview", newConnection = false }: {
  section?: ModelSettingsSection;
  newConnection?: boolean;
}) {
  const router = useAppRouter();
  const requestedProposalId = useSearchParams()?.get("configuration_proposal") ?? null;
  const [bowraCallback] = useState(() => {
    const query = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
    return { token: query.get("bowra_import"), error: query.get("bowra_error") };
  });
  const bowraImportRequest = useRef<Promise<BowraImport> | null>(null);
  const bowraImportLoaded = useRef(false);
  const [bowraImported, setBowraImported] = useState(false);
  const loadedProposal = useRef<string | null>(null);
  const draftDirtyRef = useRef(false);
  const [configurationSource, setConfigurationSource] = useState<{ id: string; fingerprint: string } | null>(null);
  const connectionInitializedRef = useRef(false);
  const connectionBaselineRef = useRef<string | null>(null);
  const t = useTranslations("settings.models");
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const confirmLeave = useCallback(() => confirm({
    title: t("v4.leave_title"),
    description: t("v4.leave_confirm"),
    confirmLabel: t("v4.leave_action"),
    cancelLabel: t("v4.leave_keep"),
  }), [confirm, t]);
  const confirmCancelVerification = useCallback(() => confirm({
    title: t("v2.verification_cancel_title"),
    description: t("v2.verification_cancel_confirm"),
    confirmLabel: t("v2.verification_cancel"),
    cancelLabel: t("v2.verification_cancel_keep"),
  }), [confirm, t]);
  const pageRef = useRef<HTMLDivElement>(null);
  const userFacingError = useCallback(
    (cause: unknown) => t(`errors.${modelSettingsErrorKey(cause)}`),
    [t],
  );
  const [data, setData] = useState<AdminLlmConfigResp | null>(null);
  const [pageMode, setPageMode] = useState<"overview" | "edit">(
    section !== "overview" ? "edit" : LLM_SETTINGS_V4_ENABLED ? "overview" : "edit",
  );
  const [overviewTab, setOverviewTab] = useState<"connections" | "assignments" | "external">("connections");
  const overviewReturnPath = section === "index"
    ? "/knowledge/?tab=files"
    : section === "image" || section === "search"
      ? modelExternalCapabilitiesPath()
      : "/team/settings/models/";
  const [baseConfig, setBaseConfig] = useState<TeamConfig>(EMPTY_CONFIG);
  const [config, setConfig] = useState<TeamConfig>(EMPTY_CONFIG);
  const [loading, setLoading] = useState(true);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [navigateAfterConnectionSave, setNavigateAfterConnectionSave] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [connect, setConnect] = useState<ConnectDraft | null>(null);
  const [discoveringModels, setDiscoveringModels] = useState(false);
  const [discovery, setDiscovery] = useState<DiscoveryResult | null>(null);
  const [verificationPlan, setVerificationPlan] = useState<VerificationPlan | null>(null);
  const [verificationBatch, setVerificationBatch] = useState<VerificationBatch | null>(null);
  const [verificationDrafts, setVerificationDrafts] = useState<V2ProviderDraft[] | null>(null);
  const [verificationSelectionOpen, setVerificationSelectionOpen] = useState(false);
  const [verificationBusy, setVerificationBusy] = useState(false);
  /** Runtime auto-adaptation observations. Treat unavailable data as empty — explanatory information must not become an error banner. */
  const [outboundAdaptations, setOutboundAdaptations] = useState<readonly OutboundAdaptation[]>([]);
  const [recoveredBatch, setRecoveredBatch] = useState<VerificationBatch | null>(null);
  const [del, setDel] = useState<string | null>(null);          // provider key pending delete
  // Return focus to the triggering button after deletion confirmation closes (Safari clicks do not focus buttons; opening-time activeElement is unreliable).
  const delReturnFocusRef = useRef<HTMLElement | null>(null);
  const [rebuild, setRebuild] = useState<{ provider: string; id: string } | null>(null);
  const [rebuildDim, setRebuildDim] = useState<string>("");
  const [rebuildOk, setRebuildOk] = useState(false);
  const [retryIndexRequested, setRetryIndexRequested] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [reviewStale, setReviewStale] = useState<"conflict" | "readiness" | null>(null);
  const [validation, setValidation] = useState<LlmConfigValidationResp | null>(null);
  const [applyReindexConfirmed, setApplyReindexConfirmed] = useState(false);
  const [acceptedRiskTokens, setAcceptedRiskTokens] = useState<Set<string>>(new Set());
  const [migrationAccepted, setMigrationAccepted] = useState(false);
  const [overviewValidation, setOverviewValidation] = useState<LlmConfigValidationResp | null>(null);
  const [overviewReadinessLoading, setOverviewReadinessLoading] = useState(false);
  const [overviewReadinessError, setOverviewReadinessError] = useState<string | null>(null);
  const [providerTests, setProviderTests] = useState<Record<string, ProviderTestEvidence>>({});
  const [draftProviderIds, setDraftProviderIds] = useState<Record<string, string>>({});
  const overviewReadinessRequestRef = useRef(0);
  const evidenceRefreshBatchRef = useRef<string | null>(null);
  const recoveryDismissedBatchRef = useRef<string | null>(null);
  const verificationPollingStartedRef = useRef<{ batchId: string; at: number } | null>(null);
  const verificationPollingFailuresRef = useRef(0);
  const recoveredPollingStartedRef = useRef<{ batchId: string; at: number } | null>(null);
  const recoveredPollingFailuresRef = useRef(0);
  const sectionsEnabled = true;
  const scoped = section !== "overview";

  const v2Enabled = Boolean(data?.features?.model_discovery_v2);
  const recoveredBatchFinished = recoveredBatch?.state === "completed";
  const recoveredBatchStopped = Boolean(
    recoveredBatch && ["cancelled", "interrupted"].includes(recoveredBatch.state),
  );
  const verificationBatchRunning = Boolean(
    verificationBatch
      && !TERMINAL_VERIFICATION_BATCH_STATES.has(verificationBatch.state),
  );
  const recoveredBatchRunning = Boolean(
    recoveredBatch
      && !TERMINAL_VERIFICATION_BATCH_STATES.has(recoveredBatch.state),
  );
  const verificationWorkflowOpen = Boolean(
    verificationPlan || verificationBatch || recoveredBatch,
  );
  const recoveredBatchSummary = {
    verified: recoveredBatch?.counts.verified || 0,
    failed: recoveredBatch?.counts.failed || 0,
    notTestable: recoveredBatch?.counts.not_testable || 0,
    unfinished: (recoveredBatch?.counts.errored || 0)
      + (recoveredBatch?.counts.cancelled || 0)
      + (recoveredBatch?.counts.interrupted || 0),
    pending: (recoveredBatch?.counts.queued || 0)
      + (recoveredBatch?.counts.verifying || 0),
  };
  const refreshOverviewReadiness = useCallback(async (
    nextConfig: TeamConfig,
    version: number,
    enabled: boolean,
  ) => {
    const requestId = overviewReadinessRequestRef.current + 1;
    overviewReadinessRequestRef.current = requestId;
    if (!enabled) {
      setOverviewValidation(null);
      setOverviewReadinessError(null);
      setOverviewReadinessLoading(false);
      return;
    }
    setOverviewReadinessLoading(true);
    setOverviewReadinessError(null);
    try {
      const result = await validateAdminLlmConfig(configForWrite(nextConfig), version);
      if (overviewReadinessRequestRef.current === requestId) {
        setOverviewValidation(result);
      }
    } catch (cause) {
      if (overviewReadinessRequestRef.current === requestId) {
        setOverviewValidation(null);
        setOverviewReadinessError(userFacingError(cause));
      }
    } finally {
      if (overviewReadinessRequestRef.current === requestId) {
        setOverviewReadinessLoading(false);
      }
    }
  }, [userFacingError]);
  const rememberDraftProviderId = useCallback(
    (runtimeKey: string, providerId: string) => {
      setDraftProviderIds((current) => (
        current[runtimeKey] === providerId
          ? current
          : { ...current, [runtimeKey]: providerId }
      ));
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void fetchAdminLlmConfig()
      .then((resp) => {
        if (!cancelled) {
          const normalized = normConfig(resp.config);
          setData(resp);
          setBaseConfig(normalized);
          setConfig(normalized);
          setDraftProviderIds(resp.provider_identities || {});
          if (!resp.features?.model_discovery_v2) setPageMode("edit");
          void refreshOverviewReadiness(
            normalized,
            resp.version,
            Boolean(resp.features?.model_discovery_v2),
          );
        }
      })
      .catch((e) => { if (!cancelled) setError(userFacingError(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [loadAttempt, refreshOverviewReadiness, userFacingError]);

  useEffect(() => {
    let cancelled = false;
    void fetchOutboundAdaptations()
      .then((resp) => { if (!cancelled) setOutboundAdaptations(resp.items || []); })
      .catch(() => { /* Normally empty; unavailable data should not disturb users. */ });
    return () => { cancelled = true; };
  }, [loadAttempt]);

  useEffect(() => {
    if (!v2Enabled || recoveredBatch || verificationBatch || (scoped && (newConnection || !["connection", "assignments"].includes(section)))) return;
    let cancelled = false;
    void fetchActiveVerificationBatches()
      .then(async ({ batches }) => {
        const eligible = batches.filter((batch) => batch.batch_id !== recoveryDismissedBatchRef.current);
        if (!eligible.length) return;
        const snapshots = scoped
          ? await Promise.all(eligible.map((batch) => fetchVerificationBatch(batch.batch_id)))
          : [await fetchVerificationBatch(eligible[0].batch_id)];
        const providerId = scoped && section === "connection" ? new URLSearchParams(window.location.search).get("id") : null;
        const snapshot = snapshots.find((batch) => !scoped || batch.items.some((item) =>
          section === "connection" ? item.provider_id === providerId : (overviewValidation?.readiness?.rows || []).some((row) =>
            row.provider_id === item.provider_id && row.model_id === item.model_id && row.capability === item.capability)));
        if (!cancelled && snapshot) setRecoveredBatch(snapshot);
      })
      .catch(() => {
        // Recovery is advisory. The page remains usable if history is temporarily unavailable.
      });
    return () => {
      cancelled = true;
    };
  }, [scoped, section, newConnection, overviewValidation?.readiness?.rows, recoveredBatch, v2Enabled, verificationBatch]);

  useEffect(() => {
    const batch = verificationBatch;
    if (!batch || ["completed", "cancelled", "interrupted"].includes(batch.state)) return;
    if (verificationPollingStartedRef.current?.batchId !== batch.batch_id) {
      verificationPollingStartedRef.current = { batchId: batch.batch_id, at: Date.now() };
      verificationPollingFailuresRef.current = 0;
    }
    let cancelled = false;
    let timer: number | undefined;
    const nextDelay = () => {
      const startedAt = verificationPollingStartedRef.current?.at || Date.now();
      const base = document.hidden
        ? 5_000
        : Date.now() - startedAt < 3_000
          ? 750
          : 2_000;
      return Math.min(
        base * (2 ** verificationPollingFailuresRef.current),
        15_000,
      );
    };
    const poll = async () => {
      try {
        const update = await fetchVerificationBatch(
          batch.batch_id,
          batch.items.length ? batch.revision : undefined,
        );
        if (!cancelled) {
          verificationPollingFailuresRef.current = 0;
          setVerificationBatch((current) =>
            current ? mergeVerificationBatch(current, update) : update);
        }
      } catch (cause) {
        if (!cancelled) {
          verificationPollingFailuresRef.current += 1;
          setError(userFacingError(cause));
          timer = window.setTimeout(() => void poll(), nextDelay());
        }
      }
    };
    timer = window.setTimeout(() => void poll(), nextDelay());
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [verificationBatch, userFacingError]);

  useEffect(() => {
    const batch = verificationBatch;
    if (
      !batch
      || !["completed", "cancelled", "interrupted"].includes(batch.state)
      || evidenceRefreshBatchRef.current === batch.batch_id
    ) return;
    evidenceRefreshBatchRef.current = batch.batch_id;
    // Evidence refreshed with the batch's terminal state; retire this row's local marker (readiness speaks for itself afterward).
    setVerifyingRowKey(null);
    // ★ Single-item verification has no closable panel: without clearing here, `verificationWorkflowOpen` stays true,
    // permanently disabling "编辑配置" and similar actions. Clear **only the batch we created**; leave the full batch's results panel alone.
    if (shouldReleaseSingleRowBatch(batch, singleRowBatchRef.current)) {
      singleRowBatchRef.current = null;
      setVerificationBatch(null);
      setVerificationDrafts(null);
    }
    void fetchAdminLlmConfig()
      .then((response) => {
        if (pageMode !== "overview") {
          setData((current) => current ? { ...current, verification: response.verification } : current);
          void refreshOverviewReadiness(config, response.version, Boolean(response.features?.model_discovery_v2));
          return;
        }
        const normalized = normConfig(response.config);
        setData(response);
        setBaseConfig(normalized);
        setConfig(normalized);
        setDraftProviderIds(response.provider_identities || {});
        void refreshOverviewReadiness(
          normalized,
          response.version,
          Boolean(response.features?.model_discovery_v2),
        );
      })
      .catch(() => {
        // Batch results remain visible; the evidence projection can refresh later.
      });
  }, [config, pageMode, refreshOverviewReadiness, verificationBatch]);

  useEffect(() => {
    const batch = recoveredBatch;
    if (!batch || ["completed", "cancelled", "interrupted"].includes(batch.state)) return;
    if (recoveredPollingStartedRef.current?.batchId !== batch.batch_id) {
      recoveredPollingStartedRef.current = { batchId: batch.batch_id, at: Date.now() };
      recoveredPollingFailuresRef.current = 0;
    }
    let cancelled = false;
    let timer: number | undefined;
    const nextDelay = () => {
      const startedAt = recoveredPollingStartedRef.current?.at || Date.now();
      const base = document.hidden
        ? 5_000
        : Date.now() - startedAt < 3_000
          ? 750
          : 2_000;
      return Math.min(base * (2 ** recoveredPollingFailuresRef.current), 15_000);
    };
    const poll = async () => {
      try {
        const update = await fetchVerificationBatch(batch.batch_id, batch.revision);
        if (!cancelled) {
          recoveredPollingFailuresRef.current = 0;
          setRecoveredBatch((current) =>
            current ? mergeVerificationBatch(current, update) : update);
        }
      } catch {
        if (!cancelled) {
          recoveredPollingFailuresRef.current += 1;
          timer = window.setTimeout(() => void poll(), nextDelay());
        }
      }
    };
    timer = window.setTimeout(() => void poll(), nextDelay());
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [recoveredBatch]);

  useEffect(() => {
    const batch = recoveredBatch;
    if (
      !batch
      || !["completed", "cancelled", "interrupted"].includes(batch.state)
      || evidenceRefreshBatchRef.current === batch.batch_id
    ) return;
    evidenceRefreshBatchRef.current = batch.batch_id;
    void fetchAdminLlmConfig()
      .then((response) => {
        if (pageMode !== "overview") return;
        const normalized = normConfig(response.config);
        setData(response);
        setBaseConfig(normalized);
        setConfig(normalized);
        setDraftProviderIds(response.provider_identities || {});
        void refreshOverviewReadiness(
          normalized,
          response.version,
          Boolean(response.features?.model_discovery_v2),
        );
      })
      .catch(() => {
        // Keep the terminal batch summary; readiness can be refreshed manually.
      });
  }, [pageMode, recoveredBatch, refreshOverviewReadiness]);

  const persist = useCallback(async (next: TeamConfig, confirmReindex = false): Promise<boolean> => {
    if (LLM_SETTINGS_V4_ENABLED) {
      setConfig(next);
      setValidation(null);
      setError(null);
      void confirmReindex;
      return true;
    }
    setBusy(true); setError(null);
    try {
      const resp = await saveAdminLlmConfig(sanitizeConfig(next), confirmReindex, data?.version);
      setData(resp);
      setConfig(normConfig(resp.config));
      return true;
    } catch (e) {
      setError(userFacingError(e));
      return false;
    } finally {
      setBusy(false);
    }
  }, [data?.version, userFacingError]);

  const isDirty = useMemo(
    () => retryIndexRequested || stableJson(configForWrite(config)) !== stableJson(configForWrite(baseConfig)),
    [baseConfig, config, retryIndexRequested],
  );
  const localIssues = useMemo(() => validateDraftLocally(config, t).filter((issue) =>
    !(scoped && section !== "assignments" && !config.tiers.primary?.provider && issue.path === "tiers.primary")),
  [config, scoped, section, t]);
  const connectionDirty = Boolean(connect && connectionBaselineRef.current !== null && connectionBaselineRef.current !== stableJson({ ...connect, test: undefined, testing: false, providerId: undefined }));
  draftDirtyRef.current = isDirty || connectionDirty;
  useEffect(() => {
    if (!requestedProposalId || loading || !data || loadedProposal.current === requestedProposalId) return;
    if (draftDirtyRef.current) {
      setError(t("configuration_proposal_unsaved"));
      return;
    }
    let cancelled = false;
    setBusy(true);
    void fetchModelConfigurationProposal(requestedProposalId).then((proposal) => {
      if (cancelled || draftDirtyRef.current) return;
      if (proposal.version !== data.version) {
        setError(t("configuration_proposal_stale"));
        loadedProposal.current = requestedProposalId;
        return;
      }
      const draft = normConfig(proposal.config);
      loadedProposal.current = requestedProposalId;
      setConfig(draft);
      setConfigurationSource({ id: requestedProposalId, fingerprint: stableJson(configForWrite(draft)) });
      setPageMode("edit");
      setValidation(null);
      setReviewOpen(false);
      setError(null);
    }).catch((error: unknown) => {
      if (!cancelled) {
        loadedProposal.current = requestedProposalId;
        setError(t(modelSettingsErrorKey(error) === "err_config_version_conflict"
          ? "configuration_proposal_stale"
          : "configuration_proposal_unavailable"));
      }
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; setBusy(false); };
  }, [requestedProposalId, loading, data?.version, isDirty, connectionDirty, t]);
  useEffect(() => {
    if (!scoped || section !== "assignments" || loading || !data) return;
    overviewReadinessRequestRef.current += 1;
    setOverviewReadinessLoading(true);
    const timer = window.setTimeout(() => void refreshOverviewReadiness(config, data.version, v2Enabled), 250);
    return () => window.clearTimeout(timer);
  }, [scoped, section, loading, config, data?.version, v2Enabled, refreshOverviewReadiness]);

  useEffect(() => {
    if (section !== "overview" || loading || !data || bowraImportLoaded.current) return;
    if (!bowraCallback.token && !bowraCallback.error) return;
    const clean = new URL(window.location.href);
    if (!clean.pathname.endsWith("/")) clean.pathname += "/";
    clean.searchParams.delete("bowra_import");
    clean.searchParams.delete("bowra_error");
    window.history.replaceState(window.history.state, "", `${clean.pathname}${clean.search}${clean.hash}`);
    if (bowraCallback.error) {
      bowraImportLoaded.current = true;
      const reasons = ["invalid_callback", "unavailable", "permission_changed", "cancelled", "rate_limited", "service_unavailable", "exchange_rejected", "exchange_uncertain", "no_models"];
      setError(t(`bowra.${reasons.includes(bowraCallback.error) ? bowraCallback.error : "exchange_uncertain"}`));
      return;
    }
    // Strict Mode may rerun this effect; share the single credential claim.
    bowraImportRequest.current ??= claimBowraImport(bowraCallback.token!);
    let cancelled = false;
    setBusy(true);
    void bowraImportRequest.current.then((result) => {
      if (cancelled) return;
      bowraImportLoaded.current = true;
      bowraImportRequest.current = null;
      let key = "bowra";
      for (let suffix = 2; config.providers[key]; suffix++) key = `bowra_${suffix}`;
      const draft: TeamConfig = {
        ...config,
        providers: { ...config.providers, [key]: {
          type: "openai_compatible", display_name: "Bowra", base_url: result.base_url, api_key: result.api_key,
          // Intended uses are declarations, never successful verification evidence.
          models: result.models.map((id) => ({ id, capabilities: ["chat", "tool"] })),
        } },
        tiers: !config.tiers.primary?.provider && result.models.length === 1
          ? { ...config.tiers, primary: { provider: key, model: result.models[0] } }
          : config.tiers,
      };
      setConfig(draft);
      setPageMode("edit");
      setBowraImported(true);
      setValidation(null);
      setError(null);
    }).catch(() => {
      if (!cancelled) {
        bowraImportLoaded.current = true;
        setError(t("bowra.import_expired"));
      }
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [section, loading, data, bowraCallback, config, t]);

  const connectBowra = async () => {
    if ((isDirty || connectionDirty) && !(await confirmLeave())) return;
    setBusy(true);
    setError(null);
    try {
      const result = await startBowraConnection();
      window.location.assign(result.authorize_url);
    } catch (cause) {
      const code = cause instanceof ApiError ? cause.code : undefined;
      setError(t(code === "bowra_https_callback_required" ? "bowra.https_required"
        : code === "bowra_encryption_required" ? "bowra.encryption_required"
        : code === "bowra_service_url_invalid" ? "bowra.service_url_invalid" : "bowra.start_failed"));
      setBusy(false);
    }
  };

  const changedSections = useMemo(
    () => ["providers", "tiers", "kb", "media", "search"].filter(
      (section) =>
        (section === "kb" && retryIndexRequested) || stableJson(configForWrite(config)[section])
        !== stableJson(configForWrite(baseConfig)[section]),
    ),
    [baseConfig, config, retryIndexRequested],
  );
  const desiredEmbed = config.kb.active.embed;
  const servingEmbed = baseConfig.kb.active.embed;
  const embedChanged = desiredEmbed !== servingEmbed;
  const reindexRequested = embedChanged || retryIndexRequested;

  useEffect(() => {
    if (!LLM_SETTINGS_V4_ENABLED || loading) return;
    const page = pageRef.current;
    if (!page) return;
    const syncDockGeometry = () => {
      const bounds = page.getBoundingClientRect();
      page.style.setProperty("--mset-dock-left", `${bounds.left}px`);
      page.style.setProperty("--mset-dock-width", `${bounds.width}px`);
    };
    syncDockGeometry();
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(syncDockGeometry);
    observer?.observe(page);
    window.addEventListener("resize", syncDockGeometry);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", syncDockGeometry);
    };
  }, [loading]);

  useEffect(() => {
    if (!LLM_SETTINGS_V4_ENABLED || (!isDirty && !connectionDirty)) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    // In-app confirmation is asynchronous: block this navigation first, then resume it unchanged with proceed() once the user confirms leaving.
    const appGuard = (event: Event) => {
      event.preventDefault();
      const { proceed } = (event as CustomEvent<AppNavigationDetail>).detail;
      void confirmLeave().then((ok) => {
        if (ok) proceed();
      });
    };
    window.addEventListener(BEFORE_APP_NAVIGATION_EVENT, appGuard);
    return () => {
      window.removeEventListener("beforeunload", guard);
      window.removeEventListener(BEFORE_APP_NAVIGATION_EVENT, appGuard);
    };
  }, [isDirty, connectionDirty, confirmLeave]);

  useEffect(() => {
    if (!navigateAfterConnectionSave || isDirty || connectionDirty) return;
    setNavigateAfterConnectionSave(false);
    router.push(overviewReturnPath);
  }, [navigateAfterConnectionSave, isDirty, connectionDirty, router, overviewReturnPath]);

  const discardDraft = useCallback(() => {
    setConfigurationSource(null);
    setBowraImported(false);
    setConfig(baseConfig);
    if (scoped) { connectionInitializedRef.current = false; connectionBaselineRef.current = null; }
    setValidation(null);
    setReviewOpen(false);
    setReviewStale(null);
    setApplyReindexConfirmed(false);
    setRetryIndexRequested(false);
    setConnect(null);
    setDiscovery(null);
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
    setAcceptedRiskTokens(new Set());
    setMigrationAccepted(false);
    setDraftProviderIds(data?.provider_identities || {});
    setDel(null);
    setError(null);
  }, [baseConfig, data?.provider_identities, scoped]);

  const exitEditMode = useCallback(async () => {
    if (isDirty && !(await confirmLeave())) return;
    discardDraft();
    setPageMode(scoped ? "edit" : "overview");
  }, [discardDraft, isDirty, scoped, confirmLeave]);

  const reviewDraft = useCallback(async () => {
    setReviewOpen(true);
    setReviewStale(null);
    setValidation(null);
    setAcceptedRiskTokens(new Set());
    setMigrationAccepted(false);
    setError(null);
    if (localIssues.length > 0) return;
    setBusy(true);
    try {
      const result = await validateAdminLlmConfig(configForWrite(config), data?.version);
      setValidation(result);
    } catch (e) {
      const errorKey = modelSettingsErrorKey(e);
      if (errorKey === "err_config_version_conflict" || errorKey === "err_review_stale") {
        setReviewStale(errorKey === "err_config_version_conflict" ? "conflict" : "readiness");
        setAcceptedRiskTokens(new Set());
        setMigrationAccepted(false);
        setApplyReindexConfirmed(false);
      }
      setError(userFacingError(e));
    } finally {
      setBusy(false);
    }
  }, [config, data?.version, localIssues.length, userFacingError]);

  const applyDraft = useCallback(async () => {
    if (!isDirty || localIssues.length > 0 || (reindexRequested && !applyReindexConfirmed)) return;
    setBusy(true);
    setError(null);
    const outgoing = configForWrite(config);
    const proposalId = configurationSource?.fingerprint === stableJson(outgoing) ? configurationSource.id : undefined;
    try {
      let checked = validation;
      if (!v2Enabled) {
        checked = await validateAdminLlmConfig(outgoing, data?.version);
        setValidation(checked);
      }
      if (!checked?.valid || checked.issues.some((issue) => issue.severity === "error")) return;
      const readiness = v2Enabled && checked.readiness
        ? {
            draft_semantic_hash: checked.readiness.draft_semantic_hash,
            acceptance_tokens: [...acceptedRiskTokens],
            migration_acceptance_token: migrationAccepted
              ? checked.readiness.migration_acceptance?.acceptance_token || null
              : null,
          }
        : undefined;
      const resp = await saveAdminLlmConfig(
        outgoing,
        reindexRequested,
        data?.version,
        readiness,
        proposalId,
      );
      if (proposalId) void refreshLiteState(proposalId);
      const normalized = normConfig(resp.config);
      setData(resp);
      setConfigurationSource(null);
      setBaseConfig(normalized);
      setConfig(normalized);
      setBowraImported(false);
      setReviewOpen(false);
      setReviewStale(null);
      setApplyReindexConfirmed(false);
      setRetryIndexRequested(false);
      setAcceptedRiskTokens(new Set());
      setMigrationAccepted(false);
      setProviderTests({});
      if (scoped && section === "connection") {
        connectionBaselineRef.current = null;
        connectionInitializedRef.current = true;
        setConnect(null);
        setNavigateAfterConnectionSave(true);
      }
      setDraftProviderIds(resp.provider_identities || {});
      setPageMode(scoped && section !== "connection" ? "edit" : "overview");
      void refreshOverviewReadiness(
        normalized,
        resp.version,
        Boolean(resp.features?.model_discovery_v2),
      );
    } catch (e) {
      // A timeout after PUT is result-unknown. Re-read once and accept only an
      // authoritative state that semantically matches this draft.
      try {
        const latest = await fetchAdminLlmConfig();
        const latestConfig = normConfig(latest.config);
        if (
          latest.version !== data?.version
          && stableJson(comparableConfig(latestConfig)) === stableJson(comparableConfig(config))
          && (!retryIndexRequested || latestConfig.kb.pending_embed?.task_id !== baseConfig.kb.pending_embed?.task_id)
        ) {
          if (proposalId) void refreshLiteState(proposalId);
          setConfigurationSource(null);
          setData(latest);
          setBaseConfig(latestConfig);
          setConfig(latestConfig);
          setReviewOpen(false);
          setReviewStale(null);
          setApplyReindexConfirmed(false);
          setRetryIndexRequested(false);
          setAcceptedRiskTokens(new Set());
          setMigrationAccepted(false);
          setProviderTests({});
          setDraftProviderIds(latest.provider_identities || {});
          setPageMode(scoped ? "edit" : "overview");
          if (scoped && section === "connection") {
            connectionBaselineRef.current = null;
            setConnect(null);
            setNavigateAfterConnectionSave(true);
          }
          void refreshOverviewReadiness(
            latestConfig,
            latest.version,
            Boolean(latest.features?.model_discovery_v2),
          );
          return;
        }
      } catch {
        // Keep the original error and the local draft for explicit recovery.
      }
      const errorKey = modelSettingsErrorKey(e);
      if (errorKey === "err_config_version_conflict" || errorKey === "err_review_stale") {
        setReviewStale(errorKey === "err_config_version_conflict" ? "conflict" : "readiness");
        setAcceptedRiskTokens(new Set());
        setMigrationAccepted(false);
        setApplyReindexConfirmed(false);
      }
      setError(userFacingError(e));
    } finally {
      setBusy(false);
    }
  }, [
    scoped,
    section,
    retryIndexRequested,
    baseConfig.kb.pending_embed?.task_id,
    applyReindexConfirmed,
    config,
    configurationSource,
    data?.version,
    reindexRequested,
    isDirty,
    localIssues.length,
    acceptedRiskTokens,
    migrationAccepted,
    validation,
    v2Enabled,
    userFacingError,
    refreshOverviewReadiness,
  ]);

  const recheckStaleReview = useCallback(async () => {
    setBusy(true);
    setError(null);
    setValidation(null);
    setAcceptedRiskTokens(new Set());
    setMigrationAccepted(false);
    setApplyReindexConfirmed(false);
    try {
      const latest = await fetchAdminLlmConfig();
      const latestConfig = normConfig(latest.config);
      setData(latest);
      setBaseConfig(latestConfig);
      setDraftProviderIds((current) => ({
        ...(latest.provider_identities || {}),
        ...current,
      }));
      const checked = await validateAdminLlmConfig(
        configForWrite(config),
        latest.version,
      );
      setValidation(checked);
      setReviewStale(null);
    } catch (cause) {
      const errorKey = modelSettingsErrorKey(cause);
      if (errorKey === "err_config_version_conflict" || errorKey === "err_review_stale") {
        setReviewStale(errorKey === "err_config_version_conflict" ? "conflict" : "readiness");
      }
      setError(userFacingError(cause));
    } finally {
      setBusy(false);
    }
  }, [config, userFacingError]);

  // ─── derived ────────────────────────────────────────────────────
  const providerEntries = useMemo(() => Object.entries(config.providers), [config.providers]);
  const fingerprints = useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of data?.providers || []) if (p.fingerprint) m[p.provider_name] = p.fingerprint;
    return m;
  }, [data]);

  const flatModels = useMemo<FlatModel[]>(() => {
    const out: FlatModel[] = [];
    for (const [key, spec] of providerEntries) {
      const ptype = (spec.type || "openai_compatible") as ProviderType;
      for (const m of normModels(spec)) {
        out.push({
          providerKey: key,
          providerType: ptype,
          id: m.id,
          caps: m.caps,
          protocol: m.protocol,
        });
      }
    }
    return out;
  }, [providerEntries]);

  const toolModels = useMemo(() => flatModels.filter((m) => m.caps.includes("tool")), [flatModels]);

  // Candidate vector/rerank models = provider models tagged embed/rerank, UNION the models
  // already registered in kb.models (embedding models are often carried there with a real_id
  // rather than listed as a provider chat model — so the currently-active one still shows).
  const kbCandidates = useCallback((cap: "embed" | "rerank" | "vision" | "row_extractor"): FlatModel[] => {
    // row_extractor is a chat-completion task; models lack a "row_extractor" capability bit → use tool-capable chat models as candidates.
    const modelCap = cap === "row_extractor" ? "tool" : cap;
    const map = new Map<string, FlatModel>();
    for (const m of flatModels) {
      const declarativeKbCapabilitySupported = (
        !["azure_openai", "custom_http"].includes(m.providerType)
        || !["rerank", "vision", "row_extractor"].includes(cap)
      );
      if (m.caps.includes(modelCap) && declarativeKbCapabilitySupported) {
        map.set(`${m.providerKey}::${m.id}`, m);
      }
    }
    for (const km of Object.values(config.kb.models)) {
      const prov = km.provider;
      if (km.capability === cap && prov && km.real_id && config.providers[prov]) {
        const providerType = config.providers[prov].type || "openai_compatible";
        if (
          ["azure_openai", "custom_http"].includes(providerType)
          && ["rerank", "vision", "row_extractor"].includes(cap)
        ) {
          continue;
        }
        const key = `${prov}::${km.real_id}`;
        if (!map.has(key)) {
          map.set(key, {
            providerKey: prov,
            providerType: (config.providers[prov].type || "openai_compatible") as ProviderType,
            id: km.real_id,
            caps: [cap],
            protocol: typeof km.protocol === "string" ? km.protocol : undefined,
          });
        }
      }
    }
    return Array.from(map.values());
  }, [flatModels, config.kb.models, config.providers]);
  const embedModels = useMemo(() => kbCandidates("embed"), [kbCandidates]);
  const rerankModels = useMemo(() => kbCandidates("rerank"), [kbCandidates]);
  const visionModels = useMemo(() => kbCandidates("vision"), [kbCandidates]);
  const rowExtractorModels = useMemo(() => kbCandidates("row_extractor"), [kbCandidates]);

  // ─── ② Derived tiers (primary / fallback / lightweight / judge) ───────────
  const chatModels = useMemo(() => flatModels.filter((m) => m.caps.includes("chat")), [flatModels]);
  const tiers = config.tiers || {};
  const primaryModel = tiers.primary?.provider && tiers.primary?.model
    ? { provider: tiers.primary.provider, model: tiers.primary.model } : null;
  const primaryOptionValue = primaryModel && toolModels.some((model) =>
    model.providerKey === primaryModel.provider && model.id === primaryModel.model)
    ? `${primaryModel.provider}::${primaryModel.model}`
    : "";
  const fallbackChain = useMemo<Pair[]>(
    () => (tiers.primary_fallback || []).filter((f) => f.provider && f.model),
    [tiers.primary_fallback],
  );
  const fastPinned = refPair(tiers.fast);
  const judgePinned = refPair(tiers.judge);
  // Judge "Auto" mirrors the backend `_pick_heterogeneous`: a chat model other than the main model, preferring
  // another provider, otherwise another model at the main model's provider. Null means the main model is the
  // team's only chat model, so evaluation falls back to it; the page says so instead of showing "—".
  const judgeAuto = useMemo<Pair | null>(
    () => autoJudgeModel(chatModels, primaryModel),
    [chatModels, primaryModel?.provider, primaryModel?.model],
  );
  const evalSameAsPrimary = Boolean(judgePinned && primaryModel && judgePinned.provider === primaryModel.provider && judgePinned.model === primaryModel.model);
  const [advOpen, setAdvOpen] = useState(false);

  const activeEmbedName = config.kb.active.embed;
  const activeEmbed = activeEmbedName ? config.kb.models[activeEmbedName] : undefined;
  const activeRerankName = config.kb.active.rerank;
  const activeRerank = activeRerankName ? config.kb.models[activeRerankName] : undefined;
  const activeVisionName = config.kb.active.vision;
  const activeVision = activeVisionName ? config.kb.models[activeVisionName] : undefined;
  const activeRowName = config.kb.active.row_extractor;
  const activeRow = activeRowName ? config.kb.models[activeRowName] : undefined;
  const responseReindex = data?.reindex as { state?: string } | null | undefined;
  const reindexFailed = config.kb.pending_embed?.state === "failed"
    || responseReindex?.state === "failed";
  const terminalReindexStates = new Set([
    "failed",
    "succeeded",
    "superseded",
    "cancelled",
  ]);
  const reindexing = Boolean(
    config.kb.pending_embed
      && !terminalReindexStates.has(config.kb.pending_embed.state || ""),
  ) || Boolean(
    responseReindex
      && !terminalReindexStates.has(responseReindex.state || ""),
  );

  useEffect(() => {
    if (!sectionsEnabled || !reindexing || section !== "index") return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void fetchAdminLlmConfig().then((response) => {
        if (cancelled) return;
        const normalized = normConfig(response.config);
        setData(response);
        setBaseConfig(normalized);
        setConfig(normalized);
        void refreshOverviewReadiness(normalized, response.version, Boolean(response.features?.model_discovery_v2));
      }).catch((cause) => { if (!cancelled) setError(userFacingError(cause)); });
    }, 3_000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [sectionsEnabled, section, reindexing, data, refreshOverviewReadiness, userFacingError]);

  // ─── ④ Derived media (image_gen) ───────────────────────────────────
  const mediaModelList = useMemo<Array<{ name: string; provider: string; real_id: string; protocol: string }>>(() => {
    const out: Array<{ name: string; provider: string; real_id: string; protocol: string }> = [];
    for (const [name, mm] of Object.entries(config.media?.models || {})) {
      if (mm.provider && mm.real_id && config.providers[mm.provider]) {
        out.push({ name, provider: mm.provider, real_id: mm.real_id, protocol: mm.protocol || DEFAULT_MEDIA_PROTOCOL });
      }
    }
    return out;
  }, [config.media?.models, config.providers]);
  const activeImageName = config.media?.active.image_gen;
  const activeImage = activeImageName ? config.media?.models[activeImageName] : undefined;
  // Image models for a provider (① card's read-only view + deletion guard).
  const imageModelsByProvider = useCallback(
    (key: string) => mediaModelList.filter((m) => m.provider === key),
    [mediaModelList],
  );

  // ─── ⑤ Derived search (web_search) ─────────────────────────────────
  const searchModelList = useMemo<Array<{ name: string; provider: string; real_id: string; protocol: string }>>(() => {
    const out: Array<{ name: string; provider: string; real_id: string; protocol: string }> = [];
    for (const [name, sm] of Object.entries(config.search?.models || {})) {
      if (sm.provider && sm.real_id && config.providers[sm.provider]) {
        out.push({ name, provider: sm.provider, real_id: sm.real_id, protocol: sm.protocol || DEFAULT_SEARCH_PROTOCOL });
      }
    }
    return out;
  }, [config.search?.models, config.providers]);
  const activeSearchName = config.search?.active.web_search;
  const activeSearch = activeSearchName ? config.search?.models[activeSearchName] : undefined;
  const searchModelsByProvider = useCallback(
    (key: string) => searchModelList.filter((m) => m.provider === key),
    [searchModelList],
  );
  const overviewProviders = useMemo<ModelSettingsOverviewProvider[]>(() => (
    Object.entries(baseConfig.providers).map(([runtimeKey, spec]) => {
      const mediaCount = Object.values(baseConfig.media?.models || {})
        .filter((model) => model.provider === runtimeKey).length;
      const searchCount = Object.values(baseConfig.search?.models || {})
        .filter((model) => model.provider === runtimeKey).length;
      return {
        runtimeKey,
        displayName: spec.display_name || runtimeKey,
        type: spec.type || "openai_compatible",
        endpoint: spec.endpoint
          || spec.base_url
          || spec.transports?.chat?.request?.url_template
          || spec.transports?.embed?.request?.url_template
          || "",
        modelCount: normModels(spec).length + mediaCount + searchCount,
        fingerprint: fingerprints[runtimeKey] || spec.fingerprint || null,
        authType: spec.auth?.type,
      };
    })
  ), [baseConfig, fingerprints]);
  const verificationCandidates = useMemo<VerificationCandidate[]>(() => {
    const readinessRows = overviewValidation?.readiness?.rows || [];
    const candidates = Object.entries(baseConfig.providers).flatMap(([runtimeKey, spec]) => {
      const providerId = draftProviderIds[runtimeKey];
      if (!providerId) return [];
      return normModels(spec).flatMap((model) =>
        verifiableModelCapabilities(model.caps).map((capability) => {
          const matchingRows = readinessRows.filter((row) =>
            row.provider_id === providerId
            && row.model_id === model.id
            && row.capability === capability);
          const rawFactState = (data?.verification?.entries || []).find((entry) => entry.provider_id === providerId && entry.model_id === model.id && entry.capability === capability)?.fact_state || matchingRows[0]?.fact_state || "unknown";
          const factState = [
            "verified",
            "aged",
            "stale",
            "failed",
            "not_testable",
            "unknown",
          ].includes(rawFactState)
            ? rawFactState
            : "unknown";
          return {
            providerId,
            providerName: runtimeKey,
            modelId: model.id,
            capability,
            factState,
            referenced: matchingRows.length > 0,
            recommended: matchingRows.length > 0 && factState !== "verified",
          };
        }));
    });
    return candidates.filter((candidate) => !sectionsEnabled || candidate.referenced).sort((left, right) =>
      Number(right.recommended) - Number(left.recommended)
      || left.providerName.localeCompare(right.providerName)
      || left.modelId.localeCompare(right.modelId)
      || left.capability.localeCompare(right.capability));
  }, [baseConfig.providers, data?.verification?.entries, sectionsEnabled, draftProviderIds, overviewValidation?.readiness?.rows]);

  const capLabel = useCallback((c: string) => t(`caps.${c}`), [t]);

  // ─── ② Tier writers (write primary/fast/judge tiers; no longer flatten into every agent binding) ───────────
  const writeTiers = useCallback(
    (cfg: TeamConfig, patch: Partial<TiersConfig>): TeamConfig => ({ ...cfg, tiers: { ...cfg.tiers, ...patch } }),
    [],
  );
  const onPickPrimary = useCallback((value: string) => {
    const [provider, ...rest] = value.split("::");
    const model = rest.join("::");
    if (!provider || !model) return;
    const primary = { provider, model };
    void persist(writeTiers(config, {
      primary,
      primary_fallback: uniqueFallbackPairs(
        config.tiers.primary_fallback || [],
        primary,
      ),
    }));
  }, [config, persist, writeTiers]);
  const setFallback = useCallback((chain: Pair[]) => {
    if (!primaryModel) return;
    void persist(writeTiers(config, {
      primary_fallback: uniqueFallbackPairs(chain, primaryModel),
    }));
  }, [config, primaryModel, persist, writeTiers]);
  const onPickFast = useCallback((value: string) => {
    let ref: TierRef;
    if (value === FOLLOW_VALUE) ref = { follow: "primary" };
    else { const [provider, ...rest] = value.split("::"); ref = { provider, model: rest.join("::") }; }
    void persist(writeTiers(config, { fast: ref }));
  }, [config, persist, writeTiers]);
  const onPickJudge = useCallback((value: string) => {
    let ref: TierRef;
    if (value === AUTO_VALUE) ref = { auto: true };
    else { const [provider, ...rest] = value.split("::"); ref = { provider, model: rest.join("::") }; }
    void persist(writeTiers(config, { judge: ref }));
  }, [config, persist, writeTiers]);

  // ─── delete provider ─────────────────────────────────────────────
  const delInfo = useMemo(() => {
    if (!del) return null;
    const hardBlock = !LLM_SETTINGS_V4_ENABLED && Boolean(activeEmbed && activeEmbed.provider === del);
    const refs: string[] = [];
    const tr = config.tiers || {};
    if (tr.primary?.provider === del) refs.push(`${t("tier.primary_label")} · ${tr.primary.model}`);
    (tr.primary_fallback || []).forEach((f, i) => { if (f.provider === del) refs.push(`${t("tier.fallback_ref")}[${i + 1}] · ${f.model}`); });
    const fp = refPair(tr.fast);
    if (fp?.provider === del) refs.push(`${t("tier.fast_label")} · ${fp.model}`);
    const jp = refPair(tr.judge);
    if (jp?.provider === del) refs.push(`${t("tier.judge_label")} · ${jp.model}`);
    // ④ Image-generation references (soft block, like tier references; clearing returns to disabled).
    if (activeImage?.provider === del) refs.push(`${t("media.del_ref")} · ${activeImage.real_id}`);
    // ⑤ Web-search references (soft block, same as above).
    if (activeSearch?.provider === del) refs.push(`${t("search.del_ref")} · ${activeSearch.real_id}`);
    if (scoped) {
      for (const sectionModels of [config.kb.models, config.media?.models || {}, config.search?.models || {}]) {
        for (const [name, model] of Object.entries(sectionModels)) {
          if (model.provider === del) refs.push(name);
        }
      }
    }
    return { hardBlock: hardBlock || (scoped && refs.length > 0), refs };
  }, [scoped, del, activeEmbed, activeImage, activeSearch, config.kb.models, config.media?.models, config.search?.models, config.tiers, t]);

  const confirmDelete = useCallback(() => {
    if (!del || !delInfo || delInfo.hardBlock) return;
    const providers = { ...config.providers };
    delete providers[del];
    void persist({ ...config, providers }).then((ok) => { if (ok) { setDel(null); if (scoped) setConnect(null); if (expanded === del) setExpanded(null); } });
  }, [scoped, del, delInfo, config, persist, expanded]);

  // ─── ③ KB ────────────────────────────────────────────────────────
  const openRebuild = useCallback((provider: string, id: string) => {
    const existingName = `emb__${provider}__${id}`;
    const existing = config.kb.models[existingName];
    setRebuild({ provider, id });
    setRebuildDim(existing?.dim ? String(existing.dim) : "");
    setRebuildOk(false);
  }, [config.kb.models]);

  const confirmRebuild = useCallback(() => {
    if (!rebuild) return;
    const dim = parseInt(rebuildDim, 10);
    if (!Number.isFinite(dim) || dim <= 0) { setError(t("kb.dim_required")); return; }
    const name = `emb__${rebuild.provider}__${rebuild.id}`;
    const kb: KbConfig = { ...config.kb, models: { ...config.kb.models }, active: { ...config.kb.active } };
    kb.models[name] = { provider: rebuild.provider, real_id: rebuild.id, capability: "embed", dim };
    kb.active = { ...kb.active, embed: name };
    void persist({ ...config, kb }, true).then((ok) => { if (ok) { setRebuild(null); setRebuildOk(false); } });
  }, [rebuild, rebuildDim, config, persist, t]);

  const onPickRerank = useCallback((value: string) => {
    const kb: KbConfig = { ...config.kb, models: { ...config.kb.models }, active: { ...config.kb.active } };
    if (!value) {
      delete kb.active.rerank;
    } else {
      const [provider, ...rest] = value.split("::");
      const id = rest.join("::");
      const name = `rr__${provider}__${id}`;
      const candidate = rerankModels.find(
        (model) => model.providerKey === provider && model.id === id,
      );
      kb.models[name] = {
        provider,
        real_id: id,
        capability: "rerank",
        protocol: candidate?.protocol || DEFAULT_RERANK_PROTOCOL,
      };
      kb.active = { ...kb.active, rerank: name };
    }
    void persist({ ...config, kb });
  }, [config, persist, rerankModels]);

  // P1.5: optional KB capabilities (vision document-image recognition / row_extractor row extraction) — like rerank, do not trigger index rebuilding.
  const onPickKbOptional = useCallback((cap: "vision" | "row_extractor", prefix: string, value: string) => {
    const kb: KbConfig = { ...config.kb, models: { ...config.kb.models }, active: { ...config.kb.active } };
    if (!value) {
      delete kb.active[cap];
    } else {
      const [provider, ...rest] = value.split("::");
      const id = rest.join("::");
      const name = `${prefix}__${provider}__${id}`;
      kb.models[name] = { provider, real_id: id, capability: cap };
      kb.active = { ...kb.active, [cap]: name };
    }
    void persist({ ...config, kb });
  }, [config, persist]);

  // ④: select active image_gen (value = logical name in media.models; empty = disabled). Image models themselves
  // are added/deleted in the ① connection panel (select "图像生成" + call protocol) — this only switches the active model.
  const onPickImage = useCallback((name: string) => {
    const media: MediaConfig = { ...(config.media || { models: {}, active: {} }), models: { ...(config.media?.models || {}) }, active: { ...(config.media?.active || {}) } };
    if (!name) delete media.active.image_gen;
    else if (media.models[name]) media.active.image_gen = name;
    void persist({ ...config, media });
  }, [config, persist]);

  // ⑤: select active web_search (value = logical name in search.models; empty = disabled). Search models themselves
  // are added/deleted in the ① connection panel (select "联网搜索" + call protocol) — this only switches the active model.
  const onPickSearch = useCallback((name: string) => {
    const search: SearchConfig = { ...(config.search || { models: {}, active: {} }), models: { ...(config.search?.models || {}) }, active: { ...(config.search?.active || {}) } };
    if (!name) delete search.active.web_search;
    else if (search.models[name]) search.active.web_search = name;
    void persist({ ...config, search });
  }, [config, persist]);

  // ─── connect panel ───────────────────────────────────────────────
  const openAdd = useCallback(() => {
    if (verificationWorkflowOpen) return;
    connectionBaselineRef.current = null;
    setPageMode("edit");
    setConnect({
      mode: "add",
      protocol: "openai_compatible",
      name: "",
      baseUrl: "",
      apiKey: "",
      messagesPath: "",
      apiVersion: DEFAULT_AZURE_API_VERSION,
      authType: "bearer",
      authHeaderName: "Authorization",
      authPrefix: "Bearer ",
      customChatUrl: "{base_url}/chat/completions",
      customEmbedUrl: "{base_url}/embeddings",
      customChatContract: "openai_chat_completions_sse",
      customChatQuery: "{}",
      customChatHeaders: "{}",
      customChatBody: DEFAULT_CUSTOM_CHAT_BODY,
      customEmbedQuery: "{}",
      customEmbedHeaders: "{}",
      customEmbedBody: DEFAULT_CUSTOM_EMBED_BODY,
      models: [],
      testing: false,
    });
    setDiscovery(null);
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
  }, [verificationWorkflowOpen]);

  const openEdit = useCallback((key: string) => {
    if (verificationWorkflowOpen) return;
    connectionBaselineRef.current = null;
    setPageMode("edit");
    const spec = config.providers[key];
    const rerankProtocols = new Map(
      Object.values(config.kb.models)
        .filter((model) => model.provider === key && model.capability === "rerank" && model.real_id)
        .map((model) => [model.real_id as string, model.protocol]),
    );
    const providerRows: DraftModel[] = normModels(spec).map((m) => ({
      id: m.id,
      caps: m.caps,
      source: m.source,
      deployment: m.deployment,
      apiSurface: m.apiSurface,
      requestBodyOverrides: JSON.stringify(
        m.requestBodyOverrides || {},
        null,
        2,
      ),
      outboundParams: m.outboundParams || {},
      runtimeParameters: m.runtimeParameters,
      contextPolicy: m.contextPolicy,
      creditRates: m.creditRates,
      contextWindow: m.contextWindow,
      maxOutputTokens: m.maxOutputTokens,
      protocol: m.caps.includes("rerank")
        ? m.protocol || rerankProtocols.get(m.id) || DEFAULT_RERANK_PROTOCOL
        : m.protocol,
    }));
    const providerModelIds = new Set(providerRows.map((model) => model.id));
    // Older configs may keep rerank models only in kb.models. Rehydrate them here so their
    // protocol is editable/testable and the next save carries the explicit protocol forward.
    const kbRerankRows: DraftModel[] = Array.from(rerankProtocols.entries())
      .filter(([id]) => !providerModelIds.has(id))
      .map(([id, protocol]) => ({
        id,
        caps: ["rerank"],
        source: "manual",
        protocol: protocol || DEFAULT_RERANK_PROTOCOL,
      }));
    // Image models live in config.media.models, outside provider.models — rebuild rows with image_gen capability
    // and protocol for editing, then split back into the media section on save.
    const imageRows: DraftModel[] = scoped ? [] : imageModelsByProvider(key).map((m) => ({
      id: m.real_id, caps: [IMAGE_CAP], source: "manual", protocol: m.protocol,
    }));
    const searchRows: DraftModel[] = scoped ? [] : searchModelsByProvider(key).map((m) => ({
      id: m.real_id, caps: [SEARCH_CAP], source: "manual", protocol: m.protocol,
    }));
    const providerId = draftProviderIds[key];
    const projectedSteps = v2Enabled
      && providerDraftMatchesApplied(config, baseConfig, key)
      ? projectedVerificationSteps(
          data?.verification?.entries || [],
          providerId,
        )
      : [];
    const allRows = [
      ...providerRows,
      ...kbRerankRows,
      ...imageRows,
      ...searchRows,
    ];
    const projectedTest = projectedSteps.length > 0
      ? {
          ok: projectedSteps.every((step) => step.ok),
          code: projectedSteps.every((step) => step.ok)
            ? "ok" as const
            : "capability_failed" as const,
          steps: projectedSteps,
          signature: providerSignature(
            (spec.type || "openai_compatible") as ProviderType,
            spec.endpoint || spec.base_url || "",
            String(spec.messages_path || ""),
            allRows,
            String(spec.api_key || fingerprints[key] || spec.fingerprint || "saved-secret"),
            {
              endpoint: spec.endpoint,
              apiVersion: spec.api_version,
              auth: spec.auth,
              transports: spec.transports,
            },
          ),
        }
      : undefined;
    setConnect({
      mode: "edit", origKey: key,
      providerId,
      protocol: (spec.type || "openai_compatible") as ProviderType,
      name: key,
      baseUrl: spec.endpoint || spec.base_url || "",
      apiKey: draftApiKeyForEdit(spec, Boolean(baseConfig.providers[key])),
      messagesPath: (spec.messages_path as string) || "",
      apiVersion: spec.api_version || DEFAULT_AZURE_API_VERSION,
      providerOutboundParams: parseOutboundParams(spec.outbound_params),
      providerContextWindow: typeof spec.context_window === "number" ? spec.context_window : undefined,
      providerMaxOutputTokens: spec.max_output_tokens,
      authType: spec.auth?.type || "bearer",
      authHeaderName: spec.auth?.header_name || "Authorization",
      authPrefix: spec.auth?.prefix ?? "Bearer ",
      customChatUrl: spec.transports?.chat?.request?.url_template
        || "{base_url}/chat/completions",
      customEmbedUrl: spec.transports?.embed?.request?.url_template
        || "{base_url}/embeddings",
      customChatContract:
        spec.transports?.chat?.contract === "openai_chat_completions"
          ? "openai_chat_completions"
          : "openai_chat_completions_sse",
      customChatQuery: JSON.stringify(
        spec.transports?.chat?.request?.query || {},
        null,
        2,
      ),
      customChatHeaders: JSON.stringify(
        spec.transports?.chat?.request?.headers || {},
        null,
        2,
      ),
      customChatBody: JSON.stringify(
        spec.transports?.chat?.request?.body_template
          || parseJsonObject(DEFAULT_CUSTOM_CHAT_BODY),
        null,
        2,
      ),
      customEmbedQuery: JSON.stringify(
        spec.transports?.embed?.request?.query || {},
        null,
        2,
      ),
      customEmbedHeaders: JSON.stringify(
        spec.transports?.embed?.request?.headers || {},
        null,
        2,
      ),
      customEmbedBody: JSON.stringify(
        spec.transports?.embed?.request?.body_template
          || parseJsonObject(DEFAULT_CUSTOM_EMBED_BODY),
        null,
        2,
      ),
      models: allRows,
      testing: false,
      test: projectedTest,
    });
  }, [
    scoped,
    config.kb.models,
    config.providers,
    baseConfig.providers,
    baseConfig.kb.models,
    baseConfig.media?.models,
    baseConfig.search?.models,
    data?.verification?.entries,
    draftProviderIds,
    fingerprints,
    imageModelsByProvider,
    searchModelsByProvider,
    verificationWorkflowOpen,
    v2Enabled,
  ]);

  useEffect(() => {
    if (!scoped || pageMode !== "edit" || section !== "connection" || loading || !data || verificationWorkflowOpen || connectionInitializedRef.current) return;
    connectionInitializedRef.current = true;
    if (newConnection) openAdd();
    else {
      const providerId = new URLSearchParams(window.location.search).get("id");
      const key = Object.keys(config.providers).find((name) => draftProviderIds[name] === providerId);
      if (key) openEdit(key);
      else setError(t("sections.connection_missing"));
    }
  }, [scoped, pageMode, section, loading, data, newConnection, config.providers, draftProviderIds, verificationWorkflowOpen, openAdd, openEdit, t]);

  useEffect(() => {
    if (connect && connectionBaselineRef.current === null) {
      connectionBaselineRef.current = stableJson({ ...connect, test: undefined, testing: false, providerId: undefined });
    }
  }, [connect]);

  useEffect(() => {
    if (!connect || !verificationBatch || !verificationDrafts?.length) return;
    const draft = v2ProviderDraftFromConnect(connect);
    if (stableJson(draft) !== stableJson({ ...verificationDrafts[0], provider_id: draft.provider_id })) return;
    const steps = uniqueCapabilitySteps(connect.test?.steps);
    const byKey = new Map(steps.map((step) => [`${step.model}:${step.capability}`, step]));
    for (const item of verificationBatch.items) {
      if (item.state !== "verified" && item.state !== "failed") continue;
      byKey.set(`${item.model_id}:${item.capability}`, {
        step: "capability", model: item.model_id, capability: item.capability,
        ok: item.state === "verified", status: item.state === "verified" ? "passed" : "failed",
        code: item.state === "verified" ? "ok" : "capability_failed",
      });
    }
    const nextSteps = [...byKey.values()];
    const nextTest = { signature: verificationBatch.plan_id, ok: nextSteps.every((step) => step.ok), code: nextSteps.every((step) => step.ok) ? "ok" as const : "capability_failed" as const, steps: nextSteps };
    const testing = !TERMINAL_VERIFICATION_BATCH_STATES.has(verificationBatch.state);
    if (stableJson(connect.test) === stableJson(nextTest) && connect.testing === testing) return;
    setConnect((current) => current ? { ...current, testing, test: nextTest } : current);
  }, [connect, verificationBatch, verificationDrafts]);

  // The two callbacks above precede providerCopilot; forward through a ref (clicks only trigger them after mounting).
  const touchProviderFieldRef = useRef<((field: "models") => void) | null>(null);

  const patchConnect = useCallback((p: Partial<ConnectDraft>) => {
    if (verificationBatchRunning) return;
    const verificationChanged = Object.keys(p).some((key) => !["test", "testing", "providerId"].includes(key));
    setConnect((current) => (current ? { ...current, ...p, ...(verificationChanged ? { test: undefined } : {}) } : current));
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
  }, [verificationBatchRunning]);

  const connectCaps = connect?.protocol === "anthropic_compatible"
    ? ANTHROPIC_CAPS
    : connect?.protocol === "azure_openai"
      || connect?.protocol === "custom_http"
      ? DECLARATIVE_HTTP_CAPS
      : scoped ? UI_CAPS.filter((cap) => cap !== IMAGE_CAP && cap !== SEARCH_CAP) : UI_CAPS;

  const toggleDraftCap = useCallback((idx: number, cap: string) => {
    if (verificationBatchRunning) return;
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
    setConnect((c) => {
      if (!c) return c;
      const models = c.models.map((m, i) => {
        if (i !== idx) return m;
        return { ...m, ...nextDraftModelCapability(m, cap) };
      });
      return { ...c, models, test: undefined };
    });
  }, [verificationBatchRunning]);

  const runTest = useCallback(async () => {
    if (!connect) return;
    setError(null);
    if (v2Enabled) {
      const draft = v2ProviderDraftFromConnect(connect);
      const items = draft.models.flatMap((model) =>
        model.capabilities.map((capability) => ({
          provider_id: "",
          model_id: model.id,
          capability,
        })));
      if (items.length) {
        setVerificationBusy(true);
        setError(null);
        setVerificationPlan(null);
        setVerificationBatch(null);
        try {
          const precheck = await precheckV2Provider(draft);
          rememberDraftProviderId(draft.runtime_key, precheck.provider_id);
          const resolvedDraft = { ...draft, provider_id: precheck.provider_id };
          setConnect((current) => current
            ? { ...current, providerId: precheck.provider_id }
            : current);
          const plan = await createVerificationPlan({
            base_config_version: data?.version ?? 0,
            provider_drafts: [resolvedDraft],
            items: items.map((item) => ({
              ...item,
              provider_id: precheck.provider_id,
            })),
          });
          setConnect((current) => current
            ? { ...current, providerId: precheck.provider_id, testing: false }
            : current);
          setVerificationDrafts([resolvedDraft]);
          setVerificationPlan(plan);
        } catch (cause) {
          setError(userFacingError(cause));
        } finally {
          setVerificationBusy(false);
        }
        return;
      }
    }
    if (
      hasBillableImageProbe(connect.models)
      && !(await confirm({
        title: t("connect.image_probe_title"),
        description: t("connect.image_probe_confirm"),
        confirmLabel: t("connect.image_probe_action"),
        tone: "primary",
      }))
    ) {
      return;
    }
    const signature = providerSignature(
      connect.protocol,
      connect.baseUrl,
      connect.messagesPath,
      connect.models,
      connect.apiKey.trim() || fingerprints[connect.origKey || connect.name.trim()] || "saved-secret",
      {
        apiVersion: connect.apiVersion,
        authType: connect.authType,
        authHeaderName: connect.authHeaderName,
        authPrefix: connect.authPrefix,
        customChatUrl: connect.customChatUrl,
        customEmbedUrl: connect.customEmbedUrl,
        customChatContract: connect.customChatContract,
        customChatQuery: parseStringRecord(connect.customChatQuery),
        customChatHeaders: parseStringRecord(connect.customChatHeaders),
        customChatBody: parseJsonObject(connect.customChatBody),
        customEmbedQuery: parseStringRecord(connect.customEmbedQuery),
        customEmbedHeaders: parseStringRecord(connect.customEmbedHeaders),
        customEmbedBody: parseJsonObject(connect.customEmbedBody),
      },
    );
    patchConnect({ testing: true, test: undefined });
    try {
      const body = {
        provider_type: connect.protocol,
        base_url: connect.baseUrl.trim(),
        model: connect.models.find((model) => model.id.trim())?.id,
        models: connect.models
          .filter((model) => model.id.trim())
          .map((model) => ({
            id: model.id.trim(),
            // `think` is a runtime routing hint inherited from legacy provider
            // supports. It has no standalone verification contract, so sending
            // it to the capability endpoint produces a hidden 422 after the
            // visible Chat/Tool probes have already passed.
            capabilities: legacyConnectionTestCapabilities(model.caps),
            capability_protocol: model.protocol || null,
          }))
          .filter((model) => model.capabilities.length > 0),
        anthropic_version: connect.protocol === "anthropic_compatible" ? DEFAULT_ANTHROPIC_VERSION : null,
        messages_path: connect.messagesPath.trim() || null,
      };
      const steps: VerifyProviderStep[] = [];

      for (const model of body.models) {
        for (const capability of model.capabilities) {
          const probeBody = {
            ...body,
            model: model.id,
            models: [],
            capability,
            capability_protocol: model.capability_protocol,
          };
          const capabilityResult = connect.mode === "edit" && connect.origKey
            ? await verifyDraftProviderCapability(connect.origKey, {
                ...probeBody,
                ...(connect.apiKey.trim() ? { api_key: connect.apiKey.trim() } : {}),
              })
            : await verifyProviderCapability({ ...probeBody, api_key: connect.apiKey.trim() });
          const capabilityStep = capabilityResult.steps?.find((step) => step.step === "capability");
          steps.push(capabilityStep || {
            step: "capability", model: model.id, capability, ok: false, code: capabilityResult.code, status: "failed",
          });
          const failed = steps.some(capabilityStepFailed);
          const degraded = steps.some((step) => step.status === "degraded");
          patchConnect({
            testing: true,
            test: {
              ok: !failed && !degraded,
              code: failed ? "capability_failed" : degraded ? "capability_degraded" : "ok",
              steps: [...steps],
              signature,
            },
          });
        }
      }
      const failed = steps.some(capabilityStepFailed);
      const degraded = steps.some((step) => step.status === "degraded");
      const testCode = failed ? "capability_failed" : degraded ? "capability_degraded" : "ok";
      patchConnect({ testing: false, test: { ok: !failed && !degraded, code: testCode, steps, signature } });
      const evidenceKey = connect.origKey || connect.name.trim();
      if (evidenceKey) {
        setProviderTests((current) => ({
          ...current,
          [evidenceKey]: {
            ok: !failed && !degraded,
            code: testCode,
            status: failed ? "failed" : degraded ? "degraded" : "passed",
            signature,
            testedAt: new Date().toISOString(),
          },
        }));
      }
    } catch (e) {
      setError(userFacingError(e));
      patchConnect({ testing: false });
    }
  }, [
    confirm,
    connect,
    data?.version,
    fingerprints,
    patchConnect,
    rememberDraftProviderId,
    t,
    userFacingError,
    v2Enabled,
  ]);

  const autoFillModels = useCallback(async () => {
    // Clicking "自动发现模型" yields a real model list — without kept markers, the next "智能填写"
    // replaces them entirely with AI guesses (R3 review P2-1; same rationale as toggleDraftCap).
    touchProviderFieldRef.current?.("models");
    if (!connect) return;
    setDiscoveringModels(true);
    setError(null);
    try {
      if (v2Enabled) {
        const draft = v2ProviderDraftFromConnect(connect);
        const precheck = await precheckV2Provider(draft);
        rememberDraftProviderId(draft.runtime_key, precheck.provider_id);
        if (!precheck.discovery.supported) {
          setError(t("v2.discovery_not_supported"));
          return;
        }
        const resolvedDraft = { ...draft, provider_id: precheck.provider_id };
        const result = await discoverV2Models(precheck.provider_id, resolvedDraft);
        setConnect((current) => current
          ? { ...current, providerId: precheck.provider_id, test: undefined }
          : current);
        setDiscovery(result);
        return;
      }
      const body = { provider_type: connect.protocol, base_url: connect.baseUrl.trim(), model: null, models: [], anthropic_version: connect.protocol === "anthropic_compatible" ? DEFAULT_ANTHROPIC_VERSION : null, messages_path: connect.messagesPath.trim() || null };
      const result = connect.mode === "edit" && connect.origKey
        ? await discoverDraftProviderModels(connect.origKey, { ...body, ...(connect.apiKey.trim() ? { api_key: connect.apiKey.trim() } : {}) })
        : await discoverProviderModels({ ...body, api_key: connect.apiKey.trim() });
      if (!result.ok) { setError(result.message || userFacingError(result.code)); return; }
      setConnect((current) => current ? {
        ...current,
        models: [
          ...current.models.map((model) => result.models.includes(model.id) ? { ...model, source: "preset" as const } : model),
          ...result.models.filter((id) => !current.models.some((model) => model.id === id)).map((id) => ({ id, caps: ["chat"], source: "preset" as const })),
        ],
        test: undefined,
      } : current);
    } catch (error) { setError(userFacingError(error)); }
    finally { setDiscoveringModels(false); }
  }, [connect, rememberDraftProviderId, t, userFacingError, v2Enabled]);

  const addDiscoveredModels = useCallback((modelIds: string[]) => {
    touchProviderFieldRef.current?.("models");  // Likewise: selections in the discovery picker are the user's choices.
    setConnect((current) => {
      if (!current) return current;
      const selected = new Set(modelIds);
      return {
        ...current,
        models: [
          ...current.models.map((model) => selected.has(model.id)
            ? { ...model, source: "preset" as const }
            : model),
          ...modelIds
            .filter((id) => !current.models.some((model) => model.id === id))
            .map((id) => ({
              id,
              caps: suggestedCapabilitiesForModelId(id),
              source: "preset" as const,
            })),
        ],
        test: undefined,
      };
    });
    setDiscovery(null);
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
  }, []);

  /**
   * Verify one item only (button on the right of each evidence-table row).
   *
   * ★ Consent was already given in that row ("将向 X 发 1 次请求"), so **do not show another confirmation dialog** here;
   * directly complete plan → batch. Full-batch verification retains its picker + confirmation surface — its request count is not 1,
   * and users need to see the count before deciding.
   */
  /**
   * ★ **Local** in-progress marker for single-item verification.
   *
   * Do not rely solely on `readiness.rows[].attempt_state`: that data only updates after the batch completes
   * and the `evidenceRefreshBatchRef` effect refetches configuration.
   * During that window, rows appear unchanged and buttons become clickable again — users may think nothing happened and click again,
   * **duplicating requests** to the customer's model service (each consumes a real request and quota).
   */
  const [verifyingRowKey, setVerifyingRowKey] = useState<string | null>(null);
  /**
   * ★ Batch ID created by single-item verification.
   *
   * `verificationBatch` also determines whether the verification workflow is open
   * (`verificationWorkflowOpen` → `configurationActionsDisabled`). Full-batch verification has a closable panel;
   * single-item verification has none — without claiming and actively clearing it, one verification **permanently disables "编辑配置" and "添加连接" without explanation**,
   * requiring a page refresh to recover (observed on machine 116).
   *
   * Claim by ID rather than a boolean: the terminal-state effect is shared with full-batch verification;
   * without ownership, it also closes someone else's results panel.
   */
  const singleRowBatchRef = useRef<string | null>(null);

  const verifySingleRow = useCallback(async (row: LlmReadinessRow) => {
    const runtimeKey = Object.keys(draftProviderIds)
      .find((key) => draftProviderIds[key] === row.provider_id);
    const spec = runtimeKey ? config.providers[runtimeKey] : undefined;
    if (!runtimeKey || !spec) {
      setError(t("overview.no_verification_items"));
      return;
    }
    const drafts = [v2ProviderDraftFromSavedConfig(runtimeKey, row.provider_id, spec)];
    setVerifyingRowKey(evidenceItemKey(row));
    setVerificationBusy(true);
    setError(null);
    try {
      const plan = await createVerificationPlan({
        base_config_version: data?.version ?? 0,
        provider_drafts: drafts,
        items: [{
          provider_id: row.provider_id,
          model_id: row.model_id,
          capability: row.capability as ModelCapability,
        }],
      });
      const created = await createVerificationBatch(
        {
          plan_id: plan.plan_id,
          confirmation_token: plan.confirmation_token,
          provider_drafts: drafts,
        },
        randomUuid(),
      );
      singleRowBatchRef.current = created.batch_id;
      setVerificationDrafts(drafts);
      setVerificationBatch(await fetchVerificationBatch(created.batch_id));
    } catch (cause) {
      // Release this row immediately on failure, or it stays in "正在验证" forever.
      singleRowBatchRef.current = null;
      setVerifyingRowKey(null);
      setError(userFacingError(cause));
    } finally {
      setVerificationBusy(false);
    }
  }, [
    config.providers,
    data?.version,
    draftProviderIds,
    t,
    userFacingError,
  ]);

  const confirmVerification = useCallback(async () => {
    if (!verificationPlan || !verificationDrafts) return;
    setVerificationBusy(true);
    setError(null);
    try {
      const created = await createVerificationBatch(
        {
          plan_id: verificationPlan.plan_id,
          confirmation_token: verificationPlan.confirmation_token,
          provider_drafts: verificationDrafts,
        },
        randomUuid(),
      );
      setVerificationBatch({
        batch_id: created.batch_id,
        plan_id: created.plan_id,
        state: created.state,
        revision: created.revision,
        base_config_version: data?.version ?? 0,
        counts: { queued: verificationPlan.item_count },
        items: [],
      });
      setRecoveredBatch(null);
      const snapshot = await fetchVerificationBatch(created.batch_id);
      setVerificationBatch(snapshot);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setVerificationBusy(false);
    }
  }, [data?.version, userFacingError, verificationDrafts, verificationPlan]);

  const cancelCurrentVerification = useCallback(async () => {
    if (!verificationBatch) return;
    if (!(await confirmCancelVerification())) return;
    setVerificationBusy(true);
    try {
      await cancelVerificationBatch(verificationBatch.batch_id);
      const snapshot = await fetchVerificationBatch(verificationBatch.batch_id);
      setVerificationBatch(snapshot);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setVerificationBusy(false);
    }
  }, [confirmCancelVerification, userFacingError, verificationBatch]);

  const cancelRecoveredVerification = useCallback(async () => {
    if (!recoveredBatch || !recoveredBatchRunning) return;
    if (!(await confirmCancelVerification())) return;
    setVerificationBusy(true);
    setError(null);
    try {
      await cancelVerificationBatch(recoveredBatch.batch_id);
      const snapshot = await fetchVerificationBatch(recoveredBatch.batch_id);
      setRecoveredBatch(snapshot);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setVerificationBusy(false);
    }
  }, [confirmCancelVerification, recoveredBatch, recoveredBatchRunning, userFacingError]);

  const dismissVerification = useCallback(() => {
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
    setVerificationSelectionOpen(false);
  }, []);

  const planSelectedVerification = useCallback(async (
    selected: VerificationCandidate[],
  ) => {
    const items = selected.map((candidate) => ({
      provider_id: candidate.providerId,
      model_id: candidate.modelId,
      capability: candidate.capability,
    }));
    const providerIds = new Set(items.map((item) => item.provider_id));
    const drafts = Object.entries(baseConfig.providers).flatMap(([runtimeKey, spec]) => {
      const providerId = draftProviderIds[runtimeKey];
      return providerId && providerIds.has(providerId)
        ? [v2ProviderDraftFromSavedConfig(runtimeKey, providerId, spec)]
        : [];
    });
    if (items.length === 0 || drafts.length !== providerIds.size) {
      setError(t("overview.no_verification_items"));
      return;
    }
    setVerificationBusy(true);
    setError(null);
    setVerificationPlan(null);
    setVerificationBatch(null);
    setVerificationDrafts(null);
    try {
      const plan = await createVerificationPlan({
        base_config_version: data?.version ?? 0,
        provider_drafts: drafts,
        items,
      });
      setVerificationDrafts(drafts);
      setVerificationPlan(plan);
      if (!sectionsEnabled) setVerificationSelectionOpen(false);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setVerificationBusy(false);
    }
  }, [
    baseConfig.providers,
    data?.version,
    draftProviderIds,
    sectionsEnabled,
    t,
    userFacingError,
  ]);

  const retryUnfinishedVerification = useCallback(async () => {
    if (!verificationBatch || !verificationDrafts) return;
    if (verificationBatch.base_config_version !== (data?.version ?? 0)) {
      setVerificationPlan(null);
      setVerificationBatch(null);
      setVerificationDrafts(null);
      setError(t("errors.err_verification_stale"));
      return;
    }
    const retryableStates = new Set([
      "failed",
      "errored",
      "cancelled",
      "interrupted",
    ]);
    const items = verificationBatch.items
      .filter((item) => retryableStates.has(item.state))
      .map((item) => ({
        provider_id: item.provider_id,
        model_id: item.model_id,
        capability: item.capability,
      }));
    if (items.length === 0) return;
    setVerificationBusy(true);
    setError(null);
    try {
      const plan = await createVerificationPlan({
        base_config_version: data?.version ?? 0,
        provider_drafts: verificationDrafts,
        items,
      });
      setVerificationBatch(null);
      setVerificationPlan(plan);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setVerificationBusy(false);
    }
  }, [data?.version, t, userFacingError, verificationBatch, verificationDrafts]);


  // ── Batch 1-d: configuration copilot on the provider surface (tech-design-batch1d §7) ──────────────
  // Add mode only: an existing provider's runtime_key necessarily collides with itself during editing; the repair round renames the user's
  // provider (design review P1-4).
  // 2026-08-18: FEATURE_CONFIG_COPILOT gate removed (Allen's decision; always enabled for every customer).
  // Keep the constant instead of inlining true: 5 downstream references; a name reads better than scattered true literals.
  // Also fix flicker — the original hook started false, only turning true after /health returned.
  const copilotEnabled = true;
  type ProviderCopilotField = "protocol" | "name" | "baseUrl" | "apiVersion" | "messagesPath" | "auth" | "models";
  const PROVIDER_COPILOT_FIELDS: ProviderCopilotField[] = ["protocol", "name", "baseUrl", "apiVersion", "messagesPath", "auth", "models"];
  const connectRef = useRef<ConnectDraft | null>(connect);
  connectRef.current = connect;
  const providerCopilot = useCopilotFields<ProviderCopilotField, Partial<ConnectDraft>>({
    enabled: copilotEnabled,
    fields: PROVIDER_COPILOT_FIELDS,
    snapshotOf: (field) => {
      const draft = connectRef.current;
      if (!draft) return {};
      if (field === "protocol") return { protocol: draft.protocol };
      if (field === "name") return { name: draft.name };
      if (field === "baseUrl") return { baseUrl: draft.baseUrl };
      if (field === "apiVersion") return { apiVersion: draft.apiVersion };
      if (field === "messagesPath") return { messagesPath: draft.messagesPath };
      if (field === "auth") return { authType: draft.authType, authHeaderName: draft.authHeaderName, authPrefix: draft.authPrefix };
      return { models: draft.models.map((model) => ({ ...model })) };
    },
    restoreSnapshot: (snapshot) => patchConnect(snapshot),
  });

  const applyProviderDraft = useCallback((result: ConfigDraftResult): AiFillApplyReceipt => {
    const session = providerCopilot.beginApply(result);
    // backendKey must come from the lookup table; call sites cannot write literals — guards added in r2 locked the constants themselves,
    // but a misspelled literal at a call site still passed everything (R3 review P1-2).
    const admit = (field: ProviderCopilotField) => session.admit(field, PROVIDER_DRAFT_BACKEND_KEYS[field]);
    const mapped = mapProviderDraftToConnect(result.fields as Parameters<typeof mapProviderDraftToConnect>[0]);
    const patch: Partial<ConnectDraft> = {};
    if (mapped.protocol && admit("protocol")) patch.protocol = mapped.protocol as ProviderType;
    if (mapped.name !== undefined && admit("name")) patch.name = mapped.name;
    if (mapped.baseUrl !== undefined && admit("baseUrl")) patch.baseUrl = mapped.baseUrl;
    if (mapped.apiVersion !== undefined && admit("apiVersion")) patch.apiVersion = mapped.apiVersion;
    if (mapped.messagesPath !== undefined && admit("messagesPath")) patch.messagesPath = mapped.messagesPath;
    if (mapped.authType !== undefined && admit("auth")) {
      patch.authType = mapped.authType as ConnectDraft["authType"];
      if (mapped.authHeaderName !== undefined) patch.authHeaderName = mapped.authHeaderName;
      if (mapped.authPrefix !== undefined) patch.authPrefix = mapped.authPrefix;
    }
    if (mapped.models?.length && admit("models")) patch.models = mapped.models;
    if (Object.keys(patch).length) patchConnect({ ...patch, test: undefined });
    return session.commit(Object.values(PROVIDER_DRAFT_BACKEND_KEYS));
  }, [providerCopilot, patchConnect]);

  // Direct-fill bypass: the original value skips the model, returning directly from pasted text to this browser (tech-design-batch1d §3).
  const fillSecretFromDraft = useCallback((hits: NonNullable<ConfigDraftResult["secrets"]>) => {
    const picked = pickSecretForApiKey(hits);
    if (!picked.apiKey) return;
    patchConnect({ apiKey: picked.apiKey, ...(picked.authType ? { authType: picked.authType } : {}), test: undefined });
  }, [patchConnect]);

  /** ConnectPanel's onTouch — explicitly invoked by **actual user-input callbacks**.
   *  ★Do not infer from patch keys: protocol-button patches include internally calculated models/apiVersion;
   *  inference falsely marks them manually edited, permanently and silently preventing copilot from filling those fields (R3 review P1-1). */
  const touchProviderField = useCallback((field: ProviderCopilotField) => {
    providerCopilot.touchField(field);
  }, [providerCopilot]);
  touchProviderFieldRef.current = touchProviderField;

  const providerAiFill = shouldOfferProviderCopilot(copilotEnabled, flatModels.length > 0, connect)
    ? (
        <AiFillPanel
          formId="llm_provider"
          includeSecrets
          failMessageKey="fail_provider"
          getCurrentValues={() => {
            const draft = connectRef.current;
            if (!draft) return {};
            // Double protection: apiKey never goes upstream (server also strips secret keys via the registry).
            return {
              protocol: draft.protocol,
              name: draft.name,
              base_url: draft.baseUrl,
              api_version: draft.apiVersion,
              messages_path: draft.messagesPath,
              auth_type: draft.authType,
            };
          }}
          onApply={applyProviderDraft}
          onSecrets={fillSecretFromDraft}
          onClear={providerCopilot.clearSuggestions}
          onAcceptAll={providerCopilot.acceptAll}
        />
      )
    : null;

  const saveConnect = useCallback(() => {
    if (!connect) return;
    const key = connect.mode === "edit" && connect.origKey ? connect.origKey : connect.name.trim();
    // Drop incomplete rows (add-row seeds a blank id); require ≥1 real model.
    const rows = connect.models.map((m) => ({
      id: m.id.trim(),
      caps: m.caps,
      source: m.source,
      protocol: m.protocol,
      deployment: m.deployment?.trim(),
      apiSurface: m.apiSurface,
      requestBodyOverrides:
        parseModelRequestBodyOverrides(m.requestBodyOverrides || "{}") || {},
      outboundParams: m.outboundParams,
      runtimeParameters: m.runtimeParameters,
      contextPolicy: m.contextPolicy,
      creditRates: m.creditRates,
      contextWindow: m.contextWindow,
      maxOutputTokens: m.maxOutputTokens,
    })).filter((m) => m.id);
    const usesCustomChat = rows.some((model) =>
      model.caps.some((capability) =>
        ["chat", "tool", "vision"].includes(capability)));
    const usesCustomEmbed = rows.some((model) =>
      model.caps.includes("embed"));
    const addressValid = connect.protocol === "custom_http"
      ? customHttpBaseUrlSatisfied(connect.baseUrl, [
          ...(usesCustomChat ? [connect.customChatUrl] : []),
          ...(usesCustomEmbed ? [connect.customEmbedUrl] : []),
        ])
      : Boolean(connect.baseUrl.trim());
    if (!key || !addressValid || (!scoped && rows.length === 0)) return;
    // Split: image-generation rows → config.media.models; web-search rows → config.search.models; others →
    // provider.models (omit provider.models for image-only/search-only sources).
    const chatRows = regularProviderRows(rows);
    const imageRows = rows.filter((m) => m.caps.includes(IMAGE_CAP));
    const searchRows = rows.filter((m) => m.caps.includes(SEARCH_CAP));
    const existing = connect.mode === "edit" && connect.origKey ? config.providers[connect.origKey] : undefined;
    const spec: ProviderSpec = {
      ...(existing || {}),
      type: connect.protocol,
    };
    if (connect.protocol === "azure_openai") {
      spec.endpoint = connect.baseUrl.trim();
      spec.api_version = connect.apiVersion.trim();
      spec.auth = { type: connect.authType };
      delete spec.base_url;
      delete spec.transports;
    } else {
      spec.base_url = connect.baseUrl.trim();
      delete spec.endpoint;
      delete spec.api_version;
      if (connect.protocol !== "custom_http") {
        delete spec.auth;
        delete spec.transports;
      }
    }
    if (connect.protocol === "custom_http") {
      const chatBody = parseJsonObject(connect.customChatBody);
      const embedBody = parseJsonObject(connect.customEmbedBody);
      const chatQuery = parseStringRecord(connect.customChatQuery);
      const chatHeaders = parseStringRecord(connect.customChatHeaders);
      const embedQuery = parseStringRecord(connect.customEmbedQuery);
      const embedHeaders = parseStringRecord(connect.customEmbedHeaders);
      spec.auth = {
        type: connect.authType,
        ...(connect.authType === "secret_header"
          ? {
              header_name: connect.authHeaderName.trim(),
              prefix: connect.authPrefix,
            }
          : {}),
      };
      spec.transports = {
        ...(chatRows.some((model) =>
          model.caps.some((capability) =>
            ["chat", "tool", "vision"].includes(capability)))
          ? {
              chat: {
                contract: connect.customChatContract,
                request: {
                  method: "POST",
                  url_template: connect.customChatUrl.trim(),
                  query: chatQuery || {},
                  headers: chatHeaders || {},
                  body_template: chatBody || {},
                },
              },
            }
          : {}),
        ...(chatRows.some((model) => model.caps.includes("embed"))
          ? {
              embed: {
                contract: "openai_embeddings",
                request: {
                  method: "POST",
                  url_template: connect.customEmbedUrl.trim(),
                  query: embedQuery || {},
                  headers: embedHeaders || {},
                  body_template: embedBody || {},
                },
              },
            }
          : {}),
      };
    }
    if (chatRows.length) {
      spec.models = chatRows.map((m) => buildProviderModelSpec(connect.protocol, m));
    }
    else delete spec.models;                // Image-only/search-only sources: omit the models key (backend rejects models:[]).
    delete spec.fingerprint;
    if (connect.apiKey.trim()) spec.api_key = connect.apiKey.trim();
    else if (typeof existing?.api_key === "string" && existing.api_key) spec.api_key = existing.api_key;
    else delete spec.api_key;               // omit → backend preserves the stored key
    if (connect.protocol === "anthropic_compatible") {
      if (!spec.anthropic_version) spec.anthropic_version = DEFAULT_ANTHROPIC_VERSION;
      // messages_path: DeepSeek Anthropic endpoints require /messages; empty = adapter default (api.anthropic.com).
      if (connect.messagesPath.trim()) spec.messages_path = connect.messagesPath.trim();
      else delete spec.messages_path;
    }
    const providers = { ...config.providers };
    providers[key] = spec;
    let nextCfg: TeamConfig = {
      ...config,
      providers,
      kb: scoped ? config.kb : syncRerankProtocols(config.kb, key, chatRows),
    };
    if (!scoped) nextCfg = { ...nextCfg, media: buildMediaForSave(config.media, key, undefined, imageRows) };
    if (!scoped) nextCfg = { ...nextCfg, search: buildSearchForSave(config.search, key, undefined, searchRows) };
    const previousModels = existing ? normModels(existing) : [];
    const replacement = previousModels.length === 1 && chatRows.length === 1
      && previousModels[0].id !== chatRows[0].id
      ? { oldId: previousModels[0].id, newId: chatRows[0].id }
      : null;
    if (replacement) {
      const replacePair = (pair: Pair): Pair => pair.provider === key && pair.model === replacement.oldId
        ? { ...pair, model: replacement.newId }
        : pair;
      const replaceTierRef = (ref: TierRef | undefined): TierRef | undefined => {
        const pair = refPair(ref);
        return pair ? replacePair(pair) : ref;
      };
      nextCfg = {
        ...nextCfg,
        tiers: {
          ...nextCfg.tiers,
          primary: nextCfg.tiers.primary ? replacePair(nextCfg.tiers.primary) : undefined,
          primary_fallback: nextCfg.tiers.primary_fallback?.map(replacePair),
          fast: replaceTierRef(nextCfg.tiers.fast),
          judge: replaceTierRef(nextCfg.tiers.judge),
        },
        kb: {
          ...nextCfg.kb,
          models: Object.fromEntries(Object.entries(nextCfg.kb.models).map(([name, model]) => [name,
            model.provider === key && model.real_id === replacement.oldId
              ? { ...model, real_id: replacement.newId }
              : model,
          ])),
        },
        media: nextCfg.media ? {
          ...nextCfg.media,
          models: Object.fromEntries(Object.entries(nextCfg.media.models).map(([name, model]) => [name,
            model.provider === key && model.real_id === replacement.oldId
              ? { ...model, real_id: replacement.newId }
              : model,
          ])),
        } : undefined,
        search: nextCfg.search ? {
          ...nextCfg.search,
          models: Object.fromEntries(Object.entries(nextCfg.search.models).map(([name, model]) => [name,
            model.provider === key && model.real_id === replacement.oldId
              ? { ...model, real_id: replacement.newId }
              : model,
          ])),
        } : undefined,
      };
    }
    if (scoped) {
      const requirements: Array<{ model: string; capabilities: string[] }> = [];
      const addRequirement = (pair: Pair | null | undefined, capabilities: string[]) => {
        if (pair?.provider === key && pair.model) requirements.push({ model: pair.model, capabilities });
      };
      addRequirement(nextCfg.tiers.primary, ["chat", "tool"]);
      for (const fallback of nextCfg.tiers.primary_fallback || []) addRequirement(fallback, ["chat", "tool"]);
      addRequirement(refPair(nextCfg.tiers.fast), ["chat"]);
      addRequirement(refPair(nextCfg.tiers.judge), ["chat"]);
      for (const [capability, name] of Object.entries(nextCfg.kb.active)) {
        const model = nextCfg.kb.models[name];
        if (model?.provider === key && model.real_id && normModels(spec).some((item) => item.id === model.real_id)) requirements.push({ model: model.real_id, capabilities: capability === "row_extractor" ? ["chat", "tool"] : [capability] });
      }
      const missing = requirements.filter((requirement) => !chatRows.some((model) => model.id === requirement.model && requirement.capabilities.every((cap) => model.caps.includes(cap))));
      if (missing.length) {
        setError(t("sections.connection_in_use", { models: [...new Set(missing.map((item) => item.model))].join(", ") }));
        return;
      }
      const references = overviewValidation?.readiness?.rows || [];
      const affected = references.filter((row) => row.provider_id === connect.providerId
        && !(replacement && row.model_id === replacement.oldId)
        && !chatRows.some((model) => model.id === row.model_id && model.caps.includes(row.capability)));
      if (affected.length) {
        setError(t("sections.connection_in_use", { models: [...new Set(affected.map((row) => row.model_id))].join(", ") }));
        return;
      }
      const referenceIssues = validateDraftLocally(nextCfg, t).filter((issue) => issue.path !== "tiers.primary" && !issue.path.startsWith("providers"));
      if (referenceIssues.length) { setError(referenceIssues.map((issue) => issue.message).join("；")); return; }
    }
    void (async () => {
      setBusy(true);
      setError(null);
      try {
        let providerId = connect.providerId;
        if (v2Enabled && !providerId) {
          const precheck = await precheckV2Provider(
            v2ProviderDraftFromConnect(connect),
          );
          providerId = precheck.provider_id;
        }
        if (providerId) rememberDraftProviderId(key, providerId);
        if (scoped) {
          const outgoing = configForWrite(nextCfg);
          const finishConnectionSave = (saved: AdminLlmConfigResp) => {
            const normalized = normConfig(saved.config);
            setData(saved);
            setBaseConfig(normalized);
            setConfig(normalized);
            setDraftProviderIds(saved.provider_identities || {});
            connectionBaselineRef.current = null;
            connectionInitializedRef.current = true;
            setConnect(null);
            setVerificationPlan(null);
            setVerificationBatch(null);
            setVerificationDrafts(null);
            setNavigateAfterConnectionSave(true);
          };
          if (stableJson(outgoing) === stableJson(configForWrite(baseConfig))) {
            connectionBaselineRef.current = null;
            setConnect(null);
            setNavigateAfterConnectionSave(true);
            return;
          }
          const checked = await validateAdminLlmConfig(outgoing, data?.version);
          const readiness = checked.readiness;
          const needsReview = !checked.valid
            || checked.issues.length > 0
            || (v2Enabled && (!readiness?.may_apply
              || readiness.blocking.length > 0
              || readiness.warnings.length > 0
              || readiness.required_acceptances.length > 0
              || Boolean(readiness.migration_acceptance)))
            || reindexRequested;
          if (needsReview) {
            setConfig(nextCfg);
            setValidation(checked);
            setReviewStale(null);
            setAcceptedRiskTokens(new Set());
            setMigrationAccepted(false);
            setReviewOpen(true);
            return;
          }
          try {
            const resp = await saveAdminLlmConfig(
              outgoing,
              false,
              data?.version,
              v2Enabled && readiness ? {
                draft_semantic_hash: readiness.draft_semantic_hash,
                acceptance_tokens: [],
              } : undefined,
            );
            finishConnectionSave(resp);
          } catch (cause) {
            try {
              const latest = await fetchAdminLlmConfig();
              if (latest.version !== data?.version
                && stableJson(comparableConfig(normConfig(latest.config))) === stableJson(comparableConfig(nextCfg))) {
                finishConnectionSave(latest);
                return;
              }
            } catch {
              // Keep the original save error and the editable draft.
            }
            throw cause;
          }
          return;
        }
        const ok = await persist(nextCfg);
        if (ok) { setConnect(null); setVerificationPlan(null); setVerificationBatch(null); setVerificationDrafts(null); }
      } catch (cause) {
        setError(userFacingError(cause));
      } finally {
        setBusy(false);
      }
    })();
  }, [
    scoped,
    overviewValidation?.readiness?.rows,
    t,
    connect,
    config,
    baseConfig,
    data?.version,
    persist,
    reindexRequested,
    rememberDraftProviderId,
    userFacingError,
    v2Enabled,
  ]);

  // Sources with only special capabilities (image_gen / web_search rows, no chat model) may explicitly skip probes on save;
  // image probes create real tasks and charges; backend save still applies SSRF guard validation to base_url.
  const pureSpecialDraft = Boolean(
    connect && connect.models.some((m) => m.id.trim() && isPureSpecialCap(m.caps))
    && !connect.models.some((m) => m.id.trim() && !isPureSpecialCap(m.caps)),
  );
  const customChatRequired = Boolean(
    connect?.protocol === "custom_http"
    && connect.models.some((model) =>
      model.id.trim()
      && model.caps.some((capability) =>
        ["chat", "tool", "vision"].includes(capability))),
  );
  const customEmbedRequired = Boolean(
    connect?.protocol === "custom_http"
    && connect.models.some((model) =>
      model.id.trim() && model.caps.includes("embed")),
  );
  const customDraftValid = Boolean(
    connect?.protocol !== "custom_http"
    || (
      (!customChatRequired || (
        connect.customChatUrl.trim()
        && parseStringRecord(connect.customChatQuery)
        && parseStringRecord(connect.customChatHeaders)
        && parseJsonObject(connect.customChatBody)
      ))
      && (!customEmbedRequired || (
        connect.customEmbedUrl.trim()
        && parseStringRecord(connect.customEmbedQuery)
        && parseStringRecord(connect.customEmbedHeaders)
        && parseJsonObject(connect.customEmbedBody)
      ))
      && (
        connect.authType !== "secret_header"
        || connect.authHeaderName.trim()
      )
      && connect.models.every((model) =>
        modelRequestBodyOverridesValid(
          model.requestBodyOverrides,
          model.caps,
        ))
      && customHttpBaseUrlSatisfied(
        connect.baseUrl,
        [
          ...(customChatRequired ? [connect.customChatUrl] : []),
          ...(customEmbedRequired ? [connect.customEmbedUrl] : []),
        ],
      )
    ),
  );
  const providerAddressValid = Boolean(
    connect
    && (
      connect.protocol === "custom_http"
        ? customHttpBaseUrlSatisfied(
            connect.baseUrl,
            [
              ...(customChatRequired ? [connect.customChatUrl] : []),
              ...(customEmbedRequired ? [connect.customEmbedUrl] : []),
            ],
          )
        : connect.baseUrl.trim()
    ),
  );
  const protocolFieldsValid = Boolean(
    connect?.protocol !== "azure_openai" || connect.apiVersion.trim(),
  ) && customDraftValid;
  const modelIdsUnique = Boolean(
    connect && connectDraftModelIdsUnique(connect.models),
  );
  // Centralize conditions in `saveBlockReason`: return the **first** unmet reason and render it beside the button.
  // Saveability and Why Saving Is Blocked must share one implementation — separate versions eventually make the page disagree with the gate.
  //
  // Meanings of each condition (original expression comments moved here too):
  // · outbound_value_missing — "固定为…" and "不超过…" require values before save: an incomplete persisted declaration
  //   fails only **at runtime**, with no actionable hint and already too late (the entire chat pipeline is unavailable).
  // · overrides_invalid — batch 1-d completion: invalid overrides previously did not block save; persistence parsed them as null
  //   and silently discarded them — a mistyped comma allowed saving, but enable_thinking never took effect.
  const modelsOptional = scoped && connect?.protocol !== "custom_http";
  const saveBlocked = saveBlockReason(connect, {
    modelsOptional,
    v2Enabled,
    addressValid: providerAddressValid,
    protocolFieldsValid,
    v1TestSatisfied: Boolean(
      connect
      && (connect.mode === "add"
        ? (providerTestAllowsAdd(connect.test) || pureSpecialDraft)
        : true),
    ),
  });
  const saveEnabled = saveBlocked === null;
  const testEnabled = Boolean(
    connect && providerAddressValid && connectDraftHasModel(connect.models)
    && protocolFieldsValid && modelIdsUnique
    && (!v2Enabled || V2_RUNTIME_KEY.test(connect.name.trim()))
    && (
      connect.authType === "none"
      || connect.mode === "edit"
      || connect.apiKey.trim()
    ),
  ) && (!v2Enabled || Boolean(connect?.models.some((model) =>
    model.id.trim()
    && model.caps.some((capability) =>
      VERIFIABLE_CAPABILITIES.has(capability as ModelCapability)))));

  // ─── render helpers ──────────────────────────────────────────────
  const modelLabel = (m: { providerKey: string; id: string }) => `${m.id}　·　${m.providerKey}`;
  const protoLabel = (ptype: ProviderType) => (
    ptype === "anthropic_compatible"
      ? t("proto.anthropic")
      : ptype === "azure_openai"
        ? t("proto.azure")
        : ptype === "custom_http"
          ? t("proto.custom_http")
          : t("proto.openai")
  );
  const reviewIssues = [
    ...localIssues,
    ...(validation?.issues || [])
      .filter((issue) => issue.severity !== "warning")
      .map((issue) => ({
        path: issue.path || "config",
        message: t(
          `errors.${modelSettingsValidationErrorKey(
            issue.code,
            issue.severity,
          )}`,
        ),
      })),
  ].filter(
    (issue, index, all) =>
      all.findIndex((candidate) => candidate.path === issue.path && candidate.message === issue.message) === index,
  );
  const reviewWarnings = (validation?.issues || []).filter(
    (issue) => issue.severity === "warning",
  );
  const readiness = validation?.readiness;
  const riskAcceptancesComplete = Boolean(
    readiness
    && readiness.required_acceptances.every((acceptance) =>
      acceptedRiskTokens.has(acceptance.acceptance_token)),
  );
  const migrationAcceptanceComplete = Boolean(
    !readiness?.migration_acceptance || migrationAccepted,
  );

  const showOverview = LLM_SETTINGS_V4_ENABLED
    && ((!scoped && pageMode === "overview") || (scoped && (section === "assignments" || section === "index" || section === "external")));
  const modelOverview = showOverview ? (
    <ModelSettingsOverview
      onVerifyItem={verifySingleRow}
      verifyingRowKey={verifyingRowKey}
      version={data?.version ?? 0}
      providers={overviewProviders}
      readiness={overviewValidation?.readiness || null}
      readinessLoading={overviewReadinessLoading}
      readinessError={overviewReadinessError}
      reindexState={reindexFailed ? "failed" : reindexing ? "running" : "idle"}
      imageConfigured={Boolean(config.media?.active?.image_gen)}
      searchConfigured={Boolean(config.search?.active?.web_search)}
      section={scoped ? section === "assignments" ? "assignments" : section === "external" ? "external" : "index" : overviewTab}
      onEdit={scoped ? undefined : () => router.push("/team/settings/models/assignments/")}
      onConfigureImage={() => router.push(modelExternalCapabilitiesPath("image"))}
      onConfigureSearch={() => router.push(modelExternalCapabilitiesPath("search"))}
      onRetryIndex={reindexFailed && section === "index" && typeof config.kb.pending_embed?.model === "string" && config.kb.models[config.kb.pending_embed.model]
        ? () => {
            const model = config.kb.pending_embed?.model as string;
            setRetryIndexRequested(true);
            setConfig({ ...config, kb: { ...config.kb, active: { ...config.kb.active, embed: model } } });
          } : undefined}
      retryIndexDisabled={busy || isDirty}
      onAddProvider={() => router.push("/team/settings/models/connections/new/")}
      onConnectBowra={data?.features?.bowra_connect ? () => void connectBowra() : undefined}
      onEditProvider={(key) => router.push(`/team/settings/models/connections/edit/?id=${encodeURIComponent(draftProviderIds[key] || "")}`)}
      onRetryReadiness={() => void refreshOverviewReadiness(baseConfig, data?.version ?? 0, v2Enabled)}
      onVerify={() => setVerificationSelectionOpen(true)}
      verificationBusy={verificationBusy}
      verificationAvailable={verificationCandidates.length > 0 && !verificationWorkflowOpen && !overviewReadinessLoading}
      configurationActionsDisabled={verificationWorkflowOpen || busy}
    />
  ) : null;

  if (loading) {
    return <div className="mset"><PageHead t={t} v4={LLM_SETTINGS_V4_ENABLED} mode="overview" titleKey={section !== "overview" ? `sections.title_${section}` : undefined} eyebrowKey={section === "index" ? "sections.index_eyebrow" : undefined} /><LoadingSkeleton rows={3} label={t("loading")} /></div>;
  }

  if (!data) {
    return (
      <div className={`mset${LLM_SETTINGS_V4_ENABLED ? " v4" : ""}`}>
        <PageHead t={t} v4={LLM_SETTINGS_V4_ENABLED} mode="overview" titleKey={section !== "overview" ? `sections.title_${section}` : undefined} eyebrowKey={section === "index" ? "sections.index_eyebrow" : undefined} />
        <div className="overview-load-failure" role="alert">
          <AlertTriangle size={18} />
          <div>
            <b>{t("overview.config_unavailable_title")}</b>
            <span>{error || t("errors.err_config_unavailable")}</span>
          </div>
          <button type="button" className="btn sec sm" onClick={() => setLoadAttempt((current) => current + 1)}>
            <RefreshCw size={13} /> {t("overview.retry")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <ConfigurationFieldProvider contracts={data?.field_contracts}><div ref={pageRef} className={`mset${LLM_SETTINGS_V4_ENABLED ? " v4" : ""}`}>
      <PageHead
        t={t}
        v4={LLM_SETTINGS_V4_ENABLED}
        mode={scoped ? "edit" : pageMode}
        isDirty={isDirty || connectionDirty}
        titleKey={sectionsEnabled ? `sections.title_${section}` : undefined}
        subtitleKey={scoped ? `sections.body_${section}` : undefined}
        // A section page opened from the overview has no draft until something changes; after Apply or a reload
        // it must not keep saying "Editing draft".
        showDraftNote={section !== "external" && (!scoped || isDirty || connectionDirty)}
        backLabelKey={section === "index" ? "sections.back_to_knowledge" : undefined}
        eyebrowKey={section === "index" ? "sections.index_eyebrow" : undefined}
        onBack={scoped ? () => router.push(overviewReturnPath) : exitEditMode}
        actionsDisabled={verificationWorkflowOpen}
        backDisabled={verificationBatchRunning}
      />
      {bowraImported ? <p role="status">{t("bowra.imported")}</p> : null}
      {scoped && section === "connection" && pageMode === "overview" ? <p role="status">{t("sections.saved")}</p> : null}
      {error ? (
        <div className="test-result err" role="alert"><span className="ti"><AlertTriangle size={15} /></span><div>{error}</div></div>
      ) : null}

      {showOverview && !scoped ? (
        <Tabs value={overviewTab} onValueChange={(value) => {
          if (value === "external") router.push(modelExternalCapabilitiesPath());
          else setOverviewTab(value);
        }} idBase="model-settings">
          <TabList appearance="underline" className="team-openapi-tabs" aria-label={t("sections.overview_tabs_label")}>
            <Tab value="connections" className={overviewTab === "connections" ? "is-active" : undefined}>{t("sections.tab_connections")}</Tab>
            <Tab value="assignments" className={overviewTab === "assignments" ? "is-active" : undefined}>{t("sections.tab_assignments")}</Tab>
            <Tab value="external" className={overviewTab === "external" ? "is-active" : undefined}>{t("sections.tab_external")}</Tab>
          </TabList>
          <TabPanel value="connections" className="team-openapi-panel">{modelOverview}</TabPanel>
          <TabPanel value="assignments" className="team-openapi-panel">
            {modelOverview}
            {/* Turn-wide model behavior lives with the role assignments; it saves on its own endpoint. */}
            <RetrySettingSection
              value={llmMaxRetriesFromConfig(data.config)}
              version={data.version}
              disabled={busy || verificationWorkflowOpen}
              userFacingError={userFacingError}
              onSaved={(response) => {
                setData((current) => current ? {
                  ...current,
                  version: response.version,
                  config: { ...current.config, retry: response.retry },
                } : current);
                // Readiness facts are tied to a configuration version; re-read them for the new one.
                void refreshOverviewReadiness(baseConfig, response.version, v2Enabled);
              }}
            />
          </TabPanel>
          <TabPanel value="external" className="team-openapi-panel">{modelOverview}</TabPanel>
        </Tabs>
      ) : modelOverview}

      {v2Enabled && verificationPlan && !verificationSelectionOpen && (pageMode === "overview" || !connect) ? (
        <VerificationPanel
          plan={verificationPlan}
          batch={verificationBatch}
          busy={verificationBusy}
          onConfirm={() => void confirmVerification()}
          onCancel={() => void cancelCurrentVerification()}
          onDismiss={dismissVerification}
          onRetry={() => void retryUnfinishedVerification()}
        />
      ) : null}

      {v2Enabled && recoveredBatch ? (
        <section className="verification-recovery" aria-live="polite">
          <span className="verification-recovery-icon">
            {recoveredBatchFinished ? (
              <Check size={14} />
            ) : recoveredBatchStopped ? (
              <AlertTriangle size={14} />
            ) : (
              <Spinner size={14} />
            )}
          </span>
          <div>
            <b>
              {t(
                recoveredBatchFinished
                  ? "v2.recovered_completed_title"
                  : recoveredBatchStopped
                    ? "v2.recovered_stopped_title"
                    : "v2.recovered_title",
              )}
            </b>
            <small>
              {t(recoveredBatchFinished || recoveredBatchStopped
                ? "v2.recovered_terminal_body"
                : "v2.recovered_body", recoveredBatchSummary)}
            </small>
          </div>
          {recoveredBatchRunning ? (
            <button
              type="button"
              className="btn ghost sm"
              disabled={verificationBusy}
              onClick={() => void cancelRecoveredVerification()}
            >
              {t("v2.verification_cancel")}
            </button>
          ) : (
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => {
                recoveryDismissedBatchRef.current = recoveredBatch.batch_id;
                setRecoveredBatch(null);
              }}
            >
              {t("v2.recovered_dismiss")}
            </button>
          )}
        </section>
      ) : null}

      {pageMode === "edit" ? (
        <div className={`model-editor${!scoped || section === "connection" ? " model-editor-connection" : ""}`} data-testid="model-settings-editor">
      {/* ① Your model sources */}
      {(!scoped || section === "connection") ? <section className="block">
        <div className="block-head">
          <span className="block-n">①</span>
          <h3 className="block-title">{t("sources.title")}</h3>
          <div className="block-sub">{t("sources.sub")}</div>
        </div>
        <DraftIssueList issues={localIssues} prefixes={scoped ? ["providers"] : ["providers", "kb", "media", "search"]} />

        {scoped && connect?.mode === "edit" && connect.origKey ? <button type="button" className="btn ghost sm" disabled={verificationWorkflowOpen} onClick={(e) => { delReturnFocusRef.current = e.currentTarget; setDel(connect.origKey || null); }}><Trash size={14} /> {t("sections.delete_connection")}</button> : null}
        {providerEntries.length === 0 && !connect ? (
          <div className="empty big">
            <span className="ei"><Plug size={20} /></span>
            <span className="et">{t("sources.empty_title")}</span>
            <span className="es">{t("sources.empty_body")}</span>
            <button
              type="button"
              className="btn pri sm legacy-mset-empty-cta"
              disabled={verificationWorkflowOpen}
              onClick={openAdd}
            ><Plus size={14} /> {t("sources.empty_cta")}</button>
          </div>
        ) : null}

        {(!scoped ? providerEntries : []).map(([key, spec]) => {
          const models = normModels(spec);
          const imgModels = imageModelsByProvider(key);        // Image models come from config.media.
          const srchModels = searchModelsByProvider(key);      // Search models come from config.search.
          const totalCount = models.length + imgModels.length + srchModels.length;
          const ptype = (spec.type || "openai_compatible") as ProviderType;
          const capSet = new Set(models.flatMap((m) => m.caps));
          if (imgModels.length) capSet.add(IMAGE_CAP);
          if (srchModels.length) capSet.add(SEARCH_CAP);
          const unionCaps = Array.from(capSet).filter((c) => UI_CAPS.includes(c as never));
          const open = expanded === key;
          const fp = fingerprints[key] || spec.fingerprint;
          const evidence = providerTests[key];
          const providerId = draftProviderIds[key];
          const projectedEvidence = (data?.verification?.entries || []).filter(
            (entry) => entry.provider_id === providerId,
          );
          const signature = providerSignature(
            ptype,
            spec.endpoint || spec.base_url || "",
            String(spec.messages_path || ""),
            [
              ...models,
              ...imgModels.map((model) => ({
                id: model.real_id,
                caps: [IMAGE_CAP],
                protocol: model.protocol,
              })),
              ...srchModels.map((model) => ({
                id: model.real_id,
                caps: [SEARCH_CAP],
                protocol: model.protocol,
              })),
            ],
            String(spec.api_key || fp || "saved-secret"),
            {
              endpoint: spec.endpoint,
              apiVersion: spec.api_version,
              auth: spec.auth,
              transports: spec.transports,
            },
          );
          const testState = v2Enabled
            ? projectedEvidence.length === 0
              ? "untested"
              : projectedEvidence.some((entry) => entry.fact_state === "failed")
                ? "failed"
                : projectedEvidence.every((entry) => entry.fact_state === "verified")
                  ? "passed"
                  : "stale"
            : !evidence
              ? "untested"
              : evidence.signature === signature
                ? evidence.status
                : "stale";
          return (
            <div key={key} className={`pcard own${open ? " open" : ""}`}>
              <div className="pcard-head">
                <button type="button" className="pcard-expand" onClick={() => setExpanded(open ? null : key)} aria-expanded={open}>
                  <div className="pcard-ico">{key.slice(0, 2).toUpperCase()}</div>
                  <div className="pcard-main">
                    <div className="pcard-titlerow">
                      <span className="pcard-name" translate="no">{key}</span>
                      <span className="proto-chip"><Server size={11} aria-hidden /> {protoLabel(ptype)}</span>
                      <span className="model-count">{t("sources.model_count", { count: totalCount })}</span>
                      {LLM_SETTINGS_V4_ENABLED ? (
                        <span className={`connection-state ${testState}`}>
                          {testState === "passed" ? <Check size={11} aria-hidden /> : testState === "failed" ? <AlertTriangle size={11} aria-hidden /> : <Info size={11} aria-hidden />}
                          {t(`v4.test_${testState}`)}
                        </span>
                      ) : null}
                    </div>
                    <div className="pcard-caps">{unionCaps.map((c) => <span key={c} className="cap-chip">{capLabel(c)}</span>)}</div>
                  </div>
                  <span className="pcard-toggle"><ChevronDown size={18} aria-hidden /></span>
                </button>
                <div className="pcard-actions">
                  <ConnectionActions
                    editLabel={t("common.edit")}
                    deleteLabel={t("common.delete")}
                    menuLabel={t("sources.more_actions_for", { name: key })}
                    disabled={verificationWorkflowOpen}
                    disabledReason={t("sources.verification_open_reason")}
                    onEdit={() => openEdit(key)}
                    onDelete={(trigger) => { delReturnFocusRef.current = trigger; setDel(key); }}
                  />
                </div>
              </div>
              {open ? (
                <div className="pcard-body">
                  <div className="pcard-2col">
                    <div className="pcard-field"><span className="fl">{t("sources.base_url")}</span><span className="fv mono" translate="no">{spec.endpoint || spec.base_url}</span></div>
                    <div className="pcard-field"><span className="fl">{t("sources.key")}</span>
                      <span className="fv mono"><span className="key-fingerprint">{
                        fp ? <><span className="key-fingerprint-label">{t("sources.fingerprint")}</span> · <span translate="no">{fp.replace(/^sha256:/, "")}</span></> : "••••••"
                      }</span> <span className="locknote"><Lock size={11} /> {t("sources.encrypted")}</span></span></div>
                  </div>
                  <div className="pcard-field"><span className="fl">{t("sources.models", { count: totalCount })}</span>
                    <div className="catalog">
                      {models.map((m) => (
                        <div key={m.id} className="cat-row">
                          <div className="cat-id" translate="no">{m.id}</div>
                          <div className="cat-caps">{m.caps.filter((c) => UI_CAPS.includes(c as never)).map((c) => <span key={c} className="cap-chip">{capLabel(c)}</span>)}</div>
                        </div>
                      ))}
                      {imgModels.map((m) => (
                        <div key={`img::${m.real_id}`} className="cat-row">
                          <div className="cat-id" translate="no">{m.real_id}</div>
                          <div className="cat-caps">
                            <span className="cap-chip">{capLabel(IMAGE_CAP)}</span>
                            <span className="proto-chip legacy-mset-proto-plain"><RefreshCw size={10} /> {t(`media.proto_${m.protocol === "ark_sync" ? "sync" : "async"}`)}</span>
                          </div>
                        </div>
                      ))}
                      {srchModels.map((m) => (
                        <div key={`srch::${m.real_id}`} className="cat-row">
                          <div className="cat-id" translate="no">{m.real_id}</div>
                          <div className="cat-caps">
                            <span className="cap-chip">{capLabel(SEARCH_CAP)}</span>
                            <span className="proto-chip legacy-mset-proto-plain"><RefreshCw size={10} /> {t(`search.proto_${m.protocol === "dashscope_native" ? "native" : "compat"}`)}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          );
        })}

        {connect ? (
          <>
            <ConnectPanel
              t={t}
              draft={connect}
              modelsOptional={modelsOptional}
              adaptations={outboundAdaptations}
              caps={connectCaps}
              busy={busy || verificationBusy || verificationBatchRunning}
              saveEnabled={saveEnabled} saveBlocked={saveBlocked} testEnabled={testEnabled}
              deploymentMode={data?.features?.deployment_mode}
              parameterProfiles={data?.features?.model_runtime_parameters ? data.parameter_profiles : undefined}
              previewKey={JSON.stringify({ connect, version: data?.version })}
              onParameterPreview={(modelId, capability) => previewModelParameters(v2ProviderDraftFromConnect(connect), modelId, capability, data?.version || 0)}
              onPatch={patchConnect} onToggleCap={toggleDraftCap}
              onTouch={touchProviderField} onTest={runTest} onAutoFill={autoFillModels} discoveringModels={discoveringModels}
              onSave={saveConnect}
              onCancel={() => {
                if (scoped) { router.push(overviewReturnPath); return; }
                setConnect(null);
                setDiscovery(null);
                dismissVerification();
                setError(null);
              }}
              capLabel={capLabel}
              verification={sectionsEnabled && verificationPlan ? <VerificationPanel
                plan={verificationPlan} batch={verificationBatch} busy={verificationBusy}
                onConfirm={() => void confirmVerification()} onCancel={() => void cancelCurrentVerification()}
                onDismiss={dismissVerification} onRetry={() => void retryUnfinishedVerification()}
              /> : undefined}
              aiFill={providerAiFill}
              v2={v2Enabled}
              azureEnabled={Boolean(data?.features?.azure_openai)}
              customHttpEnabled={Boolean(data?.features?.custom_http)}
            />
            {!sectionsEnabled && v2Enabled && verificationPlan ? (
              <VerificationPanel
                plan={verificationPlan}
                batch={verificationBatch}
                busy={verificationBusy}
                onConfirm={() => void confirmVerification()}
                onCancel={() => void cancelCurrentVerification()}
                onDismiss={dismissVerification}
                onRetry={() => void retryUnfinishedVerification()}
              />
            ) : null}
          </>
        ) : !scoped && providerEntries.length > 0 ? (
          <button
            type="button"
            className="add-card"
            disabled={verificationWorkflowOpen}
            onClick={openAdd}
          ><Plus size={16} /> {t("sources.add")}</button>
        ) : null}
      </section> : null}

      {scoped && section === "connection" && isDirty ? <p>{t("sections.connection_staged")}</p> : null}
      {(!scoped || section === "assignments") ? <>
      {/* ② Model allocation (tiers: primary + advanced [lightweight/judge]) */}
      <section className="block">
        <div className="block-head">
          <span className="block-n">②</span>
          <h3 className="block-title">{t("tier.title")}</h3>
          <div className="block-sub">{t("tier.sub")}</div>
        </div>
        <p className="block-desc">{t("tier.desc")}</p>
        <DraftIssueList issues={localIssues} prefixes={["tiers"]} />
        {toolModels.length === 0 ? (
          <div className="empty big">
            <span className="ei"><Bot size={20} /></span>
            <span className="et">{t("tier.empty_title")}</span>
            <span className="es">{t("tier.empty_body")}</span>
          </div>
        ) : (
          <div className="default-sel">
            {/* Primary model */}
            <div className="tier-row">
              <div className="tier-label"><span className="tdot" /> {t("tier.primary_label")}<ConfigurationFieldHelp includeChildren field="team_model_config.tiers.primary" label={t("tier.primary_label")} /></div>
              <span className="tier-hint">{t("default.tool_only")}</span>
            </div>
            <div className="selbox">
              <select
                name="primary_model"
                aria-label={t("tier.primary_label")}
                value={primaryOptionValue}
                onChange={(e) => onPickPrimary(e.target.value)} disabled={busy}>
                {!primaryOptionValue ? <option value="">{t("tier.primary_placeholder")}</option> : null}
                {Object.entries(groupBy(toolModels, (m) => m.providerKey)).map(([pk, list]) => (
                  <optgroup key={pk} label={pk}>
                    {list.map((m) => <option key={`${pk}::${m.id}`} value={`${pk}::${m.id}`}>{m.id}</option>)}
                  </optgroup>
                ))}
              </select>
              <span className="chev"><ChevronDown size={16} /></span>
            </div>
            {primaryModel ? (
              <div className="sel-effect pinned">{t("tier.primary_effect_prefix")} <span className="mchip"><span className="prov">{primaryModel.provider} ·</span>{primaryModel.model}</span> ｜ {t("tier.primary_effect_suffix")}</div>
            ) : null}
            {/* Fallback chain (primary model only) */}
            <div className="fallback">
              <div className="fl-lab"><Layers size={11} /> {t("default.fallback_label")}<ConfigurationFieldHelp includeChildren field="team_model_config.tiers.primary_fallback" label={t("default.fallback_label")} /> <span className="fl-note">{t("default.fallback_note")}</span></div>
              {fallbackChain.length > 0 ? (
                <div className="fb-chain">
                  {fallbackChain.map((f, i) => (
                    <div key={`${f.provider}::${f.model}`} className="fb-item">
                      <span className="idx">{i + 1}</span>
                      <span className="fb-provider">{f.provider} ·</span>
                      <span className="fb-model">{f.model}</span>
                      <div className="fb-ctrl">
                        <button type="button" title={t("common.up")} aria-label={t("common.up")} disabled={i === 0 || busy} onClick={() => setFallback(moveItem(fallbackChain, i, -1))}><span className="legacy-mset-rot180"><ChevronDown size={13} /></span></button>
                        <button type="button" title={t("common.down")} aria-label={t("common.down")} disabled={i === fallbackChain.length - 1 || busy} onClick={() => setFallback(moveItem(fallbackChain, i, 1))}><ChevronDown size={13} /></button>
                        <button type="button" title={t("common.remove")} aria-label={t("common.remove")} disabled={busy} onClick={() => setFallback(fallbackChain.filter((_, j) => j !== i))}><XSm size={13} /></button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="fb-empty"><AlertTriangle size={13} /> {t("default.fallback_empty")}</div>
              )}
              {(() => {
                const used = new Set([primaryModel ? `${primaryModel.provider}::${primaryModel.model}` : "", ...fallbackChain.map((f) => `${f.provider}::${f.model}`)]);
                const avail = toolModels.filter((m) => !used.has(`${m.providerKey}::${m.id}`));
                if (avail.length === 0) return null;
                return (
                  <div className="fb-add-picker">
                    <Plus size={11} aria-hidden="true" />
                    <select
                      aria-label={t("default.fallback_add")}
                      value=""
                      disabled={busy}
                      onChange={(event) => {
                        const [provider, ...modelParts] = event.target.value.split("::");
                        const model = modelParts.join("::");
                        if (provider && model) {
                          setFallback([...fallbackChain, { provider, model }]);
                        }
                      }}
                    >
                      <option value="">{t("default.fallback_add")}</option>
                      {Object.entries(groupBy(avail, (model) => model.providerKey))
                        .map(([provider, models]) => (
                          <optgroup key={provider} label={provider}>
                            {models.map((model) => (
                              <option
                                key={`${provider}::${model.id}`}
                                value={`${provider}::${model.id}`}
                              >
                                {model.id}
                              </option>
                            ))}
                          </optgroup>
                        ))}
                    </select>
                    <ChevronDown size={13} aria-hidden="true" />
                  </div>
                );
              })()}
            </div>
            {/* Advanced disclosure: lightweight + judge (all follow the primary model by default) */}
            <div className={`tier-adv${advOpen ? " open" : ""}`}>
              <button type="button" className="tier-adv-head" onClick={() => setAdvOpen((v) => !v)} aria-expanded={advOpen}>
                <span className="tchev"><ChevronRight size={15} /></span>
                <span className="tlab">{t("tier.adv_label")}</span>
                <span className="tsum">{
                  // State the effective choice: the judge defaults to Auto (a different model), not the main model.
                  !fastPinned && !judgePinned
                    ? judgeAuto?.model
                      ? t("tier.adv_summary_follow", { judge: judgeAuto.model })
                      : primaryModel
                        ? t("tier.adv_summary_follow_same", { model: primaryModel.model })
                        : t("tier.adv_summary_follow_pending")
                    : t("tier.adv_summary_custom", {
                        fast: fastPinned ? `${fastPinned.provider}·${fastPinned.model}` : t("tier.follow_short"),
                        judge: judgePinned ? `${judgePinned.provider}·${judgePinned.model}` : judgeAuto?.model ?? primaryModel?.model ?? t("tier.auto_short"),
                      })
                }</span>
              </button>
              <div className="tier-adv-body">
                {/* Lightweight model */}
                <div className="tier-item">
                  <div className="tier-row"><div className="tier-label"><span className="tdot soft" /> {t("tier.fast_label")}<ConfigurationFieldHelp includeChildren field="team_model_config.tiers.fast" label={t("tier.fast_label")} /></div></div>
                  <div className="ti-desc"><span>{t("tier.fast_desc1")}</span><span>{t("tier.fast_desc2")}</span></div>
                  <div className="selbox">
                    <select
                      name="fast_model"
                      aria-label={t("tier.fast_label")}
                      value={fastPinned ? `${fastPinned.provider}::${fastPinned.model}` : FOLLOW_VALUE}
                      onChange={(e) => onPickFast(e.target.value)} disabled={busy}>
                      <option value={FOLLOW_VALUE}>{t("tier.fast_follow_option")}</option>
                      {Object.entries(groupBy(toolModels, (m) => m.providerKey)).map(([pk, list]) => (
                        <optgroup key={pk} label={pk}>
                          {list.map((m) => <option key={`${pk}::${m.id}`} value={`${pk}::${m.id}`}>{m.id}</option>)}
                        </optgroup>
                      ))}
                    </select>
                    <span className="chev"><ChevronDown size={16} /></span>
                  </div>
                  {fastPinned ? (
                    <div className="sel-effect pinned">{t("tier.fast_effect_pinned")} <span className="mchip"><span className="prov">{fastPinned.provider} ·</span>{fastPinned.model}</span></div>
                  ) : (
                    <div className="sel-effect follow">{t("tier.follow_effect")} <span className="k">{primaryModel ? `${primaryModel.provider}·${primaryModel.model}` : "—"}</span></div>
                  )}
                </div>
                {/* Judge model */}
                <div className={`tier-item${evalSameAsPrimary ? " warn" : ""}`}>
                  <div className="tier-row"><div className="tier-label"><span className="tdot soft" /> {t("tier.judge_label")}<ConfigurationFieldHelp includeChildren field="team_model_config.tiers.judge" label={t("tier.judge_label")} /></div></div>
                  <div className="ti-desc"><span>{t("tier.judge_desc1")}</span><span>{t("tier.judge_desc2")}</span></div>
                  <div className="selbox">
                    <select
                      name="judge_model"
                      aria-label={t("tier.judge_label")}
                      value={judgePinned ? `${judgePinned.provider}::${judgePinned.model}` : AUTO_VALUE}
                      onChange={(e) => onPickJudge(e.target.value)} disabled={busy}>
                      <option value={AUTO_VALUE}>{t("tier.judge_auto_option")}</option>
                      {Object.entries(groupBy(chatModels, (m) => m.providerKey)).map(([pk, list]) => (
                        <optgroup key={pk} label={pk}>
                          {list.map((m) => <option key={`${pk}::${m.id}`} value={`${pk}::${m.id}`}>{m.id}</option>)}
                        </optgroup>
                      ))}
                    </select>
                    <span className="chev"><ChevronDown size={16} /></span>
                  </div>
                  {evalSameAsPrimary ? (
                    <div className="warn-inline"><AlertTriangle size={13} /> {t("tier.judge_same_warn")}</div>
                  ) : judgePinned ? (
                    <div className="sel-effect pinned">{t("tier.judge_effect_pinned")} <span className="mchip"><span className="prov">{judgePinned.provider} ·</span>{judgePinned.model}</span></div>
                  ) : (
                    judgeAuto ? (
                      <div className="sel-effect follow">{t("tier.judge_auto_effect")} <span className="k">{`${judgeAuto.provider}·${judgeAuto.model}`}</span></div>
                    ) : primaryModel ? (
                      <div className="warn-inline"><AlertTriangle size={13} /> {t("tier.judge_auto_none", { model: primaryModel.model })}</div>
                    ) : (
                      <div className="sel-effect follow">{t("tier.judge_auto_no_primary")}</div>
                    )
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </section>

      {/* ③ Knowledge-base models */}
      <section className="block">
        <div className="block-head">
          <span className="block-n">③</span>
          <h3 className="block-title">{t("kb.title")}</h3>
        </div>
        <p className="block-desc">{t("kb.desc")}</p>
        <div className="kb-sel">
          <div className="kb-row kb-retrieval-row">
            <div className="kbl">
              <span className="configuration-field-label"><label className="n" htmlFor="kb-retrieval-count">{t("kb.retrieval_count")}</label><ConfigurationFieldHelp field="team_model_config.kb.retrieval_top_k" label={t("kb.retrieval_count")} /></span>
              <div id="kb-retrieval-count-hint" className="opt">{t("kb.retrieval_hint", { count: 15, max: 50 })}</div>
            </div>
            <div className="selbox">
              <select
                id="kb-retrieval-count"
                aria-describedby="kb-retrieval-count-hint"
                name="retrieval_top_k"
                value={config.kb.retrieval_top_k ?? 15}
                disabled={busy}
                onChange={(event) => {
                  const next = { ...config, kb: { ...config.kb, retrieval_top_k: Number(event.target.value) } };
                  setConfig(next);
                  void persist(next);
                }}
              >
                {Array.from(new Set([5, 10, 15, 20, 30, 50, config.kb.retrieval_top_k ?? 15])).sort((a, b) => a - b).map((count) => (
                  <option key={count} value={count}>{count === 15 ? t("kb.retrieval_default", { count }) : t("kb.retrieval_option", { count })}</option>
                ))}
              </select>
              <span className="chev"><ChevronDown size={15} /></span>
            </div>
          </div>
        </div>
        {embedModels.length === 0 ? (
          <div className="empty big">
            <span className="ei"><Database size={20} /></span>
            <span className="et">{t("kb.empty_title")}</span>
            <span className="es">{t("kb.empty_body")}</span>
          </div>
        ) : (
          <div className={`kb-sel${!activeEmbed ? " required" : ""}`}>
            <div className="kb-row">
              <div className="kbl"><div className="n"><Database size={14} /> {t("kb.vector")}<ConfigurationFieldHelp includeChildren field="team_model_config.kb.active.embed" label={t("kb.vector")} /></div>
                <div className={activeEmbed ? "opt" : "req"}>{activeEmbed ? t("kb.embedding") : t("kb.required_unset")}</div></div>
              <div className="selbox">
                <select
                  name="embedding_model"
                  aria-label={t("kb.vector")}
                  value={activeEmbed ? `${activeEmbed.provider}::${activeEmbed.real_id}` : ""}
                  onChange={(e) => { const [p, ...r] = e.target.value.split("::"); if (p) openRebuild(p, r.join("::")); }} disabled={busy || reindexing}>
                  {!activeEmbed ? <option value="">{t("kb.vector_placeholder")}</option> : null}
                  {embedModels.map((m) => <option key={`${m.providerKey}::${m.id}`} value={`${m.providerKey}::${m.id}`}>{modelLabel({ providerKey: m.providerKey, id: m.id })}</option>)}
                </select>
                <span className="chev"><ChevronDown size={15} /></span>
              </div>
            </div>
            {!activeEmbed ? (
              <div className="guard hard legacy-mset-guard-tight"><span className="gi"><AlertTriangle size={15} /></span>
                <div className="gt"><b>{t("kb.required_guard_title")}</b> — {t("kb.required_guard_body")}</div></div>
            ) : null}
            {reindexFailed ? (
              <div className="guard hard"><span className="gi"><AlertTriangle size={15} /></span><div className="gt">{t("errors.err_reindex_failed")}</div></div>
            ) : reindexing ? (
              <div className="kb-inflight"><span className="spin" /><span>{t("kb.inflight")}</span><span className="bar" /></div>
            ) : null}
            <div className="kb-row">
              <div className="kbl"><div className="n"><Layers size={14} /> {t("kb.rerank")}<ConfigurationFieldHelp includeChildren field="team_model_config.kb.active.rerank" label={t("kb.rerank")} /></div><div className="opt">{t("kb.rerank_opt")}</div></div>
              <div className="selbox">
                <select
                  name="rerank_model"
                  aria-label={t("kb.rerank")}
                  value={activeRerank ? `${activeRerank.provider}::${activeRerank.real_id}` : ""}
                  onChange={(e) => onPickRerank(e.target.value)} disabled={busy}>
                  <option value="">{t("kb.rerank_none")}</option>
                  {rerankModels.map((m) => <option key={`${m.providerKey}::${m.id}`} value={`${m.providerKey}::${m.id}`}>{modelLabel({ providerKey: m.providerKey, id: m.id })}</option>)}
                </select>
                <span className="chev"><ChevronDown size={15} /></span>
              </div>
            </div>
            {/* P1.5: document-image recognition (vision) + row extraction (row_extractor) — optional, do not trigger index rebuilding. */}
            <div className="kb-row">
              <div className="kbl"><div className="n"><Database size={14} /> {t("kb.vision")}<ConfigurationFieldHelp field="team_model_config.kb.active.vision" label={t("kb.vision")} /></div><div className="opt">{t("kb.vision_opt")}</div></div>
              <div className="selbox">
                <select
                  name="vision_model"
                  aria-label={t("kb.vision")}
                  value={activeVision ? `${activeVision.provider}::${activeVision.real_id}` : ""}
                  onChange={(e) => onPickKbOptional("vision", "vis", e.target.value)} disabled={busy}>
                  <option value="">{t("kb.optional_none")}</option>
                  {visionModels.map((m) => <option key={`${m.providerKey}::${m.id}`} value={`${m.providerKey}::${m.id}`}>{modelLabel({ providerKey: m.providerKey, id: m.id })}</option>)}
                </select>
                <span className="chev"><ChevronDown size={15} /></span>
              </div>
            </div>
            <div className="kb-row">
              <div className="kbl"><div className="n"><Bot size={14} /> {t("kb.row_extractor")}<ConfigurationFieldHelp field="team_model_config.kb.active.row_extractor" label={t("kb.row_extractor")} /></div><div className="opt">{t("kb.row_extractor_opt")}</div></div>
              <div className="selbox">
                <select
                  name="row_extractor_model"
                  aria-label={t("kb.row_extractor")}
                  value={activeRow ? `${activeRow.provider}::${activeRow.real_id}` : ""}
                  onChange={(e) => onPickKbOptional("row_extractor", "row", e.target.value)} disabled={busy}>
                  <option value="">{t("kb.optional_none")}</option>
                  {rowExtractorModels.map((m) => <option key={`${m.providerKey}::${m.id}`} value={`${m.providerKey}::${m.id}`}>{modelLabel({ providerKey: m.providerKey, id: m.id })}</option>)}
                </select>
                <span className="chev"><ChevronDown size={15} /></span>
              </div>
            </div>
          </div>
        )}
      </section>

      </> : null}

      {scoped && (section === "image" || section === "search") ? <>
      <DraftIssueList issues={localIssues} prefixes={[section === "image" ? "media" : "search"]} />
      <ExternalCapabilityEditor
        kind={section} providers={config.providers} value={section === "image" ? config.media : config.search}
        busy={busy} onChange={(value) => void persist({ ...config, [section === "image" ? "media" : "search"]: value })}
      /></> : null}
      {(!scoped || section === "image") ? <>
      {/* ④ Image generation (media · image_gen) */}
      <section className="block">
        <div className="block-head">
          <span className="block-n">④</span>
          <h3 className="block-title">{t("media.title")}</h3>
          <div className="block-sub">{t("media.sub")}</div>
        </div>
        <p className="block-desc">{t("media.desc")}</p>
        {mediaModelList.length === 0 ? (
          <div className="empty big">
            <span className="ei"><Palette size={20} /></span>
            <span className="et">{t("media.empty_title")}</span>
            <span className="es">{t("media.empty_body")}</span>
          </div>
        ) : (
          <div className="kb-sel">
            <div className="kb-row">
              <div className="kbl"><div className="n"><Palette size={14} /> {t("media.image_label")}<ConfigurationFieldHelp includeChildren field="team_model_config.media.active.image_gen" label={t("media.image_label")} /></div><div className="opt">{t("media.optional")}</div></div>
              <div className="selbox">
                <select
                  name="image_generation_model"
                  aria-label={t("media.image_label")}
                  value={activeImageName || ""}
                  onChange={(e) => onPickImage(e.target.value)}
                  disabled={busy}
                >
                  <option value="">{t("media.none")}</option>
                  {mediaModelList.map((m) => <option key={m.name} value={m.name}>{`${m.real_id}　·　${m.provider}`}</option>)}
                </select>
                <span className="chev"><ChevronDown size={15} /></span>
              </div>
            </div>
            {activeImage ? (
              <div className="sel-effect legacy-mset-effect-indent">{t("media.effect")}</div>
            ) : null}
          </div>
        )}
      </section>

      </> : null}
      {(!scoped || section === "search") ? <>
      {/* ⑤ Web search (search · web_search) */}
      <section className="block">
        <div className="block-head">
          <span className="block-n">⑤</span>
          <h3 className="block-title">{t("search.title")}</h3>
          <div className="block-sub">{t("search.sub")}</div>
        </div>
        <p className="block-desc">{t("search.desc")}</p>
        {searchModelList.length === 0 ? (
          <div className="empty big">
            <span className="ei"><Search size={20} /></span>
            <span className="et">{t("search.empty_title")}</span>
            <span className="es">{t("search.empty_body")}</span>
          </div>
        ) : (
          <div className="kb-sel">
            <div className="kb-row">
              <div className="kbl"><div className="n"><Search size={14} /> {t("search.model_label")}<ConfigurationFieldHelp includeChildren field="team_model_config.search.active.web_search" label={t("search.model_label")} /></div><div className="opt">{t("search.optional")}</div></div>
              <div className="selbox">
                <select
                  name="web_search_model"
                  aria-label={t("search.model_label")}
                  value={activeSearchName || ""}
                  onChange={(e) => onPickSearch(e.target.value)}
                  disabled={busy}
                >
                  <option value="">{t("search.none")}</option>
                  {searchModelList.map((m) => <option key={m.name} value={m.name}>{`${m.real_id}　·　${m.provider}`}</option>)}
                </select>
                <span className="chev"><ChevronDown size={15} /></span>
              </div>
            </div>
            {activeSearch ? (
              <div className="sel-effect legacy-mset-effect-indent">{t("search.effect")}</div>
            ) : null}
          </div>
        )}
      </section>

      </> : null}
        </div>
      ) : null}

      {/* Unified save bar for dirty drafts (tradeoff 17); "检查并应用" retains the original review flow, with this page's leave confirmation. */}
      {LLM_SETTINGS_V4_ENABLED ? <SaveBar dirty={pageMode === "edit" && isDirty && !reviewOpen} guard={false}
        message={<>{t("v4.draft_title")}<span className="ui-savebar-meta"> · {t("v4.draft_sections", { count: changedSections.length })}</span></>}
        saveLabel={t("v4.review")} onDiscard={discardDraft} onSave={() => void reviewDraft()} /> : null}

      {discovery ? (
        <DiscoveryPicker
          candidates={discovery.candidates}
          truncated={discovery.truncated}
          onAdd={addDiscoveredModels}
          onClose={() => setDiscovery(null)}
        />
      ) : null}

      {verificationSelectionOpen ? (
        <VerificationSelector
          candidates={verificationCandidates}
          error={error}
          busy={verificationBusy}
          onPlan={(selected) => void planSelectedVerification(selected)}
          onClose={sectionsEnabled ? dismissVerification : () => setVerificationSelectionOpen(false)}
          onBack={sectionsEnabled && verificationPlan && !verificationBatch ? () => { setVerificationPlan(null); setVerificationDrafts(null); } : undefined}
          workflow={sectionsEnabled && verificationPlan ? <VerificationPanel
            plan={verificationPlan} batch={verificationBatch} busy={verificationBusy}
            onConfirm={() => void confirmVerification()} onCancel={() => void cancelCurrentVerification()}
            onDismiss={dismissVerification} onRetry={() => void retryUnfinishedVerification()}
          /> : undefined}
        />
      ) : null}

      {/* ─── delete provider modal ─── */}
      {del && delInfo ? (
        <DialogShell
          onClose={() => setDel(null)}
          busy={busy}
          mount="inline"
          labelledBy="mset-del-title"
          describedBy="mset-del-body"
          returnFocusRef={delReturnFocusRef}
          backdropClassName="mset-scrim"
          panelClassName="mset-modal"
        >
          <span className="modal-ico danger">{delInfo.hardBlock ? <Slash size={18} /> : <Trash size={18} />}</span>
          <h3 id="mset-del-title" className="modal-title">{delInfo.hardBlock ? t("del.hard_title") : t("del.title", { name: del })}</h3>
          {delInfo.hardBlock ? (
            <>
              <p id="mset-del-body" className="modal-body">{t("del.hard_body")}</p>
              <div className="modal-foot"><button type="button" className="btn ghost sm" onClick={() => setDel(null)}>{t("common.cancel")}</button></div>
            </>
          ) : (
            <>
              <p id="mset-del-body" className="modal-body">{
                delInfo.refs.length > 0
                  ? (LLM_SETTINGS_V4_ENABLED ? t("v4.delete_ref_body", { count: delInfo.refs.length }) : t("del.soft_body", { count: delInfo.refs.length }))
                  : t("del.clean_body")
              }</p>
              {delInfo.refs.length > 0 ? (
                <div className="modal-warn"><AlertTriangle size={15} /><div>{delInfo.refs.slice(0, 4).join(" · ")}</div></div>
              ) : null}
              <div className="modal-foot">
                <button type="button" className="btn ghost sm" disabled={busy} onClick={() => setDel(null)}>{t("common.cancel")}</button>
                <button type="button" className="btn danger sm" disabled={busy} onClick={confirmDelete}><Trash size={13} /> {
                  LLM_SETTINGS_V4_ENABLED ? t("v4.delete_draft") : delInfo.refs.length > 0 ? t("del.confirm_refs") : t("del.confirm")
                }</button>
              </div>
            </>
          )}
        </DialogShell>
      ) : null}

      {LLM_SETTINGS_V4_ENABLED && reviewOpen ? (
        <DialogShell
          presentation="center"
          onClose={() => setReviewOpen(false)}
          busy={busy}
          mount="inline"
          labelledBy="mset-review-title"
          backdropClassName="mset-scrim review-scrim"
          panelClassName="review-panel"
        >
          <div className="review-head">
            <div>
              <span className="review-kicker">{t("v4.review_kicker")}</span>
              <h2 id="mset-review-title">{t("v4.review_title")}</h2>
              <p>{t("v4.review_sub")}</p>
            </div>
            <button type="button" className="iconbtn" aria-label={t("v4.close")} onClick={() => setReviewOpen(false)}><XSm size={17} /></button>
          </div>
          <div className="review-body">
            {reviewStale ? (
              <div className="review-stale" role="alert">
                <AlertTriangle size={15} />
                <div>
                  <b>{t("v4.review_stale_title")}</b>
                  <span>{t(reviewStale === "conflict"
                    ? "v4.review_conflict_body"
                    : "v4.review_evidence_stale_body")}</span>
                </div>
              </div>
            ) : null}
            <section className="review-section">
              <h3>{t("v4.changed")}</h3>
              <div className="change-chips">
                {changedSections.map((section) => <span key={section}>{t(`v4.section_${section}`)}</span>)}
              </div>
            </section>
            <section className="review-section" aria-live="polite">
              <h3>{t("v4.validation")}</h3>
              {busy && !validation ? (
                <div className="review-neutral"><Spinner size={14} /> {t("v4.validating")}</div>
              ) : error ? (
                <div className="review-issues">
                  <div><AlertTriangle size={14} /><span>{error}</span></div>
                </div>
              ) : reviewIssues.length > 0 ? (
                <div className="review-issues">
                  {reviewIssues.map((issue) => (
                    <div key={`${issue.path}:${issue.message}`}><AlertTriangle size={14} /><span>{issue.message}</span></div>
                  ))}
                </div>
              ) : (
                <div className="review-ok"><Check size={14} /> {t("v4.validation_ok")}</div>
              )}
              {reviewWarnings.length > 0 ? (
                <div className="review-warnings">
                  {reviewWarnings.map((issue) => (
                    <div key={`${issue.path}:${issue.code}`}><Info size={13} /><ModelSettingsValidationIssueText issue={issue} /></div>
                  ))}
                </div>
              ) : null}
            </section>
            {v2Enabled && readiness ? (
              <ReadinessReview
                readiness={readiness}
                acceptedTokens={acceptedRiskTokens}
                migrationAccepted={migrationAccepted}
                onToggleAcceptance={(token) => {
                  setAcceptedRiskTokens((current) => {
                    const next = new Set(current);
                    if (next.has(token)) next.delete(token);
                    else next.add(token);
                    return next;
                  });
                }}
                onToggleMigration={() => setMigrationAccepted((current) => !current)}
                disabled={Boolean(reviewStale)}
              />
            ) : null}
            {(!scoped || section === "connection") ? <section className="review-section">
              <h3>{v2Enabled ? t("v2.evidence_title") : t("v4.connection_evidence")}</h3>
              <div className="evidence-list">
                {providerEntries.filter(([key]) => !scoped || section === "connection" && key === (connect?.origKey || connect?.name.trim())).map(([key, spec]) => {
                  const evidence = providerTests[key];
                  const providerId = draftProviderIds[key];
                  const projectedEvidence = (data?.verification?.entries || []).filter(
                    (entry) => entry.provider_id === providerId,
                  );
                  const signature = providerSignature(
                    (spec.type || "openai_compatible") as ProviderType,
                    spec.endpoint || spec.base_url || "",
                    String(spec.messages_path || ""),
                    [
                      ...normModels(spec),
                      ...imageModelsByProvider(key).map((model) => ({
                        id: model.real_id,
                        caps: [IMAGE_CAP],
                        protocol: model.protocol,
                      })),
                      ...searchModelsByProvider(key).map((model) => ({
                        id: model.real_id,
                        caps: [SEARCH_CAP],
                        protocol: model.protocol,
                      })),
                    ],
                    String(spec.api_key || fingerprints[key] || "saved-secret"),
                    {
                      endpoint: spec.endpoint,
                      apiVersion: spec.api_version,
                      auth: spec.auth,
                      transports: spec.transports,
                    },
                  );
                  const state = v2Enabled
                    ? projectedEvidence.length === 0
                      ? "untested"
                      : projectedEvidence.some((entry) => entry.fact_state === "failed")
                        ? "failed"
                        : projectedEvidence.every((entry) => entry.fact_state === "verified")
                          ? "passed"
                          : "stale"
                    : !evidence
                      ? "untested"
                      : evidence.signature === signature
                        ? evidence.status
                        : "stale";
                  return <div key={key}><span>{key}</span><b className={state}>{t(`v4.test_${state}`)}</b></div>;
                })}
              </div>
            </section> : null}
            {reindexRequested ? (
              <section className="review-section impact">
                <h3>{t("v4.reindex_title")}</h3>
                <p>{t("v4.reindex_body", { count: validation?.impact?.active_files ?? "—" })}</p>
                <label className="review-check">
                  <input
                    type="checkbox"
                    disabled={Boolean(reviewStale)}
                    name="confirm_embedding_reindex"
                    checked={applyReindexConfirmed}
                    onChange={(event) => setApplyReindexConfirmed(event.target.checked)}
                  />
                  <span>{t("v4.reindex_confirm")}</span>
                </label>
              </section>
            ) : null}
          </div>
          <div className="review-foot">
            {reviewStale ? (
              <>
                <button type="button" className="btn ghost sm" onClick={() => setReviewOpen(false)}>{t("v4.back")}</button>
                <button type="button" className="btn pri sm" disabled={busy} onClick={() => void recheckStaleReview()}>
                  {busy ? <Spinner size={13} /> : <RefreshCw size={13} />} {t("v4.review_recheck")}
                </button>
              </>
            ) : (
              <>
                <button type="button" className="btn ghost sm" onClick={discardDraft}>{t("v4.discard")}</button>
                <button type="button" className="btn ghost sm" onClick={() => setReviewOpen(false)}>{t("v4.back")}</button>
                <button
                  type="button"
                  className="btn pri sm"
                  disabled={
                    busy
                    || !validation?.valid
                    || reviewIssues.length > 0
                    || (reindexRequested && !applyReindexConfirmed)
                    || (v2Enabled && (
                      !readiness
                      || readiness.blocking.length > 0
                      || !riskAcceptancesComplete
                      || !migrationAcceptanceComplete
                    ))
                  }
                  onClick={() => void applyDraft()}
                >
                  {busy ? <Spinner size={13} /> : <Check size={13} />} {t("v4.apply")}
                </button>
              </>
            )}
          </div>
        </DialogShell>
      ) : null}

      {/* ─── rebuild-index modal ─── */}
      {rebuild ? (
        <DialogShell
          onClose={() => { setRebuild(null); setRebuildOk(false); }}
          busy={busy}
          mount="inline"
          labelledBy="mset-rebuild-title"
          describedBy="mset-rebuild-body"
          backdropClassName="mset-scrim"
          panelClassName="mset-modal"
        >
          <span className="modal-ico danger"><Database size={18} /></span>
          <h3 id="mset-rebuild-title" className="modal-title">{t("kb.rebuild_title")}</h3>
          <p id="mset-rebuild-body" className="modal-body">{t("kb.rebuild_body", { model: rebuild.id })}</p>
          <div className="fld">
            <span className="configuration-field-label"><label htmlFor="mset-embedding-dim">{t("kb.dim_label")}</label><ConfigurationFieldHelp field="team_model_config.kb.models.*.dim" label={t("kb.dim_label")} /></span>
            <input id="mset-embedding-dim" name="embedding_dim" className="inp mono" inputMode="numeric" value={rebuildDim} placeholder="1024"
              onChange={(e) => setRebuildDim(e.target.value.replace(/[^0-9]/g, ""))} />
            <span className="hint">{t("kb.dim_hint")}</span>
          </div>
          {!LLM_SETTINGS_V4_ENABLED ? (
            <div className={`mcheck${rebuildOk ? " on" : ""}`} role="checkbox" aria-checked={rebuildOk} tabIndex={0}
              onClick={() => setRebuildOk((v) => !v)} onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") setRebuildOk((v) => !v); }}>
              <span className="box"><Check size={12} /></span> {t("kb.rebuild_confirm_check")}
            </div>
          ) : null}
          <div className="modal-foot">
            <button type="button" className="btn ghost sm" disabled={busy} onClick={() => { setRebuild(null); setRebuildOk(false); }}>{t("common.cancel")}</button>
            <button
              type="button"
              className={`btn ${LLM_SETTINGS_V4_ENABLED ? "pri" : "danger"} sm${(LLM_SETTINGS_V4_ENABLED || rebuildOk) && rebuildDim ? "" : " dis"}`}
              disabled={(!LLM_SETTINGS_V4_ENABLED && !rebuildOk) || !rebuildDim || busy}
              onClick={confirmRebuild}
            >
              <RefreshCw size={13} /> {LLM_SETTINGS_V4_ENABLED ? t("v4.stage_embedding") : t("kb.rebuild_confirm")}
            </button>
          </div>
        </DialogShell>
      ) : null}
      {confirmDialog}
    </div></ConfigurationFieldProvider>
  );
}

export function ModelSettingsValidationIssueText({
  issue,
}: {
  issue: LlmConfigValidationIssue;
}) {
  const t = useTranslations("settings.models");
  return (
    <span>
      {t(
        `errors.${modelSettingsValidationErrorKey(
          issue.code,
          issue.severity,
        )}`,
      )}
    </span>
  );
}

// ─── page head ─────────────────────────────────────────────────────
function PageHead({
  t,
  titleKey,
  subtitleKey,
  backLabelKey,
  eyebrowKey,
  showDraftNote = true,
  v4,
  mode,
  isDirty = false,
  onEdit,
  onBack,
  actionsDisabled = false,
  backDisabled = false,
}: {
  t: ReturnType<typeof useTranslations>;
  titleKey?: string;
  subtitleKey?: string;
  backLabelKey?: string;
  eyebrowKey?: string;
  showDraftNote?: boolean;
  v4: boolean;
  mode: "overview" | "edit";
  isDirty?: boolean;
  onEdit?: () => void;
  onBack?: () => void;
  actionsDisabled?: boolean;
  backDisabled?: boolean;
}) {
  return (
    <div className="ph">
      <div className="ph-main">
        <div className="ph-eyebrow"><Users size={12} /> {t(eyebrowKey || "eyebrow")}</div>
        <h1 className="ph-title ui-page-title">{t(titleKey || "title")}</h1>
        <p className="ph-sub ui-page-desc">
          {t(subtitleKey || (!v4
            ? "subtitle"
            : mode === "overview"
              ? "overview.page_subtitle"
              : "v4.editing_note"))}
        </p>
      </div>
      {v4 ? (
        <div className="ph-actions">
          {mode === "overview" ? (
            onEdit ? (
              <button
                type="button"
                className="btn pri sm"
                disabled={actionsDisabled}
                onClick={onEdit}
              >
                <Edit size={13} /> {t("overview.edit_configuration")}
              </button>
            ) : null
          ) : (
            <>
              {showDraftNote ? <span className={`draft-model-note${isDirty ? " is-dirty" : ""}`}>
                {t(isDirty ? "overview.draft_changed" : "overview.draft_clean")}
              </span> : null}
              {onBack ? (
                <button
                  type="button"
                  className="btn sec sm"
                  disabled={backDisabled}
                  onClick={onBack}
                >
                  {t(backLabelKey || "overview.back_to_overview")}
                </button>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

// ─── connect panel ─────────────────────────────────────────────────
/** Exported only for testing: R3 review showed inferring manual edits from patch keys mislabels internal component updates;
 *  only rendering the real panel and clicking real buttons catches this bug (pure-function guards cannot lock call sites). */
export function ConnectPanel(props: {
  t: ReturnType<typeof useTranslations>;
  draft: ConnectDraft;
  modelsOptional?: boolean;
  caps: readonly string[];
  busy: boolean;
  saveEnabled: boolean;
  /** Outbound URL policy (`local` / `saas`). When unknown, URL hints omit format guidance — wrong guesses mislead users. */
  deploymentMode?: "local" | "saas";
  /** **Sole** reason Save is disabled (same source as saveEnabled; see `saveBlockReason`). */
  saveBlocked: SaveBlockReason | null;
  testEnabled: boolean;
  capLabel: (c: string) => string;
  onPatch: (p: Partial<ConnectDraft>) => void;
  onToggleCap: (idx: number, cap: string) => void;
  onTest: () => void;
  onAutoFill: () => void;
  discoveringModels: boolean;
  onSave: () => void;
  onCancel: () => void;
  v2: boolean;
  azureEnabled: boolean;
  customHttpEnabled: boolean;
  /** Batch 1-d: the user **manually edited** a copilot field (protect it from subsequent generation overwrites).
   *  ★Actual user-input callbacks must invoke this explicitly; **do not** infer from patch keys — protocol-button patches
   *  include internally calculated `models`/`apiVersion`; key-based markers permanently and silently prevent copilot from filling those
   *  fields (R3 review P1-1; `scheduled.tsx`'s 12 callbacks use explicit invocation). */
  onTouch?: (field: "protocol" | "name" | "baseUrl" | "apiVersion" | "messagesPath" | "auth" | "models") => void;
  /** Batch 1-d: copilot "智能填写" slot. The parent constructs the panel (draft state and direct filling live there);
   *  this component only positions it — decisions belong at the layer owning the shape conditions (batch 1-b lesson). */
  verification?: React.ReactNode;
  aiFill?: React.ReactNode;
  /** Runtime auto-adaptation observations (`GET .../outbound-adaptations`). Normally an empty array.
   *  Same layering as aiFill: parent fetches data; panel only renders. */
  adaptations?: readonly OutboundAdaptation[];
  parameterProfiles?: ParameterProfile[];
  previewKey?: string;
  onParameterPreview?: (modelId: string, capability: string) => Promise<ParameterPreview>;
}) {
  const tKeyField = useTranslations("provider_key_field");
  const locale = useLocale();
  const {
    t,
    draft,
    modelsOptional,
    caps,
    adaptations,
    busy,
    saveEnabled,
    deploymentMode,
    saveBlocked,
    testEnabled,
    capLabel,
    onPatch,
    onToggleCap,
    onTest,
    onAutoFill,
    discoveringModels,
    onSave,
    onCancel,
    v2,
    azureEnabled,
    customHttpEnabled,
    verification,
  aiFill,
    onTouch,
  } = props;
  const touch = onTouch ?? (() => {});
  const hasManualCaps = draft.models.some((m) => m.source === "manual" && m.caps.length > 0);
  const hasUnknownCapabilities = draft.models.some((model) =>
    model.id.trim() && !isPureSpecialCap(model.caps)
    && !model.caps.some((capability) =>
      VERIFIABLE_CAPABILITIES.has(capability as ModelCapability)));
  const hasDuplicateModelIds = !connectDraftModelIdsUnique(draft.models);
  /** Unified row-editing entry: user edits a model row → mark kept before storing the value (R3 review P1-1). */
  const patchModelRows = (models: DraftModel[]) => {
    touch("models");
    onPatch({ models, test: undefined });
  };
  const addRow = () => patchModelRows([...draft.models, { id: "", caps: v2 ? [] : ["chat"], source: "manual" }]);
  const setRowId = (idx: number, id: string) =>
    patchModelRows(
      draft.models.map((model, index) =>
        index === idx
          ? { ...model, id, caps: model.caps.length ? model.caps : suggestedCapabilitiesForModelId(id) }
          : model),
    );
  const setRowDeployment = (idx: number, deployment: string) =>
    patchModelRows(draft.models.map((model, index) => (index === idx ? { ...model, deployment } : model)));
  const setRowApiSurface = (idx: number, apiSurface: string) =>
    patchModelRows(draft.models.map((model, index) => (index === idx ? { ...model, apiSurface: apiSurface === "responses" ? "responses" : undefined } : model)));
  // Reviewed catalog for prefilling the window; an unread catalog just leaves the field empty.
  const [windowCatalog, setWindowCatalog] = useState<ContextWindowCatalog | null>(null);
  useEffect(() => {
    let active = true;
    void Promise.resolve()
      .then(() => fetchContextWindowCatalog())
      .then((catalog) => { if (active && catalog && typeof catalog.default === "number") setWindowCatalog(catalog); })
      .catch(() => { /* optional prefill */ });
    return () => { active = false; };
  }, []);
  const setRowContextWindow = (idx: number, raw: string) => {
    const value = raw === "" ? undefined : Number(raw);
    if (value !== undefined && (!Number.isSafeInteger(value) || value <= 0)) return;
    patchModelRows(draft.models.map((model, index) => (index === idx ? { ...model, contextWindow: value } : model)));
  };
  const setRowCreditRate = (idx: number, kind: keyof CreditRates, raw: string) =>
    patchModelRows(draft.models.map((model, index) => index === idx
      ? { ...model, creditRates: { ...DEFAULT_CREDIT_RATES, ...model.creditRates, [kind]: raw } }
      : model));
  const setRowRequestBodyOverrides = (idx: number, value: string) =>
    patchModelRows(draft.models.map((model, index) => (index === idx ? { ...model, requestBodyOverrides: value } : model)));
  const setRowOutboundParams = (idx: number, next: OutboundParams) =>
    patchModelRows(draft.models.map((model, index) => (index === idx ? { ...model, outboundParams: next } : model)));
  const setRowProtocol = (idx: number, protocol: string) =>
    patchModelRows(draft.models.map((m, i) => (i === idx ? { ...m, protocol } : m)));
  const removeRow = (idx: number) => patchModelRows(draft.models.filter((_, i) => i !== idx));
  // Protocols form a single-choice group (role="radiogroup"; arrow-key movement selects); clicking the selected item also resets verification, matching original buttons.
  const selectProtocol = (protocol: ConnectDraft["protocol"]) => {
    touch("protocol");
    if (protocol === "anthropic_compatible") {
      onPatch({ protocol, test: undefined, models: draft.models.map((m) => ({ ...m, caps: m.caps.filter((c) => c !== "embed" && c !== "rerank") })) });
    } else if (protocol === "azure_openai") {
      onPatch({
        protocol,
        apiVersion: draft.apiVersion || DEFAULT_AZURE_API_VERSION,
        models: draft.models.map((model) => ({
          ...model,
          caps: model.caps.filter((capability) =>
            DECLARATIVE_HTTP_CAPS.includes(capability as never)),
        })),
        test: undefined,
      });
    } else if (protocol === "custom_http") {
      onPatch({
        protocol,
        models: draft.models.map((model) => ({
          ...model,
          caps: model.caps.filter((capability) =>
            DECLARATIVE_HTTP_CAPS.includes(capability as never)),
        })),
        test: undefined,
      });
    } else {
      onPatch({ protocol, test: undefined });
    }
  };
  // Image-only source: image rows only, no chat model — allow saving (inform users).
  const pureImage = draft.models.some((m) => m.id.trim() && m.caps.includes(IMAGE_CAP))
    && !draft.models.some((m) => m.id.trim() && !isPureSpecialCap(m.caps));
  const pureSearch = draft.models.some((m) => m.id.trim() && m.caps.includes(SEARCH_CAP))
    && !draft.models.some((m) => m.id.trim() && !isPureSpecialCap(m.caps));

  const testResult = draft.test;
  const capabilitySteps = uniqueCapabilitySteps(testResult?.steps);
  const testSummary = modelCapabilityTestSummary(testResult);
  const summary = () => {
    if (!testResult || !testSummary) return null;
    if (testSummary.state === "connection_failed") return t(`verify.${testResult.code}`);
    if (testSummary.state === "connection_only") return t("verify.connection_only");
    if (testSummary.state === "capability_failed") {
      return `${t("verify.connection_verified")}；${testSummary.failed}/${testSummary.total} ${t("verify.capability_failed_suffix")}`;
    }
    if (testSummary.state === "capability_degraded") {
      return t("verify.capability_degraded", { degraded: testSummary.degraded, total: testSummary.total });
    }
    return t(`verify.${testSummary.state}`, { failed: testSummary.failed, total: testSummary.total });
  };
  return (
    <div className="connect">
      <div className="connect-head"><Plug size={16} /><span className="t">{t("connect.title")}</span>
        <span className="step">{connectDraftHasModel(draft.models) ? t("connect.step2") : t("connect.step1")}</span></div>
      {/* Batch 1-d completion: patchConnect is entirely a no-op while a verification batch runs, but inputs were not disabled —
          typed text vanished silently (and copilot retained a Manually Edited protection). A fieldset disables
          all form controls together, aligning values and protection. */}
      <fieldset className="connect-body legacy-mset-connect-fieldset" disabled={busy}>
        {aiFill ? <div className="ccp">{aiFill}</div> : null}
        <div className="fld"><span className="configuration-field-label"><label>{t("connect.protocol")}</label><ConfigurationFieldHelp field="team_model_config.providers.*.type" label={t("connect.protocol")} /></span>
          {/* Protocol switches filter model capabilities: arrowing through Anthropic deletes embed/rerank,
              so arrows only move focus; space / enter / click switches protocols. */}
          <RadioGroup
            className="proto-sel"
            orientation="both"
            activation="manual"
            aria-label={t("connect.protocol")}
            value={draft.protocol}
            onValueChange={(protocol) => selectProtocol(protocol as ConnectDraft["protocol"])}
          >
            <Radio value="openai_compatible" className={`proto-opt${draft.protocol === "openai_compatible" ? " on" : ""}`}>
              <span className="pn">{t("proto.openai")}</span><span className="ps">openai-compat · {t("connect.default")}</span></Radio>
            <Radio value="anthropic_compatible" className={`proto-opt${draft.protocol === "anthropic_compatible" ? " on" : ""}`}>
              <span className="pn">{t("proto.anthropic")}</span><span className="ps">anthropic-compat</span></Radio>
            {v2 && azureEnabled ? (
              <Radio value="azure_openai" className={`proto-opt${draft.protocol === "azure_openai" ? " on" : ""}`}>
                <span className="pn">{t("proto.azure")}</span>
                <span className="ps">azure-openai</span>
              </Radio>
            ) : null}
            {v2 && customHttpEnabled ? (
              <Radio value="custom_http" className={`proto-opt${draft.protocol === "custom_http" ? " on" : ""}`}>
                <span className="pn">{t("proto.custom_http")}</span>
                <span className="ps">{t("connect.advanced")}</span>
              </Radio>
            ) : null}
          </RadioGroup>
          <div className="proto-hint">
            <Info size={13} />
            <span>
              {draft.protocol === "azure_openai"
                ? t("connect.proto_hint_azure")
                : draft.protocol === "custom_http"
                  ? t("connect.proto_hint_custom")
                  : t("connect.proto_hint")}
            </span>
          </div>
        </div>
        <div className="fld"><span className="configuration-field-label"><label htmlFor="mset-provider-name">{v2 ? t("v2.runtime_key") : t("connect.name")}<RequiredMark /></label><ConfigurationFieldHelp field="team_model_config.providers.*.display_name" label={t("connect.name")} /></span>
          <input
            id="mset-provider-name"
            name="provider_name"
            className="inp"
            aria-required="true"
            value={draft.name}
            placeholder={v2 ? "corp-openai" : t("connect.name_ph")}
            disabled={LLM_SETTINGS_V4_ENABLED && draft.mode === "edit"}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => { touch("name"); onPatch({ name: e.target.value, test: undefined }); }}
          />
          {LLM_SETTINGS_V4_ENABLED && draft.mode === "edit"
            ? <span className="hint">{t("v4.provider_id_immutable")}</span>
            : v2
              ? <span className="hint">{t("v2.runtime_key_hint")}</span>
              : null}</div>
        <div className="fld"><span className="configuration-field-label"><label htmlFor="mset-provider-base-url">
          {draft.protocol === "azure_openai"
            ? t("sources.base_url")
            : draft.protocol === "custom_http"
              ? t("connect.custom_base_url")
              : t("sources.base_url")}
          {/* custom_http templates may contain a complete URL, allowing empty base_url (the label already says "可选"). */}
          {draft.protocol === "custom_http" ? null : <RequiredMark />}
        </label><ConfigurationFieldHelp field="team_model_config.providers.*.base_url" label={t("sources.base_url")} /></span>
          <input
            id="mset-provider-base-url"
            name="provider_base_url"
            className="inp mono"
            type="url"
            aria-required={draft.protocol !== "custom_http"}
            value={draft.baseUrl}
            placeholder={draft.protocol === "azure_openai"
              ? "https://api.example.com"
              : "https://…/v1"}
            autoComplete="url"
            spellCheck={false}
            onChange={(e) => { touch("baseUrl"); onPatch({ baseUrl: e.target.value, test: undefined }); }}
          />
          <span className="hint">
            {draft.protocol === "azure_openai"
              ? t("connect.azure_endpoint_hint")
              : draft.protocol === "custom_http"
                ? t("connect.custom_base_url_hint")
                : t("connect.base_url_hint")}
            {/* ★ URL format follows **deployment mode**, never our assumptions: `local` (default for private deployments)
                allows HTTP / internal networks / bare IPs; `saas` always requires public HTTPS.
                Say **nothing** when mode is unknown — wrong guesses mislead users. */}
            {deploymentMode
              ? ` ${t(`connect.base_url_scope_${deploymentMode}`)}`
              : null}
          </span>
          {/* Whether a vendor prefix like /openai or /v1 belongs in base_url is
              the single most common setup mistake, and the adapter's appended
              path is invisible in the form. Show the URL that will actually be
              called so the answer needs no docs. */}
          {(
            draft.protocol === "custom_http"
              ? Boolean(
                  draft.customChatUrl.trim()
                  && customHttpBaseUrlSatisfied(
                    draft.baseUrl,
                    [draft.customChatUrl],
                  ),
                )
              : Boolean(draft.baseUrl.trim())
          ) ? (
            <span className="hint mono" data-testid="provider-effective-endpoint">
              {t("connect.effective_endpoint")}
              {" "}
              {effectiveEndpoint(
                draft.protocol,
                draft.baseUrl,
                draft.messagesPath,
                draft.models[0]?.deployment || draft.models[0]?.id,
                draft.apiVersion,
                draft.customChatUrl,
              )}
            </span>
          ) : null}</div>
        {draft.protocol === "azure_openai" ? (
          <div className="fld">
            <span className="configuration-field-label"><label htmlFor="mset-provider-api-version">API Version<RequiredMark /></label><ConfigurationFieldHelp field="team_model_config.providers.*.api_version" label="API Version" /></span>
            <input
              id="mset-provider-api-version"
              name="provider_api_version"
              className="inp mono"
              value={draft.apiVersion}
              placeholder={DEFAULT_AZURE_API_VERSION}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => { touch("apiVersion"); onPatch({
                apiVersion: event.target.value,
                test: undefined,
              }); }}
            />
            <span className="hint">{t("connect.azure_api_version_hint")}</span>
          </div>
        ) : null}
        {draft.protocol === "azure_openai" || draft.protocol === "custom_http" ? (
          <div className="fld">
            <label htmlFor="mset-provider-auth-type">{t("connect.auth_type")}</label>
            <div className="selbox">
              <select
                id="mset-provider-auth-type"
                name="provider_auth_type"
                value={draft.authType}
                onChange={(event) => { touch("auth"); onPatch({
                  authType: event.target.value as ConnectDraft["authType"],
                  test: undefined,
                }); }}
              >
                <option value="bearer">Authorization: Bearer</option>
                <option value="api_key">api-key</option>
                {draft.protocol === "custom_http"
                  ? <option value="secret_header">{t("connect.secret_header")}</option>
                  : null}
                {draft.protocol === "custom_http"
                  ? <option value="none">{t("connect.no_auth")}</option>
                  : null}
              </select>
              <span className="chev"><ChevronDown size={15} /></span>
            </div>
          </div>
        ) : null}
        {draft.protocol === "custom_http" && draft.authType === "secret_header" ? (
          <div className="fld">
            <label htmlFor="mset-provider-auth-header">{t("connect.secret_header_name")}</label>
            <input
              id="mset-provider-auth-header"
              name="provider_auth_header"
              className="inp mono"
              value={draft.authHeaderName}
              placeholder="X-API-Key"
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onPatch({
                authHeaderName: event.target.value,
                test: undefined,
              })}
            />
            <label htmlFor="mset-provider-auth-prefix">{t("connect.secret_header_prefix")}</label>
            <input
              id="mset-provider-auth-prefix"
              name="provider_auth_prefix"
              className="inp mono"
              value={draft.authPrefix}
              placeholder="Bearer "
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onPatch({
                authPrefix: event.target.value,
                test: undefined,
              })}
            />
          </div>
        ) : null}
        {draft.protocol !== "custom_http" || draft.authType !== "none" ? (
          <div className="fld">
            <label htmlFor="mset-provider-api-key">
              {draft.protocol === "custom_http" && draft.authType === "secret_header"
                ? t("connect.secret_value")
                : t("connect.api_key")}
            </label>
            <input
              id="mset-provider-api-key"
              name="provider_api_key"
              className="inp mono"
              type="password"
              value={draft.apiKey}
              autoComplete="new-password"
              spellCheck={false}
              placeholder={draft.mode === "edit" ? tKeyField("keep_existing_placeholder") : "sk-…"}
              onChange={(e) => onPatch({ apiKey: e.target.value, test: undefined })}
            />
          </div>
        ) : null}
        {draft.protocol === "anthropic_compatible" ? (
          <div className="fld"><span className="configuration-field-label"><label htmlFor="mset-provider-messages-path">messages_path</label><ConfigurationFieldHelp field="team_model_config.providers.*.messages_path" label="messages_path" /></span>
            <input id="mset-provider-messages-path" name="provider_messages_path" className="inp mono" value={draft.messagesPath} placeholder="/messages"
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => { touch("messagesPath"); onPatch({ messagesPath: e.target.value, test: undefined }); }} />
            <span className="hint">{t("connect.messages_path_hint")}</span></div>
        ) : null}
        {draft.protocol === "custom_http" ? (
          <div className="fld custom-http-contract">
            <label htmlFor="mset-custom-chat-url">{t("connect.chat_url_template")}</label>
            <input
              id="mset-custom-chat-url"
              name="custom_chat_url_template"
              className="inp mono"
              value={draft.customChatUrl}
              placeholder="{base_url}/chat/completions"
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onPatch({
                customChatUrl: event.target.value,
                test: undefined,
              })}
            />
            <label htmlFor="mset-custom-chat-contract">{t("connect.response_contract")}</label>
            <div className="selbox">
              <select
                id="mset-custom-chat-contract"
                name="custom_chat_response_contract"
                value={draft.customChatContract}
                onChange={(event) => onPatch({
                  customChatContract: event.target.value as ConnectDraft["customChatContract"],
                  test: undefined,
                })}
              >
                <option value="openai_chat_completions_sse">OpenAI Chat Completions · SSE</option>
                <option value="openai_chat_completions">OpenAI Chat Completions · JSON</option>
              </select>
              <span className="chev"><ChevronDown size={15} /></span>
            </div>
            <div className="custom-http-pair">
              <div>
                <label htmlFor="mset-custom-chat-query">{t("connect.query_json")}</label>
                <textarea
                  id="mset-custom-chat-query"
                  name="custom_chat_query"
                  className="inp mono"
                  value={draft.customChatQuery}
                  rows={3}
                  spellCheck={false}
                  onChange={(event) => onPatch({
                    customChatQuery: event.target.value,
                    test: undefined,
                  })}
                />
              </div>
              <div>
                <label htmlFor="mset-custom-chat-headers">{t("connect.headers_json")}</label>
                <textarea
                  id="mset-custom-chat-headers"
                  name="custom_chat_headers"
                  className="inp mono"
                  value={draft.customChatHeaders}
                  rows={3}
                  spellCheck={false}
                  onChange={(event) => onPatch({
                    customChatHeaders: event.target.value,
                    test: undefined,
                  })}
                />
              </div>
            </div>
            <label htmlFor="mset-custom-chat-body">{t("connect.chat_body_template")}</label>
            <textarea
              id="mset-custom-chat-body"
              name="custom_chat_body_template"
              className="inp mono custom-http-template"
              value={draft.customChatBody}
              rows={9}
              spellCheck={false}
              onChange={(event) => onPatch({
                customChatBody: event.target.value,
                test: undefined,
              })}
            />
            {parseJsonObject(draft.customChatBody)
              ? null
              : <span className="hint field-error">{t("connect.json_object_required")}</span>}
            {draft.models.some((model) => model.caps.includes("embed")) ? (
              <>
                <label htmlFor="mset-custom-embed-url">{t("connect.embed_url_template")}</label>
                <input
                  id="mset-custom-embed-url"
                  name="custom_embed_url_template"
                  className="inp mono"
                  value={draft.customEmbedUrl}
                  placeholder="{base_url}/embeddings"
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => onPatch({
                    customEmbedUrl: event.target.value,
                    test: undefined,
                  })}
                />
                <div className="custom-http-pair">
                  <div>
                    <label htmlFor="mset-custom-embed-query">{t("connect.query_json")}</label>
                    <textarea
                      id="mset-custom-embed-query"
                      name="custom_embed_query"
                      className="inp mono"
                      value={draft.customEmbedQuery}
                      rows={3}
                      spellCheck={false}
                      onChange={(event) => onPatch({
                        customEmbedQuery: event.target.value,
                        test: undefined,
                      })}
                    />
                  </div>
                  <div>
                    <label htmlFor="mset-custom-embed-headers">{t("connect.headers_json")}</label>
                    <textarea
                      id="mset-custom-embed-headers"
                      name="custom_embed_headers"
                      className="inp mono"
                      value={draft.customEmbedHeaders}
                      rows={3}
                      spellCheck={false}
                      onChange={(event) => onPatch({
                        customEmbedHeaders: event.target.value,
                        test: undefined,
                      })}
                    />
                  </div>
                </div>
                <label htmlFor="mset-custom-embed-body">{t("connect.embed_body_template")}</label>
                <textarea
                  id="mset-custom-embed-body"
                  name="custom_embed_body_template"
                  className="inp mono custom-http-template"
                  value={draft.customEmbedBody}
                  rows={5}
                  spellCheck={false}
                  onChange={(event) => onPatch({
                    customEmbedBody: event.target.value,
                    test: undefined,
                  })}
                />
              </>
            ) : null}
            {(
              parseStringRecord(draft.customChatQuery)
              && parseStringRecord(draft.customChatHeaders)
              && (
                !draft.models.some((model) => model.caps.includes("embed"))
                || (
                  parseStringRecord(draft.customEmbedQuery)
                  && parseStringRecord(draft.customEmbedHeaders)
                  && parseJsonObject(draft.customEmbedBody)
                )
              )
            )
              ? null
              : <span className="hint field-error">{t("connect.json_object_required")}</span>}
            <span className="hint">{t("connect.custom_http_safety_hint")}</span>
          </div>
        ) : null}
        {/* Every model must declare capabilities; independent external-service connections may save URL and authentication first. */}
        <div className="fld"><span className="configuration-field-label"><label>{t("connect.catalog")}{!modelsOptional ? <RequiredMark /> : null}</label><ConfigurationFieldHelp field="team_model_config.providers.*.models" label={t("connect.catalog")} /></span>
          <div className="catalog-tools">
            <span className="lab">{t("connect.catalog_hint")}</span>
            <button
              type="button"
              className="mini-btn"
              disabled={
                busy
                || discoveringModels
                || (v2 && draft.protocol !== "openai_compatible")
                || !draft.name.trim()
                || (v2 && !V2_RUNTIME_KEY.test(draft.name.trim()))
                || !draft.baseUrl.trim()
                || (draft.mode === "add" && !draft.apiKey.trim())
              }
              onClick={onAutoFill}
            >
              <RefreshCw size={12} /> {v2 ? t("v2.discover_models") : t("connect.auto_fill")}
            </button>
            <button type="button" className="mini-btn" onClick={addRow}><Plus size={12} /> {t("connect.add_row")}</button>
          </div>
          {draft.models.length === 0 ? (
            <div className="empty"><span className="ei"><Cpu size={18} /></span><span className="et">{t("connect.catalog_empty_title")}</span><span className="es">{t("connect.catalog_empty_body")}</span></div>
          ) : (
            <div className="medit">
              {draft.models.map((m, idx) => (
                <div key={idx} className="medit-row">
                  <div className="medit-id">
                    <span className="cat-src">{m.source === "manual" ? t("connect.src_manual") : t("connect.src_preset")}</span>
                    <ConfigurationFieldHelp field="team_model_config.providers.*.models.*.id" label={t("connect.catalog")} />
                    <input
                      className="mid"
                      name={`provider_model_${idx}`}
                      aria-label={`${t("connect.catalog")} ${idx + 1}`}
                      value={m.id}
                      placeholder="model-id"
                      autoComplete="off"
                      spellCheck={false}
                      onChange={(e) => setRowId(idx, e.target.value)}
                    />
                  </div>
                  {draft.protocol === "azure_openai"
                    || draft.protocol === "custom_http" ? (
                    <div className="medit-proto">
                      <span className="configuration-field-label"><span className="mp-lab">Deployment</span><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.deployment" label="Deployment" /></span>
                      <input
                        className="inp mono"
                        name={`provider_deployment_${idx}`}
                        aria-label={`Deployment ${idx + 1}`}
                        value={m.deployment || ""}
                        placeholder={m.id || t("connect.deployment_default_placeholder")}
                        autoComplete="off"
                        spellCheck={false}
                        onChange={(event) =>
                          setRowDeployment(idx, event.target.value)}
                      />
                      <span className="mp-hint">
                        <Info size={12} />{" "}
                        {draft.protocol === "azure_openai"
                          ? t("connect.azure_deployment_hint")
                          : t("connect.custom_deployment_hint")}
                      </span>
                    </div>
                  ) : null}
                  {/* API surface (Responses API): only openai_compatible consumes it — Azure's
                      deployment-path logic is the opposite of Responses throughout; other protocols omit it rather than
                      disable it (design S4 negative control). Default state has no noisy hints; switching to responses shows
                      the hint (design S1/S3). */}
                  {draft.protocol === "openai_compatible" && m.caps.some((c) => ["chat", "tool", "vision"].includes(c)) ? (
                    <div className="medit-proto medit-setting-row">
                      <span className="configuration-field-label"><label className="mp-lab" htmlFor={`mset-model-api-surface-${idx}`}>{t("connect.api_surface_label")}</label><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.api_surface" label={t("connect.api_surface_label")} /></span>
                      <div className="selbox">
                        <select
                          id={`mset-model-api-surface-${idx}`}
                          name={`provider_model_api_surface_${idx}`}
                          aria-label={`${t("connect.api_surface_label")} ${idx + 1}`}
                          value={m.apiSurface === "responses" ? "responses" : "chat_completions"}
                          onChange={(e) => { touch("models"); setRowApiSurface(idx, e.target.value); }}
                        >
                          <option value="chat_completions">{t("connect.api_surface_chat")}</option>
                          <option value="responses">{t("connect.api_surface_responses")}</option>
                        </select>
                        <span className="chev"><ChevronDown size={15} /></span>
                      </div>
                      {m.apiSurface === "responses" ? (
                        <span className="mp-hint">
                          <Info size={12} /> {t("connect.api_surface_hint_responses")}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                  {m.caps.some((c) => c === "chat" || c === "tool") ? (
                    <div className="medit-proto medit-setting-row">
                      <span className="configuration-field-label"><label className="mp-lab" htmlFor={`mset-model-context-window-${idx}`}>{t("connect.context_window_label")}</label><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.context_window" label={t("connect.context_window_label")} /></span>
                      <input
                        id={`mset-model-context-window-${idx}`}
                        className="inp mono"
                        type="number"
                        min={1}
                        step={1}
                        name={`provider_model_context_window_${idx}`}
                        value={m.contextWindow ?? (m.id.trim() ? suggestedContextWindow(windowCatalog, m.id, draft.providerContextWindow)?.value ?? "" : "")}
                        placeholder={t("connect.context_window_placeholder")}
                        onChange={(event) => setRowContextWindow(idx, event.target.value)}
                      />
                      {(() => {
                        // The source tag shows only while the prefilled value is unchanged (design §8.2).
                        const suggestion = m.contextWindow === undefined && m.id.trim() ? suggestedContextWindow(windowCatalog, m.id, draft.providerContextWindow) : null;
                        return (
                          <span className="mp-hint">
                            <span>
                              {suggestion ? `${suggestion.source === "provider"
                                ? t("connect.context_window_from_provider")
                                : suggestion.source === "catalog"
                                ? t("connect.context_window_from_catalog")
                                : t("connect.context_window_default_tag", { value: suggestion.value.toLocaleString(locale) })} · ` : ""}
                              {/* The explanation always shows; an unknown model adds what to do about the default. */}
                              {t("connect.context_window_hint")}
                              {suggestion?.source === "default" ? ` ${t("connect.context_window_hint_unknown")}` : ""}
                            </span>
                          </span>
                        );
                      })()}
                    </div>
                  ) : null}
                  {m.caps.some((capability) => ["chat", "tool", "vision"].includes(capability)) ? (
                    <ContextPolicyFields index={idx} value={m.contextPolicy} disabled={busy}
                      onChange={(value) => patchModelRows(draft.models.map((model, index) => index === idx ? { ...model, contextPolicy: value } : model))} />
                  ) : null}
                  {m.caps.some((capability) => ["chat", "tool", "vision"].includes(capability)) ? (
                    <OutputBudgetField
                      index={idx} modelId={m.id} value={m.maxOutputTokens} inherited={draft.providerMaxOutputTokens}
                      defaults={windowCatalog?.output_budget_defaults} capabilities={m.caps}
                      params={{ ...draft.providerOutboundParams, ...m.outboundParams }} runtime={m.runtimeParameters}
                      raw={parseModelRequestBodyOverrides(m.requestBodyOverrides || "{}")} disabled={busy}
                      onChange={(value) => patchModelRows(draft.models.map((model, index) => index === idx ? { ...model, maxOutputTokens: value } : model))}
                    />
                  ) : null}
                  {(
                    <fieldset className="medit-proto medit-rates">
                      <legend className="mp-lab">{t("connect.credit_rates_label")}</legend>
                      <div className="mp-rate-fields">
                        {(["input", "cached_input", "output"] as const).map((kind) => (
                          <div key={kind} className="mp-rate-row">
                            <span className="configuration-field-label"><label className="mp-lab" htmlFor={`mset-model-${idx}-credit-${kind}`}>{t(`connect.credit_rate_${kind}`)}</label><ConfigurationFieldHelp field={`team_model_config.providers.*.models.*.credit_rates.${kind}`} label={t(`connect.credit_rate_${kind}`)} /></span>
                            <input
                              className="inp mono"
                              type="number"
                              min={0}
                              max={1000}
                              step="0.000001"
                              id={`mset-model-${idx}-credit-${kind}`}
                              name={`provider_model_credit_rate_${kind}_${idx}`}
                              value={m.creditRates?.[kind] ?? "1"}
                              onChange={(event) => setRowCreditRate(idx, kind, event.target.value)}
                            />
                          </div>
                        ))}
                      </div>
                      {!creditRatesValid(m.creditRates) ? <span className="hint field-error">{t("connect.credit_rates_invalid")}</span> : null}
                    </fieldset>
                  )}
                  {/* These gates differ: **both adapters consume** outbound parameters; only OpenAI-side adapters read raw JSON.
                      The whole section previously used the overrides gate, leaving Anthropic-compatible sources without any entry
                      (found during dogfooding on 116; see providerConsumesOutboundParams comments). */}
                  {props.parameterProfiles && props.onParameterPreview && m.caps.some((capability) => ["chat", "tool", "vision"].includes(capability)) ? (
                    <RuntimeParametersSection
                      index={idx} modelId={m.id} protocol={draft.protocol} apiSurface={m.apiSurface} capabilities={m.caps}
                      runtime={m.runtimeParameters} legacyParams={{ ...draft.providerOutboundParams, ...m.outboundParams }} legacyRaw={m.requestBodyOverrides || "{}"}
                      profiles={props.parameterProfiles} disabled={busy} previewKey={props.previewKey || ""}
                      onChange={(patch) => patchModelRows(draft.models.map((model, index) => index === idx ? { ...model, ...patch } : model))}
                      onPreview={(capability) => props.onParameterPreview!(m.id, capability)}
                    />
                  ) : providerConsumesOutboundParams(draft.protocol) ? (
                    <OutboundParamsSection
                      index={idx}
                      providerKey={draft.origKey || draft.name.trim()}
                      modelId={m.id}
                      protocol={draft.protocol}
                      params={m.outboundParams || {}}
                      onChange={(next) => { touch("models"); setRowOutboundParams(idx, next); }}
                      rawOverrides={parseModelRequestBodyOverrides(m.requestBodyOverrides || "{}")}
                      adaptations={adaptations}
                      rawSlot={providerConsumesBodyOverrides(draft.protocol) ? (
                        <>
                          <ConfigurationFieldHelp field="team_model_config.providers.*.models.*.request_body_overrides" label={t("connect.model_body_overrides")} />
                          <textarea
                            className="inp mono"
                            name={`provider_model_overrides_${idx}`}
                            aria-label={`${t("connect.model_body_overrides")} ${idx + 1}`}
                            value={m.requestBodyOverrides || "{}"}
                            rows={4}
                            autoComplete="off"
                            spellCheck={false}
                            onChange={(event) =>
                              setRowRequestBodyOverrides(idx, event.target.value)}
                          />
                          {modelRequestBodyOverridesValid(
                            m.requestBodyOverrides || "{}",
                            m.caps,
                          )
                            ? null
                            : (
                                <span className="hint field-error">
                                  {t("connect.model_body_overrides_invalid")}
                                </span>
                              )}
                        </>
                      ) : null}
                    />
                  ) : null}
                  <div className="medit-caps">
                    <ConfigurationFieldHelp field="team_model_config.providers.*.models.*.capabilities" label={t("v2.capability_required_title")} />{caps.map((c) => {
                      const state = modelCapabilityTestState(m.id, m.caps, c, draft.testing, testResult?.steps);
                      const status = state === "verified" ? t("connect.verified")
                        : state === "degraded" ? t("connect.degraded")
                        : state === "failed" ? t("connect.verification_failed")
                          : state === "testing" ? t("connect.verifying")
                            : state === "unverified" ? t("connect.unverified")
                              : "";
                      return <button type="button" key={c} className={`captoggle ${state}`} onClick={() => { touch("models"); onToggleCap(idx, c); }}><span className="tk" />{capLabel(c)}{status ? ` · ${status}` : ""}</button>;
                    })}
                  </div>
                  <button type="button" className="medit-x" title={t("common.remove")} aria-label={`${t("common.remove")} ${m.id || idx + 1}`} onClick={() => removeRow(idx)}><XSm size={14} /></button>
                  {m.caps.includes(IMAGE_CAP) ? (
                    <div className="medit-proto">
                      <span className="configuration-field-label"><span className="mp-lab">{t("connect.protocol_label")}</span><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.protocol" label={t("connect.protocol_label")} /></span>
                      <div className="selbox">
                        <select
                          name={`provider_model_protocol_${idx}`}
                          aria-label={`${t("connect.protocol_label")} ${idx + 1}`}
                          value={m.protocol || DEFAULT_MEDIA_PROTOCOL}
                          onChange={(e) => setRowProtocol(idx, e.target.value)}
                        >
                          {MEDIA_PROTOCOLS.map((p) => <option key={p} value={p}>{t(`media.proto_${p === "ark_sync" ? "sync" : "async"}`)}</option>)}
                        </select>
                        <span className="chev"><ChevronDown size={15} /></span>
                      </div>
                      <span className="mp-hint"><Info size={12} /> {t("connect.protocol_hint")}</span>
                    </div>
                  ) : m.caps.includes(SEARCH_CAP) ? (
                    <div className="medit-proto">
                      <span className="configuration-field-label"><span className="mp-lab">{t("connect.protocol_label")}</span><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.protocol" label={t("connect.protocol_label")} /></span>
                      <div className="selbox">
                        <select
                          name={`provider_model_protocol_${idx}`}
                          aria-label={`${t("connect.protocol_label")} ${idx + 1}`}
                          value={m.protocol || DEFAULT_SEARCH_PROTOCOL}
                          onChange={(e) => setRowProtocol(idx, e.target.value)}
                        >
                          {SEARCH_PROTOCOLS.map((p) => <option key={p} value={p}>{t(`search.proto_${p === "dashscope_native" ? "native" : "compat"}`)}</option>)}
                        </select>
                        <span className="chev"><ChevronDown size={15} /></span>
                      </div>
                      <span className="mp-hint"><Info size={12} /> {t("connect.protocol_hint_search")}</span>
                    </div>
                  ) : m.caps.includes("rerank") ? (
                    <div className="medit-proto">
                      <span className="configuration-field-label"><span className="mp-lab">{t("connect.protocol_label")}</span><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.protocol" label={t("connect.protocol_label")} /></span>
                      <div className="selbox">
                        <select
                          name={`provider_model_protocol_${idx}`}
                          aria-label={`${t("connect.protocol_label")} ${idx + 1}`}
                          value={m.protocol || DEFAULT_RERANK_PROTOCOL}
                          onChange={(e) => setRowProtocol(idx, e.target.value)}
                        >
                          {RERANK_PROTOCOLS.map((protocol) => (
                            <option key={protocol} value={protocol}>{protocol}</option>
                          ))}
                        </select>
                        <span className="chev"><ChevronDown size={15} /></span>
                      </div>
                      <span className="mp-hint"><Info size={12} /> {t("connect.protocol_hint_rerank")}</span>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
          {modelsOptional && draft.models.length === 0 ? <p className="hint">{t("sections.models_optional")}</p> : null}
          {hasManualCaps && !capabilitySteps.length && !draft.testing ? <div className="unver-note"><AlertTriangle size={12} /> <span><b>{t("connect.unverified")}</b>{t("connect.unverified_note")}</span></div> : null}
          {v2 && hasUnknownCapabilities ? (
            <div className="unver-note">
              <AlertTriangle size={12} />
              <span><b>{t("v2.capability_required_title")}</b>{t("v2.capability_required_body")}</span>
            </div>
          ) : null}
          {hasDuplicateModelIds ? (
            <div className="unver-note">
              <AlertTriangle size={12} />
              <span><b>{t("connect.duplicate_model_title")}</b>{t("connect.duplicate_model_note")}</span>
            </div>
          ) : null}
          {pureImage ? <div className="unver-note legacy-mset-unver-neutral"><Info size={12} /> <span><b>{t("connect.pure_image_title")}</b>{t("connect.pure_image_note")}</span></div> : null}
          {pureSearch ? <div className="unver-note legacy-mset-unver-neutral"><Info size={12} /> <span><b>{t("connect.pure_search_title")}</b>{t("connect.pure_search_note")}</span></div> : null}
        </div>
        {testResult ? (
          <div className={`test-result ${testResult.code === "capability_degraded" ? "warn" : testResult.ok ? "ok" : "err"}`} role="status">
            <span className="ti">{testResult.ok && testResult.code !== "capability_degraded" ? <Check size={15} /> : <AlertTriangle size={15} />}</span>
            <div>{summary()}</div>
          </div>
        ) : null}
        {capabilitySteps.map((step) => (
          <div key={`${step.model}:${step.capability}`} className={`test-result ${step.status === "degraded" ? "warn" : step.ok ? "ok" : "err"}`} role="status">
            <span className="ti">{step.ok && step.status !== "degraded" ? <Check size={15} /> : <AlertTriangle size={15} />}</span>
            <div>{step.model} · {capLabel(step.capability || "")} · {step.status === "degraded" ? t("verify.stream_degraded") : step.ok ? t("connect.verified") : t(`verify.${step.code}`)}</div>
          </div>
        ))}
      </fieldset>
      {verification}
      <div className="connect-foot">
        <div className="test"><button type="button" className={`btn sec sm${testEnabled ? "" : " dis"}${draft.testing ? " loading" : ""}`} disabled={busy || !testEnabled || draft.testing} onClick={onTest}><Bolt size={13} /> {v2 ? t("v2.plan_verification") : t("connect.test")}</button></div>
        {saveBlocked ? (
          <span className="save-blocked" id="connect-save-blocked" role="status">
            {saveBlocked === "name_missing"
              // The field is "来源标识" in v2, "名称" in v1 — reasons must use the same name as the field,
              // or users will look for a box that does not exist on the page.
              ? t("connect.blocked_name_missing", {
                  field: v2 ? t("v2.runtime_key") : t("connect.name"),
                })
              : t(`connect.blocked_${saveBlocked}`)}
          </span>
        ) : null}
        <button type="button" className="btn ghost sm" disabled={busy} onClick={onCancel}>{t("common.cancel")}</button>
        <button
          type="button"
          className={`btn pri sm${saveEnabled ? "" : " dis"}`}
          disabled={!saveEnabled || busy}
          aria-describedby={saveBlocked ? "connect-save-blocked" : undefined}
          onClick={onSave}
        >
          {t("common.save")}
        </button>
      </div>
    </div>
  );
}

// ─── small utils ────────────────────────────────────────────────────
function groupBy<T>(arr: T[], key: (t: T) => string): Record<string, T[]> {
  const out: Record<string, T[]> = {};
  for (const item of arr) { const k = key(item); (out[k] ||= []).push(item); }
  return out;
}
function moveItem<T>(arr: T[], idx: number, dir: -1 | 1): T[] {
  const next = [...arr];
  const j = idx + dir;
  if (j < 0 || j >= next.length) return next;
  [next[idx], next[j]] = [next[j], next[idx]];
  return next;
}

function syncRerankProtocols(
  kb: KbConfig,
  providerKey: string,
  rows: Array<{ id: string; caps: string[]; protocol?: string }>,
): KbConfig {
  const protocols = new Map(
    rows
      .filter((row) => row.caps.includes("rerank"))
      .map((row) => [row.id, row.protocol || DEFAULT_RERANK_PROTOCOL]),
  );
  if (protocols.size === 0) return kb;
  const models = Object.fromEntries(
    Object.entries(kb.models).map(([name, model]) => {
      const protocol = model.provider === providerKey && model.real_id
        ? protocols.get(model.real_id)
        : undefined;
      return [
        name,
        model.capability === "rerank" && protocol
          ? { ...model, protocol }
          : model,
      ];
    }),
  );
  return { ...kb, models };
}

// Rebuild the media (image_gen) section for a single provider from its draft image rows.
// - keep other providers' media models untouched;
// - drop this provider's prior image models (matched by providerKey OR the pre-rename oldKey);
// - re-add current draft image rows under `img__<providerKey>__<id>`;
// - repoint media.active.image_gen when it belonged to this provider (survives rename; cleared
//   if the active model was removed). v4 keeps invalid references visible for explicit review.
function buildMediaForSave(
  prev: MediaConfig | undefined,
  providerKey: string,
  oldKey: string | undefined,
  imageRows: Array<{ id: string; protocol?: string }>,
): MediaConfig {
  const models: Record<string, MediaModel> = {};
  for (const [name, mm] of Object.entries(prev?.models || {})) {
    if (mm.provider !== providerKey && mm.provider !== oldKey) models[name] = mm;
  }
  for (const r of imageRows) {
    models[`img__${providerKey}__${r.id}`] = {
      provider: providerKey, real_id: r.id, capability: "image_gen", protocol: r.protocol || DEFAULT_MEDIA_PROTOCOL,
    };
  }
  const active: Record<string, string> = { ...(prev?.active || {}) };
  const activePrev = prev?.active?.image_gen;
  const activeSpec = activePrev ? prev?.models?.[activePrev] : undefined;
  if (activeSpec && (activeSpec.provider === providerKey || activeSpec.provider === oldKey)) {
    const kept = imageRows.find((r) => r.id === activeSpec.real_id);
    if (kept) active.image_gen = `img__${providerKey}__${activeSpec.real_id}`;
    else delete active.image_gen;
  } else if (activePrev && !models[activePrev]) {
    delete active.image_gen;
  }
  return { ...(prev || {}), models, active };
}

// Rebuild the search (web_search) section for a single provider from its draft search rows
// (mirror buildMediaForSave overall; use srch__ key prefixes to distinguish from media's img__).
function buildSearchForSave(
  prev: SearchConfig | undefined,
  providerKey: string,
  oldKey: string | undefined,
  searchRows: Array<{ id: string; protocol?: string }>,
): SearchConfig {
  const models: Record<string, SearchModel> = {};
  for (const [name, sm] of Object.entries(prev?.models || {})) {
    if (sm.provider !== providerKey && sm.provider !== oldKey) models[name] = sm;
  }
  for (const r of searchRows) {
    models[`srch__${providerKey}__${r.id}`] = {
      provider: providerKey, real_id: r.id, capability: "web_search", protocol: r.protocol || DEFAULT_SEARCH_PROTOCOL,
    };
  }
  const active: Record<string, string> = { ...(prev?.active || {}) };
  const activePrev = prev?.active?.web_search;
  const activeSpec = activePrev ? prev?.models?.[activePrev] : undefined;
  if (activeSpec && (activeSpec.provider === providerKey || activeSpec.provider === oldKey)) {
    const kept = searchRows.find((r) => r.id === activeSpec.real_id);
    if (kept) active.web_search = `srch__${providerKey}__${activeSpec.real_id}`;
    else delete active.web_search;
  } else if (activePrev && !models[activePrev]) {
    delete active.web_search;
  }
  return { ...(prev || {}), models, active };
}
