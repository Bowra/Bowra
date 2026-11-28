"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import {
  AlertTriangle,
  Check,
  Clock,
  Edit,
  Info,
  Plug,
  Plus,
  RefreshCw,
  Server,
  Spinner,
} from "@/components/icons";
import type { LlmReadiness, LlmReadinessRow } from "@/lib/llm-config-api";
import { appPath } from "@/lib/base-path";
import { formatDateTime } from "@/lib/locale";
import { useListDate } from "@/components/ui/list-time";
import {
  attentionBreakdown,
  attentionCount,
  evidenceItemKey,
  evidenceItemState,
  evidenceItems,
} from "@/lib/readiness-items";

export interface AssignmentSummaryRow {
  role: string;
  target: string;
  providerKey?: string;
  modelId?: string;
  mode: "specified" | "follow" | "automatic" | "off" | "missing";
}

/** Knowledge-base roles a model reaches through `kb.active`, with the capabilities readiness requires of each. */
export const KNOWLEDGE_BASE_ROLE_CAPABILITIES = {
  embed: ["embed"],
  rerank: ["rerank"],
  vision: ["vision"],
  row_extractor: ["chat", "tool"],
  source_description: ["chat"],
} as const;
export type KnowledgeBaseRole = keyof typeof KNOWLEDGE_BASE_ROLE_CAPABILITIES;
/** Where a connection's model is used outside its catalog (design: connection-purposes-design.md, 2026-11-25). */
export type ConnectionUseRole = KnowledgeBaseRole | "image" | "search";
export interface ConnectionUse { modelId: string; role: ConnectionUseRole }

export function isKnowledgeBaseRole(role: string): role is KnowledgeBaseRole {
  return Object.hasOwn(KNOWLEDGE_BASE_ROLE_CAPABILITIES, role);
}

/** Overview message key naming a use. */
export function connectionUseLabelKey(role: ConnectionUseRole): string {
  return role === "image" ? "external_media_image_generation" : role === "search" ? "external_web_search" : `role_${role}`;
}

/** A link to the page where a use is changed: the path for the anchor, and the in-app navigation. */
export interface ConnectionUseLink { href: string; open: () => void }

type OverviewCapabilityFact = { capability: string; state: string; observedAt?: string | null };

export interface ModelSettingsOverviewProvider {
  providerId?: string;
  runtimeKey: string;
  displayName: string;
  type: string;
  endpoint: string;
  /** Distinct models the connection serves: catalog, knowledge base, image and search. */
  modelCount: number;
  models?: Array<{ id: string; capabilities: OverviewCapabilityFact[]; alsoUsedFor?: ConnectionUseRole[] }>;
  /** Knowledge-base models outside the catalog. */
  knowledgeBase?: Array<{ id: string; roles: KnowledgeBaseRole[]; capabilities: OverviewCapabilityFact[] }>;
  /** Image-generation and web-search models outside the catalog. */
  external?: Array<{ id: string; kind: "image" | "search" }>;
  fingerprint?: string | null;
  authType?: "bearer" | "api_key" | "secret_header" | "none";
}

export function safeOverviewEndpoint(value: string): string {
  const endpoint = value.trim();
  if (!endpoint) return "—";
  try {
    const parsed = new URL(endpoint);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "—";
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
  } catch {
    // Invalid endpoints should already be rejected by server validation. Keep
    // the read-only Overview fail-closed instead of reflecting a possibly
    // credential-bearing value from an older config.
    return "—";
  }
}

const FACT_STATES = new Set([
  "verified",
  "aged",
  "stale",
  "failed",
  "not_testable",
  "unknown",
]);
const ATTEMPT_STATES = new Set([
  "queued",
  "verifying",
  "errored",
  "cancelled",
  "interrupted",
]);
const CAPABILITY_KEYS = new Set(["chat", "tool", "vision", "embed", "rerank"]);
const PROVIDER_TYPE_KEYS = new Set([
  "openai_compatible",
  "anthropic_compatible",
  "azure_openai",
  "custom_http",
]);

