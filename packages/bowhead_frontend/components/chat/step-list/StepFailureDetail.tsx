"use client";

/**
 * StepFailureDetail — the inline explanation a failed trace row discloses when the user opens it.
 *
 * Two short lines inside the row's existing detail slot: the reason in the interface language with the
 * published facts that name what went wrong (`field=columns` → "Input: columns."), the user's next step when
 * one exists, then the diagnostic reference with a copy action. No panel, raw code, JSON or internal
 * identifier: the earlier failure body stacked a two-sentence paragraph on a "View technical details" panel
 * listing surface, version, error and diagnostic codes, and it took more room than the row it explained.
 *
 * Every input is persisted and replayed (`step_done.error` code and diagnostics, `done.step_outcome`), so
 * live, refreshed and replayed rows read the same.
 */
import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { copyDiagnostics } from "@/components/error-notice";
import { LucideGlyph } from "@/components/icon-refresh";
import { normalizeErrorDiagnostics } from "@/lib/error-presentation";

import { useStepIssuesRecovered } from "./step-recovery";
import { useTurnRunning } from "./turn-running";
import type { StepView } from "./types";

type StepError = NonNullable<StepView["error"]>;

const FACT_MESSAGE_KEYS = {
  field: "fact_field",
  missing_key: "fact_missing_key",
  exception: "fact_exception",
  exit_code: "fact_exit_code",
} as const;
type FailureFactKey = keyof typeof FACT_MESSAGE_KEYS;

/** Internal record identifiers (`sec_…`, `art_…`, `t_…`) never reach the row, even inside a published fact. */
const INTERNAL_ID = /\b[a-z][a-z0-9]{0,11}_[0-9a-f]{6,}\b/i;

/**
 * Published `safe_detail` facts the row names (`field=columns; missing_key=bm`), at most two: the argument, then
 * the most specific cause of a code failure (a missing key, else the exception type, else the exit code).
 * Unknown facts, such as the finite `phase=…; failure_class=…` pairs, stay in the copied diagnostics only.
 */
export function readableFailureFacts(safeDetail: string | undefined): Array<{ key: FailureFactKey; value: string }> {
  const published = new Map<FailureFactKey, string>();
  for (const part of (safeDetail ?? "").split(";")) {
    const separator = part.indexOf("=");
    const key = part.slice(0, separator).trim();
    const value = part.slice(separator + 1).trim();
    if (separator > 0 && Object.hasOwn(FACT_MESSAGE_KEYS, key) && value && !INTERNAL_ID.test(value) && !published.has(key as FailureFactKey)) {
      published.set(key as FailureFactKey, value);
    }
  }
  const cause = (["missing_key", "exception", "exit_code"] as const).find((key) => published.has(key));
  return (["field", cause] as const).flatMap((key) => (key && published.has(key) ? [{ key, value: published.get(key)! }] : []));
}

/**
 * The user's next step after the Turn, for the shared Tool failure codes. Other codes carry their own action in
 * their reason sentence. An unconfirmed outcome keeps its check even after a later success: a repeated
 * operation may have taken effect twice.
 */
const ACTION_KEYS: Record<string, "action_check_source" | "action_check_effect" | "action_follow_up"> = {
  tool_resource_unavailable: "action_check_source",
  tool_execution_timeout: "action_check_effect",
  tool_execution_unknown: "action_check_effect",
  tool_arguments_invalid: "action_follow_up",
  tool_code_failed: "action_follow_up",
  tool_execution_failed: "action_follow_up",
  tool_delivery_review_required: "action_follow_up",
};
const UNCONFIRMED_OUTCOME_CODES = new Set(["tool_execution_timeout", "tool_execution_unknown"]);

export interface StepFailureDetailProps {
  error: StepError;
  /** Localized reason sentence for this error (see `stepFailureReason`). */
  reason: string;
}

export function StepFailureDetail({ error, reason }: StepFailureDetailProps) {
  const t = useTranslations("step_list.failure_detail");
  const tDiagnostics = useTranslations("errors.diagnostics");
  const running = useTurnRunning();
  const recovered = useStepIssuesRecovered();
  const [copyState, setCopyState] = useState<"idle" | "success" | "failed">("idle");
  const code = error.code ?? "";
  const facts = readableFailureFacts(error.diagnostics?.safeDetail);
  // Only diagnostics that pass the shared disclosure checks can be copied, as in the shared diagnostics panel.
  const diagnostics = error.diagnostics && normalizeErrorDiagnostics(error.diagnostics);
  const reference = diagnostics?.occurrenceId;
  // While the Turn runs the agent may still handle the failure; afterwards the persisted receipt decides.
  const actionKey = running
    ? null
    : recovered && !UNCONFIRMED_OUTCOME_CODES.has(code)
      ? "action_recovered"
      : (ACTION_KEYS[code] ?? null);
  // Sentences after a full-width stop (Chinese) follow without a space; the other interface languages separate them.
  const gap = /[\u3002\uff01\uff1f]$/.test(reason) ? "" : " ";
  const copy = async () => {
    if (!diagnostics) return;
    setCopyState((await copyDiagnostics(diagnostics, "chat", tDiagnostics("copyFallbackPrompt"))) ? "success" : "failed");
  };
  return (
    <div className="stp-fail" data-testid="step-failure-detail">
      <p className="stp-fail__why">
        {reason}
        {facts.map(({ key, value }) => (
          <span key={key} className="stp-fail__fact">
            {gap + t(FACT_MESSAGE_KEYS[key], { value })}
          </span>
        ))}
        {actionKey ? <span className="stp-fail__action">{gap + t(actionKey)}</span> : null}
      </p>
      {diagnostics ? (
        // The reference identifies this failure for support; without one the copied diagnostics still carry its facts.
        <p className="stp-fail__ref">
          {reference ? (
            <>
              <span>{tDiagnostics("occurrenceId")}</span>{" "}
              <code translate="no" title={reference}>{reference}</code>
            </>
          ) : null}
          <button
            type="button"
            className={reference ? "stp-fail__copy" : "stp-fail__copy stp-fail__copy--text"}
            onClick={() => void copy()}
            aria-label={reference ? tDiagnostics("copy") : undefined}
            title={tDiagnostics("copy")}
          >
            <LucideGlyph icon={copyState === "success" ? Check : Copy} size={13} aria-hidden="true" />
            {reference ? null : tDiagnostics("copyShort")}
          </button>
          <span className="sr-only" role="status">
            {copyState === "success" ? tDiagnostics("copySuccess") : copyState === "failed" ? tDiagnostics("copyFailed") : ""}
          </span>
        </p>
      ) : null}
    </div>
  );
}
