"use client";

/**
 * PlanCardConnected — binds PlanCard to lib/plan-store by planId and wires the
 * happy-path + A (editable) actions. Rendered from a `plan` beat in BeatStream.
 *
 * - subscribes to plan-store → re-renders as plan_* / step_* events land
 * - Cancel: client-side dismiss (no backend cancel endpoint in M1)
 * - Approve execution / View report: delegated to the parent (owns workspace/session + SSE attach)
 * - A editing (goal/select/delete/reorder): local optimistic mutator → onPersistEdits (parent
 *   PATCH /steps). NL plan amendments: onAmendPlan (parent) previews → confirm persists / discard reloads.
 *
 * 2026-07 (planning-ux-reliability): the card now survives reload — F5 hydrate
 * replays plan_proposed/step_* into plan-store (turn-store-replay) and the
 * archived path renders the plan beat; because replay is unordered across
 * turns, archived cards reconcile against the authed GET /plans/{id}/state on
 * mount (`reconcileOnMount`). Cancel now really cancels server-side (sticky).
 */

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  PlanCard,
  type PlanEditHandlers,
  type PlanRecoveryHandlers,
  type PlanReplanView,
  type PlanReviseView,
} from "./PlanCard";
import {
  applyPlanSnapshot,
  deleteStep,
  dismissPlan,
  editStepObjective,
  getPlanSnapshot,
  isPlanDismissed,
  reorderSteps,
  toggleStepIncluded,
  usePlan,
} from "@/lib/plan-store";
import {
  cancelPlan,
  fetchPlanStateAuthed,
  replanConfirm,
  replanPreview,
  reviseStepConfirm,
  reviseStepPreview,
  skipPlanStep,
  type RevisedStepPayload,
} from "@/lib/plan-api";
import { toast } from "@/lib/use-toast";
import { getChatSendErrorMessage } from "@/lib/chat-send-errors";
import { useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { planActionErrorKey, type PlanAction } from "./plan-action-errors";
import { fileError } from "@/lib/file-error";
import { getActiveSpaceId } from "@/lib/space-storage";

/** Space id from the same source as the approve path (TurnContainer.onApprovePlan). */
function _wsId(): string {
  if (typeof window === "undefined") return "";
  return getActiveSpaceId();
}

/** Space for card actions: prefer the plan's own value (persisted truth from hook1; even an empty string is valid
 *  because it matches the database row). Only old events lacking this field fall back to localStorage (which can diverge;
 *  dev-stub observed "" vs "1" → cancel/approve 404). */
function _planWs(planId: string): string {
  const ws = getPlanSnapshot(planId)?.workspaceId;
  return ws !== undefined ? ws : _wsId();
}

/** Mount reconcile runs once per planId per session (read-only correction, not polling). */
const _reconciled = new Set<string>();
const RUNNING_RECONCILE_INTERVAL_MS = 5_000;
const RUNNING_RECONCILE_READ_TIMEOUT_MS = 10_000;
const RUNNING_RECONCILE_MAX_MS = 2 * 60 * 60 * 1_000;

export interface PlanCardConnectedProps {
  planId: string;
  /** Approve → start execution. Resolves when the request has been accepted. */
  onApprove?: (planId: string) => Promise<void> | void;
  /** Open the generated report in the canvas pane. */
  onViewReport?: (planId: string) => void;
  /** A: persist the current (locally-edited) steps via PATCH /steps. */
  onPersistEdits?: (planId: string) => Promise<void> | void;
  /** A: NL amend → applies a preview to plan-store; resolves true if a preview is shown. */
  onAmendPlan?: (planId: string, instruction: string) => Promise<boolean>;
  /** A: reload plan from backend (discard an unconfirmed amend preview). */
  onReloadPlan?: (planId: string) => Promise<void> | void;
  /** 2026-07: true on archived/F5 paths; unordered replay needs one correction from authed state. */
  reconcileOnMount?: boolean;
  /** chat-ux-2026-08 plan integration batch: plan step id → the Team's **configured** subagent display name.
   *  TurnContainer resolves turn-store peerDisplayNames by join key and passes it in
   *  (the card has no turnId, so it does not look up the table here). */
  displayNameFor?: (planStepId: string) => string | undefined;
}

export function PlanCardConnected({
  planId,
  displayNameFor,
  onApprove,
  onViewReport,
  onPersistEdits,
  onAmendPlan,
  onReloadPlan,
  reconcileOnMount,
}: PlanCardConnectedProps) {
  const plan = usePlan(planId);
  const [approving, setApproving] = useState(false);
  const [amendBusy, setAmendBusy] = useState(false);
  const [amendPreview, setAmendPreview] = useState(false);
  const [recovBusy, setRecovBusy] = useState(false);
  // Batch 3: revise/replan previews (local state, discarded on unmount).
  const [revisePv, setRevisePv] = useState<{
    stepId: string;
    expectedRev: number;
    revised: RevisedStepPayload;
    warning?: string;
  } | null>(null);
  const [replanPv, setReplanPv] = useState<{
    expectedRev: number;
    newSteps: Array<Record<string, unknown>>;
    keptDone: number;
    superseded: number;
    warning?: string;
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const tp = useTranslations("plan_card");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  const filesT = useTranslations("files_page");
  // Action failures explain what happened and how to proceed; status codes and request IDs go in "复制诊断".
  const toastPlanError = (err: unknown, action: PlanAction) => {
    toast({
      kind: "error",
      text: tp(`action_errors.${planActionErrorKey(err, action)}`),
      action: copyDiagnosticsAction(err, "chat"),
    });
  };

  useEffect(() => {
    if (!reconcileOnMount || _reconciled.has(planId)) return;
    _reconciled.add(planId);
    const ws = _planWs(planId);
    const startedSnapshot = getPlanSnapshot(planId);
    void fetchPlanStateAuthed(planId, { workspaceId: ws })
      .then((st) => {
        if (isPlanDismissed(planId)) return; // Cancellation wins: do not revive the card during this window.
        // Preserve live progress and unsaved edits. A completed replay can
        // race this read, but cannot invalidate the same completed revision's
        // authoritative steps and criteria.
        const current = getPlanSnapshot(planId);
        if (current !== startedSnapshot && !(
          current?.status === "completed"
          && st.plan?.status === "completed"
          && Number.isSafeInteger(st.plan.current_rev)
          && (st.plan.current_rev ?? 0) >= (current.currentRev ?? 0)
        )) return;
        if (String(st.plan?.status ?? "") === "cancelled") {
          dismissPlan(planId); // The user already cancelled elsewhere → do not revive the card.
          return;
        }
        applyPlanSnapshot({ plan_id: planId, plan: st.plan, steps: st.steps });
      })
      .catch(() => {
        /* Read-only correction; preserve replayed state on failure. */
      });
  }, [planId, reconcileOnMount]);

  // SSE remains the live path. This bounded read-only reconcile is the
  // liveness backstop for a second tab (409 plan_already_running), a lost
  // approval response, or a missed terminal event. Without it the RUNNING
  // footer has no action and can stay frozen until F5.
  useEffect(() => {
    if (plan?.status !== "running") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();
    const deadline = Date.now() + RUNNING_RECONCILE_MAX_MS;
    const deadlineTimer = setTimeout(() => controller.abort(), RUNNING_RECONCILE_MAX_MS);

    const refresh = async () => {
      if (cancelled || controller.signal.aborted) return;
      const attemptController = new AbortController();
      const abortAttempt = () => attemptController.abort();
      controller.signal.addEventListener("abort", abortAttempt, { once: true });
      const attemptTimer = setTimeout(
        abortAttempt,
        RUNNING_RECONCILE_READ_TIMEOUT_MS,
      );
      try {
        const st = await fetchPlanStateAuthed(planId, {
          workspaceId: _planWs(planId),
          signal: attemptController.signal,
        });
        if (
          cancelled
          || isPlanDismissed(planId)
          || getPlanSnapshot(planId)?.status !== "running"
        ) return;
        const remoteStatus = String(st.plan?.status ?? "");
        if (remoteStatus === "cancelled") {
          dismissPlan(planId);
          return;
        }
        // RUNNING snapshots can be older than live SSE step progress. They are
        // only a liveness observation; apply the snapshot once it is terminal.
        if (
          remoteStatus === "completed"
          || remoteStatus === "blocked"
          || remoteStatus === "failed"
          || remoteStatus === "awaiting_amend"
        ) {
          applyPlanSnapshot({ plan_id: planId, plan: st.plan, steps: st.steps });
          return;
        }
      } catch {
        // A transient read failure must not turn an accepted execution back
        // into an editable proposal. The next bounded observation retries.
      } finally {
        clearTimeout(attemptTimer);
        controller.signal.removeEventListener("abort", abortAttempt);
      }
      if (!cancelled && !controller.signal.aborted && Date.now() < deadline) {
        timer = setTimeout(() => void refresh(), RUNNING_RECONCILE_INTERVAL_MS);
      }
    };

    timer = setTimeout(() => void refresh(), RUNNING_RECONCILE_INTERVAL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(deadlineTimer);
      if (timer !== null) clearTimeout(timer);
    };
  }, [plan?.status, planId]);

  if (!plan) return null;

  const handleApprove = async () => {
    if (!onApprove || approving) return;
    setApproving(true);
    try {
      await onApprove(planId);
    } finally {
      setApproving(false);
    }
  };

  // A: every structured edit mutates the store optimistically, then persists the
  // full edited set (PATCH /steps). Persist failures are surfaced + reverted by
  // the parent (it re-fetches plan state on error).
  const persist = () => {
    if (onPersistEdits) void onPersistEdits(planId);
  };
  const edit: PlanEditHandlers | undefined = onPersistEdits
    ? {
        onEditObjective: (stepId, objective) => {
          editStepObjective(planId, stepId, objective);
          persist();
        },
        onToggleInclude: (stepId) => {
          toggleStepIncluded(planId, stepId);
          persist();
        },
        onDeleteStep: (stepId) => {
          deleteStep(planId, stepId);
          persist();
        },
        onReorder: (draggedId, targetId) => {
          reorderSteps(planId, draggedId, targetId);
          persist();
        },
      }
    : undefined;

  const handleAmend = async (instruction: string): Promise<boolean> => {
    if (!onAmendPlan || amendBusy) return false;
    setAmendBusy(true);
    try {
      const shown = await onAmendPlan(planId, instruction);
      if (shown) setAmendPreview(true);
      return shown;
    } finally {
      setAmendBusy(false);
    }
  };
  const confirmAmend = () => {
    setAmendPreview(false);
    persist(); // persist the preview steps now in the store
  };
  const discardAmend = async () => {
    setAmendBusy(true);
    try {
      if (onReloadPlan) await onReloadPlan(planId);
    } finally {
      setAmendBusy(false);
      setAmendPreview(false);
    }
  };

  // B (blocked recovery): retry = re-approve (approve accepts BLOCKED → resume_level continues, without rerunning completed steps);
  // skip = skip endpoint + re-approve; supply = KB upload (asynchronous indexing, no automatic retry; user retries after indexing).
  // workspace_id prefers the plan's own value (_planWs); skip/upload are authed (server-derived identity).
  const _ws = () => _planWs(planId);
  const applyRecoverySnapshot = (
    result: { current_rev: number; steps: Array<Record<string, unknown>> },
    expectedRev: number | undefined,
  ) => {
    const current = getPlanSnapshot(planId);
    if (
      expectedRev === undefined || result.current_rev !== expectedRev + 1
      || current?.currentRev !== expectedRev || current.status !== "blocked"
    ) throw Object.assign(new Error(getChatSendErrorMessage("plan_not_approvable")), { code: "plan_not_approvable" });
    applyPlanSnapshot({ plan_id: planId, plan: { status: "blocked", current_rev: result.current_rev }, steps: result.steps });
  };
  const onSkipRecover = async (stepId: string) => {
    const ws = _ws();
    if (!ws || recovBusy) return;
    setRecovBusy(true);
    try {
      const expectedRev = getPlanSnapshot(planId)?.currentRev;
      const res = await skipPlanStep(planId, stepId, { workspaceId: ws, expectedRev });
      applyRecoverySnapshot(res, expectedRev);
    } catch (err) {
      toastPlanError(err, "skip");
      setRecovBusy(false);
      return;
    }
    setRecovBusy(false);
    await handleApprove(); // resume without the skipped step
  };
  const onSupplyFile = async (file: File) => {
    const ws = _ws();
    if (!ws) return;
    setRecovBusy(true);
    try {
      const { uploadKbFile } = await import("@/lib/kb-api");
      await uploadKbFile(file, { scope: "space", scope_id: ws, use_llm: true });
      toast({ kind: "success", text: tp("recover_uploaded") });
    } catch (err) {
      // Same upload-failure explanations as the files page; status codes and request IDs go in "复制诊断".
      toast({ kind: "error", text: filesT(fileError(err, "upload")), action: copyDiagnosticsAction(err, "upload") });
    } finally {
      setRecovBusy(false);
    }
  };
  // ── Batch 3: revise/replan use component-local state throughout; previews never enter plan-store and vanish on unmount,
  // eliminating orphan preview paths (root fix for review R2-F6). 409 (plan_busy_or_conflict) means
  // worker tail window/concurrency: ask the user to wait, **without downgrading card state** (R2-F2).
  const onReviseStart = async (stepId: string) => {
    const ws = _ws();
    if (recovBusy || revisePv || replanPv) return; // Mutual exclusion (implementation review P2-3).
    setRecovBusy(true);
    try {
      const pv = await reviseStepPreview(planId, stepId, { workspaceId: ws });
      setRevisePv({ stepId, expectedRev: pv.expected_rev, revised: pv.revised, warning: pv.warning });
    } catch (err) {
      toastPlanError(err, "revise_preview");
    } finally {
      setRecovBusy(false);
    }
  };
  const onReviseConfirm = async () => {
    if (!revisePv || recovBusy) return;
    setRecovBusy(true);
    try {
      const res = await reviseStepConfirm(planId, revisePv.stepId, {
        workspaceId: _ws(),
        expectedRev: revisePv.expectedRev,
        revised: revisePv.revised,
      });
      // Implementation review P1-1: the confirm response's authoritative snapshot must enter the store; otherwise new steps
      // do not render, dead rows remain, and a second revision always gets 409 on the old id (REST snapshot is the source, design S5).
      applyRecoverySnapshot(res, revisePv.expectedRev);
    } catch (err) {
      toastPlanError(err, "revise_confirm");
      setRecovBusy(false);
      return;
    }
    setRevisePv(null);
    setRecovBusy(false);
    await handleApprove(); // Confirm immediately reruns the revised step and its downstream steps.
  };
  const onReplanStart = async (instruction: string) => {
    const ws = _ws();
    if (recovBusy || replanPv || revisePv) return; // Mutual exclusion (implementation review P2-3).
    setRecovBusy(true);
    try {
      const pv = await replanPreview(planId, { workspaceId: ws, instruction });
      setReplanPv({
        expectedRev: pv.expected_rev,
        newSteps: pv.new_steps,
        keptDone: pv.kept_done.length,
        superseded: pv.superseded.length,
        warning: pv.warning,
      });
    } catch (err) {
      toastPlanError(err, "replan_preview");
    } finally {
      setRecovBusy(false);
    }
  };
  const onReplanConfirm = async () => {
    if (!replanPv || recovBusy) return;
    setRecovBusy(true);
    try {
      const res = await replanConfirm(planId, { workspaceId: _ws(), expectedRev: replanPv.expectedRev, newSteps: replanPv.newSteps });
      applyRecoverySnapshot(res, replanPv.expectedRev);
    } catch (err) {
      toastPlanError(err, "replan_confirm");
      setRecovBusy(false);
      return;
    }
    setReplanPv(null);
    setRecovBusy(false);
    await handleApprove(); // Confirm immediately executes the new graph.
  };

  const recovery: PlanRecoveryHandlers | undefined =
    plan.status === "blocked"
      ? {
          onRetry: () => void handleApprove(),
          onSkip: (stepId) => void onSkipRecover(stepId),
          onSupply: () => fileInputRef.current?.click(),
          onRevise: (stepId) => void onReviseStart(stepId),
          busy: recovBusy || approving,
        }
      : undefined;
  // Implementation review P2-2: outside blocked (e.g. approved to running elsewhere), do not render the confirmation strip;
  // confirm would inevitably return 409, and a wait-and-retry hint would mislead.
  const reviseView: PlanReviseView | null = revisePv && plan.status === "blocked"
    ? {
        stepId: revisePv.stepId,
        agent: revisePv.revised.agent_name,
        objective: revisePv.revised.objective,
        deliverable: revisePv.revised.deliverable,
        criteria: revisePv.revised.success_criteria,
        warning: revisePv.warning,
        onConfirm: () => void onReviseConfirm(),
        onDiscard: () => setRevisePv(null),
        busy: recovBusy || approving,
      }
    : null;
  const replanView: PlanReplanView | null =
    plan.status === "blocked"
      ? {
          onStart: (instruction) => void onReplanStart(instruction),
          busy: recovBusy || approving,
          preview: replanPv
            ? {
                newSteps: replanPv.newSteps.map((s) => ({
                  id: String(s.id ?? ""),
                  agent: String(s.agent ?? ""),
                  objective: String(s.objective ?? ""),
                  criteria: Array.isArray(s.success_criteria) ? (s.success_criteria as unknown[]).map(String) : [],
                })),
                keptDone: replanPv.keptDone,
                superseded: replanPv.superseded,
                warning: replanPv.warning,
                onConfirm: () => void onReplanConfirm(),
                onDiscard: () => setReplanPv(null),
              }
            : undefined,
        }
      : null;

  return (
    <>
      <input
        ref={fileInputRef}
        type="file"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) void onSupplyFile(f);
        }}
      />
      <PlanCard
        displayNameFor={displayNameFor}
        plan={plan}
        approving={approving}
        onApprove={onApprove ? handleApprove : undefined}
        onCancel={() => {
          // 2026-07: really cancel on the backend first (CANCELLED sticky), then remove the card; previously only the frontend store was cleared,
          // leaving the backend plan PROPOSED forever and reviving the card on F5 replay. Even on backend failure,
          // dismiss anyway (the next mount reconcile corrects it).
          void cancelPlan(planId, { workspaceId: _planWs(planId) }).catch(() => {});
          dismissPlan(planId);
        }}
        onViewReport={onViewReport ? () => onViewReport(planId) : undefined}
        edit={edit}
        onAmend={onAmendPlan ? handleAmend : undefined}
        amendBusy={amendBusy}
        amendPreview={amendPreview}
        onConfirmAmend={confirmAmend}
        onDiscardAmend={discardAmend}
        recovery={recovery}
        revise={reviseView}
        replan={replanView}
      />
    </>
  );
}
