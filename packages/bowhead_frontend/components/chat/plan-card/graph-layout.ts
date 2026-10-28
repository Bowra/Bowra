/**
 * Dependency graph view · pure layout/edge logic (graph-mode batch 5)
 *
 * Design authority = `docs/sprints/graph-mode/design/batch5/graph-core.js` (literal port),
 * with **two deliberate departures** (design review P1, batch 5 in design-review-2026-08-04.md):
 *
 * 1. Resolve supersede through **the entire chain**, not one hop. The design's `steps.find(s => s.supersedes === id)`
 *    finds only the direct successor; after A→A′→A″ revisions, downstream depends_on still contains the oldest A, while A′
 *    is itself superseded and filtered out → single-hop resolution returns null → edge silently disappears. Batch 3 driver aliases
 *    aggregate along the chain + impose a hop limit + fail-open on cycles; use the same approach here.
 * 2. Deterministic layout: columns by level, then seq within each column; no force-directed layout or third-party graph library (mandate ⑩-2).
 */

import type { PlanStepView } from "./types";

/** Maximum hops along supersededBy (same value as plan_driver's _SUPERSESSION_MAX_HOPS). */
export const SUPERSESSION_MAX_HOPS = 32;

/** Above this node count in one layer, wrap into 2 columns (spec: >6 nodes per layer → 2 columns). */
export const LAYER_GRID_THRESHOLD = 6;

/** Enable horizontal scrolling at this layer count (spec: ≥3 layers → horizontal scrolling). */
export const SCROLL_LEVEL_THRESHOLD = 3;

/**
 * Whether horizontal scrolling is needed. Layer count is a **first-frame fallback** before geometry is measured; the actual criterion is **fit**.
 *
 * ★Caught on-device 2026-08-13: the threshold was 3, so **2-layer plans never scrolled**;
 * `.gcanvas` defaults to `overflow-x: visible`, so when layer two extends past the card's right edge, that content
 * **neither wraps nor scrolls: it is clipped and inaccessible**. User screenshot: a 6-step plan
 * (5 parallel steps → 1 summary) with the summary node "合规风险" half clipped at the right edge.
 * Device comparison: another 2-layer plan measured 442+96+240=778 against an 802 canvas, **just fitting**,
 * so the issue stayed hidden (fewer layers ≠ fit; node width is the variable).
 *
 * Enable only on **actual overflow**: fitting plans behave identically, gaining neither an unnecessary scrollbar nor
 * the `.is-scroll` side effect of expanding details in place (see plan-graph.css's note that
 * `overflow-x:auto` also makes `overflow-y` auto).
 */
export function needsHorizontalScroll(opts: { layerCount: number; contentW: number; availW: number }): boolean {
  const { layerCount, contentW, availW } = opts;
  if (layerCount >= SCROLL_LEVEL_THRESHOLD) return true;
  if (!(contentW > 0) || !(availW > 0)) return false; // Not measured yet (jsdom / first frame) → do not guess.
  return contentW > availW + 1; // 1px tolerance: subpixel rounding must not create a scrollbar.
}

/**
 * Two states for the stepper layout's left numbered column (design 1c). Design decision: this column is not mere decoration;
 * **it is the progress bar**. A glance shows progress and breaks without reading node text.
 *
 * Ring = how this layer itself ran; link = quality of the baton it passes to the next layer.
 * Keep them separate because **they can differ**: if three steps succeed and one fails, the ring should say
 * this layer has run, while the link must say downstream input is incomplete. Merging the states
 * loses the latter signal, precisely the one users most need to see immediately.
 */
export type StepperRingState = "done" | "running" | "pending";
export type StepperLinkState = "done" | "running" | "broken" | "pending";

/** Terminal states counting as successful; include skipped because a user-initiated skip is not a fault. */
const _SETTLED_OK = new Set(["done", "skipped"]);
const _BROKEN = new Set(["failed", "blocked"]);

