"use client";

/**
 * Chat receipt rows for Coach's writes to an existing Agent's draft (C1, C6). They come from the Turn's own
 * `agent_change` operation pointers, which live and replayed Turns project through the same dispatcher, and
 * read the operation's current receipt; the row only reports it and opens the Agent in the Canvas.
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";

import { CircleCheck, Info } from "@/components/icons";
import {
  coachOperationOpens,
  coachOperationsVersion,
  readCoachOperation,
  subscribeCoachOperations,
  type CoachOperationOpen,
  type CoachOperationReceipt,
} from "@/lib/coach-operations";
import { requestCoachOperationOpen } from "@/lib/coach-operation-requests";

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** What the row says about one receipt: written into the draft, or only saved as a proposal. */
export function coachChangeReceiptSummary(receipt: CoachOperationReceipt): { written: boolean; count: number; name: string | null } {
  const result = receipt.domain_result;
  const proposal = record(result.proposal);
  const listing = record(record(proposal.base_document).listing);
  const changed = Array.isArray(result.changed_paths) ? result.changed_paths.length : 0;
  return {
    written: result.effect === "draft_updated",
    count: changed,
    name: typeof listing.display_name === "string" && listing.display_name.trim() ? listing.display_name : null,
  };
}

export function CoachChangeReceipts({ turnId, sessionId }: { turnId: string | null | undefined; sessionId: string | null | undefined }) {
  useSyncExternalStore(subscribeCoachOperations, coachOperationsVersion, coachOperationsVersion);
  if (!turnId || !sessionId) return null;
  const changes = coachOperationOpens(turnId).filter((pointer) => pointer.reference.kind === "agent_change" && pointer.sessionId === sessionId);
  if (!changes.length) return null;
  return <>{changes.map((pointer) => <CoachChangeReceiptRow key={pointer.requestId} pointer={pointer} />)}</>;
}

function CoachChangeReceiptRow({ pointer }: { pointer: CoachOperationOpen }) {
  const t = useTranslations("coach_operation");
  const [receipt, setReceipt] = useState<CoachOperationReceipt | null | "unavailable">(null);
  useEffect(() => {
    const controller = new AbortController();
    void readCoachOperation(pointer.reference, pointer.workspaceId, pointer.sessionId, controller.signal, pointer.teamId)
      .then((value) => { if (!controller.signal.aborted) setReceipt(value); })
      .catch(() => { if (!controller.signal.aborted) setReceipt("unavailable"); });
    return () => controller.abort();
  }, [pointer]);
  const summary = receipt && receipt !== "unavailable" ? coachChangeReceiptSummary(receipt) : null;
  const title = summary?.written
    ? t("chat_receipt_written", { count: summary.count })
    : summary ? t("chat_receipt_proposed") : t("chat_receipt_pending");
  return (
    <div className="coach-change-receipt" data-state={summary?.written ? "written" : "other"}>
      <span className="coach-change-receipt-head">
        {summary?.written ? <CircleCheck size={14} aria-hidden /> : <Info size={14} aria-hidden />}
        <strong>{title}</strong>
        <button type="button" onClick={() => requestCoachOperationOpen({ turnId: pointer.turnId, requestId: pointer.requestId })}>
          {t("chat_receipt_open")}
        </button>
      </span>
      <small>{[summary?.name, summary?.written ? t("chat_receipt_not_released") : null].filter(Boolean).join(" · ")}</small>
    </div>
  );
}
