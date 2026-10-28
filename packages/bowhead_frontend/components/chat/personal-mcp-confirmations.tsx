"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  personalMcpApi,
  type PersonalMcpConfirmation,
} from "@/lib/personal-mcp-api";
import { DialogShell } from "@/components/ui/dialog";
import { useConfirmationQueuePoll } from "@/lib/use-confirmation-queue-poll";

export { CONFIRMATION_POLL_MS } from "@/lib/use-confirmation-queue-poll";

/** Consent is read from the durable owner-only queue, including after reload. */
export function PersonalMcpConfirmations() {
  const t = useTranslations("personalMcp");
  const locale = useLocale();
  const [pending, setPending] = useState<PersonalMcpConfirmation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const current = pending[0];
  useConfirmationQueuePoll({
    gate: async () =>
      (await personalMcpApi<{ enabled: boolean }>("/capabilities")).enabled,
    load: async () =>
      (
        await personalMcpApi<{ confirmations: PersonalMcpConfirmation[] }>(
          "/calls/confirmations",
        )
      ).confirmations,
    onItems: setPending,
  });
  if (!current) return null;
  async function decide(decision: "approve" | "approve_session" | "reject") {
    if (!current || busy) return;
    setBusy(true);
    setError(false);
    try {
      await personalMcpApi(`/calls/${current.id}/decision`, "POST", {
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
    // Only buttons can decide consent: Esc and backdrop clicks do not dismiss it (onClose is empty). Remount by item key
    // so focus returns to「拒绝」when the next confirmation arrives.
    // Mount at the body top layer: polling can deliver confirmations while another dialog is open. Inline rendering is trapped by desktop
    // .v2-app-root (fixed, its own stacking context) at layer 0, below that dialog and made inert by it (design section「图层」).
    <DialogShell
      key={current.id}
      onClose={() => undefined}
      busy={busy}
      closeOnBackdrop={false}
      layer="top"
      labelledBy="personal-mcp-confirmation-title"
      backdropClassName="personal-mcp-confirmation-backdrop"
      panelClassName="personal-mcp-confirmation"
    >
      <h2 id="personal-mcp-confirmation-title">{t("confirmTitle")}</h2>
      <p>
        {current.agent_name} · {current.connection_name} · {current.tool_name}
      </p>
      <p>{current.description}</p>
      <p>{t("confirmWarning")}</p>
      <pre>{JSON.stringify(current.arguments, null, 2)}</pre>
      <p>
        {t("expires", {
          time: new Date(current.expires_at).toLocaleTimeString(locale),
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
          {t("approveOnce")}
        </button>
        <button
          className="personal-mcp-confirmation-allow-session"
          type="button"
          disabled={busy}
          onClick={() => void decide("approve_session")}
        >
          {t("approveSession")}
        </button>
      </div>
      <p className="personal-mcp-confirmation-session-note">{t("approveSessionNote")}</p>
    </DialogShell>
  );
}
