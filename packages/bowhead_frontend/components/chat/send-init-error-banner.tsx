"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { AlertTriangle } from "@/components/icons";
import { appPath } from "@/lib/base-path";
import {
  getChatSendErrorMessage,
  resolveModelUnconfiguredPresentation,
} from "@/lib/chat-send-errors";

export function SendInitErrorBanner({
  reasonCode,
}: {
  reasonCode: string | null | undefined;
}) {
  const t = useTranslations("chat_send_errors.model_unconfigured");
  // credential-readiness D5: team_model_unconfigured branches by can_act—admins get
  // a direct Configure link (appPath adds the runtime prefix, CLAUDE.md P0); members get a contact list.
  // Before async enhancement, render role-neutral baseline copy; any query failure also stays at that baseline (never throws).
  const [presentation, setPresentation] = useState<{
    message: string;
    canAct: boolean;
  } | null>(null);

  useEffect(() => {
    setPresentation(null);
    if (reasonCode !== "team_model_unconfigured") return;
    let cancelled = false;
    void resolveModelUnconfiguredPresentation().then((p) => {
      if (!cancelled) setPresentation(p);
    });
    return () => {
      cancelled = true;
    };
  }, [reasonCode]);

  if (!reasonCode) return null;

  const message = presentation?.message ?? getChatSendErrorMessage(reasonCode);
  const showCta = reasonCode === "team_model_unconfigured" && presentation?.canAct;

  return (
    <div
      role="alert"
      aria-live="assertive"
      data-testid="send-init-error"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "10px 16px",
        color: "var(--c-danger)",
        background: "color-mix(in srgb, var(--c-danger) 8%, var(--c-bg))",
        borderBottom: "1px solid color-mix(in srgb, var(--c-danger) 24%, var(--c-line))",
        fontSize: "0.8125rem",
        lineHeight: 1.45,
      }}
    >
      <AlertTriangle size={15} aria-hidden="true" />
      <span>{message}</span>
      {showCta ? (
        <a
          href={appPath("/team/settings/models/")}
          data-testid="send-init-error-configure"
          style={{ color: "inherit", fontWeight: 600, textDecoration: "underline", whiteSpace: "nowrap" }}
        >
          {t("configure")}
        </a>
      ) : null}
    </div>
  );
}
