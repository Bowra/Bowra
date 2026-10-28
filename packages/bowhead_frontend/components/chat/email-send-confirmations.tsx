"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  emailSendConfirmationApi,
  type EmailSendConfirmation,
} from "@/lib/email-send-confirmation-api";
import { useHasActiveToolStep } from "@/lib/turn-store";
import { DialogShell } from "@/components/ui/dialog";
import { useConfirmationQueuePoll } from "@/lib/use-confirmation-queue-poll";
import { formatByteSize } from "@/lib/format";

/**
 * The Lead's send_email tool waits here before delivery: the owner sees the exact
 * recipient, subject and body, and only "send" releases it. Same dialog as personal
 * MCP call confirmation and the same polling policy (useConfirmationQueuePoll); a read
 * failure never implies consent (the server expires it).
 */
export function EmailSendConfirmations() {
  const t = useTranslations("emailSendConfirmation");
  const [pending, setPending] = useState<EmailSendConfirmation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const current = pending[0];
  // Only poll while an email can actually be waiting: a send_email step is running in a
  // turn this tab knows about, or a confirmation is already on screen. One read on mount
  // covers a reload in the middle of a wait (replay repopulates the step afterwards anyway).
  const sendEmailRunning = useHasActiveToolStep("send_email");
  const [mountCheck, setMountCheck] = useState(true);
  useConfirmationQueuePoll({
    enabled: mountCheck || sendEmailRunning || pending.length > 0,
    load: async () => {
      try {
        const data = await emailSendConfirmationApi<{
          confirmations: EmailSendConfirmation[];
        }>("");
        return data.confirmations;
      } catch (failure) {
        setMountCheck(false);
        throw failure;
      }
    },
    // Same update batch as the items, so `enabled` never flickers off while a
    // confirmation is arriving (a flicker would restart the poll).
    onItems: (items) => {
      setPending(items);
      setMountCheck(false);
    },
  });
  if (!current) return null;
  async function decide(decision: "approve" | "reject") {
    if (!current || busy) return;
    setBusy(true);
    setError(false);
    try {
      await emailSendConfirmationApi(`/${current.id}/decision`, "POST", {
        decision,
      });
      setPending((items) => items.filter((item) => item.id !== current.id));
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    // Only buttons may decide consent: Esc and backdrop clicks do not close (onClose empty). Remount by item key,
    // refocusing Reject when the next confirmation appears. Mount to body at the top layer, as with personal MCP consent.
    <DialogShell
      key={current.id}
      onClose={() => undefined}
      busy={busy}
      closeOnBackdrop={false}
      layer="top"
      labelledBy="email-send-confirmation-title"
      backdropClassName="personal-mcp-confirmation-backdrop"
      panelClassName="personal-mcp-confirmation"
    >
      <h2 id="email-send-confirmation-title">{t("title")}</h2>
      <p>{t("warning")}</p>
      <p>{t("to", { to: current.to })}</p>
      <p>{t("subject", { subject: current.subject })}</p>
      <pre>{current.body}</pre>
      {current.attachments?.length ? (
        <section aria-label={t("attachments")}>
          <p>{t("attachments")}</p>
          <ul>
            {current.attachments.map((attachment) => (
              <li key={attachment.artifact_id}>
                {attachment.filename} ({formatByteSize(attachment.size) || "0 B"})
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <p>
        {t("expires", {
          time: new Date(current.expires_at).toLocaleTimeString(),
        })}
      </p>
      {error ? <p role="alert">{t("decisionError")}</p> : null}
      <div className="personal-mcp-confirmation-actions">
        <button
          data-dialog-initial-focus=""
          type="button"
          disabled={busy}
          onClick={() => void decide("reject")}
        >
          {t("reject")}
        </button>
        <button
          className="personal-mcp-confirmation-allow-once"
          type="button"
          disabled={busy}
          onClick={() => void decide("approve")}
        >
          {t("approve")}
        </button>
      </div>
    </DialogShell>
  );
}