/**
 * Ring criterion: has this layer finished? **Failed also counts as finished** (blocked does not: its turn has not arrived).
 *
 * ★Caught on device 116, 2026-08-14: the old `every(_SETTLED_OK)` meant **any failed step**
 * returned the entire ring to `pending` = gray = not reached. Both steps failed on-device, yet the left column
 * showed the layer as not started. That column is the designed progress bar, so a glance gave users **false information**.
 * More direct evidence: the comment above itself says with three successes and one failure, the ring should say the layer has run;
 * **the code never did that** (`every` is false for a mixed layer).
 *
 * Failure signaling does not belong to the ring: design 2b explicitly assigns it to **links** (dashed red `is-broken`) + node color +
 * breakpoint badges. The ring answers only how far execution progressed. Device screenshots confirmed all three correctly red.
 */
const _RING_SETTLED = new Set(["done", "skipped", "failed"]);

export function stepperRingState(steps: readonly PlanStepView[]): StepperRingState {
  if (!steps.length) return "pending";
  if (steps.some((s) => s.status === "running")) return "running";
  return steps.every((s) => _RING_SETTLED.has(s.status)) ? "done" : "pending";
}

export function stepperLinkState(steps: readonly PlanStepView[]): StepperLinkState {
  if (!steps.length) return "pending";
  // broken takes precedence over done: three successes and one failure give downstream incomplete input,
  // so this link must be red; a successful majority must not hide the signal.
  if (steps.some((s) => _BROKEN.has(s.status))) return "broken";
  const ring = stepperRingState(steps);
  return ring === "done" ? "done" : ring === "running" ? "running" : "pending";
}

/** Number of failed/blocked steps in this layer, for the heading's "N 路上游失败" count. */
export function brokenCount(steps: readonly PlanStepView[]): number {
  return steps.filter((s) => _BROKEN.has(s.status)).length;
}

export type GraphEdgeState = "norm" | "active" | "done" | "broken";

export interface GraphEdge {
  from: string;
  to: string;
  state: GraphEdgeState;
}

export interface GraphLayer {
  level: number;
  steps: PlanStepView[];
  /** First layer with more than one node → parallel vertical rail. */
  isParallel: boolean;
  /** Non-first layer → fan-in column, headed "N 路汇合". */
  isFan: boolean;
  /** Too many nodes → 2-column grid. */
  isGrid: boolean;
  /** Maximum incoming-edge count in this layer (>1 means fan-in; otherwise 0 = no note). */
  joinCount: number;
}

export interface GraphModel {
  layers: GraphLayer[];
  edges: GraphEdge[];
  /** First-layer parallel node count (0 = no parallel group), for the eyebrow's "N 步并行". */
  parallelCount: number;
  /** Incoming-edge counts at fan-in points, for the eyebrow's "N 路汇合". */
  joinCounts: number[];
  /** Layer count ≥ threshold → horizontal canvas scrolling. */
  needsScroll: boolean;
  /** Replaced old step → successor (attach ghost nodes when revision history expands). */
  ghostBySuccessor: Map<string, PlanStepView>;
}

/**
 * Resolve a dependency id that may point to an old step to its currently visible successor.
 *
 * null = unresolved (no successor / broken chain / cycle / hop limit exceeded); fail-open:
 * callers discard the edge without guessing.
 */
export function resolveDependency(
  depId: string,
  byId: Map<string, PlanStepView>,
  visible: Set<string>,
): string | null {
  if (visible.has(depId)) return depId;
  const seen = new Set<string>([depId]);
  let cur = byId.get(depId);
  for (let hop = 0; hop < SUPERSESSION_MAX_HOPS; hop += 1) {
    const next = cur?.supersededBy;
    if (!next || seen.has(next)) return null; // Broken chain / cycle.
    if (visible.has(next)) return next;
    seen.add(next);
    cur = byId.get(next);
    if (!cur) return null;
  }
  return null; // Hop limit exceeded.
}

function edgeState(
  fromStatus: PlanStepView["status"],
  toStatus: PlanStepView["status"],
): GraphEdgeState {
  if (fromStatus === "failed") return "broken";
  if (fromStatus === "done" && toStatus === "running") return "active";
  if (fromStatus === "done" && toStatus === "done") return "done";
  return "norm";
}

