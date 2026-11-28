"use client";

/**
 * Compact rows for original Agent draft writes and saved case suggestions. Live and replayed Turns
 * project the same durable metadata; current domain readers authorize content and navigation.
 */
import { useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";

import { CircleCheck, Info } from "@/components/icons";
import {
  coachChangeEffects,
  coachChangeGroups,
  coachOperationOpens,
  coachCaseEffects,
  coachOperationsVersion,
  readCoachOperation,
  subscribeCoachOperations,
  uncoveredChangeOpens,
  type CoachChangeGroup,
  type CoachOperationOpen,
  type CoachOperationReceipt,
  type CoachCaseEffect,
} from "@/lib/coach-operations";
import { requestCoachOperationOpen } from "@/lib/coach-operation-requests";
import { CoachReceiptScopeContext } from "@/lib/coach-receipt-scope";
import { agentCaseProposalPath } from "@/lib/agent-case-proposal-path";
import { draftFieldLabelKey } from "@/lib/agent-draft-changes";
import { ApiError } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAgentReleaseBarCapability } from "@/lib/use-agent-release-bar-capability";
import { CoachOpenAgentEditorsContext } from "@/lib/use-coach-resource-editors";
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
  // FEATURE_AGENT_RELEASE_BAR: Agent draft writes come from the run record (slices 2-5 tech design §6).
  const fromRecord = useAgentReleaseBarCapability();
  if (!turnId || !sessionId) return null;
  const opens = coachOperationOpens(turnId).filter((pointer) => pointer.reference.kind === "agent_change" && pointer.sessionId === sessionId);
  const recorded = fromRecord && scope?.sessionId === sessionId ? coachChangeEffects(turnId) : [];
  // An "open" event for a write the run record also carries is the same receipt; older Turns keep their row.
  const changes = fromRecord ? uncoveredChangeOpens(opens, recorded) : opens;
  const cases = scope?.sessionId === sessionId ? coachCaseEffects(turnId) : [];
  return <>{scope && coachChangeGroups(recorded).map((group) => <CoachChangeEffectRow key={JSON.stringify([scope, group.agentId])} group={group} turnId={turnId} scope={scope} />)}
    {changes.map((pointer) => <CoachChangeReceiptRow key={pointer.requestId} pointer={pointer} />)}
    {scope && cases.map((effect) => <CoachCaseReceiptRow key={JSON.stringify([scope, effect.reference])} effect={effect} scope={scope} current={current} />)}</>;
}

/** What one Agent's row says: its name before the change, whether Coach wrote the draft and the changed fields. */
export function coachChangeGroupSummary(receipts: readonly CoachOperationReceipt[]): { written: boolean; name: string | null; paths: string[] } {
  const paths: string[] = [];
  let name: string | null = null;
  let written = false;
  for (const receipt of receipts) {
    const summary = coachChangeReceiptSummary(receipt);
    written ||= summary.written;
    name ??= summary.name;
    const changed = receipt.domain_result.changed_paths;
    for (const path of Array.isArray(changed) ? changed : []) if (typeof path === "string" && !paths.includes(path)) paths.push(path);
  }
  return { written, name, paths };
}

/**
 * One row per Agent Coach wrote in the Turn, read through the scoped reader. The read always answers "not
 * released", so the row does not say it; the release bar states the truth. A 404 (not the Owner, or archived)
 * leaves only the first words. While the Agent's editor is open in the Canvas the row only says so.
 */
function CoachChangeEffectRow({ group, turnId, scope }: {
  group: CoachChangeGroup; turnId: string; scope: NonNullable<React.ContextType<typeof CoachReceiptScopeContext>>;
}) {
  const t = useTranslations("coach_receipt");
  const tAgents = useTranslations("agents_marketplace");
  const tConfig = useTranslations("agent_config_v2");
  const router = useAppRouter();
  const openEditors = useContext(CoachOpenAgentEditorsContext);
  const [receipts, setReceipts] = useState<CoachOperationReceipt[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const referencesKey = JSON.stringify(group.references);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled(group.references.map((reference) => readCoachOperation(reference, scope.workspaceId, scope.sessionId, controller.signal, scope.teamId)))
      .then((results) => {
        if (controller.signal.aborted) return;
        const read = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
        setReceipts(read);
        setUnavailable(!read.length && results.some((result) => result.status === "rejected"
          && result.reason instanceof ApiError && result.reason.status === 404));
      });
    return () => controller.abort();
  }, [referencesKey, scope.workspaceId, scope.sessionId, scope.teamId]);
  const summary = receipts?.length ? coachChangeGroupSummary(receipts) : null;
  const open = openEditors.has(group.agentId);
  // The row is the redesign's: 工作说明 is named as on the Configure page.
  const labels = summary ? [...new Set(summary.paths.map((path) => path === "/definition/system_prompt" ? tConfig("field_instructions") : tAgents(draftFieldLabelKey(path) as never)))] : [];
  const parts = summary ? [
    t(summary.written ? "written" : "proposed"),
    summary.name,
    labels.length ? t("fields", { fields: labels.join(t("field_separator")), count: summary.paths.length }) : null,
    open ? t("already_open") : null,
  ].filter(Boolean) : [t("pending")];
  const managePath = `/agents/manage/?${new URLSearchParams({ agent_id: group.agentId })}`;
  const latest = group.references[group.references.length - 1];
  return (
    <div className="coach-change-receipt" role="group" aria-label={t("group_label")} data-state={summary?.written ? "written" : "other"} data-source="record">
      <span className="coach-change-receipt-head">
        {summary?.written ? <CircleCheck size={14} aria-hidden /> : <Info size={14} aria-hidden />}
        <strong>{parts.join(" · ")}</strong>
      </span>
      {!open && !unavailable ? (
        <span className="coach-change-receipt-actions">
          <button type="button" onClick={() => requestCoachOperationOpen({ turnId, reference: latest })}>{t("open_in_canvas")}</button>
          <a href={appPath(managePath)} onClick={(event) => {
            if (!shouldHandleAppLinkClick(event)) return;
            event.preventDefault();
            router.push(managePath);
          }}>{t("go_manage")}</a>
        </span>
      ) : null}
    </div>
  );
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
