"use client";

/**
 * Session list states before real data (Agents & Reports polish spec §3b): the first load shows skeleton rows
 * (never "No sessions yet"), a failed load shows an error row with Retry, and only a completed empty load reaches the
 * empty state in WorkRecovery. Shared by the desktop sidebar and the phone drawer.
 */

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { CircleAlert } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";

/** Six rows at the two-line session row height, shown after the shared 300ms delay. */
export function SessionListSkeleton() {
  const t = useTranslations("session");
  return <LoadingSkeleton variant="sessions" rows={6} label={t("rail_loading")} />;
}

/** What a first load is still waiting for after the bounded wait (see `useSessionList().stalledOn`). */
export type SessionListStall = "spaces" | "sessions" | null;

export function SessionListError({ onRetry, stalledOn = null }: { onRetry: () => void; stalledOn?: SessionListStall }) {
  const t = useTranslations("session");
  // A stalled load names what has not answered (the Space list or the session list) and what to do; a refused
  // load keeps the short failure line.
  const text =
    stalledOn === "spaces"
      ? t("rail_load_stalled_spaces")
      : stalledOn === "sessions"
        ? t("rail_load_stalled_sessions")
        : t("rail_load_failed");
  return (
    <div className="session-list-error" data-stalled-on={stalledOn ?? undefined}>
      <p role="status">
        <LucideGlyph icon={CircleAlert} size={14} aria-hidden="true" />
        <span>{text}</span>
      </p>
      <button type="button" onClick={onRetry}>
        {t("rail_search_retry")}
      </button>
    </div>
  );
}

/**
 * Which state the list area shows before rows: error, then first-load skeleton. A list that already has rows keeps
 * them while refreshing (no skeleton over cached data).
 */
export function sessionListStatus({
  loading,
  error,
  hasRows,
  onRetry,
  stalledOn = null,
}: {
  loading: boolean;
  error: boolean;
  hasRows: boolean;
  onRetry: () => void;
  stalledOn?: SessionListStall;
}): ReactNode {
  if (hasRows) return null;
  if (error) return <SessionListError onRetry={onRetry} stalledOn={stalledOn} />;
  if (loading) return <SessionListSkeleton />;
  return null;
}
