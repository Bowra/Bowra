"use client";

import { useTranslations } from "next-intl";

import { Spinner } from "@/components/icons";
import { Button } from "@/components/ui/button";
import type { VerificationBatch } from "@/lib/llm-config-v2-api";

/**
 * One line in the connection editor's save bar for a running or finished verification batch.
 *
 * The per-capability results live on the model rows (chips and failure lines), so this line carries only progress,
 * the count and the next action (design: docs/sprints/model-settings-save-flow-2026-11-25/design.md §3.3).
 */
export function VerificationStatus({
  batch,
  busy,
  onCancel,
  onRetry,
  onDismiss,
}: {
  batch: VerificationBatch;
  busy: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const t = useTranslations("settings.models.v2");
  const counts = batch.counts || {};
  const verified = counts.verified || 0;
  const failed = (counts.failed || 0) + (counts.errored || 0);
  const notTestable = counts.not_testable || 0;
  const unfinished = (counts.cancelled || 0) + (counts.interrupted || 0);
  const pending = (counts.queued || 0) + (counts.verifying || 0);
  const total = verified + failed + notTestable + unfinished + pending;
  const running = pending > 0 || !["completed", "cancelled", "interrupted"].includes(batch.state);

  if (running) {
    return (
      <span className="verification-status" role="status">
        <Spinner size={12} aria-hidden /> {t("status_running", { done: total - pending, total })}
        <Button type="button" size="sm" disabled={busy} onClick={onCancel}>{t("status_stop")}</Button>
      </span>
    );
  }
  const parts = [t("status_verified", { count: verified })];
  if (failed) parts.push(t("status_failed", { count: failed }));
  if (notTestable) parts.push(t("status_not_testable", { count: notTestable }));
  if (unfinished) parts.push(t("status_unfinished", { count: unfinished }));
  const retryable = failed + unfinished;
  return (
    <span className="verification-status" role="status">
      {t(batch.state === "completed" ? "status_done" : "status_stopped", { summary: parts.join(t("status_separator")) })}
      {retryable ? (
        <Button type="button" size="sm" disabled={busy} onClick={onRetry}>
          {t("verification_retry_unfinished", { count: retryable })}
        </Button>
      ) : null}
      <Button type="button" size="sm" variant="ghost" onClick={onDismiss}>{t("status_dismiss")}</Button>
    </span>
  );
}
