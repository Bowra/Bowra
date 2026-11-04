"use client";

import { useState } from "react";

import { useTranslations } from "next-intl";

import { AlertTriangle, Check, Spinner, XSm } from "@/components/icons";
import { copyTextWithFallback } from "@/lib/clipboard";
import type {
  VerificationDiagnostics,
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

function EmptyResponseDetails({ diagnostics }: { diagnostics: VerificationDiagnostics }) {
  const t = useTranslations("settings.models.v2");
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const summary = diagnostics.response_summary;
  const limited = summary.stop_reason === "max_tokens" || summary.stop_reason === "max_output_tokens" || summary.stop_reason === "length";
  const stopLabel = limited ? t("diagnostic_stop_limit")
    : summary.stop_reason === "stop" || summary.stop_reason === "end_turn" ? t("diagnostic_stop_finished") : null;
  const contentLabels: Record<string, string> = {
    text: t("diagnostic_text"), output_text: t("diagnostic_text"),
    thinking: t("diagnostic_thinking"), reasoning: t("diagnostic_thinking"),
    tool_use: t("diagnostic_tool"), function_call: t("diagnostic_tool"),
  };

  return (
    <details className="verification-response-details" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{t("diagnostic_expand")}</summary>
      {open ? <div>
        <p>{t(limited ? "diagnostic_limit_hint" : "diagnostic_hint")}</p>
        <dl>
          <dt>{t("diagnostic_model")}</dt><dd>{summary.model || t("diagnostic_not_reported")}</dd>
          <dt>{t("diagnostic_stop")}</dt><dd>{stopLabel ? `${stopLabel} (${summary.stop_reason})` : summary.stop_reason || t("diagnostic_not_reported")}</dd>
          <dt>{t("diagnostic_content")}</dt><dd>{[...new Set(summary.content_types.map((kind) => contentLabels[kind] || kind))].join(", ") || t("diagnostic_none")}</dd>
          <dt>{t("diagnostic_usage")}</dt><dd className="verification-response-usage">{summary.usage ? <pre translate="no">{JSON.stringify(summary.usage, null, 2)}</pre> : t("diagnostic_not_reported")}</dd>
        </dl>
        <p>{t("diagnostic_request")}</p>
        <p>{t("diagnostic_request_hint")}</p>
        <pre translate="no">{JSON.stringify(diagnostics.request, null, 2)}</pre>
        <p>{t("diagnostic_response")}</p>
        <pre translate="no">{diagnostics.response_body}</pre>
        {diagnostics.response_truncated ? <p>{t("diagnostic_truncated")}</p> : null}
        <p>{t("diagnostic_keep_details")}</p>
        <p>{t("diagnostic_privacy")}</p>
        <button type="button" className="btn ghost sm" onClick={async () => {
          const bundle = JSON.stringify(diagnostics, null, 2);
          setCopied(await copyTextWithFallback(bundle, t("diagnostic_copy_fallback"), JSON.stringify(diagnostics)));
        }}>{t("diagnostic_copy")}</button>
        <span role="status">{copied ? t("diagnostic_copied") : ""}</span>
      </div> : null}
    </details>
  );
}

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
              <div className="verification-item-main">
                <b>{item.model_id}</b>
                <small>
                  {item.provider_display_name} · {capabilityLabel(item.capability)}
                </small>
                {state === "not_testable" && (current?.safe_code || item.safe_code) === "parameter_policy_exceeds_probe_limits" ? <small>{t("parameter_probe_limits")}</small> : null}
                {failureKey ? (
                  <div className="verification-failure-detail">
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
                    {current?.safe_code === "empty_completion" ? (
                      current.diagnostics
                        ? <EmptyResponseDetails diagnostics={current.diagnostics} />
                        : <small>{t("diagnostic_unavailable")}</small>
                    ) : null}
                  </div>
                ) : null}
              </div>
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