/** Provider interface type is raw configuration value, never directly user-visible; share
 *  settings.llm.provider_types with team models, unknown values use neutral “其他接口”. */
function providerTypeLabelKey(providerType: string): string {
  return PROVIDER_TYPE_KEYS.has(providerType) ? providerType : "other";
}

export function roleTranslationKey(role: string): string {
  if (role === "tier.primary") return "role_primary";
  if (role.startsWith("tier.primary_fallback")) return "role_fallback";
  if (role === "tier.fast") return "role_fast";
  if (role === "tier.judge") return "role_judge";
  if (role === "kb.embed") return "role_embed";
  if (role === "kb.rerank") return "role_rerank";
  if (role === "kb.vision") return "role_vision";
  if (role === "kb.source_description") return "role_source_description";
  if (role === "kb.row_extractor") return "role_row_extractor";
  return "role_other";
}

const EVIDENCE_PRIORITY: Record<ReturnType<typeof evidenceItemState>, number> = {
  running: 0, failed: 1, never: 2, aged: 3, verified: 4,
};
const COLLAPSED_EVIDENCE_COUNT = 5;

function FactState({ row }: { row: LlmReadinessRow }) {
  const t = useTranslations("settings.models.overview");
  const state = FACT_STATES.has(row.fact_state) ? row.fact_state : "unknown";
  const icon = state === "verified"
    ? <Check size={13} aria-hidden />
    : state === "aged"
      ? <Clock size={13} aria-hidden />
      : state === "stale"
        ? <RefreshCw size={13} aria-hidden />
        : state === "failed"
          ? <AlertTriangle size={13} aria-hidden />
          : <Info size={13} aria-hidden />;
  return (
    <span className={`overview-state fact-${state}`}>
      {icon}
      <span>{t(`fact_${state}`)}</span>
    </span>
  );
}

function AttemptState({ row }: { row: LlmReadinessRow }) {
  const t = useTranslations("settings.models.overview");
  const state = row.attempt_state && ATTEMPT_STATES.has(row.attempt_state)
    ? row.attempt_state
    : null;
  if (!state) return <span className="overview-state attempt-none">{t("attempt_none")}</span>;
  return (
    <span className={`overview-state attempt-${state}`}>
      {state === "queued" || state === "verifying"
        ? <Spinner size={13} aria-hidden />
        : state === "errored"
          ? <AlertTriangle size={13} aria-hidden />
          : <Info size={13} aria-hidden />}
      <span>{t(`attempt_${state}`)}</span>
    </span>
  );
}

/** Hint wording only describes available actions, never declares unsafe/unready. */
function attentionSummary(
  t: ReturnType<typeof useTranslations>,
  breakdown: { needsConfirm: number; aged: number; failed: number },
): string {
  const parts: string[] = [];
  if (breakdown.failed) parts.push(t("sum_failed", { count: breakdown.failed }));
  if (breakdown.needsConfirm) parts.push(t("sum_confirm", { count: breakdown.needsConfirm }));
  if (breakdown.aged) parts.push(t("sum_aged", { count: breakdown.aged }));
  return parts.join(t("sum_join"));
}