/**
 * Compute the graph model from all plan steps (including superseded dead rows, which supply lineage).
 *
 * `visibleSteps` contains only live rows to draw; superseded steps stay out of view (mandate ⑩-4), but remain in
 * `all` as dependency-resolution intermediates and ghost-node data sources.
 */
export function buildGraphModel(all: PlanStepView[]): GraphModel {
  const byId = new Map(all.map((s) => [s.id, s]));
  const visibleSteps = all.filter((s) => s.status !== "superseded");
  const visible = new Set(visibleSteps.map((s) => s.id));

  const edges: GraphEdge[] = [];
  const inDegree = new Map<string, number>();
  for (const target of visibleSteps) {
    for (const dep of target.dependsOn ?? []) {
      const src = resolveDependency(dep, byId, visible);
      if (!src || src === target.id) continue;
      if (edges.some((e) => e.from === src && e.to === target.id)) continue; // Two old edges resolve to the same successor.
      const from = byId.get(src);
      if (!from) continue;
      edges.push({ from: src, to: target.id, state: edgeState(from.status, target.status) });
      inDegree.set(target.id, (inDegree.get(target.id) ?? 0) + 1);
    }
  }

  // Layers: columns by level, then seq within each. Incoming counts are ready for each layer's "N 路汇合" note.
  const levels = [...new Set(visibleSteps.map((s) => s.level))].sort((a, b) => a - b);
  const layers: GraphLayer[] = levels.map((level, idx) => {
    const steps = visibleSteps
      .filter((s) => s.level === level)
      .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    const maxIn = steps.reduce((m, s) => Math.max(m, inDegree.get(s.id) ?? 0), 0);
    return {
      level,
      steps,
      isParallel: idx === 0 && steps.length > 1,
      isFan: idx > 0,
      isGrid: steps.length > LAYER_GRID_THRESHOLD,
      joinCount: maxIn > 1 ? maxIn : 0, // A single upstream edge is not fan-in.
    };
  });

  // Ghost old step: replaced step → its visible successor. For repeated revisions, attach only the latest hop,
  // keeping earlier history out of view (mandate ⑩-4: revisions must not increase clutter).
  const ghostBySuccessor = new Map<string, PlanStepView>();
  for (const old of all) {
    if (old.status !== "superseded" || !old.supersededBy) continue;
    if (visible.has(old.supersededBy)) ghostBySuccessor.set(old.supersededBy, old);
  }

  const first = layers[0];
  return {
    layers,
    edges,
    parallelCount: first?.isParallel ? first.steps.length : 0,
    joinCounts: [...inDegree.values()].filter((n) => n > 1),
    needsScroll: layers.length >= SCROLL_LEVEL_THRESHOLD,
    ghostBySuccessor,
  };
}

/** Single-step plans (0 or 1 visible node) render no graph: it adds no information (mandate ④4.5). */
export function shouldRenderGraph(all: PlanStepView[]): boolean {
  return all.filter((s) => s.status !== "superseded").length > 1;
}

/** Verification verdict → design's four-state glyph keys; shown only on done steps. */
export function verifyGlyphKey(
  step: PlanStepView,
): "ok" | "part" | "unmet" | "none" | null {
  if (step.status !== "done" || !step.verify) return null;
  switch (step.verify.verdict) {
    case "pass":
      return "ok";
    case "partial":
      return "part";
    case "fail":
      return "unmet";
    default:
      return "none";
  }
}

/** Is downstream blocked due to upstream failure? Determines causal notes versus actions. */
export function isBlockedByUpstream(
  step: PlanStepView,
  byId: Map<string, PlanStepView>,
  visible: Set<string>,
): boolean {
  if (step.status !== "blocked") return false;
  return (step.dependsOn ?? []).some((dep) => {
    const src = resolveDependency(dep, byId, visible);
    return !!src && byId.get(src)?.status === "failed";
  });
}
