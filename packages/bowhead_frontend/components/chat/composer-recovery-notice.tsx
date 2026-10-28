"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

interface Props {
  onRetry: () => Promise<boolean>;
  onAbandon: () => Promise<boolean>;
  variant?: "submission" | "terminalAction";
  allowAbandon?: boolean;
}

function focusVisibleComposer(): void {
  window.setTimeout(() => {
    const inputs = Array.from(
      document.querySelectorAll<HTMLElement>(
        '[data-composer-layout-v2] [contenteditable="true"], [data-composer-layout-v2] textarea:not([disabled])',
      ),
    );
    const target = inputs.find((input) => input.getClientRects().length > 0)
      ?? inputs[0];
    target?.focus();
  }, 0);
}

export function ComposerRecoveryNotice({
  onRetry,
  onAbandon,
  variant = "submission",
  allowAbandon = true,
}: Props) {
  const t = useTranslations("composer");
  const [busy, setBusy] = useState<"retry" | "abandon" | null>(null);
  const [failed, setFailed] = useState(false);

  const run = async (
    action: "retry" | "abandon",
    callback: () => Promise<boolean>,
  ) => {
    if (busy) return;
    setBusy(action);
    setFailed(false);
    try {
      if (await callback()) focusVisibleComposer();
      else setFailed(true);
    } catch {
      setFailed(true);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      className="composer-recovery-notice"
      aria-label={t("submission_recovery_title")}
    >
      <div className="composer-recovery-notice__copy">
        <strong>{t("submission_recovery_title")}</strong>
        <p role="status">
          {t(variant === "terminalAction"
            ? "action_recovery_pending"
            : "submission_recovered")}
        </p>
        {failed ? (
          <p className="composer-recovery-notice__error" role="alert">
            {t(variant === "terminalAction"
              ? "action_recovery_failed"
              : "submission_recovery_failed")}
          </p>
        ) : null}
      </div>
      <div className="composer-recovery-notice__actions">
        <button
          type="button"
          className="composer-recovery-notice__primary"
          disabled={busy !== null}
          onClick={() => void run("retry", onRetry)}
        >
          {busy === "retry"
            ? t(variant === "terminalAction"
              ? "action_recovery_checking"
              : "submission_retrying")
            : t(variant === "terminalAction"
              ? "action_recovery_check"
              : "submission_retry")}
        </button>
        {allowAbandon ? (
          <button
            type="button"
            className="composer-recovery-notice__secondary"
            disabled={busy !== null}
            onClick={() => void run("abandon", onAbandon)}
          >
            {busy === "abandon"
              ? t(variant === "terminalAction"
                ? "action_recovery_resending"
                : "submission_abandoning")
              : t(variant === "terminalAction"
                ? "action_recovery_resend"
                : "submission_abandon")}
          </button>
        ) : null}
      </div>
    </section>
  );
}
