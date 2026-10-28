"use client";

/** Configuration draft chip (batch 1-b, tech-design-batch1b §4.2) — chat-receipt / lite-1 design form.
 *
 * In-stream beat rendering: draft=drawer-opening chip; consumed=one-line save receipt (View + Next Run);
 * discarded/expired/stale=one-line muted trace. Hydrate state from the server (consistent across devices/F5).
 */

import { AlertCircle, Check, ChevronRight, Info, Sparkles, X } from "@/components/icons";
import { useTranslations } from "next-intl";
import { useEffect, useSyncExternalStore } from "react";

import { useAppRouter } from "@/lib/use-app-router";
import {
  draftVersion,
  getDraftPayload,
  getDraftResult,
  getDraftState,
  hydrateDraftState,
  subscribeDraft,
} from "@/lib/config-copilot-draft";

export function DraftChip({ draftId, onOpen }: { draftId: string; onOpen: (draftId: string) => void }) {
  const t = useTranslations("settings.config_copilot_drawer");
  // Subscribe to the store: state changes after hydrate/consume/discard must rerender (same as lite)
  useSyncExternalStore(subscribeDraft, draftVersion, draftVersion);
  const payload = getDraftPayload(draftId);
  const state = getDraftState(draftId);
  const result = getDraftResult(draftId);

  useEffect(() => {
    void hydrateDraftState(draftId);
  }, [draftId]);

  if (state === "consumed") {
    return result?.kind === "team_subagent"
      ? <SubagentConsumedReceipt name={result.label ?? null} refId={result.ref_id ?? null} workingCopy={result.stage === "working_copy"} />
      : <ConsumedReceipt name={result?.name ?? null} nextRunAt={result?.next_run_at ?? null} />;
  }
  if (state === "discarded") {
    return (
      <div className="lite-1 muted" data-testid="draft-discarded">
        <span className="ic"><X size={14} /></span>
        {t("discarded")}
      </div>
    );
  }
  if (state === "expired" || state === "stale") {
    return (
      <div className="lite-1 muted" data-testid="draft-stale">
        <span className="ic"><AlertCircle size={14} /></span>
        {t(state === "expired" ? "expired" : "stale")}
      </div>
    );
  }
  if (!payload) return null;

  return (
    <div className="lite-1" data-testid="draft-chip">
      <span className="ic ai"><Sparkles size={14} /></span>
      <b>{t(payload.form_id === "team_subagent" ? "subagent_chip_title" : "chip_title")}</b>
      <span className="sp" />
      <button type="button" className="lnk" onClick={() => onOpen(draftId)}>
        {t("chip_open")}
      </button>
    </div>
  );
}

function SubagentConsumedReceipt({ name, refId, workingCopy }: { name: string | null; refId: string | null; workingCopy: boolean }) {
  const t = useTranslations("settings.config_copilot_drawer");
  const router = useAppRouter();
  return (
    <div className="draft-receipt-wrap" data-testid="draft-receipt">
      <div className="receipt">
        <span className="ic"><Check size={15} /></span>
        <b>{t(workingCopy ? "subagent_working_copy_saved" : "subagent_receipt_named", { name: name ?? "" })}</b>
        {refId ? <button type="button" className="lnk" onClick={() => router.push(`/agents/manage/?agent_id=${encodeURIComponent(refId)}`)}>{t("view")}</button> : null}
      </div>
    </div>
  );
}

function ConsumedReceipt({ name, nextRunAt }: { name: string | null; nextRunAt: string | null }) {
  const t = useTranslations("settings.config_copilot_drawer");
  const router = useAppRouter();
  return (
    <div className="draft-receipt-wrap" data-testid="draft-receipt">
      <div className="receipt">
        <span className="ic"><Check size={15} /></span>
        <b>{name ? t("receipt_named", { name }) : t("receipt")}</b>
        <ChevronRight size={13} />
        <button type="button" className="lnk" onClick={() => router.push("/team/settings/scheduled")}>
          {t("view")}
        </button>
      </div>
      {nextRunAt ? (
        <div className="sysline">
          <span className="ic"><Info size={13} /></span>
          {t("next_run", { time: formatNextRun(nextRunAt) })}
        </div>
      ) : null}
    </div>
  );
}

function formatNextRun(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
