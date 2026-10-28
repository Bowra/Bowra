"use client";

import { useTranslations } from "next-intl";

import { AlertTriangle, Check, Spinner, XSm } from "@/components/icons";
import type {
  VerificationBatch,
  VerificationPlan,
} from "@/lib/llm-config-v2-api";

const TERMINAL = new Set(["completed", "cancelled", "interrupted"]);
const FAILURE_MESSAGE_KEYS: Readonly<Record<string, string>> = {
  auth_failed: "failure_auth",
  forbidden: "failure_forbidden",
  api_version_invalid: "failure_api_version",
  deployment_not_found: "failure_deployment",
  endpoint_not_found: "failure_endpoint",
  parameter_incompatible: "failure_parameter",
  invalid_request: "failure_request",
  connection_timeout: "failure_timeout",
  dns_error: "failure_dns",
  tls_error: "failure_tls",
  provider_unavailable: "failure_unavailable",
  rate_limited: "failure_rate_limited",
  invalid_response: "failure_response",
  tool_call_missing: "failure_tool",
  embedding_missing: "failure_embedding",
  model_identity_mismatch: "failure_model_identity",
  blocked_by_security_policy: "failure_security_policy",
  empty_completion: "failure_empty_completion",
  embedding_non_finite: "failure_embedding_non_finite",
  internal_probe_error: "failure_internal_probe",
  capability_probes_disabled: "failure_probes_disabled",
  runner_interrupted: "failure_runner_interrupted",
};

export function VerificationPanel({
  plan,
  batch,
  busy,
  onConfirm,
  onCancel,
  onDismiss,
  onRetry,
}: {
  plan: VerificationPlan;
  batch: VerificationBatch | null;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  onDismiss: () => void;
  onRetry?: () => void;
}) {
  const t = useTranslations("settings.models.v2");
  const capabilityLabel = useTranslations("settings.models.caps");
  const terminal = Boolean(batch && TERMINAL.has(batch.state));
  const stateByItem = new Map(
    (batch?.items || []).map((item) => [
      `${item.provider_id}:${item.model_id}:${item.capability}`,
      item,
    ]),
  );
  const verifiedCount = batch?.counts.verified || 0;
  const failedCount = batch?.counts.failed || 0;
  const notTestableCount = batch?.counts.not_testable || 0;
  const unfinishedCount = (batch?.counts.errored || 0)
    + (batch?.counts.cancelled || 0)
    + (batch?.counts.interrupted || 0);
  const pendingCount = (batch?.counts.queued || 0) + (batch?.counts.verifying || 0);
  const retryableCount = failedCount + unfinishedCount;

  return (
    <section className="verification-plan">
      <header>
        <div>
          <span>{t("verification_kicker")}</span>
          <h3>{batch
            ? t(terminal ? "verification_finished" : "verification_running")
            : t("verification_confirm_title")}</h3>
        </div>
        {terminal ? (
          <button type="button" className="iconbtn" onClick={onDismiss} aria-label={t("close")}>
            <XSm size={14} />
          </button>
        ) : null}
      </header>
      {!batch ? (
        <div className="verification-cost">
          <AlertTriangle size={14} />
          <div>
            <b>{t("verification_item_count", { count: plan.item_count })}</b>
            <b>{t("verification_request_count", { count: plan.provider_request_count })}</b>
            <b>{t("verification_concurrency", { count: plan.max_concurrency })}</b>
            <span>{t("verification_charge_note")}</span>
          </div>
        </div>
      ) : null}
      {batch ? (
        <div className="verification-progress" role="status" aria-live="polite">
          {t("verification_progress", {
            verified: verifiedCount,
            failed: failedCount,
            notTestable: notTestableCount,
            unfinished: unfinishedCount,
            pending: pendingCount,
          })}
        </div>
      ) : null}
      <div className="verification-items" aria-live="polite">
        {plan.items.map((item) => {
          const current = stateByItem.get(
            `${item.provider_id}:${item.model_id}:${item.capability}`,
          );
          const state = current?.state
            || (batch ? "queued" : item.testability);
          const positive = state === "verified";
          const failed = state === "failed" || state === "errored";
          const failureKey = failed
            ? FAILURE_MESSAGE_KEYS[current?.safe_code || ""] || "failure_generic"
            : null;
          return (
            <div
              key={`${item.provider_id}:${item.model_id}:${item.capability}`}
              className={`verification-item state-${state}`}
            >
              <span className="verification-state">
                {state === "verifying" || state === "queued"
                  ? <Spinner size={12} />
                  : positive
                    ? <Check size={12} />
                    : state === "failed" || state === "errored"
                      ? <AlertTriangle size={12} />
                      : null}
              </span>
              <span>
                <b>{item.model_id}</b>
                <small>
                  {item.provider_display_name} · {capabilityLabel(item.capability)}
                </small>
                {state === "not_testable" && (current?.safe_code || item.safe_code) === "parameter_policy_exceeds_probe_limits" ? <small>{t("parameter_probe_limits")}</small> : null}
                {failureKey ? (
                  <span className="verification-failure-detail">
                    <strong>{t(failureKey)}</strong>
                    <span className="verification-diagnostics">
                      {current?.safe_http_status ? (
                        <span>{t("failure_http_status", { status: current.safe_http_status })}</span>
                      ) : null}
                      {current?.safe_code ? (
                        <span>
                          {t("failure_code_label")} {" "}
                          <code translate="no">{current.safe_code}</code>
                        </span>
                      ) : null}
                      {current?.safe_summary_code ? (
                        <span>
                          {t("failure_detail_label")} {" "}
                          <code translate="no">{current.safe_summary_code}</code>
                        </span>
                      ) : null}
                    </span>
                  </span>
                ) : null}
              </span>
              <em>{t(`item_state_${state}`)}</em>
            </div>
          );
        })}
      </div>
      <footer>
        {!batch ? (
          <>
            <button type="button" className="btn ghost sm" onClick={onDismiss}>{t("cancel")}</button>
            <button type="button" className="btn pri sm" disabled={busy} onClick={onConfirm}>
              {busy ? <Spinner size={13} /> : null}
              {t("verification_confirm")}
            </button>
          </>
        ) : !terminal ? (
          <button type="button" className="btn ghost sm" disabled={busy} onClick={onCancel}>
            {t("verification_cancel")}
          </button>
        ) : (
          <>
            {retryableCount > 0 && onRetry ? (
              <button type="button" className="btn sec sm" disabled={busy} onClick={onRetry}>
                {t("verification_retry_unfinished", { count: retryableCount })}
              </button>
            ) : null}
            <button type="button" className="btn ghost sm" onClick={onDismiss}>{t("close")}</button>
          </>
        )}
      </footer>
    </section>
  );
}
