"use client";

import { useTranslations } from "next-intl";

import { ErrorDiagnosticsDisclosure } from "@/components/error-notice";
import type { ErrorDiagnostics } from "@/lib/error-presentation";

import type { FailureKind } from "./types";
import { useTurnRunning } from "./turn-running";

export interface FailureAckCardProps {
  kind: FailureKind;
  message: string;
  /** Known partial-batch outcomes, independent of current task activity. */
  failedCount?: number;
  succeededCount?: number;
  /** When true, render as a compact row inset (used inside StepRow). */
  inset?: boolean;
  diagnostics?: ErrorDiagnostics;
  retryable?: boolean;
}

/**
 * Keep the original failure visible without predicting another attempt.
 * Only the enclosing running task supports current Composer guidance.
 * Unknown, archived, paused and terminal views retain historical outcomes.
 */
export function FailureAckCard({
  kind,
  message,
  failedCount,
  succeededCount,
  inset = false,
  diagnostics,
  retryable,
}: FailureAckCardProps) {
  const tStep = useTranslations("step_list");
  const turnRunning = useTurnRunning();
  const showHint = turnRunning && kind !== "permanent"
    && retryable !== false && diagnostics?.retryable !== false;
  const showCounts = typeof failedCount === "number" && typeof succeededCount === "number";
  return (
    // Chat Cards Spec §7: a running step's failure is one sentence in its step row, not a card; the turn's
    // terminal failure is only the failure card. Styles: .bh-step-failure* in styles/ui-refresh-workbench.css.
    <div className={inset ? "bh-step-failure bh-step-failure--inset" : "bh-step-failure"}>
      <div className={showHint || showCounts ? "bh-step-failure__msg bh-step-failure__msg--hint" : "bh-step-failure__msg"}>
        {message}
      </div>
      {diagnostics ? (
        <ErrorDiagnosticsDisclosure
          diagnostics={diagnostics}
          surface="chat"
        />
      ) : null}
      {showCounts ? (
        <div className="bh-step-failure__hint">
          {tStep("partial_failure_counts", { failed: failedCount, ok: succeededCount })}
        </div>
      ) : null}
      {showHint ? (
        <span className="bh-step-failure__hint">
          {tStep("failure_running_hint")}
        </span>
      ) : null}
    </div>
  );
}
