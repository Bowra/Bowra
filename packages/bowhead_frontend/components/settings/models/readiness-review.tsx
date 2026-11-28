"use client";

import { useTranslations } from "next-intl";

import { AlertTriangle, Check, Info } from "@/components/icons";
import { roleTranslationKey } from "@/components/settings/models/model-settings-overview";
import type { LlmReadiness, LlmReadinessRow } from "@/lib/llm-config-api";

/**
 * Acknowledgment copy must follow **risk kind**—the user signs this statement.
 *
 * Before 2026-08-14 allowed acknowledging failed / stale / unknown and applying, this had one hardcoded sentence:
 * “当前协议无法自动验证”. Reusing it would make users sign a false statement when probes explicitly fail (claiming it cannot be tested),
 * precisely where knowing the truth matters most.
 *
 * Unknown risk_code falls back to not_testable (most conservative wording), without throwing.
 */
const RISK_COPY_CODES = new Set([
  "capability_failed",
  "capability_stale",
  "capability_unknown",
]);

const CAPABILITIES = new Set(["chat", "tool", "vision", "embed", "rerank"]);

/** Rows that need nothing from the reader; they fold under one line (2026-11-25 save-flow audit, F4). */
const SETTLED = new Set(["ready", "risk_accepted", "grandfathered"]);

function riskCopyKey(kind: "title" | "body", riskCode: string): string {
  return RISK_COPY_CODES.has(riskCode)
    ? `risk_accept_${kind}_${riskCode}`
    : `risk_accept_${kind}`;
}

/** Number of outstanding risk acknowledgments. */
function pendingAcceptances(
  readiness: LlmReadiness,
  acceptedTokens: Set<string>,
): number {
  return readiness.required_acceptances
    .filter((acceptance) => !acceptedTokens.has(acceptance.acceptance_token)).length;
}

function headerTone(readiness: LlmReadiness, acceptedTokens: Set<string>): string {
  if (readiness.blocking.length) return "blocked";
  return pendingAcceptances(readiness, acceptedTokens) ? "pending" : "ready";
}

