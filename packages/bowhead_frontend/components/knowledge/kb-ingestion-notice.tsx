"use client";

import { useTranslations } from "next-intl";

import { appPath } from "@/lib/base-path";
import type { KbIngestionReadiness } from "@/lib/kb-ingestion-readiness";

export function KbIngestionNotice({ state, canConfigure, onRetry }: {
  state: KbIngestionReadiness;
  canConfigure: boolean;
  onRetry: () => void;
}) {
  const t = useTranslations("knowledge_base.ingestion_readiness");
  if (state === "ready") return null;
  return (
    <div role={state === "checking" ? "status" : "alert"} className={`kb-ingestion-notice ${state === "checking" ? "checking" : "error"}`} data-testid="kb-ingestion-readiness">
      <span>{t(state === "missing" && !canConfigure ? "missing_member" : state)}</span>
      {state === "missing" && canConfigure ? (
        <a href={appPath("/team/settings/models/")} target="_blank" rel="noopener noreferrer">{t("configure")}</a>
      ) : null}
      {state !== "checking" ? <button type="button" onClick={onRetry}>{t("retry")}</button> : null}
    </div>
  );
}