function EvidenceRow({
  row,
  onVerifyItem,
  verifyingRowKey,
  disabled,
}: {
  row: LlmReadinessRow;
  /** Single-item verification. Absent = no per-item actions on this surface (e.g. read-only preview). */
  onVerifyItem?: (row: LlmReadinessRow) => void;
  verifyingRowKey?: string | null;
  disabled?: boolean;
}) {
  const t = useTranslations("settings.models.overview");
  const locale = useLocale();
  const [confirming, setConfirming] = useState(false);
  const observedAt = formatDateTime(row.observed_at, locale, { dateStyle: "medium" }) || null;
  // ★ Both sources essential: `attempt_state` is truth **after** refresh,
  // `verifyingRowKey` covers just-clicked, not-yet-finished-batch window;
  // using only first would reenable button there, duplicating requests to customer model service.
  const running = Boolean(
    row.attempt_state === "queued"
    || row.attempt_state === "verifying"
    || (verifyingRowKey && verifyingRowKey === evidenceItemKey(row)),
  );
  const rowTone = row.status === "blocked"
    ? " is-blocked"
    : row.status === "warning" || row.status === "risk_acceptance_required"
      ? " is-warning"
      : "";
  return (
    <div className={`overview-ledger-row${rowTone}`}>
      <div className="overview-ledger-role">
        <b>{t(roleTranslationKey(row.role_key))}</b>
      </div>
      <div className="overview-ledger-target">
        <b translate="no">{row.model_id}</b>
        <small>{row.provider_display_name}</small>
      </div>
      <div className="overview-ledger-capability">
        <span>{t("planned_for")}</span>
        <b>{CAPABILITY_KEYS.has(row.capability)
          ? t(`capability_${row.capability}`)
          : row.capability}</b>
      </div>
      <div className="overview-ledger-fact">
        <FactState row={row} />
        {/* Identity mismatch needs remote returned id, or operations cannot distinguish wrong config from overly strict criteria
            (vendors often return versioned ids). Show only real differences to avoid noisy normal rows. */}
        {row.actual_model_id && row.actual_model_id !== row.model_id ? (
          <small className="overview-ledger-actual-model" translate="no">
            {t("evidence_actual_model", { model: row.actual_model_id })}
          </small>
        ) : null}
        {observedAt ? <small>{t("evidence_checked_at", { time: observedAt })}</small> : null}
      </div>
      <div className="overview-ledger-attempt">
        <AttemptState row={row} />
      </div>
      {/* Per-item verification: this table previously only verified whole batches, with no way to reverify one item.
          ★ Consent remains essential: verification really calls customer service and spends its credits.
          But one item needs no modal; expand a confirmation sentence inline. */}
      {onVerifyItem
        ? (
            <div className="overview-ledger-act">
              {running
                ? null
                : (
                    <button
                      type="button"
                      className="btn sec sm"
                      disabled={disabled}
                      aria-label={t("verify_one_label", { model: row.model_id })}
                      onClick={() => setConfirming((open) => !open)}
                    >
                      {t(row.fact_state === "failed" ? "verify_one_retry" : "verify_one")}
                    </button>
                  )}
            </div>
          )
        : null}
      {onVerifyItem && confirming
        ? (
            <div className="overview-ledger-confirm" role="status">
              <span>{t("verify_one_confirm", { provider: row.provider_display_name })}</span>
              <button
                type="button"
                className="btn pri sm"
                onClick={() => {
                  setConfirming(false);
                  onVerifyItem(row);
                }}
              >
                {t("verify_one_go")}
              </button>
              <button type="button" className="btn ghost sm" onClick={() => setConfirming(false)}>
                {t("verify_one_cancel")}
              </button>
            </div>
          )
        : null}
    </div>
  );
}

