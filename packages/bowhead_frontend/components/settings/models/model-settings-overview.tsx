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
import {
  attentionBreakdown,
  attentionCount,
  evidenceItemKey,
  evidenceItemState,
  evidenceItems,
} from "@/lib/readiness-items";

export interface ModelSettingsOverviewProvider {
  runtimeKey: string;
  displayName: string;
  type: string;
  endpoint: string;
  modelCount: number;
  fingerprint?: string | null;
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

function roleTranslationKey(role: string): string {
  if (role === "tier.primary") return "role_primary";
  if (role.startsWith("tier.primary_fallback")) return "role_fallback";
  if (role === "tier.fast") return "role_fast";
  if (role === "tier.judge") return "role_judge";
  if (role === "kb.embed") return "role_embed";
  if (role === "kb.rerank") return "role_rerank";
  if (role === "kb.vision") return "role_vision";
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
  const observedDate = row.observed_at ? new Date(row.observed_at) : null;
  const observedAt = observedDate && !Number.isNaN(observedDate.valueOf())
    ? new Intl.DateTimeFormat(locale, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(observedDate)
    : null;
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
        {observedAt ? <small>{observedAt}</small> : null}
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
  onEditProvider,
  onRetryReadiness,
  onVerify,
  onVerifyItem,
  verifyingRowKey,
  verificationBusy,
  verificationAvailable,
  configurationActionsDisabled,
}: {
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
  onEditProvider: (runtimeKey: string) => void;
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
  const tProviderType = useTranslations("settings.llm.provider_types");
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
            <a className="rd-link" href="#mset-overview-evidence">{t("evidence_jump")}</a>
          </span>
        </p>
      ) : null}

      {!section || section === "connections" ? <section className="overview-section" aria-labelledby="mset-overview-connections">
        <div className="overview-section-head">
          <div>
            {!section ? <span className="overview-section-index">01</span> : null}
            <h2 id="mset-overview-connections">{t("connections_title")}</h2>
          </div>
          <button
            type="button"
            className="btn sec sm"
            disabled={configurationActionsDisabled}
            onClick={onAddProvider}
          >
            <Plus size={13} /> {t("add_connection")}
          </button>
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
                </div>
                <div className="overview-provider-secret">
                  <span>{t("secret")}</span>
                  <code translate="no">{provider.fingerprint || t("secret_saved")}</code>
                </div>
                <button
                  type="button"
                  className="iconbtn"
                  disabled={configurationActionsDisabled}
                  aria-label={t("edit_connection", { name: provider.displayName })}
                  onClick={() => onEditProvider(provider.runtimeKey)}
                >
                  <Edit size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </section> : null}

      {!section || section === "assignments" ? <section className="overview-section" aria-labelledby="mset-overview-evidence">
        <div className="overview-section-head">
          <div>
            {!section ? <span className="overview-section-index">02</span> : null}
            <h2 id="mset-overview-evidence">{t("evidence_title")}</h2>
            <p>{t("evidence_body")}</p>
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
              className="btn ghost sm"
              disabled={configurationActionsDisabled}
              onClick={onEdit}
            >
              <Edit size={13} /> {t("edit_assignments")}
            </button> : null}
          </div>
        </div>
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
              <b>{providers.length ? t("no_assignments_title") : t("no_evidence_title")}</b>
              <span>{providers.length ? t("no_assignments_body") : t("no_evidence_body")}</span>
            </div>
          </div>
        )}
      </section> : null}

      {section === "external" ? <section className="overview-section overview-external" aria-labelledby="mset-overview-external">
        <div className="overview-section-head">
          <div>
            {!section ? <span className="overview-section-index">03</span> : null}
            <h2 id="mset-overview-external">{t("external_title")}</h2>
            <p>{t("external_body")}</p>
          </div>
        </div>
        <div className="overview-external-rows">
          {([
            ["media_image_generation", imageConfigured, onConfigureImage],
            ["web_search", searchConfigured, onConfigureSearch],
          ] as const).map(([kind, configured, onConfigure]) => (
            <div key={kind}>
              <span>{t(`external_${kind}`)}</span>
              <span className="overview-external-action">
                <b className={configured ? "is-configured" : ""}>{t(configured ? "external_status_configured" : "external_status_unavailable")}</b>
                <button type="button" className="btn sec sm" disabled={configurationActionsDisabled || !onConfigure} onClick={onConfigure}>
                  {t(configured ? "external_edit" : "external_configure")}
                </button>
              </span>
            </div>
          ))}
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
