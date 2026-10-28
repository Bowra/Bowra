"use client";

import type { ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import type { AgentCatalogItem } from "@/lib/agents-api";
import { availabilityReason, limitedAvailability, type AvailabilityReasonView, type LimitedView } from "@/lib/agent-availability";

/** Reason keys are chosen at run time from the public code, so they cannot be typed per key; this is the one loose call. */
export function reasonText(t: unknown, key: string, values?: AvailabilityReasonView["values"] | LimitedView["values"]): string {
  return (t as (key: string, values?: Record<string, string | number>) => string)(key, values);
}

/** Why this Agent is not available to the viewer (one public reason), or null when nothing refuses. */
export function useAvailabilityReason(item: AgentCatalogItem): AvailabilityReasonView | null {
  const t = useTranslations("agents_marketplace");
  return availabilityReason(item, t("team_member"));
}

export function ReasonChip({ view }: { view: AvailabilityReasonView }) {
  const t = useTranslations("agents_marketplace");
  return (
    <span className={`am-status ${view.code === "temporarily_unavailable" ? "am-status-warning" : "am-status-unavailable"}`} data-reason={view.code}>
      {t(view.chipKey)}
    </span>
  );
}

/** The short sentence and, when the member can do nothing, who can. */
export function ReasonLine({ view, className = "am-reason-line" }: { view: AvailabilityReasonView; className?: string }) {
  const t = useTranslations("agents_marketplace");
  return (
    <p className={className} data-reason={view.code} title={reasonText(t, view.fullKey, view.values)}>
      <span>
        {reasonText(t, view.shortKey, view.values)}
      </span>
      {view.noteKey ? <small>{reasonText(t, view.noteKey, view.values)}</small> : null}
    </p>
  );
}

/** The full sentence for the detail page. */
export function ReasonFull({ view }: { view: AvailabilityReasonView }) {
  const t = useTranslations("agents_marketplace");
  return (
    <>
      <strong>{t(view.code === "temporarily_unavailable" ? "reason_panel_title_checking" : "reason_panel_title")}</strong>
      <p>
        {reasonText(t, view.fullKey, view.values)}
      </p>
      {view.noteKey ? <small>{reasonText(t, view.noteKey, view.values)}</small> : null}
    </>
  );
}

/** In a list row: the reason's chip and short line when the Agent cannot run for the viewer, otherwise `children`. */
export function ReasonStatus({ item, enabled = true, children }: { item: AgentCatalogItem; enabled?: boolean; children: ReactNode }) {
  const view = useAvailabilityReason(item);
  // Not hired and turned off keep their own labels and buttons.
  if (!enabled || !view || view.code === "not_hired" || view.code === "personally_disabled") return <>{children}</>;
  return (
    <>
      <ReasonChip view={view} />
      <ReasonLine view={view} className="am-management-reason" />
    </>
  );
}

/** Only reason 11 is worth retrying; everything else is a settled state. */
export function ReasonRetry({ item, disabled, onRetry }: { item: AgentCatalogItem; disabled?: boolean; onRetry: () => void }) {
  const t = useTranslations("agents_marketplace");
  const view = useAvailabilityReason(item);
  if (view?.action !== "retry") return null;
  return (
    <button type="button" disabled={disabled} onClick={onRetry}>
      {t("reason_retry")}
    </button>
  );
}

export function useLimitedAvailability(item: AgentCatalogItem): LimitedView | null {
  return limitedAvailability(item, useLocale());
}

/** Runs, but a dependency is not available: a marker next to the normal status, never "unavailable". */
export function LimitedNote({ view, className = "am-reason-line" }: { view: LimitedView; className?: string }) {
  const t = useTranslations("agents_marketplace");
  return (
    <p className={className} data-limited="true" title={reasonText(t, view.fullKey, view.values)}>
      <span>
        <b className="am-limited-chip">{t("limited_chip")}</b>
        {reasonText(t, view.shortKey, view.values)}
      </span>
    </p>
  );
}

/** In a list row: the limited marker under the normal status, when there is one. */
export function LimitedStatus({ item, enabled = true }: { item: AgentCatalogItem; enabled?: boolean }) {
  const view = useLimitedAvailability(item);
  return enabled && view ? <LimitedNote view={view} className="am-management-reason" /> : null;
}
