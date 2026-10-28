/**
 * plan-store — external store for the Planning Mode card, keyed by planId.
 *
 * Pattern mirrors lib/turn-store.ts + lib/canvas-store.ts: a Map<planId,
 * PlanView> with per-planId listener buckets; subscribers register via
 * subscribePlan() and re-render via usePlan() (React 18 useSyncExternalStore).
 *
 * It is fed from two convergent sources (both land here so the card has one
 * source of truth):
 *   1. SSE events — plan_proposed / step_* / plan_* (lib/turn-store-dispatch.ts).
 *   2. dev REST — create / get / approve responses (lib/plan-api.ts), used for
 *      dogfood + the blocking-approve path until the authed streaming approve
 *      endpoint lands.
 *
 * OFF-flag safety: nothing seeds this store unless a plan_proposed event (flag
 * ON) or an explicit dev REST call arrives, so the card is dormant when the
 * backend flag is OFF.
 */

import { useSyncExternalStore } from "react";

import type { PlanStatus, PlanStepStatus, PlanStepVerify, PlanStepView, PlanView } from "@/components/chat/plan-card/types";

// ── backend → FE enum mapping ────────────────────────────────────
function mapPlanStatus(raw: unknown): PlanStatus {
  switch (String(raw ?? "")) {
    case "running":
      return "running";
    case "blocked":
      return "blocked";
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    // proposed | awaiting_amend | "" → proposed
    default:
      return "proposed";
  }
}

function mapStepStatus(raw: unknown): PlanStepStatus {
  switch (String(raw ?? "")) {
    case "running":
      return "running";
    case "done":
      return "done";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    case "blocked":
      return "blocked";
    case "superseded":
      return "superseded";
    // pending | ready | "" → pending
    default:
      return "pending";
  }
}

/** Normalize a backend step (SSE plan_proposed shape OR dev REST shape). */
function normalizeStep(raw: Record<string, unknown>): PlanStepView {
  return {
    id: String(raw.id ?? ""),
    agent: String(raw.agent ?? raw.agent_name ?? ""),
    objective: String(raw.objective ?? ""),
    level: typeof raw.level === "number" ? raw.level : 0,
    dependsOn: Array.isArray(raw.depends_on) ? (raw.depends_on as unknown[]).map((d) => String(d)) : [],
    needsCorpus: raw.needs_corpus === true,
    status: mapStepStatus(raw.status),
    included: raw.included !== false, // default true (M1 payloads omit it)
    seq: typeof raw.seq === "number" ? raw.seq : undefined,
    origin: typeof raw.origin === "string" ? raw.origin : undefined,
    deliverable: typeof raw.deliverable === "string" && raw.deliverable ? raw.deliverable : null,
    // Batch 3 (review R2-F8): REST _step_view now forwards error; failed-step details after F5
    // no longer contain only a raw kind string (SSE still uses reason).
    reason: typeof raw.reason === "string" ? raw.reason : typeof raw.error === "string" ? raw.error : null,
    errorKind: typeof raw.error_kind === "string" ? raw.error_kind : null,
    supersededBy: typeof raw.superseded_by === "string" ? raw.superseded_by : null,
    agentChoice:
      raw.agent_choice && typeof raw.agent_choice === "object" && !Array.isArray(raw.agent_choice)
        ? raw.agent_choice as Record<string, unknown>
        : raw.agent_choice === null
          ? null
          : undefined,
    needsWeb: raw.needs_web === true,
    feasibility:
      raw.feasibility === "needs_context" || raw.feasibility === "no_corpus" || raw.feasibility === "ok"
        ? raw.feasibility
        : null,
    successCriteria: Array.isArray(raw.success_criteria)
      ? (raw.success_criteria as unknown[]).map((c) => String(c)).filter(Boolean)
      : [],
    verify: normalizeVerify(raw.verify),
  };
}

