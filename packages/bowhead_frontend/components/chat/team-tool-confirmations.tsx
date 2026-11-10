"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { DialogShell } from "@/components/ui/dialog";
import { formatTime } from "@/lib/locale";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { useHasActiveToolStep } from "@/lib/turn-store";
import {
  teamToolConfirmationApi,
  type TeamToolConfirmation,
} from "@/lib/team-tool-confirmation-api";
import { useConfirmationQueuePoll } from "@/lib/use-confirmation-queue-poll";

/** Requester review of non-read Team MCP calls; shown only when the deployment enables it. */
export function TeamToolConfirmations() {
  const { me } = useAuthSession();
  return me?.capabilities?.team_tool_confirmations === true ? <TeamToolConfirmationQueue /> : null;
}

function TeamToolConfirmationQueue() {
  const t = useTranslations("teamToolConfirmation");
  const locale = useLocale();
  const [pending, setPending] = useState<TeamToolConfirmation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  // Same gating as email and Shopify: poll only while a Team tool step is running in a turn this
  // tab knows about, or a confirmation is already on screen. One read on mount covers a reload in
  // the middle of a wait.
  const teamToolRunning = useHasActiveToolStep("teammcp.", "prefix");
  const [mountCheck, setMountCheck] = useState(true);
  useConfirmationQueuePoll({
    enabled: mountCheck || teamToolRunning || pending.length > 0,
    load: async () => {
      try {
        return (await teamToolConfirmationApi<{ confirmations: TeamToolConfirmation[] }>("")).confirmations;
      } catch (failure) {
        setMountCheck(false);
        throw failure;
      }
    },
    // Same update batch as the items, so `enabled` never flickers off while a confirmation is arriving.
    onItems: (items) => {
      setPending(items);
      setMountCheck(false);
    },
  });
  const current = pending[0];
  if (!current) return null;
  async function decide(decision: "approve" | "approve_session" | "reject") {
    if (!current || busy) return;
    setBusy(true);
    setError(false);
    try {
      await teamToolConfirmationApi(`/${encodeURIComponent(current.id)}/decision`, "POST", { decision });
      setPending((items) => items.filter((item) => item.id !== current.id));
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    // Same layer and focus rules as the personal MCP consent dialog: only buttons decide, and the
    // top layer keeps it above other open dialogs while polling delivers new items.
    <DialogShell
      key={current.id}
      onClose={() => undefined}
      busy={busy}
      closeOnBackdrop={false}
      layer="top"
      labelledBy="team-tool-confirmation-title"
      backdropClassName="personal-mcp-confirmation-backdrop"
      panelClassName="personal-mcp-confirmation"
    >
      <h2 id="team-tool-confirmation-title">{t("title")}</h2>
      <p>
        {current.source_name} · {current.tool_name}
      </p>
      <p>{current.description}</p>
      <p>
        {t("notice")}{" "}
        {current.category === "external_write"
          ? t("riskExternalWrite")
          : current.category === "destructive"
            ? t("riskDestructive")
            : t("riskUnknown")}
      </p>
      <pre>{JSON.stringify(current.arguments, null, 2)}</pre>
      <p>
        {t("expires", {
          time: formatTime(current.expires_at, locale, { seconds: true }),
        })}
      </p>
      {error ? <p role="alert">{t("decisionError")}</p> : null}
      <div className="personal-mcp-confirmation-actions">
        <button data-dialog-initial-focus="" type="button" disabled={busy} onClick={() => void decide("reject")}>
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
