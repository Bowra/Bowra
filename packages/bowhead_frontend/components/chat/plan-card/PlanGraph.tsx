"use client";

/**
 * Plan card · dependency graph view (graph-mode batch 5)
 *
 * Design authority = `docs/sprints/graph-mode/design/batch5/` (graph.css visuals / graph-core.js layout).
 * The graph **stays** atop the card, with the list below; no graph/list switch: the graph shows shape and bottlenecks,
 * while the list provides all actions and details (mandate ⑤: no functionality loss). Only the graph area collapses.
 *
 * Deliberate departures from the design (design review P1, batch 5 in design-review-2026-08-04.md):
 * 1. Resolve supersede chains (in graph-layout.ts): the design's single-hop resolution loses edges after a second revision.
 * 2. Do not implement running-node shimmer with `overflow:hidden` (it also clips hover details; see plan-graph.css).
 * 3. "联网检索" is not a role: real data has no web agent; web access is a step's needsWeb
 *    flag. Node identity colors still use roleForAgent(step.agent); web access uses an extra badge.
 */

import { useRef, useState } from "react";

import { PlanIcon, type PlanIconName } from "./icons";
import {
  buildGraphModel,
  isBlockedByUpstream,
  shouldRenderGraph,
  verifyGlyphKey,
  brokenCount,
  stepperLinkState,
  stepperRingState,
} from "./graph-layout";
import { planAgentLabel, roleForAgent } from "./roles";
import type { PlanStepStatus, PlanStepView } from "./types";
import { CharIcon } from "@/components/char-icon";

type T = (key: string, values?: Record<string, string | number>) => string;

/** Below this card width, layers switch from columns to rows (JS counterpart of the container query; same design value). */
const VERTICAL_BELOW = 460;

/** Status → glyph (distinguish by shape as well as color; mandate ⑤-4 a11y). */
const ST_GLYPH: Record<PlanStepStatus, { icon: PlanIconName; stroke: number }> = {
  pending: { icon: "circleSm", stroke: 1.7 },
  running: { icon: "circleSm", stroke: 0 }, // Render an actual spinner, not an icon.
  done: { icon: "check", stroke: 2.6 },
  failed: { icon: "x", stroke: 2.6 },
  blocked: { icon: "alertTriangle", stroke: 2.1 },
  skipped: { icon: "slash", stroke: 1.9 },
  superseded: { icon: "rotateCw", stroke: 2 },
};

const VF_GLYPH = { ok: "✓", part: "◐", unmet: "✕", none: "○" } as const;
/** ★Implementation review P2-C: reuse the list's existing verify_* copy: one verification result on one card
 *  must not have two Chinese labels (new graph_verify_* would make the graph say "部分满足" and the list "部分达标").
 *  Mandate ⑦ marks these four as existing copy to retain, precisely to require reuse. */
const VF_LABEL_KEY = { ok: "verify_pass", part: "verify_partial", unmet: "verify_fail", none: "verify_skipped" } as const;

function statusLabel(status: PlanStepStatus, t: T): string {
  return status === "superseded" ? t("revised_tag") : t(`status_${status}`);
}

function StatusGlyph({ status }: { status: PlanStepStatus }) {
  if (status === "running") {
    return (
      <span className="gn-st">
        <span className="spin" />
      </span>
    );
  }
  const g = ST_GLYPH[status] ?? ST_GLYPH.pending;
  return (
    <span className="gn-st">
      <PlanIcon name={g.icon} size={13} />
    </span>
  );
}

function VerifyPill({ kind, t }: { kind: keyof typeof VF_GLYPH; t: T }) {
  const label = t(VF_LABEL_KEY[kind]);
  return (
    <span className={`vpill ${kind}`}>
      <CharIcon ch={VF_GLYPH[kind]} />
      {label}
    </span>
  );
}

function StatusPillG({ status, t }: { status: PlanStepStatus; t: T }) {
  const cls =
    { pending: "todo", running: "run", done: "done", failed: "failed", blocked: "blocked", skipped: "skip", superseded: "skip" }[
      status
    ] ?? "todo";
  return (
    <span className={`pp ${cls}`}>
      {status === "running" ? <span className="spin" /> : <PlanIcon name={(ST_GLYPH[status] ?? ST_GLYPH.pending).icon} size={12} />}
      {statusLabel(status, t)}
    </span>
  );
}