function normalizeVerify(raw: unknown): PlanStepVerify | null {
  if (!raw || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const verdict = String(v.verdict ?? "");
  // ⚠ This is an allowlist: an unknown verdict discards **the entire verify object**, making its badge vanish.
  // Add new backend verdicts here in the same change; contract fields enter/leave as a group.
  const KNOWN = ["pass", "partial", "fail", "skipped", "out_of_domain"] as const;
  if (!(KNOWN as readonly string[]).includes(verdict)) return null;
  return {
    verdict: verdict as PlanStepVerify["verdict"],
    unmet: Array.isArray(v.unmet) ? (v.unmet as unknown[]).map((x) => String(x)) : [],
    note: typeof v.note === "string" ? v.note : undefined,
    items: Array.isArray(v.items) ? v.items.flatMap((rawItem: unknown) => {
      if (!rawItem || typeof rawItem !== "object") return [];
      const item = rawItem as Record<string, unknown>;
      if (typeof item.text !== "string" || !item.text) return [];
      if (!["met", "unmet", "cannot_tell", "not_applicable"].includes(String(item.status))) return [];
      return [{
        text: item.text,
        status: item.status as NonNullable<PlanStepVerify["items"]>[number]["status"],
        note: typeof item.note === "string" ? item.note : "",
      }];
    }) : undefined,
  };
}

// ── internal state ───────────────────────────────────────────────
const _states = new Map<string, PlanView>();
const _listeners = new Map<string, Set<() => void>>();
const _dismissedPlanIds = new Set<string>();

function _notify(planId: string): void {
  const bucket = _listeners.get(planId);
  if (!bucket) return;
  for (const cb of bucket) cb();
}

function _commit(planId: string, next: PlanView): void {
  _states.set(planId, next);
  _notify(planId);
}

function _patchStep(
  planId: string,
  stepId: string,
  patch: (s: PlanStepView) => PlanStepView,
): void {
  const prev = _states.get(planId);
  if (!prev) return;
  const idx = prev.steps.findIndex((s) => s.id === stepId);
  if (idx < 0) return;
  // Batch 3 (review R2-F7): superseded is terminal; F5 replay across turns is unordered, so old execution
  // turn step_failed/step_started must not revive dead rows as failed/running (otherwise after
  // replan, old failed rows render beside the new subgraph and gain recovery buttons).
  if (prev.steps[idx].status === "superseded") return;
  const nextSteps = prev.steps.map((s, i) => (i === idx ? patch(s) : s));
  _commit(planId, { ...prev, steps: nextSteps });
}

// ── read API ─────────────────────────────────────────────────────
export function getPlanSnapshot(planId: string): PlanView | undefined {
  return _states.get(planId);
}

function planRevision(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : undefined;
}

/** Optimistic edits without a confirmed save must be refreshed before approval. */
export function invalidatePlanRevision(planId: string, expectedRev: number | undefined): void {
  const current = _states.get(planId);
  if (current && current.currentRev === expectedRev) _commit(planId, { ...current, currentRev: undefined });
}

/** Advance a confirmed save without replacing edits queued during that request. */
export function acknowledgePlanEdit(planId: string, expectedRev: number, savedRev: number): void {
  const current = _states.get(planId);
  if (current?.currentRev !== expectedRev || savedRev !== expectedRev + 1) return;
  _commit(planId, { ...current, currentRev: savedRev });
}

/** Feishu 07.06 #7: currently running plan IDs. Stop optimistically removes running plan cards (backend
 * cancel_turn cascades durable cancellation; the cancel-execution button was removed, leaving Stop as the sole entry). */
export function runningPlanIds(): string[] {
  const out: string[] = [];
  _states.forEach((v, id) => {
    if (v.status === "running") out.push(id);
  });
  return out;
}

export function subscribePlan(planId: string, listener: () => void): () => void {
  let bucket = _listeners.get(planId);
  if (!bucket) {
    bucket = new Set();
    _listeners.set(planId, bucket);
  }
  bucket.add(listener);
  return () => {
    const b = _listeners.get(planId);
    if (!b) return;
    b.delete(listener);
    if (b.size === 0) _listeners.delete(planId);
  };
}

export function usePlan(planId: string): PlanView | undefined {
  return useSyncExternalStore(
    (cb) => subscribePlan(planId, cb),
    () => getPlanSnapshot(planId),
    () => undefined,
  );
}

/** Test helper — clears all plans. */
export function _resetAllPlansForTesting(): void {
  const ids = Array.from(_states.keys());
  _states.clear();
  _dismissedPlanIds.clear();
  for (const id of ids) _notify(id);
}

// ── mutators ─────────────────────────────────────────────────────

/** plan_proposed SSE / dev REST create → seed the card (steps default pending). */
export function applyPlanProposed(payload: {
  plan_id: string;
  current_rev?: number;
  title?: string;
  status?: string;
  steps?: unknown[];
  workspace_id?: string;
  space_id?: string;  // Batch 3/c5 forward safeguard (BE may switch to a new key).
  deliverable_kind?: string;  // C3/P2: completed ANSWER plans show no report surface.
  /**
   * ★2026-08-15 user decision: **Auto does not await approval**. The three-state descriptions themselves define this:
   * "计划模式 = 先出计划，等你批准再执行", while "自动 = 需要时才出计划" never promises an approval gate.
   * Backend executes in the same turn after creating an Auto card, so the card **starts running**, without "批准".
   * (No extra brake needed: composer's "停止" already cascades turn cancellation; the running footer's cancel button
   * was removed precisely to avoid a second entry.)
   */
  auto_run?: boolean;
}): void {
  const planId = String(payload.plan_id ?? "");
  if (!planId || _dismissedPlanIds.has(planId)) return;
  // No replay clobbering (2026-07): F5 hydrate replayTurnEvents runs concurrently across turns,
  // without ordering. Execution-turn plan_*/step_* may arrive first; later proposal events must not reset advanced
  // state to proposed (mount reconciliation provides authoritative-state fallback).
  const prev = _states.get(planId);
  if (prev && prev.status !== "proposed") return;
  const revision = planRevision(payload.current_rev);
  if (prev?.currentRev !== undefined && (revision === undefined || revision < prev.currentRev)) return;
  const steps = Array.isArray(payload.steps)
    ? payload.steps.map((s) => normalizeStep(s as Record<string, unknown>))
    : [];
  _commit(planId, {
    planId,
    currentRev: revision,
    title: String(payload.title ?? ""),
    status: mapPlanStatus(payload.status ?? (payload.auto_run ? "running" : "proposed")),
    steps,
    workspaceId: typeof payload.workspace_id === "string" ? payload.workspace_id
      : (typeof payload.space_id === "string" ? payload.space_id : undefined),  // Batch 3/c5 forward dual-read.
    deliverableKind: typeof payload.deliverable_kind === "string" ? payload.deliverable_kind : undefined,  // C3/P2
  });
}

/**
 * dev REST get/approve snapshot → replace plan + step statuses wholesale.
 * Preserves the existing title (REST get returns plan.title; approve doesn't).
 */
export function applyPlanSnapshot(payload: {
  plan_id: string;
  plan_status?: string;
  plan?: { id?: string; status?: string; title?: string; workspace_id?: string; space_id?: string; deliverable_kind?: string; current_rev?: number };
  steps?: unknown[];
}): void {
  const planId = String(payload.plan_id ?? payload.plan?.id ?? "");
  if (!planId || _dismissedPlanIds.has(planId)) return;
  const prev = _states.get(planId);
  const revision = planRevision(payload.plan?.current_rev);
  if (revision !== undefined && prev?.currentRev !== undefined && revision < prev.currentRev) return;
  const steps = Array.isArray(payload.steps)
    ? payload.steps.map((s) => {
        const n = normalizeStep(s as Record<string, unknown>);
        // planning-mode-audit WP-7 review-P2: feasibility is computed only on plan_proposed
        // (deterministic proposal-time signal, not persisted); snapshot/F5 reconciliation must not clear it.
        // needs_web is persisted; preserve prev when a snapshot omits it (old backend) too.
        const old = prev?.steps.find((p) => p.id === n.id);
        return {
          ...n,
          feasibility: n.feasibility ?? old?.feasibility ?? null,
          needsWeb: n.needsWeb || old?.needsWeb || false,
          // graph-mode batch 1 (review B5/C1 precedent): snapshot merging must preserve received
          // criteria/verify; retain prev when old-backend/intermediate snapshots omit fields.
          successCriteria: n.successCriteria?.length ? n.successCriteria : old?.successCriteria ?? [],
          verify: n.verify ?? old?.verify ?? null,
          // Batch 3: preserve revision pointers too (old-backend snapshots omitting fields do not clear them);
          // superseded never downgrades (R2-F7): late stale reconciliation must not revive dead rows.
          supersededBy: n.supersededBy ?? old?.supersededBy ?? null,
          agentChoice: n.agentChoice !== undefined ? n.agentChoice : old?.agentChoice,
          status: old?.status === "superseded" ? ("superseded" as const) : n.status,
        };
      })
    : prev?.steps ?? [];
  const status = mapPlanStatus(payload.plan?.status ?? payload.plan_status ?? prev?.status);
  _commit(planId, {
    planId,
    title: payload.plan?.title ?? prev?.title ?? "",
    currentRev: revision ?? (payload.steps === undefined ? prev?.currentRev : undefined),
    status,
    steps,
    workspaceId: typeof payload.plan?.workspace_id === "string"
      ? payload.plan.workspace_id
      : (typeof payload.plan?.space_id === "string" ? payload.plan.space_id : prev?.workspaceId),  // Batch 3/c5
    deliverableKind: typeof payload.plan?.deliverable_kind === "string"
      ? payload.plan.deliverable_kind
      : prev?.deliverableKind,  // C3/P2: preserve prev when snapshots omit fields (old backend).
  });
}

/** step_started — mark the given step ids running (never revive superseded rows, R2-F7). */
export function applyPlanStepStarted(planId: string, stepIds: string[]): void {
  const prev = _states.get(planId);
  if (!prev) return;
  const ids = new Set(stepIds);
  const nextSteps = prev.steps.map((s) =>
    ids.has(s.id) && s.status !== "superseded" ? { ...s, status: "running" as PlanStepStatus } : s,
  );
  _commit(planId, { ...prev, steps: nextSteps });
}

/** step_done — mark a step done (graph-mode batch 1: may carry verification results). */
export function applyPlanStepDone(planId: string, stepId: string, verify?: unknown): void {
  const v = normalizeVerify(verify);
  _patchStep(planId, stepId, (s) => ({ ...s, status: "done", verify: v ?? s.verify ?? null }));
}

/** step_blocked — status is "blocked" | "skipped"; carries error_kind + reason. */
export function applyPlanStepBlocked(
  planId: string,
  stepId: string,
  status: string,
  errorKind?: string | null,
  reason?: string | null,
): void {
  _patchStep(planId, stepId, (s) => ({
    ...s,
    status: mapStepStatus(status),
    errorKind: errorKind ?? s.errorKind ?? null,
    reason: reason ?? s.reason ?? null,
  }));
}

/** step_failed — mark a step failed.
 *
 * `verify` is critical enforce-phase payload: backend criteria_unmet step_failed already included
 * unmet items (plan_driver `_verify_event_field`), but FE previously discarded the entire field, so
 * enforce left users only a truncated system error, without showing which acceptance criteria failed
 * (tech-design §5 before enforce②). */
export function applyPlanStepFailed(
  planId: string,
  stepId: string,
  errorKind?: string | null,
  verify?: unknown,
  reason?: string | null,
): void {
  const v = normalizeVerify(verify);
  _patchStep(planId, stepId, (s) => ({
    ...s,
    status: "failed",
    errorKind: errorKind ?? s.errorKind ?? null,
    // ★2026-08-15 116 dogfood: exactly the same problem as `verify` above, in the same event.
    // Live cards showed **blank** after "Failed:"; only F5 revealed full text (always present in storage),
    // because step_failed emitted no reason and this parameter was absent here. Adjacent step_blocked always carried it.
    reason: reason ?? s.reason ?? null,
    verify: v ?? s.verify ?? null,
  }));
}

/** plan_running / plan_completed / plan_blocked / plan_failed → plan-level status. */
export function applyPlanStatus(planId: string, status: PlanStatus): void {
  const prev = _states.get(planId);
  if (!prev) return;
  if (prev.status === status) return;
  _commit(planId, { ...prev, status });
}

/** Optimistic local status flip (e.g. approve → running before SSE/REST lands). */
export function setPlanLocalStatus(planId: string, status: PlanStatus): void {
  applyPlanStatus(planId, status);
}

/** Sticky in-page dismissal: late reconcile/SSE snapshots must not revive it. */
export function dismissPlan(planId: string): void {
  _dismissedPlanIds.add(planId);
  if (!_states.has(planId)) return;
  _states.delete(planId);
  _notify(planId);
}

export function isPlanDismissed(planId: string): boolean {
  return _dismissedPlanIds.has(planId);
}

// ── A (editable): local optimistic editing mutators ──────────────────────────────
// All update local store immediately for UX; components then persist the entire set through plan-api.patchPlanSteps,
// with failure rollback via component applyPlanSnapshot (refetch).

/** Change one step's goal (save inline edit). */
export function editStepObjective(planId: string, stepId: string, objective: string): void {
  _patchStep(planId, stepId, (s) => ({ ...s, objective }));
}

/** Check/uncheck (soft include/exclude). */
export function toggleStepIncluded(planId: string, stepId: string): void {
  _patchStep(planId, stepId, (s) => ({ ...s, included: !s.included }));
}

/** Delete a step and remove it from others' dependsOn (avoid dangling references). */
export function deleteStep(planId: string, stepId: string): void {
  const prev = _states.get(planId);
  if (!prev) return;
  const steps = prev.steps
    .filter((s) => s.id !== stepId)
    .map((s) => ({ ...s, dependsOn: s.dependsOn.filter((d) => d !== stepId) }));
  _commit(planId, { ...prev, steps });
}

/** Drag reorder: move draggedId before targetId (within one layer; backend resets seq after persistence). */
export function reorderSteps(planId: string, draggedId: string, targetId: string): void {
  const prev = _states.get(planId);
  if (!prev || draggedId === targetId) return;
  const arr = [...prev.steps];
  const from = arr.findIndex((s) => s.id === draggedId);
  const to = arr.findIndex((s) => s.id === targetId);
  if (from < 0 || to < 0) return;
  const [moved] = arr.splice(from, 1);
  const insertAt = arr.findIndex((s) => s.id === targetId);
  arr.splice(insertAt, 0, moved);
  _commit(planId, { ...prev, steps: arr });
}