export function ModelSettingsOverview({
  version,
  section,
  assignments,
  imageConfigured = false,
  searchConfigured = false,
  onConfigureImage,
  onConfigureSearch,
  onRetryIndex,
  retryIndexDisabled,
  providers,
  readiness,
  readinessLoading,
  readinessError,
  reindexState,
  onEdit,
  onAddProvider,
  onConnectBowra,
  onEditProvider,
  onVerifyProvider,
  onVerifyCapability,
  externalTargets,
  externalTests,
  onTestExternal,
  useLinks,
  onRetryReadiness,
  onVerify,
  onVerifyItem,
  verifyingRowKey,
  verificationBusy,
  verificationAvailable,
  configurationActionsDisabled,
}: {
  assignments?: AssignmentSummaryRow[];
  section?: "connections" | "assignments" | "external" | "index";
  imageConfigured?: boolean;
  searchConfigured?: boolean;
  onConfigureImage?: () => void;
  onConfigureSearch?: () => void;
  onRetryIndex?: () => void;
  retryIndexDisabled?: boolean;
  version: number;
  providers: ModelSettingsOverviewProvider[];
  readiness: LlmReadiness | null;
  readinessLoading: boolean;
  readinessError: string | null;
  reindexState: "idle" | "running" | "failed";
  onEdit?: () => void;
  onAddProvider: () => void;
  onConnectBowra?: () => void;
  onEditProvider: (runtimeKey: string) => void;
  onVerifyProvider?: (runtimeKey: string) => void;
  /** Verify one capability that needs it, from its label on the connection list. */
  onVerifyCapability?: (runtimeKey: string, modelId: string, capability: string) => void;
  /** Saved image-generation / web-search model and connection, for 外部能力. */
  externalTargets?: Partial<Record<"image" | "search", { model: string; connection: string }>>;
  externalTests?: Partial<Record<"image" | "search", { state: "running" | "passed" | "failed"; reason?: string }>>;
  onTestExternal?: (kind: "image" | "search") => void;
  /** Where a connection's knowledge-base and external uses are changed. */
  useLinks?: { assignments: ConnectionUseLink; external: ConnectionUseLink };
  onRetryReadiness: () => void;
  onVerify: () => void;
  /** Verify one item only (absent = batch verification only). */
  onVerifyItem?: (row: LlmReadinessRow) => void;
  /** Local row-verifying marker covers interval until readiness updates after batch completion. */
  verifyingRowKey?: string | null;
  verificationBusy: boolean;
  verificationAvailable: boolean;
  configurationActionsDisabled: boolean;
}) {
  const t = useTranslations("settings.models.overview");
  const tTier = useTranslations("settings.models.tier");
  const tConnect = useTranslations("settings.models.connect");
  const tProviderType = useTranslations("settings.llm.provider_types");
  const listDate = useListDate();
  const locale = useLocale();
  const listOf = (items: string[]) => new Intl.ListFormat(locale, { type: "conjunction" }).format(items);
  const useLabels = (roles: readonly ConnectionUseRole[]) => listOf([...new Set(roles)].map((role) => t(connectionUseLabelKey(role))));
  const capabilityLabel = (runtimeKey: string, modelId: string, { capability, state, observedAt }: OverviewCapabilityFact) => {
    // A pass keeps its date: it stays valid until the connection changes (2026-11-25).
    const verifiedAt = (state === "verified" || state === "aged") ? listDate(observedAt) : null;
    const label = <>{t(`capability_${capability}`)} · {t(`fact_${FACT_STATES.has(state) ? state : "unknown"}`)}{verifiedAt ? ` · ${verifiedAt.label}` : null}</>;
    // A state that asks for action says why and verifies just this item (walk W2).
    const reason = state === "stale" || state === "failed" || state === "unknown" ? t(`fact_reason_${state}`) : null;
    return reason && onVerifyCapability
      ? <button type="button" className={`overview-state fact-${state} is-action`} key={capability} title={reason} aria-label={`${modelId} ${t(`capability_${capability}`)}: ${reason}`}
        disabled={configurationActionsDisabled} onClick={() => onVerifyCapability(runtimeKey, modelId, capability)}>{label}</button>
      : <span className={`overview-state fact-${state}`} key={capability} title={verifiedAt?.title}>{label}</span>;
  };
  const useLink = (link: ConnectionUseLink | undefined, labelKey: string) => link
    ? <a className="overview-uses-link" href={appPath(link.href)} onClick={(event) => { event.preventDefault(); link.open(); }}>{t(labelKey)}</a>
    : null;
  const externalState = (runtimeKey: string, model: { id: string; kind: "image" | "search" }) => {
    const target = externalTargets?.[model.kind];
    if (target?.model !== model.id || target.connection !== runtimeKey) return null;
    const test = externalTests?.[model.kind];
    if (test?.state === "running") return <span className="overview-state fact-unknown">{t("external_test_running")}</span>;
    if (test?.state === "passed") return <span className="overview-state fact-verified">{t("external_test_passed")}</span>;
    if (test?.state === "failed") return <span className="overview-state fact-failed" title={test.reason}>{t("external_test_failed", { reason: test.reason || "" })}</span>;
    return <span className="overview-state fact-unknown">{t("uses_untested")}</span>;
  };
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [showAllEvidence, setShowAllEvidence] = useState(false);
  const items = evidenceItems(readiness);
  const orderedRows = [...(readiness?.rows ?? [])].sort((left, right) =>
    EVIDENCE_PRIORITY[evidenceItemState(left)] - EVIDENCE_PRIORITY[evidenceItemState(right)]
    || left.role_key.localeCompare(right.role_key)
    || left.model_id.localeCompare(right.model_id),
  );
  const visibleRows = showAllEvidence
    ? orderedRows
    : orderedRows.slice(0, COLLAPSED_EVIDENCE_COUNT);
  /**
   * Server has not concluded readiness is a **third state**, distinct from unready.
   *
   * Server computes readiness only for configuration **without validation issues** (`admin_billing.validate`:
   * `if model_discovery_v2_enabled() and not issues`). Incomplete configuration, e.g. no primary model,
   * is never evaluated; `readiness` null. Previously `isReady=false` rendered red
    * an unavailable verdict while the counts still had no data:
   * server never judged, but page declared a verdict on its behalf.
   *
    * Gateway-blocked readiness requests also fall into this unknown state.
   */
  const readinessUnknown = !readiness && !readinessLoading;

  return (
    <div className="model-overview">
      {/* ★ Resting state no longer has a persistent readiness verdict panel.
          Two existing authorities independently agree: `docs/design/团队模型配置.md` requires
          readiness computed **before V2 Apply** against **frozen Draft semantics**, while original model-page design
          forbids a persistent currently-effective panorama table. This panel violated both:
          **without a draft**, it assessed live production configuration as a draft for applicability
          (`draft_config = effective_cfg`), permanently showing red “当前配置还不能安全应用”,
          while `querySelectorAll('button, a, input, select')` inside panel returned empty array:
          requesting resolve / accept / reverify without any entry point.
          Now show a quiet hint only for **actual issues**, with actions on each evidence-table row below. */}
      {/* Loading **does not render here**: evidence table below already has `overview-ledger-loading`,
          duplicating here would repeat identical wording on page. */}
      {section === "connections" || section === "external" || section === "index" || readinessLoading ? null : readinessError ? (
        <p className="overview-quiet is-error">
          <AlertTriangle size={13} /> {t("readiness_unavailable")}
          <button type="button" className="rd-link" onClick={onRetryReadiness}>{t("retry")}</button>
        </p>
      ) : readinessUnknown ? (
        /* Server unevaluated is third state, not unready; never decide on server's behalf. */
        <p className="overview-quiet">
          <Info size={13} /> {t("readiness_unknown_body")}
        </p>
      ) : attentionCount(items) > 0 ? (
        <p className="overview-quiet is-attention">
          <span className="rd-pip" aria-hidden="true" />
          <span>
            {attentionSummary(t, attentionBreakdown(items))}
            {" — "}
            <a className="rd-link" href="#mset-overview-evidence" onClick={(event) => {
              setDetailsOpen(true);
              if (!assignments) return;
              // The summary table has no Verify buttons; the per-item ones sit in the details (walk W7).
              event.preventDefault();
              window.requestAnimationFrame(() => document.getElementById("assignment-verification-details")
                ?.scrollIntoView({ block: "start", behavior: "smooth" }));
            }}>{t("evidence_jump")}</a>
          </span>
        </p>
      ) : null}

      {!section || section === "connections" ? <section className="overview-section" aria-labelledby="mset-overview-connections">
        <div className="overview-section-head">
          <div>
            {!section ? <span className="overview-section-index">01</span> : null}
            <h2 id="mset-overview-connections">{t("connections_title")}</h2>
          </div>
          <div className="overview-connection-actions">
            {onConnectBowra ? <button type="button" className="btn sec sm" disabled={configurationActionsDisabled} onClick={onConnectBowra}>
              {t("use_bowra")}
            </button> : null}
            <button
              type="button"
              className="btn sec sm"
              disabled={configurationActionsDisabled}
              onClick={onAddProvider}
            >
              <Plus size={13} /> {t("add_connection")}
            </button>
          </div>
        </div>
        {providers.length === 0 ? (
          <div className="overview-empty">
            <Plug size={20} />
            <div><b>{t("empty_connections_title")}</b><span>{t("empty_connections_body")}</span></div>
            <button type="button" className="btn pri sm" onClick={onAddProvider}>
              {t("add_first_connection")}
            </button>
          </div>
        ) : (
          <div className="overview-provider-rows">
            {providers.map((provider) => (
              <div className="overview-provider-row" key={provider.runtimeKey}>
                <span className="overview-provider-icon" aria-hidden>
                  <Server size={15} />
                </span>
                <div className="overview-provider-main">
                  <div>
                    <b>{provider.displayName}</b>
                    <span>{tProviderType(providerTypeLabelKey(provider.type))}</span>
                    <small>{t("model_count", { count: provider.modelCount })}</small>
                  </div>
                  <code translate="no">{safeOverviewEndpoint(provider.endpoint)}</code>
                  {/* Everything the connection serves, grouped by where it is used (design W4); group names show only
                      when there is more than the catalog. */}
                  {provider.models?.length && (provider.knowledgeBase?.length || provider.external?.length)
                    ? <div className="overview-uses-head"><span className="overview-uses-label">{t("uses_catalog")}</span></div> : null}
                  {provider.models?.map((model) => <div className="overview-model-capabilities" key={model.id}>
                    <span>{model.id}</span>
                    {model.capabilities.map((fact) => capabilityLabel(provider.runtimeKey, model.id, fact))}
                    {model.alsoUsedFor?.length ? <small className="overview-uses-roles">{t("uses_also", { roles: useLabels(model.alsoUsedFor) })}</small> : null}
                  </div>)}
                  {provider.knowledgeBase?.length ? <div className="overview-uses-group">
                    <div className="overview-uses-head"><span className="overview-uses-label">{t("uses_knowledge_base")}</span>{useLink(useLinks?.assignments, "uses_open_assignments")}</div>
                    {provider.knowledgeBase.map((model) => <div className="overview-model-capabilities" key={model.id}>
                      <span>{model.id}</span>
                      <small className="overview-uses-roles">{useLabels(model.roles)}</small>
                      {model.capabilities.map((fact) => capabilityLabel(provider.runtimeKey, model.id, fact))}
                    </div>)}
                  </div> : null}
                  {provider.external?.length ? <div className="overview-uses-group">
                    <div className="overview-uses-head"><span className="overview-uses-label">{t("external_title")}</span>{useLink(useLinks?.external, "uses_open_external")}</div>
                    {provider.external.map((model) => <div className="overview-model-capabilities" key={`${model.kind}:${model.id}`}>
                      <span>{model.id}</span>
                      <small className="overview-uses-roles">{useLabels([model.kind])}</small>
                      {externalState(provider.runtimeKey, model)}
                    </div>)}
                  </div> : null}
                </div>
                <div className="overview-provider-secret">
                  <span>{provider.authType === "none" ? tConnect("auth_type") : t("secret")}</span>
                  {provider.authType === "none"
                    ? <span>{tConnect("no_auth")}</span>
                    : <code translate="no">{provider.fingerprint || t("secret_saved")}</code>}
                </div>
                <div className="overview-connection-actions">
                {onVerifyProvider && [...(provider.models || []), ...(provider.knowledgeBase || [])].some((model) => model.capabilities.length > 0) ? <button type="button" className="btn sec sm" disabled={configurationActionsDisabled} onClick={() => onVerifyProvider(provider.runtimeKey)}>{t("verify_one")}</button> : null}
                <button
                  type="button"
                  className="btn sec sm"
                  disabled={configurationActionsDisabled}
                  aria-label={t("edit_connection", { name: provider.displayName })}
                  onClick={() => onEditProvider(provider.runtimeKey)}
                >
                  <Edit size={14} /> {t("edit")}
                </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section> : null}

      {!section || section === "assignments" ? <section className="overview-section" aria-labelledby="mset-overview-evidence">
        <div className="overview-section-head">
          <div>
            {!section ? <span className="overview-section-index">02</span> : null}
            <h2 id="mset-overview-evidence">{t(assignments ? "assignments_title" : "evidence_title")}</h2>
            <p>{t(assignments ? "assignments_body" : "evidence_body")}</p>
          </div>
          <div className="overview-section-actions">
            <button
              type="button"
              className="btn sec sm"
              disabled={
                readinessLoading
                || !verificationAvailable
                || verificationBusy
                || configurationActionsDisabled
              }
              onClick={onVerify}
            >
              {verificationBusy ? <Spinner size={13} /> : <RefreshCw size={13} />}
              {t(verificationBusy ? "planning_verification" : "verify_assignments")}
            </button>
            {onEdit && (!section || section === "assignments") ? <button
              type="button"
              className="btn pri sm"
              disabled={configurationActionsDisabled}
              onClick={onEdit}
            >
              <Edit size={13} /> {t("edit_assignments")}
            </button> : null}
          </div>
        </div>
        {assignments ? <>
          {["tier.", "kb."].map((prefix) => <section key={prefix} className="assignment-group" aria-label={t(prefix === "tier." ? "chat_assignments" : "kb_assignments")}>
            <h3>{t(prefix === "tier." ? "chat_assignments" : "kb_assignments")}</h3>
            {assignments.filter((assignment) => assignment.role.startsWith(prefix)).map((assignment) => {
              const evidence = readiness?.rows.filter((row) =>
                row.role_key === (assignment.role.startsWith("tier.primary_fallback.") ? "tier.primary_fallback" : assignment.role)
                && row.model_id === assignment.modelId
                && row.provider_id === providers.find((provider) => provider.runtimeKey === assignment.providerKey)?.providerId) ?? [];
              return <div className="assignment-summary-row" key={assignment.role}>
                <b>{t(roleTranslationKey(assignment.role))}</b>
                <div className="assignment-summary-target"><span translate="no">{assignment.target || "—"}</span><small>{t(assignment.role.startsWith("kb.") && assignment.mode === "automatic" ? "automatic_pending" : `selection_${assignment.mode}`)}</small>
                  {assignment.role === "tier.judge" && assignment.mode === "automatic" && assignment.modelId && assignments.some((row) => row.role === "tier.primary" && row.modelId === assignment.modelId && row.providerKey === assignment.providerKey) ? <small>{tTier("judge_auto_none", { model: assignment.modelId })}<button type="button" className="btn ghost sm" disabled={configurationActionsDisabled} onClick={onAddProvider}>{t("add_connection")}</button></small> : null}</div>
                <div className="assignment-summary-evidence">{!assignment.modelId ? "—" : readinessLoading ? t("readiness_loading") : readinessError ? t("evidence_unavailable_title") : evidence.length ? evidence.map((row) => <span key={row.capability}>
                  <span>{CAPABILITY_KEYS.has(row.capability) ? t(`capability_${row.capability}`) : row.capability}: </span><FactState row={row} />{row.attempt_state ? <AttemptState row={row} /> : null}
                </span>) : assignment.mode === "off" ? "—" : t("fact_unknown")}</div>
              </div>;
            })}
          </section>)}
          <p className="overview-quiet">{t("verification_boundary")}</p>
          <button type="button" className="btn ghost sm" aria-expanded={detailsOpen} aria-controls="assignment-verification-details" onClick={() => setDetailsOpen((open) => !open)}>{t(detailsOpen ? "hide_details" : "show_details")}</button>
        </> : null}
        <div id={assignments ? "assignment-verification-details" : undefined} hidden={Boolean(assignments) && !detailsOpen}>
        {readinessLoading ? (
          <div className="overview-ledger-loading" aria-live="polite">
            <Spinner size={15} /> {t("readiness_loading")}
          </div>
        ) : readinessError ? (
          <div className="overview-ledger-empty is-error">
            <AlertTriangle size={17} />
            <div><b>{t("evidence_unavailable_title")}</b><span>{t("evidence_unavailable_body")}</span></div>
          </div>
        ) : readiness?.rows.length ? (
          <div className="overview-ledger">
            <div className="overview-ledger-header" aria-hidden>
              <span>{t("column_role")}</span>
              <span>{t("column_target")}</span>
              <span>{t("column_plan")}</span>
              <span>{t("column_evidence")}</span>
              <span>{t("column_attempt")}</span>
              <span />
            </div>
            {visibleRows.map((row) => (
              <EvidenceRow
                key={`${row.role_key}:${row.provider_id}:${row.model_id}:${row.capability}`}
                row={row}
                onVerifyItem={onVerifyItem}
                verifyingRowKey={verifyingRowKey}
                disabled={verificationBusy || configurationActionsDisabled}
              />
            ))}
            {orderedRows.length > COLLAPSED_EVIDENCE_COUNT ? (
              <button
                type="button"
                className="overview-ledger-more"
                aria-expanded={showAllEvidence}
                onClick={() => setShowAllEvidence((current) => !current)}
              >
                {showAllEvidence
                  ? t("show_less_evidence")
                  : t("show_remaining_evidence", { count: orderedRows.length - COLLAPSED_EVIDENCE_COUNT })}
              </button>
            ) : null}
          </div>
        ) : (
          <div className="overview-ledger-empty">
            <Info size={17} />
            <div>
              <b>{assignments?.some((row) => row.modelId) ? t("no_records_title") : providers.length ? t("no_assignments_title") : t("no_evidence_title")}</b>
              <span>{assignments?.some((row) => row.modelId) ? t("no_records_body") : providers.length ? t("no_assignments_body") : t("no_evidence_body")}</span>
            </div>
          </div>
        )}
        </div>
      </section> : null}

      {section === "external" ? <section className="overview-section overview-external" aria-label={t("external_title")}>
        {/* The page heading already says 外部能力 (walk W8). */}
        <div className="overview-external-rows">
          {([
            ["media_image_generation", "image", imageConfigured, onConfigureImage],
            ["web_search", "search", searchConfigured, onConfigureSearch],
          ] as const).map(([kind, shortKind, configured, onConfigure]) => {
            const target = externalTargets?.[shortKind];
            const test = externalTests?.[shortKind];
            return (
              <div key={kind}>
                <span className="overview-external-name">
                  <span>{t(`external_${kind}`)}</span>
                  {/* Which model and connection serve it (walk W10). */}
                  {target ? <small translate="no">{target.model} · {target.connection}</small> : null}
                  {test ? <small className={`overview-external-test is-${test.state}`} role="status">
                    {test.state === "running" ? t("external_test_running")
                      : test.state === "passed" ? t("external_test_passed")
                        : t("external_test_failed", { reason: test.reason || "" })}
                  </small> : null}
                </span>
                <span className="overview-external-action">
                  <b className={configured ? "is-configured" : ""}>{t(configured ? "external_status_configured" : "external_status_unavailable")}</b>
                  {configured && target && onTestExternal ? (
                    <button type="button" className="btn sec sm" disabled={configurationActionsDisabled || test?.state === "running"} onClick={() => onTestExternal(shortKind)}>
                      {t("external_test")}
                    </button>
                  ) : null}
                  <button type="button" className="btn sec sm" disabled={configurationActionsDisabled || !onConfigure} onClick={onConfigure}>
                    {t(configured ? "external_edit" : "external_configure")}
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      </section> : null}
      {section === "index" ? <section className="overview-section overview-external" aria-label={t("kb_reindex")}>
        <div className="overview-external-rows">
          <div>
            <span>{t("kb_reindex")}</span>
            <span className="overview-external-action">
              <b className={reindexState === "failed" ? "is-failed" : reindexState === "running" ? "is-running" : ""}>
                {t(`reindex_${reindexState}`)}
              </b>
              {reindexState === "failed" && onRetryIndex ? <button type="button" className="btn sec sm" disabled={configurationActionsDisabled || retryIndexDisabled} onClick={onRetryIndex}>{t("reindex_retry")}</button> : null}
            </span>
          </div>
        </div>
      </section> : null}
    </div>
  );
}