export interface PlanGraphRecovery {
  onRevise?: (stepId: string) => void;
  onSkip?: (stepId: string) => void;
  onRetry?: () => void;
  /** Same signature as the list's PlanRecoveryHandlers.onSupply (no stepId). */
  onSupply?: () => void;
  /** ★Implementation review P1-5/P1-C: all four list buttons are disabled while a request is in flight; the graph must match
   *  or it becomes a second entry bypassing that guard (duplicate revise/skip → double confirm/409 surfaces). */
  busy?: boolean;
}

interface NodeProps {
  step: PlanStepView;
  t: T;
  ghost?: boolean;
  /** Unchecked steps in edit mode (proposed). */
  off?: boolean;
  open: boolean;
  blockedByUpstream: boolean;
  linked: boolean;
  onToggle: (id: string) => void;
  recovery?: PlanGraphRecovery;
  displayNameFor?: (planStepId: string) => string | undefined;
}

function GraphNode({
  step,
  t,
  ghost,
  off,
  open,
  blockedByUpstream,
  linked,
  onToggle,
  recovery,
  displayNameFor,
}: NodeProps) {
  const role = roleForAgent(step.agent);
  const label = planAgentLabel(step.agent, displayNameFor?.(step.id));
  const status: PlanStepStatus = ghost ? "superseded" : step.status;
  const vf = ghost ? null : verifyGlyphKey(step);
  const revised = !ghost && (step.origin === "revision" || step.origin === "replan");

  const cls = ["gnode", `is-${status}`];
  if (off) cls.push("is-off");
  if (open) cls.push("is-open");
  if (linked) cls.push("is-linked");

  const aria = `${label} · ${statusLabel(status, t)}${vf ? ` · ${t(VF_LABEL_KEY[vf])}` : ""}`;

  return (
    <div
      className={cls.join(" ")}
      data-node={step.id}
      data-testid={`graph-node-${step.id}`}
      role="button"
      tabIndex={ghost ? -1 : 0}
      aria-label={aria}
      aria-expanded={ghost ? undefined : open}
      onClick={ghost ? undefined : () => onToggle(step.id)}
      onKeyDown={
        ghost
          ? undefined
          : (e) => {
              // ★Implementation review P1-4/P1-B: handle only keys targeting the node itself. Recovery
              // buttons in the details card are descendants, so keydown bubbles here; unconditional preventDefault
              // swallowed their entire Enter/Space activation (observed onRevise never firing,
              // while the node collapsed), making graph recovery unavailable to keyboard users.
              if (e.target !== e.currentTarget) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onToggle(step.id);
              }
            }
      }
    >
      <span className="gn-ic" style={{ background: role.tintVar, color: role.colorVar }}>
        <PlanIcon name={role.icon} size={13} />
      </span>
      <div className="gn-tx">
        <span className="gn-name">{label}</span>
        {step.deliverable ? <span className="gn-topic">{step.deliverable}</span> : null}
      </div>
      <div className="gn-marks">
        {revised ? (
          <span className="gn-stamp">
            <PlanIcon name="rotateCw" size={8} />
            {t("revised_tag")}
          </span>
        ) : null}
        {/* "联网检索" is the step's needsWeb flag, not a role (see header comment 3). */}
        {step.needsWeb ? (
          <span className="gn-web" title={t("feas_web")} aria-label={t("feas_web")}>
            <PlanIcon name="globe" size={11} />
          </span>
        ) : null}
        {vf ? (
          <span className={`gn-vf ${vf}`} title={t(VF_LABEL_KEY[vf])} aria-hidden="true">
            <CharIcon ch={VF_GLYPH[vf]} aria-hidden />
          </span>
        ) : null}
        <StatusGlyph status={status} />
      </div>

      {!ghost ? (
        <div className="gpop" onClick={(e) => e.stopPropagation()}>
          <div className="gpop-h">
            <span className="gn-ic" style={{ background: role.tintVar, color: role.colorVar }}>
              <PlanIcon name={role.icon} size={13} />
            </span>
            <span className="nm">{label}</span>
            <span className="sp" />
            <StatusPillG status={status} t={t} />
          </div>
          {step.objective ? <div className="gpop-goal">{step.objective}</div> : null}
          {vf ? (
            <div className="gpop-row">
              <span className="gpop-k">{t("criteria_label")}</span>
              <VerifyPill kind={vf} t={t} />
            </div>
          ) : null}
          {status === "failed" ? (
            <div className="gpop-acts">
              {recovery?.onRevise ? (
                <button type="button" className="gact primary" disabled={recovery.busy} onClick={() => recovery.onRevise?.(step.id)}>
                  <PlanIcon name="rotateCw" size={12} />
                  {t("recover_revise")}
                </button>
              ) : null}
              {recovery?.onSkip ? (
                <button type="button" className="gact warn" disabled={recovery.busy} onClick={() => recovery.onSkip?.(step.id)}>
                  <PlanIcon name="skipForward" size={12} />
                  {t("recover_skip")}
                </button>
              ) : null}
            </div>
          ) : null}
          {status === "blocked" && blockedByUpstream ? (
            <div className="gpop-note">{t("graph_blocked_cause")}</div>
          ) : status === "blocked" ? (
            <div className="gpop-acts">
              {recovery?.onSupply && step.needsCorpus ? (
                <button type="button" className="gact primary" disabled={recovery.busy} onClick={() => recovery.onSupply?.()}>
                  <PlanIcon name="upload" size={12} />
                  {t("recover_supply")}
                </button>
              ) : null}
              {recovery?.onRetry ? (
                <button type="button" className="gact" disabled={recovery.busy} onClick={() => recovery.onRetry?.()}>
                  <PlanIcon name="arrowUpRight" size={12} />
                  {t("recover_continue")}
                </button>
              ) : null}
              {recovery?.onSkip ? (
                <button type="button" className="gact warn" disabled={recovery.busy} onClick={() => recovery.onSkip?.(step.id)}>
                  <PlanIcon name="skipForward" size={12} />
                  {t("recover_skip")}
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export interface PlanGraphProps {
  steps: PlanStepView[];
  t: T;
  /** In proposed state, dim unchecked steps in the graph too. */
  editable?: boolean;
  recovery?: PlanGraphRecovery;
  /** Coordinate with the list: clicking a node highlights its corresponding row. */
  onSelectStep?: (stepId: string | null) => void;
  selectedStepId?: string | null;
  /** Share agent naming with the list (see roles.ts `planAgentLabel`): both are visible together,
   *  so different names make one node look like two agents. */
  displayNameFor?: (planStepId: string) => string | undefined;
}

export function PlanGraph({
  steps,
  displayNameFor,
  t,
  editable,
  recovery,
  onSelectStep,
  selectedStepId,
}: PlanGraphProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [folded, setFolded] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [revOpen, setRevOpen] = useState(false);

  const model = buildGraphModel(steps);
  const byId = new Map(steps.map((s) => [s.id, s]));
  const visible = new Set(steps.filter((s) => s.status !== "superseded").map((s) => s.id));
  const hasGhosts = model.ghostBySuccessor.size > 0;

  // ★The stepper layout (design 1c) has no per-node SVG edges: dependencies use rings in the left numbered column and
  // connecting lines (see graph-layout.ts:stepperRingState/stepperLinkState). The old
  // DOM box measurement → Bézier calculation → SVG rendering was removed along with graph-edges.ts:
  //   ·It depends on getBoundingClientRect, which always returns 0 in jsdom, making testing difficult;
  //   ·It needs ResizeObserver + fonts.ready redraws, formerly this component's largest state block;
  //   ·In vertical mode the graph is no longer a scroll container, so hover details are not clipped; the expand-in-place-while-scrolling
  //     patch is therefore obsolete too (cleaned up in plan-graph.css in the same batch).

  if (!shouldRenderGraph(steps)) return null;

  const toggleNode = (id: string) => {
    const next = openId === id ? null : id;
    setOpenId(next);
    onSelectStep?.(next);
  };

  const wrapCls = ["gwrap"];
  if (folded) wrapCls.push("folded");

  return (
    <div ref={wrapRef} className={wrapCls.join(" ")} data-testid="plan-graph">
      <div className="gtop">
        <span className="gtop-lb">
          <span className="gm" />
          {t("graph_label")}
        </span>
        {model.parallelCount > 1 ? (
          <span className="gtop-hint">{t("graph_parallel_hint", { count: model.parallelCount })}</span>
        ) : null}
        {model.joinCounts.map((n, i) => (
          <span key={i} className="gtop-hint">
            <span className="mdot" />
            {t("graph_join_hint", { count: n })}
          </span>
        ))}
        <span className="sp" />
        {hasGhosts ? (
          <button
            type="button"
            className={"gtop-btn" + (revOpen ? " on" : "")}
            aria-pressed={revOpen}
            onClick={() => setRevOpen((v) => !v)}
          >
            <PlanIcon name="rotateCw" size={10} />
            {t("graph_revision_history")}
          </button>
        ) : null}
        <button
          type="button"
          className="gtop-fold"
          aria-label={t("graph_fold")}
          aria-expanded={!folded}
          onClick={() => setFolded((v) => !v)}
        >
          <PlanIcon name="chevronDown" size={13} />
        </button>
      </div>

      <div ref={canvasRef} className="gcanvas">
        <div className="gsteps">
          {model.layers.map((layer, li) => {
            const ring = stepperRingState(layer.steps);
            const link = stepperLinkState(layer.steps);
            const broke = brokenCount(layer.steps);
            const last = li === model.layers.length - 1;
            const cls = ["glayer"];
            if (layer.isParallel) cls.push("is-par");
            if (layer.isFan) cls.push("is-fan");
            if (layer.isGrid) cls.push("is-grid");
            return (
              <div key={layer.level} className="gstep-row">
                {/* Left numbered column = progress bar (design 1c). aria-hidden: each node's own aria-label
                    already conveys layer progress to screen-reader users; this column is a
                    visual duplicate, so reading it would only repeat the information. */}
                <div className="gstep-gut" aria-hidden="true">
                  <span className={"gstep-ring is-" + ring}>
                    {ring === "done" ? <PlanIcon name="check" size={10} /> : li + 1}
                  </span>
                  {last ? null : <span className={"gstep-link is-" + link} />}
                </div>
                <div className={"gstep-body " + cls.join(" ")}>
                  <div className="gstep-cap">
                    {layer.isParallel
                      ? t("rail_parallel", { count: layer.steps.length })
                      : `${t("join_before", { count: model.layers[li - 1]?.steps.length ?? layer.joinCount })} ${t("join_after")} ${t("join_word")}`}
                    {broke ? <b className="gstep-broke">{t("graph_upstream_failed", { count: broke })}</b> : null}
                  </div>
                {layer.steps.map((s) => {
                  const ghost = revOpen ? model.ghostBySuccessor.get(s.id) : undefined;
                  const upstreamBlocked = isBlockedByUpstream(s, byId, visible);
                  const node = (
                    <GraphNode
                      step={s}
                      t={t}
                      off={editable && !s.included}
                      open={openId === s.id}
                      blockedByUpstream={upstreamBlocked}
                      linked={selectedStepId === s.id}
                      onToggle={toggleNode}
                      recovery={recovery}
                      displayNameFor={displayNameFor}
                    />
                  );
                  if (ghost) {
                    return (
                      <div key={s.id} className="gsup-slot">
                        <GraphNode
                          step={ghost}
                          t={t}
                          ghost
                          open={false}
                          blockedByUpstream={false}
                          linked={false}
                          onToggle={() => {}}
                          displayNameFor={displayNameFor}
                        />
                        <div className="gghost-note">↳ {t("graph_revision_replaced")}</div>
                        {node}
                        {/* Implementation review P3-G: the ghost branch previously returned immediately, losing the
                            successor's broken-upstream note when revision history expanded. */}
                        {upstreamBlocked ? (
                          <div className="gcause">
                            <PlanIcon name="alertTriangle" size={11} />
                            {t("graph_blocked_cause")}
                          </div>
                        ) : null}
                      </div>
                    );
                  }
                  return (
                    <div key={s.id} className="gnode-cell">
                      {node}
                      {upstreamBlocked ? (
                        <div className="gcause">
                          <PlanIcon name="alertTriangle" size={11} />
                          {t("graph_blocked_cause")}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
