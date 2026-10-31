"use client";

/**
 * Compact rows for original Agent draft writes and saved case suggestions. Live and replayed Turns
 * project the same durable metadata; current domain readers authorize content and navigation.
 */
import { useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";

import { CircleCheck, Info } from "@/components/icons";
import {
  coachOperationOpens,
  coachCaseEffects,
  coachOperationsVersion,
  readCoachOperation,
  subscribeCoachOperations,
  type CoachOperationOpen,
  type CoachOperationReceipt,
  type CoachCaseEffect,
} from "@/lib/coach-operations";
import { requestCoachOperationOpen } from "@/lib/coach-operation-requests";
import { CoachReceiptScopeContext } from "@/lib/coach-receipt-scope";
import { agentCaseProposalPath } from "@/lib/agent-case-proposal-path";
import { caseProposalState } from "@/components/canvas/coach-operation-view";
import { useAppRouter } from "@/lib/use-app-router";

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

export function CoachChangeReceipts({ turnId, sessionId, current = false }: { turnId: string | null | undefined; sessionId: string | null | undefined; current?: boolean }) {
  useSyncExternalStore(subscribeCoachOperations, coachOperationsVersion, coachOperationsVersion);
  const scope = useContext(CoachReceiptScopeContext);
  if (!turnId || !sessionId) return null;
  const changes = coachOperationOpens(turnId).filter((pointer) => pointer.reference.kind === "agent_change" && pointer.sessionId === sessionId);
  const cases = scope?.sessionId === sessionId ? coachCaseEffects(turnId) : [];
  return <>{changes.map((pointer) => <CoachChangeReceiptRow key={pointer.requestId} pointer={pointer} />)}
    {scope && cases.map((effect) => <CoachCaseReceiptRow key={JSON.stringify([scope, effect.reference])} effect={effect} scope={scope} current={current} />)}</>;
}

function CoachCaseReceiptRow({ effect, scope, current }: {
  effect: CoachCaseEffect; scope: NonNullable<React.ContextType<typeof CoachReceiptScopeContext>>; current: boolean;
}) {
  const t = useTranslations("coach_operation");
  const router = useAppRouter();
  const [receipt, setReceipt] = useState<CoachOperationReceipt | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const load = async (navigate: boolean) => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setBusy(true);
    setFailed(false);
    try {
      const value = await readCoachOperation(effect.reference, scope.workspaceId, scope.sessionId, request.signal, scope.teamId);
      if (request.signal.aborted) return;
      setReceipt(value);
      if (navigate) router.push(agentCaseProposalPath(effect.reference.agent_id, caseProposalState(value) === "pending"));
    } catch {
      if (!request.signal.aborted) { setReceipt(null); setFailed(true); }
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  };
  useEffect(() => {
    if (current) void load(false);
    return () => controller.current?.abort();
  }, []);
  const state = receipt ? caseProposalState(receipt) : "unknown";
  return <div className="coach-change-receipt" data-state={state}>
    <span className="coach-change-receipt-head">
      <Info size={14} aria-hidden />
      <strong>{t(failed ? "chat_case_unavailable" : `chat_case_${state}`)}</strong>
      {!failed && <button type="button" disabled={busy} onClick={() => void load(true)}>
        {t(state === "pending" ? "chat_case_review" : state === "unknown" ? "chat_case_open" : "actions.view_current_cases")}
      </button>}
    </span>
    {failed && <small>{t("chat_case_unavailable_action")}</small>}
  </div>;
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