export function ReadinessReview({
  readiness,
  acceptedTokens,
  migrationAccepted,
  onToggleAcceptance,
  onToggleMigration,
  disabled = false,
  decisionsOnly = false,
}: {
  readiness: LlmReadiness;
  acceptedTokens: Set<string>;
  migrationAccepted: boolean;
  onToggleAcceptance: (token: string) => void;
  onToggleMigration: () => void;
  disabled?: boolean;
  /** Saving one connection: show only what blocks the save or must be accepted, not the Team-wide table. */
  decisionsOnly?: boolean;
}) {
  const t = useTranslations("settings.models.v2");
  const tOverview = useTranslations("settings.models.overview");
  // Role keys and capability codes are identifiers; the reader sees the names used on the assignments tab (F4).
  const roleName = (roleKey: string) => tOverview(roleTranslationKey(roleKey));
  const capabilityName = (capability: string) =>
    CAPABILITIES.has(capability) ? tOverview(`capability_${capability}`) : capability;

  const pending = pendingAcceptances(readiness, acceptedTokens);
  const status = (
    <b className={headerTone(readiness, acceptedTokens)}>
      {/* ★ Three states, not two: with `blocking=0` but outstanding risk acknowledgments, Apply is actually disabled—
          this previously still said Can Apply, making page copy disagree with system behavior. */}
      {readiness.blocking.length
        ? t("readiness_blocked", { count: readiness.blocking.length })
        : pending
          ? t("readiness_pending_acceptance", { count: pending })
          : t("readiness_ready")}
    </b>
  );
  const rowView = (row: LlmReadinessRow) => (
    <div
      key={`${row.role_key}:${row.provider_id}:${row.model_id}:${row.capability}`}
      className={`readiness-row status-${row.status}`}
    >
      <span className="readiness-icon">
        {row.status === "ready" || row.status === "risk_accepted"
          ? <Check size={12} />
          : row.status === "blocked"
            ? <AlertTriangle size={12} />
            : <Info size={12} />}
      </span>
      <span>
        <b>{row.model_id}</b>
        <small>{row.provider_display_name} · {roleName(row.role_key)} · {capabilityName(row.capability)}</small>
      </span>
      <em>{t(`readiness_status_${row.status}`)}</em>
    </div>
  );
  // ★ Explicitly say users can save without verification; Apply checks only blocking rows and completed
  // acknowledgments. Verifying while this review is open invalidates it (the hash covers each row's fact), so the
  // copy sends the reader back rather than offering verification here.
  const acceptances = (
    <>
      {readiness.required_acceptances.length ? (
        <p className="readiness-optional">{t("readiness_verification_optional")}</p>
      ) : null}
      {readiness.required_acceptances.map((acceptance) => (
        <label className="readiness-acceptance" key={acceptance.acceptance_token}>
          <input
            type="checkbox"
            disabled={disabled}
            checked={acceptedTokens.has(acceptance.acceptance_token)}
            onChange={() => onToggleAcceptance(acceptance.acceptance_token)}
          />
          <span>
            <b>{t(riskCopyKey("title", acceptance.risk_code))}</b>
            <small>
              {t(riskCopyKey("body", acceptance.risk_code), {
                model: acceptance.model_id,
                capability: capabilityName(acceptance.capability),
                role: roleName(acceptance.role_key),
              })}
            </small>
          </span>
        </label>
      ))}
      {readiness.migration_acceptance ? (
        <label className="readiness-acceptance migration">
          <input
            type="checkbox"
            disabled={disabled}
            checked={migrationAccepted}
            onChange={onToggleMigration}
          />
          <span>
            <b>{t("migration_accept_title")}</b>
            <small>{t("migration_accept_body")}</small>
          </span>
        </label>
      ) : null}
      {readiness.baseline_will_invalidate ? (
        <div className="readiness-invalidation">
          <AlertTriangle size={14} />
          <span>{t("baseline_invalidation")}</span>
        </div>
      ) : null}
    </>
  );

  if (decisionsOnly) {
    const blocked = readiness.rows.filter((row) => row.status === "blocked");
    return (
      <section className="readiness-review" aria-labelledby="mset-readiness-title">
        {/* No status pill: the dialog's Save button carries the same state, and "apply" is not this dialog's word. */}
        <header>
          <div>
            <h3 id="mset-readiness-title">{t("decisions_title")}</h3>
          </div>
        </header>
        {blocked.length ? <div className="readiness-rows">{blocked.map(rowView)}</div> : null}
        {acceptances}
      </section>
    );
  }

  const attention = readiness.rows.filter((row) => !SETTLED.has(row.status));
  const settled = readiness.rows.filter((row) => SETTLED.has(row.status));
  return (
    <section className="readiness-review" aria-labelledby="mset-readiness-title">
      <header>
        <div>
          <span>{t("readiness_kicker")}</span>
          <h3 id="mset-readiness-title">{t("readiness_title")}</h3>
        </div>
        {status}
      </header>
      {acceptances}
      {attention.length ? <div className="readiness-rows">{attention.map(rowView)}</div> : null}
      {settled.length ? (
        <details className="readiness-settled">
          {/* "其余" only makes sense under rows shown above it (walk W8). */}
          <summary>{t(attention.length ? "readiness_settled" : "readiness_settled_all", { count: settled.length })}</summary>
          <div className="readiness-rows">{settled.map(rowView)}</div>
        </details>
      ) : null}
      <div className="readiness-external">
        {readiness.external_rows.map((row) => (
          <span key={row.subject_kind}>
            {t(`external_${row.subject_kind}`)} · {t(`external_status_${row.status}`)}
          </span>
        ))}
      </div>
    </section>
  );
}
