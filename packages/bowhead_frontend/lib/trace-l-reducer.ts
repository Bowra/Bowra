/**
 * V2.8 L · Trace-L reducer pure logic (extracted from use-turn-runner for unit tests).
 *
 * Only state-transition helpers for plan / nested-subagent / progress belong here. Other
 * reducer logic (chips / answer / clarifications) stays in use-turn-runner because it is
 * unrelated to layer L.
 */

import type { PlanPreview, PlanStepStatus, TraceStep } from "./types";

// ─────────────────────────────────────────────────────────────
// L1 · plan ↔ trace association.
// ─────────────────────────────────────────────────────────────

/** Find next pending plan step index matching tool_name; -1 = no match. */
import { isCanvasTool } from "@/lib/tool-labels";

export function matchPlanStepForTool(
  plan: PlanPreview,
  toolName: string,
): number {
  return plan.steps.findIndex(
    (p) =>
      p.status === "pending" &&
      (p.skillTag === toolName ||
        // tool-domain P2e: accept post-omicron underscore names alongside legacy dot.
        (p.skillTag === "compute" && (toolName === "skill.invoke" || toolName === "skill_invoke")) ||
        // `canvas.` is the pre-omicron legacy dot spelling; `isCanvasTool` covers underscore names plus renamed
        // add_chart / add_image without a canvas prefix (docs/design/工具改名迁移.md).
        (p.skillTag === "canvas" &&
          (toolName.startsWith("canvas.") || isCanvasTool(toolName)))),
  );
}

/** tool_call → pending plan step becomes running (new plan; unchanged if unmatched). */
export function planStepRunning(
  plan: PlanPreview,
  toolName: string,
): PlanPreview {
  if (plan.status !== "running") return plan;
  const idx = matchPlanStepForTool(plan, toolName);
  if (idx < 0) return plan;
  const updated = [...plan.steps];
  updated[idx] = { ...updated[idx], status: "running" };
  return { ...plan, steps: updated };
}

/** tool_result → running step becomes done/failed; failure skips subsequent pending steps. */
export function planStepResolved(
  plan: PlanPreview,
  ok: boolean,
  durMs: number,
  errMsg?: string | null,
): PlanPreview {
  if (plan.status !== "running") return plan;
  const idx = plan.steps.findIndex((p) => p.status === "running");
  if (idx < 0) return plan;
  const updated = [...plan.steps];
  updated[idx] = {
    ...updated[idx],
    status: ok ? "done" : "failed",
    actualMs: durMs,
    ...(ok ? {} : { errMsg: errMsg || "failed" }),
  };
  // After failure, skip all subsequent pending steps.
  if (!ok) {
    for (let i = idx + 1; i < updated.length; i++) {
      if (updated[i].status === "pending") {
        updated[i] = { ...updated[i], status: "skipped" };
      }
    }
  }
  return { ...plan, steps: updated };
}

/** Turn done → mark all pending steps skipped, plan done. */
export function planFinish(
  plan: PlanPreview,
  finishedAt: number,
  status: "done" | "failed",
): PlanPreview {
  const finalSteps = plan.steps.map((p) =>
    p.status === "pending" ? { ...p, status: "skipped" as PlanStepStatus } : p,
  );
  return { ...plan, steps: finalSteps, status, finishedAt };
}

// ─────────────────────────────────────────────────────────────
// L2 · subagent_id backfill (CTO review fix)
// ─────────────────────────────────────────────────────────────

/**
 * On a child tool_call (backend payload has subagent_id), find the latest
 * running subagent.spawn step without a bound subagentId and stamp
 * that subagentId. Later children use this true ID to match nested folding.
 *
 * Return new traceSteps (unchanged if no changes). subagent.spawn itself must not trigger backfill.
 */
export function backfillSpawnSubagentId(
  steps: readonly TraceStep[],
  toolName: string,
  subagentId: string | undefined,
): TraceStep[] {
  if (!subagentId || toolName === "subagent.spawn") {
    return [...steps];
  }
  const alreadyLinked = steps.some(
    (st) => st.toolName === "subagent.spawn" && st.subagentId === subagentId,
  );
  if (alreadyLinked) return [...steps];
  // Search backward for the latest running spawn step without a bound ID.
  for (let i = steps.length - 1; i >= 0; i--) {
    const st = steps[i];
    if (
      st.toolName === "subagent.spawn" &&
      st.kind === "running" &&
      !st.subagentId
    ) {
      const out = [...steps];
      out[i] = { ...st, subagentId };
      return out;
    }
  }
  return [...steps];
}

// ─────────────────────────────────────────────────────────────
// Render time: attach children to spawn steps by step.subagentId.
// ─────────────────────────────────────────────────────────────

/** Parent subagent.spawn row's detail.__children holds this spawn's child steps. */
export function attachSubagentChildren(steps: TraceStep[]): TraceStep[] {
  const topLevel: TraceStep[] = [];
  const childrenByParent: Record<string, TraceStep[]> = {};
  for (const s of steps) {
    if (s.subagentId && s.toolName !== "subagent.spawn") {
      const arr = childrenByParent[s.subagentId] || [];
      arr.push(s);
      childrenByParent[s.subagentId] = arr;
    } else {
      topLevel.push(s);
    }
  }
  return topLevel.map((s) => {
    if (s.toolName === "subagent.spawn" && s.subagentId) {
      const kids = childrenByParent[s.subagentId];
      if (kids && kids.length > 0) {
        return { ...s, detail: { ...(s.detail || {}), __children: kids } };
      }
    }
    return s;
  });
}
