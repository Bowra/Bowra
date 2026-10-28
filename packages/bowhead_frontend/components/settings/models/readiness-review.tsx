"use client";

import { useTranslations } from "next-intl";

import { AlertTriangle, Check, Info } from "@/components/icons";
import type { LlmReadiness } from "@/lib/llm-config-api";

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
}: {
  readiness: LlmReadiness;
  acceptedTokens: Set<string>;
  migrationAccepted: boolean;
  onToggleAcceptance: (token: string) => void;
  onToggleMigration: () => void;
  disabled?: boolean;
}) {
  const t = useTranslations("settings.models.v2");
  return (
    <section className="readiness-review" aria-labelledby="mset-readiness-title">
      <header>
        <div>
          <span>{t("readiness_kicker")}</span>
          <h3 id="mset-readiness-title">{t("readiness_title")}</h3>
        </div>
        {/* ★ Three states, not two: with `blocking=0` but outstanding risk acknowledgments, Apply is actually disabled—
            this previously still said Can Apply, making page copy disagree with system behavior. */}
        <b className={headerTone(readiness, acceptedTokens)}>
          {readiness.blocking.length
            ? t("readiness_blocked", { count: readiness.blocking.length })
            : pendingAcceptances(readiness, acceptedTokens)
              ? t("readiness_pending_acceptance", {
                  count: pendingAcceptances(readiness, acceptedTokens),
                })
              : t("readiness_ready")}
        </b>
      </header>
      <div className="readiness-rows">
        {readiness.rows.map((row) => (
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
              <small>{row.provider_display_name} · {row.role_key} · {row.capability}</small>
            </span>
            <em>{t(`readiness_status_${row.status}`)}</em>
          </div>
        ))}
      </div>
      {/* ★ Explicitly say users can apply without verification. This was always the mechanism (Apply checks only blocking and completed acknowledgments),
          but users had to infer it from checkboxes. Also identify verification's location—**outside this surface**:
          review uses a frozen draft semantic snapshot; `draft_semantic_hash` includes each row's fact_state and
          evidence_id, so verifying during review immediately invalidates it (ReadinessReviewStale).
          Keep verification in the overview evidence table, where each item can be checked. */}
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
                capability: acceptance.capability,
                role: acceptance.role_key,
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
