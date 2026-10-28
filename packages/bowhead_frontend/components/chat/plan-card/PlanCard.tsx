"use client";

/**
 * PlanCard — Planning Mode plan card. Verbatim translation of the Claude Design
 * handoff (planning-mode/Planning Mode.html · planning.js renderPlanCard).
 *
 * Presentational only: takes a PlanView + action callbacks; the connected
 * wrapper (PlanCardConnected) subscribes to plan-store and supplies them.
 *
 * M1 happy-path scope: renders proposed / running / blocked / completed from
 * real backend data. The design's editable affordances (include checkbox,
 * drag/delete, inline goal edit, NL-modify, AI-ghost step) and blocked
 * recovery actions (retry/skip/upload) need M2 backend, so they are NOT
 * rendered here — only the lifecycle the backend supports. Their CSS lives in
 * styles/plan-card.css ready for M2.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { PlanIcon } from "./icons";
import { shouldRenderGraph } from "./graph-layout";
import { PlanGraph } from "./PlanGraph";
import { planAgentLabel, roleForAgent } from "./roles";
import {
  type PlanStepStatus,
  type PlanStepView,
  type PlanView,
  variantForStatus,
} from "./types";
import { CharIcon } from "@/components/char-icon";

/** B (blocked recovery): blocked/failed-step recovery actions (only wired when blocked). */
export interface PlanRecoveryHandlers {
  /** Re-approve the plan → resume from the blocked level (copy = "继续执行":
   *  this continues the whole plan, not a single-step retry, review R2-F8). */
  onRetry: () => void;
  /** Skip a blocked/failed step → then resume downstream without it. */
  onSkip: (stepId: string) => void;
  /** Supply corpus (upload to workspace KB) so a needs_corpus step can run on retry. */
  onSupply: () => void;
  /** Batch 3 R3.2: revise and retry; LLM rewrites this step (preview→confirm→auto re-approve). */
  onRevise?: (stepId: string) => void;
  busy?: boolean;
}

/** Batch 3: revise preview (component-local state, outside plan-store; no orphan preview path, R2-F6). */
export interface PlanReviseView {
  stepId: string;
  agent: string;
  objective: string;
  deliverable: string;
  criteria: string[];
  warning?: string;
  onConfirm: () => void;
  onDiscard: () => void;
  busy?: boolean;
}

/** Batch 3: full replan entry + preview (also local state). */
export interface PlanReplanView {
  onStart: (instruction: string) => void;
  busy?: boolean;
  preview?: {
    newSteps: Array<{ id: string; agent: string; objective: string; criteria: string[] }>;
    keptDone: number;
    superseded: number;
    warning?: string;
    onConfirm: () => void;
    onDiscard: () => void;
  };
}

/** A (editable): per-step structured edit callbacks (only wired when proposed). */
export interface PlanEditHandlers {
  onEditObjective: (stepId: string, objective: string) => void;
  onToggleInclude: (stepId: string) => void;
  onDeleteStep: (stepId: string) => void;
  onReorder: (draggedId: string, targetId: string) => void;
}

export interface PlanCardProps {
  plan: PlanView;
  /** Plan step id → the Team's **configured** subagent display name.
   *  Source: turn-store `peerDisplayNames["sub_" + planStepId]`, using exactly
   *  provenance's join key (`agent_runner_adapter` derives subagent_id from the ps_ prefix).
   *  Without a configured name, return undefined; `planAgentLabel` falls back to agent-meta's canonical name. */
  displayNameFor?: (planStepId: string) => string | undefined;
  /** Approve the plan → start execution. */
  onApprove?: () => void;
  /** Dismiss the proposed plan (client-side; no backend cancel in M1). */
  onCancel?: () => void;
  /** Open the report in the canvas pane (completed). */
  onViewReport?: () => void;
  /** True while an approve request is in flight (disables the button). */
  approving?: boolean;
  // ── A (editable): present only when editing is enabled (proposed + flag) ──
  /** Structured edit handlers; when present, proposed steps render editable. */
  edit?: PlanEditHandlers;
  /** Natural-language amend (re-reify). Return false to keep the draft text. */
  onAmend?: (instruction: string) => boolean | Promise<boolean | void> | void;
  /** True while an amend request is in flight. */
  amendBusy?: boolean;
  /** True when an amend preview is currently shown (confirm/discard bar). */
  amendPreview?: boolean;
  onConfirmAmend?: () => void;
  onDiscardAmend?: () => void;
  // ── B (blocked recovery): present only when the plan is blocked ──
  recovery?: PlanRecoveryHandlers;
  // ── Batch 3: revise/replan (blocked variant only) ──
  revise?: PlanReviseView | null;
  replan?: PlanReplanView | null;
}

type T = ReturnType<typeof useTranslations>;

/* ── role icon chip ── */
function RoleChip({ agent }: { agent: string }) {
  const role = roleForAgent(agent);
  return (
    <span className="pstep-ic" style={{ background: role.tintVar, color: role.colorVar }}>
      <PlanIcon name={role.icon} size={17} />
    </span>
  );
}

/* ── status pill (running / blocked / completed) ── */
/** graph-mode batch 1 acceptance criteria; batch 5 applies the design visuals: mono items (`.pacc`),
 *  no longer squeezing multiple criteria into one prose line. Approval covers goal + acceptance, requiring individually readable criteria. */
function CriteriaList({ step, t }: { step: PlanStepView; t: T }) {
  const items = step.successCriteria ?? [];
  if (!items.length) return null;
  return (
    <div className="pacc" data-testid="step-criteria">
      <div className="pacc-label">{t("criteria_label")}</div>
      {items.map((c, i) => (
        <div key={i} className="pacc-r">
          <span className="mk" aria-hidden="true">
            ·
          </span>
          <span>{c}</span>
        </div>
      ))}
    </div>
  );
}

/** graph-mode batch 1: verification badge. skipped and pass must differ visually (coverage signal: broken verifier
 *  configuration must not look entirely green, review A8); fail uses the existing status=failed display
 *  (status overrides verdict). */
function VerifyBadge({ step, t }: { step: PlanStepView; t: T }) {
  const v = step.verify;
  if (!v || step.status !== "done") return null;
  // Keep fail separate from partial and skipped visually distinct from pass: coverage signal (broken verifier
  // configuration must not look entirely green, batch 1 review A8/F-5). Batch 5: share .vpill with graph details.
  const map = {
    pass: { cls: "ok", glyph: "✓", key: "verify_pass", testid: "verify-pass" },
    partial: { cls: "part", glyph: "◐", key: "verify_partial", testid: "verify-partial" },
    fail: { cls: "unmet", glyph: "✕", key: "verify_fail", testid: "verify-fail" },
    skipped: { cls: "none", glyph: "○", key: "verify_skipped", testid: "verify-skipped" },
    // ★2026-08-16 before enforce: honest out-of-domain refusal must not look "未达标".
    out_of_domain: { cls: "none", glyph: "⃠", key: "verify_out_of_domain", testid: "verify-out-of-domain" },
  } as const;
  const m = map[v.verdict] ?? map.skipped;
  // Include **evidence** for unmet criteria: criterion — what's missing. The backend always generated this text
  // (plan_verify prompt requires note for unmet), but the API only exposed it starting 2026-08-15;
  // previously only original criteria appeared: users saw three identical criteria + ✕ with no way to tell which failed.
  // If `items` is absent (old events/responses), fall back to listing original text only.
  const gapLines = (v.items ?? [])
    .filter((i) => i.status === "unmet" || i.status === "cannot_tell")
    .map((i) => (i.note ? `${i.text} —— ${i.note}` : i.text));
  const title = gapLines.length
    ? gapLines.join("；")
    : v.unmet.length
      ? v.unmet.join("；")
      : v.note || t(m.key);
  return (
    <span className={`vpill ${m.cls}`} data-testid={m.testid} title={title}>
      <CharIcon ch={m.glyph} />
      {t(m.key)}
    </span>
  );
}

function StatusPill({ status, t }: { status: PlanStepStatus; t: T }) {
  switch (status) {
    case "running":
      return (
        <span className="pp run">
          <span className="spin" />
          {t("status_running")}
        </span>
      );
    case "done":
      return (
        <span className="pp done">
          <PlanIcon name="check" size={12} />
          {t("status_done")}
        </span>
      );
    case "failed":
      return (
        <span className="pp failed">
          <PlanIcon name="x" size={12} />
          {t("status_failed")}
        </span>
      );
    case "blocked":
      return (
        <span className="pp blocked">
          <PlanIcon name="alertTriangle" size={12} />
          {t("status_blocked")}
        </span>
      );
    case "skipped":
      return (
        <span className="pp skip">
          <PlanIcon name="slash" size={12} />
          {t("status_skipped")}
        </span>
      );
    default:
      return (
        <span className="pp todo">
          <PlanIcon name="clock" size={12} />
          {t("status_pending")}
        </span>
      );
  }
}

/* ── meta badges ── */
function DepBadge({ step, isFinal, t }: { step: PlanStepView; isFinal: boolean; t: T }) {
  if (isFinal) {
    return (
      <span className="pbadge dep">
        <PlanIcon name="cornerDownRight" size={11} />
        {t("dep_all")}
      </span>
    );
  }
  if (step.dependsOn.length > 0) {
    return (
      <span className="pbadge dep">
        <PlanIcon name="cornerDownRight" size={11} />
        {t("dep_n", { count: step.dependsOn.length })}
      </span>
    );
  }
  return null;
}

function CorpusBadge({ t }: { t: T }) {
  // M1: neutral "needs corpus" — the backend only sends needs_corpus at
  // proposal time, not whether the data room is actually populated. The
  // ready/missing split surfaces at execution (step_blocked needs_corpus).
  return (
    <span className="pbadge">
      <PlanIcon name="paperclip" size={11} />
      {t("corpus_needed")}
    </span>
  );
}

function FeasibilityBadge({ step, t }: { step: PlanStepView; t: T }) {
  // planning-mode-audit WP-7 (P3-b): deterministic feasibility signals during proposal, before users approve:
  // show that a step awaits Datarc workspace selection / lacks corpus, rather than approve a graph destined to block.
  if (step.needsWeb) {
    return (
      <span className="pbadge">
        <PlanIcon name="fileText" size={11} />
        {t("feas_web")}
      </span>
    );
  }
  if (step.feasibility === "needs_context") {
    return (
      <span className="pbadge warn">
        <PlanIcon name="alertTriangle" size={11} />
        {t("feas_needs_context")}
      </span>
    );
  }
  if (step.feasibility === "no_corpus") {
    return (
      <span className="pbadge warn">
        <PlanIcon name="alertTriangle" size={11} />
        {t("feas_no_corpus")}
      </span>
    );
  }
  return null;
}

function sortPlanSteps(a: PlanStepView, b: PlanStepView): number {
  return a.level - b.level || (a.seq ?? 0) - (b.seq ?? 0) || a.id.localeCompare(b.id);
}

/* chat-ux-2026-08 plan integration: **entirely remove** `RunningDetail` (.prun-detail).
 *
 * It showed which step is running: role + status + goal + deliverable. Before batch 5, it was the card's
 * sole surface for this; after batch 5 added a dependency graph (running nodes highlighted) and list (Running rows),
 * the card repeated the same information three times.
 *
 * Real-browser evidence (/spike/plan-integration, **minimum** 4 plan steps + 4 tool steps):
 * five simultaneous progress surfaces, "Financial audit" 4 times, "Running" 5 times, 1202px for the turn.
 * This occupied 108px, the sole surface **without any execution details**: it occupied the what-is-running slot
 * but said nothing; actual execution details were in disclosure outside the card.
 *
 * Removal loses no information: graph + list carry status, external disclosure carries execution detail (this batch also
 * unified agent names on both sides; see roles.ts `planAgentLabel`).
 */

/* ── inline goal editor (click goal → textarea → ⏎ save / esc cancel) ── */
function EditableGoal({
  step,
  onSave,
  t,
}: {
  step: PlanStepView;
  onSave: (stepId: string, objective: string) => void;
  t: T;
}) {
  const [editing, setEditing] = useState(false);
  const [buf, setBuf] = useState(step.objective);
  if (!editing) {
    return (
      <div
        className="pstep-goal editable"
        role="button"
        tabIndex={0}
        title={t("edit_goal_hint")}
        onClick={() => {
          setBuf(step.objective);
          setEditing(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            setBuf(step.objective);
            setEditing(true);
          }
        }}
      >
        {step.objective}
      </div>
    );
  }
  const commit = () => {
    const v = buf.trim();
    if (v && v !== step.objective) onSave(step.id, v);
    setEditing(false);
  };
  return (
    <textarea
      className="pgoal-edit"
      value={buf}
      autoFocus
      aria-label={t("edit_goal_hint")}
      onChange={(e) => setBuf(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          commit();
        } else if (e.key === "Escape") {
          setEditing(false);
        }
      }}
    />
  );
}

export interface StepDragProps {
  draggable: boolean;
  isOver: boolean;
  isDragging: boolean;
  onDragStart: () => void;
  onDragOver: () => void;
  onDrop: () => void;
  onDragEnd: () => void;
}

/* ── one step row ── */
function StepRow({
  step,
  isFinal,
  showStatus,
  showCorpus,
  t,
  edit,
  drag,
  highlighted,
  displayNameFor,
}: {
  step: PlanStepView;
  isFinal: boolean;
  showStatus: boolean;
  showCorpus: boolean;
  t: T;
  edit?: PlanEditHandlers;
  drag?: StepDragProps;
  /** Batch 5: selecting a graph node highlights its corresponding list row. */
  highlighted?: boolean;
  displayNameFor?: (planStepId: string) => string | undefined;
}) {
  const role = roleForAgent(step.agent);
  const label = planAgentLabel(step.agent, displayNameFor?.(step.id));
  const editable = !!edit;
  const cls = ["pstep"];
  if (isFinal) cls.push("is-final");
  if (editable && !step.included) cls.push("off");
  if (step.status === "running") cls.push("running-now");
  if (step.status === "blocked") cls.push("blocked-now");
  if (drag?.isOver) cls.push("dragover");
  if (drag?.isDragging) cls.push("dragging"); // drag-source lift (design .pstep.dragging)
  if (highlighted) cls.push("is-hl"); // Batch 5: graph → list coordination.

  const dep = <DepBadge step={step} isFinal={isFinal} t={t} />;
  const corpus = showCorpus && step.needsCorpus ? <CorpusBadge t={t} /> : null;
  const feas = <FeasibilityBadge step={step} t={t} />;

  return (
    <div
      className={cls.join(" ")}
      data-row={step.id}
      draggable={drag?.draggable || undefined}
      onDragStart={drag ? () => drag.onDragStart() : undefined}
      onDragOver={drag ? (e) => { e.preventDefault(); drag.onDragOver(); } : undefined}
      onDrop={drag ? (e) => { e.preventDefault(); drag.onDrop(); } : undefined}
      onDragEnd={drag ? () => drag.onDragEnd() : undefined}
    >
      {editable && !isFinal ? (
        <button
          type="button"
          className={"pstep-chk" + (step.included ? " on" : "")}
          aria-label={t("include_step")}
          aria-pressed={step.included}
          onClick={() => edit.onToggleInclude(step.id)}
        >
          <PlanIcon name="check" size={12} />
        </button>
      ) : null}
      <RoleChip agent={step.agent} />
      <div className="pstep-main">
        <div className="pstep-head">
          <span className="pstep-name">{label}</span>
          {/* Batch 3: badge on successors from revision/replan (revive the ghost-tag from types.ts comments). */}
          {step.origin === "revision" || step.origin === "replan" ? (
            <span className="ghost-tag">{t("revised_tag")}</span>
          ) : null}
          {step.deliverable ? (
            <span className="pstep-deliv">
              <PlanIcon name="fileText" size={11} />
              {step.deliverable}
            </span>
          ) : null}
          {showStatus ? (
            <>
              <span className="legacy-pc-grow" />
              <StatusPill status={step.status} t={t} />
            </>
          ) : null}
        </div>
        {editable ? (
          <EditableGoal step={step} onSave={edit.onEditObjective} t={t} />
        ) : (
          <div className="pstep-goal">{step.objective}</div>
        )}
        <CriteriaList step={step} t={t} />
        <VerifyBadge step={step} t={t} />
        {dep || corpus || feas ? (
          <div className="pstep-metarow">
            {dep}
            {corpus}
            {feas}
          </div>
        ) : null}
      </div>
      {/* Feishu 07.06 #6: remove Delete, keeping only uncheck (pstep-chk): deletion is irreversible and redundant,
          whereas uncheck (included=false) is safe and reversible. Retain the drag handle. */}
      {editable && !isFinal && drag?.draggable ? (
        <div className="pstep-tail">
          <button type="button" className="pstep-ctl grab" aria-label={t("drag_reorder")}>
            <PlanIcon name="grip" size={15} />
          </button>
        </div>
      ) : null}
    </div>
  );
}

/* ── NL modify input (re-reify via amend) ── */
function NlInput({
  onAmend,
  busy,
  t,
  placeholderKey = "nl_placeholder",
}: {
  onAmend: (instruction: string) => boolean | Promise<boolean | void> | void;
  busy: boolean;
  t: T;
  // 116-dogfood: replan reuses this component, but reusing amend's placeholder left **nothing indicating
  // replan mode**: the input after Replan looked identical to ordinary "编辑计划",
  // and I mistook it even with source code in hand. Let the caller specify copy.
  placeholderKey?: "nl_placeholder" | "replan_nl_placeholder";
}) {
  const [text, setText] = useState("");
  const submit = () => {
    const v = text.trim();
    if (v && !busy) {
      const result = onAmend(v);
      if (result && typeof (result as Promise<boolean | void>).then === "function") {
        void (result as Promise<boolean | void>).then((ok) => {
          if (ok !== false) {
            setText((current) => (current.trim() === v ? "" : current));
          }
        }).catch(() => {
          // Keep the user's instruction in place when amend delivery fails.
        });
      } else if (result !== false) {
        setText("");
      }
    }
  };
  return (
    <div className={"pnl" + (text ? " focus" : "")}>
      <span className={"pen" + (text ? " on" : "")}>
        <PlanIcon name="pencil" size={15} />
      </span>
      <input
        className="pnl-tx"
        value={text}
        placeholder={t(placeholderKey)}
        aria-label={t(placeholderKey)}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button
        type="button"
        className={"pnl-send" + (text && !busy ? " on" : "")}
        aria-label={t("nl_send")}
        disabled={busy || !text.trim()}
        onClick={submit}
      >
        {/* ④b (bug 0708): busy = amend REST in flight (→ new plan). Swap send glyph
            for a spinner so the send has visible feedback (was silently disabled). */}
        {busy ? <span className="spin" /> : <PlanIcon name="send" size={14} />}
      </button>
    </div>
  );
}

/* Internal error_kind → plain language. ★2026-08-14 116 dogfood: this row previously inserted raw kind into
   "已受阻 · {kind}", showing users `已受阻 · transient` / `upstream_failed`.
   The adjacent comment (users do not know what needs_user_input means) already identified the problem,
   but only special-cased needs_user_input, leaving everything else exposed.
   ⚠ Unknown kinds **still fall back to their raw string**: backend exception names (TimeoutError, etc.) form an open set;
   exposing an English string is better than inventing Chinese copy that may contradict reality. */
const _KIND_LABEL_KEYS: Record<string, string> = {
  transient: "blocked_kind_transient",
  tool_failure: "blocked_kind_tool_failure",
  domain_mismatch: "blocked_kind_domain_mismatch",
  ambiguity: "blocked_kind_ambiguity",
  schema_invalid: "blocked_kind_schema_invalid",
  peer_bail: "blocked_kind_peer_bail",
  empty_output: "blocked_kind_empty_output",
  upstream_failed: "blocked_kind_upstream_failed",
  skill_unavailable: "blocked_kind_skill_unavailable",
  Timeout: "blocked_kind_timeout",
  TimeoutError: "blocked_kind_timeout",
  Cancelled: "blocked_kind_cancelled",
  CancelledError: "blocked_kind_cancelled",
};

export function blockedKindLabel(kind: string, t: T): string {
  const key = _KIND_LABEL_KEYS[kind];
  return key ? t(key) : kind;
}

/* ── blocked/failed detail (under a blocked|failed step) + B recovery actions ── */
function BlockedDetail({
  step,
  t,
  recovery,
  revise,
}: {
  step: PlanStepView;
  t: T;
  recovery?: PlanRecoveryHandlers;
  revise?: PlanReviseView | null;
}) {
  const failed = step.status === "failed";
  const criteriaUnmet = failed && step.errorKind === "criteria_unmet";
  // Batch 3 (review R2-F8): order failed-step CTAs by cause; criteria_unmet/execution failure →
  // revise and retry is primary; "补充材料" only makes sense for blocked needs_corpus steps.
  const showSupply = !failed && step.needsCorpus;
  const revisePrimary = failed && !!recovery?.onRevise;
  const showRevisePanel = revise && revise.stepId === step.id;
  return (
    <div className={"pblock" + (failed ? " is-failed" : "")}>
      <div className="pblock-r">
        <span className="bi">
          <PlanIcon name="alertTriangle" size={17} />
        </span>
        <div className="pblock-tx">
          {/* R1.3a: unmet acceptance must show acceptance unmet + unmet items, not look like a system error:
              it did not crash; output failed criteria. Users therefore need different next actions (change criteria /
              revise and retry, rather than rerun). */}
          <b>{failed ? t(criteriaUnmet ? "failed_criteria_prefix" : "failed_prefix") : t("blocked_prefix")}</b>
          {/* planning-mode-audit WP-2: Datarc workspace preflight blocked → point to the picker card above,
              rather than a raw kind string (users do not know what needs_user_input means). */}
          {step.errorKind === "needs_user_input" ? t("blocked_needs_context") : (step.reason || "")}
          {step.errorKind && step.errorKind !== "needs_user_input" && !criteriaUnmet ? (
            <span className="why">{t("blocked_why", { kind: blockedKindLabel(step.errorKind, t) })}</span>
          ) : null}
          {step.verify && step.verify.unmet.length ? (
            <span className="why">{t("revise_unmet", { items: step.verify.unmet.join("、") })}</span>
          ) : null}
        </div>
      </div>
      {recovery && !showRevisePanel ? (
        <>
          <div className="pblock-acts">
            {showSupply ? (
              <button type="button" className="bact primary" disabled={recovery.busy} onClick={recovery.onSupply}>
                <PlanIcon name="upload" size={13} />
                {t("recover_supply")}
              </button>
            ) : null}
            {recovery.onRevise ? (
              <button
                type="button"
                className={`bact${revisePrimary ? " primary" : ""}`}
                disabled={recovery.busy}
                onClick={() => recovery.onRevise?.(step.id)}
              >
                <PlanIcon name="pencil" size={13} />
                {t("recover_revise")}
              </button>
            ) : null}
            <button type="button" className="bact" disabled={recovery.busy} onClick={recovery.onRetry}>
              <PlanIcon name="rotateCw" size={13} />
              {t("recover_continue")}
            </button>
            <button type="button" className="bact warn" disabled={recovery.busy} onClick={() => recovery.onSkip(step.id)}>
              <PlanIcon name="skipForward" size={13} />
              {t("recover_skip")}
            </button>
          </div>
          <div className="pblock-scope">{t("blocked_scope")}</div>
        </>
      ) : null}
      {showRevisePanel && revise ? (
        <div className="previse">
          <div className="prv-title">
            <b>{t("revise_preview_title")}</b>
          </div>
          <div className="prv-row">
            <RoleChip agent={revise.agent} />
            <span className="prv-obj">{revise.objective}</span>
          </div>
          {revise.criteria.length ? (
            <ul className="prv-crit">
              {revise.criteria.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          ) : null}
          {revise.warning ? <div className="prv-warn">{revise.warning}</div> : null}
          <div className="pblock-acts">
            <button type="button" className="bact primary" disabled={revise.busy} onClick={revise.onConfirm}>
              {t("revise_confirm")}
            </button>
            <button type="button" className="bact" disabled={revise.busy} onClick={revise.onDiscard}>
              {t("revise_discard")}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/* ── progress meter (running / blocked) ── */
function progressClass(status: PlanStepStatus): string {
  if (status === "done") return "done";
  if (status === "running") return "run";
  if (status === "blocked") return "block";
  // Batch 3 (review R2-F8): failed no longer uses not-started color (default todo).
  if (status === "failed") return "fail";
  if (status === "skipped") return "skip";
  return "todo";
}

export function PlanCard({
  plan,
  onApprove,
  onCancel,
  onViewReport,
  approving,
  edit,
  onAmend,
  amendBusy,
  amendPreview,
  onConfirmAmend,
  onDiscardAmend,
  recovery,
  revise,
  replan,
  displayNameFor,
}: PlanCardProps) {
  const t = useTranslations("plan_card");
  const variant = variantForStatus(plan.status);
  const editable = variant === "proposed";
  const editing = editable && !!edit; // edit affordances on (proposed + handlers)
  const showStatus = variant !== "proposed";
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
  const [replanInput, setReplanInput] = useState(false);
  // Batch 5: graph ↔ list coordination; selecting a graph node highlights the corresponding row (graph locates, without actions).
  const [hlStepId, setHlStepId] = useState<string | null>(null);

  // Batch 3: filter superseded dead rows from rendering (revision history is outside the progress denominator; store retains lineage).
  const visibleSteps = plan.steps.filter((s) => s.status !== "superseded");

  // Split the DAG: the highest topological level is the join/final step(s);
  // everything below runs in the parallel rail. Matches the design's
  // diamond (N experts → 1 synthesis) and degrades for deeper DAGs.
  const maxLevel = visibleSteps.reduce((m, s) => Math.max(m, s.level), 0);
  const hasJoin = maxLevel > 0 && visibleSteps.some((s) => s.level === maxLevel);
  const railSteps = hasJoin ? visibleSteps.filter((s) => s.level < maxLevel) : visibleSteps;
  const finalSteps = hasJoin ? visibleSteps.filter((s) => s.level === maxLevel) : [];

  const total = visibleSteps.length;
  const doneN = visibleSteps.filter((s) => s.status === "done").length;
  const skippedN = visibleSteps.filter((s) => s.status === "skipped").length;
  // Actual verification coverage: only a present, non-skipped verdict counts as verified (skipped means verifier did not run successfully,
  // distinct from the user skipping a step; the old copy conflated them).
  //
  // chat-ux-2026-08 dogfood fix: this one count previously combined pass / partial / **fail**
  // as "已核验". Observed on dev 1.0.77: all three steps done, acceptance showed "○未核验 ○未核验
  // ✕未达标", yet the heading said "1 步已核验", with a green completed card + Report ready ✓,
  // avoiding gray when verifiedN>0. A plan with all acceptance unmet would show "3 步已核验".
  // Per-step badges were deliberately distinct (see VerifyBadge comment separating fail and partial),
  // but the header merged them again: the partial fix excluded skipped but not fail.
  // Report separate segments by verdict, without merging.
  const passedN = visibleSteps.filter((s) => s.verify?.verdict === "pass").length;
  const partialN = visibleSteps.filter((s) => s.verify?.verdict === "partial").length;
  const unmetN = visibleSteps.filter((s) => s.verify?.verdict === "fail").length;
  const activeNames = visibleSteps
    .filter((s) => s.status === "running")
    .map((s) => {
      return planAgentLabel(s.agent, displayNameFor?.(s.id));
    });

  return (
    <div className={`pcard is-${variant}`} role="group" aria-label={t("aria_card")}>
      {/* ── header ── */}
      <div className="pc-head">
        <div className="pc-toprow">
          <span className="pc-eyebrow">
            <span className="gem" />
            {t(`eyebrow_${variant}`)}
          </span>
          <span className="pc-sp" />
          {variant === "proposed" ? (
            <span className="pc-modetag">
              <PlanIcon name="list" size={11} />
              <b>{t("modetag")}</b>
            </span>
          ) : variant === "completed" && plan.deliverableKind !== "answer" ? (
            // 116 dogfood 1.0.93 P2: a completed ANSWER plan (deliverable = chat answer) must not show
            // the "报告已生成" badge: this turn has no report. undefined (old events) defaults to report.
            <span className="pc-modetag legacy-pc-modetag--done">
              <PlanIcon name="check" size={11} />
              {t("report_generated")}
            </span>
          ) : null}
        </div>

        <h3 className="pc-title">{plan.title}</h3>

        {variant === "running" || variant === "blocked" ? (
          <>
            <div className="pc-progress">
              <div className="pc-bar">
                {visibleSteps.map((s) => (
                  <i key={s.id} className={progressClass(s.status)} />
                ))}
              </div>
              <span className="pc-meta legacy-pc-meta--fixed">
                <b>{t("meta_done_count", { done: doneN, total })}</b>
              </span>
            </div>
            <div className="pc-meta">
              {variant === "blocked" ? (
                <>
                  <PlanIcon name="alertTriangle" size={11} />
                  {/* Batch 3 (review R2-F8): failed also needs user action; a blocked plan with only failed
                      steps must not show "0 步受阻". */}
                  {t("meta_blocked", {
                    count: visibleSteps.filter((s) => s.status === "blocked" || s.status === "failed").length,
                  })}
                  <span className="mdot" />
                  {t("meta_paused_rest")}
                </>
              ) : (
                <>
                  {t("meta_running_active")}
                  {activeNames.length ? <b>&nbsp;{activeNames.join("、")}</b> : null}
                  <span className="mdot" />
                  {t("meta_running_hint")}
                </>
              )}
            </div>
          </>
        ) : variant === "completed" ? (
          <div className="pc-meta">
            {/* tech-design §5 before enforce③: old copy said "{done}/{total} 已核查完成",
                but doneN counts **completed** steps, unrelated to verification. A broken verifier configuration (all verdicts
                skipped) still claimed "已核查完成": a dead flag looking all green, exactly what batch 1
                review A8 guards. Report separately: completion count + actual verification coverage. */}
            <b>{t("meta_completed", { done: doneN, total })}</b>
            <span className="mdot" />
            <span className={passedN > 0 ? undefined : "legacy-pc-verify--none"}>
              {passedN > 0 ? t("meta_verified", { verified: passedN }) : t("meta_unverified")}
            </span>
            {/* Unmet/partially met criteria get **separate segments** and warning colors; never merge into the "已核验" count. */}
            {partialN > 0 ? (
              <>
                <span className="mdot" />
                <span className="legacy-pc-verify--warn">
                  {t("meta_verify_partial", { count: partialN })}
                </span>
              </>
            ) : null}
            {unmetN > 0 ? (
              <>
                <span className="mdot" />
                <span className="legacy-pc-verify--danger">
                  {t("meta_verify_unmet", { count: unmetN })}
                </span>
              </>
            ) : null}
            {skippedN > 0 ? (
              <>
                <span className="mdot" />
                <span className="legacy-pc-verify--warn">
                  {t("meta_completed_skipped", { count: skippedN })}
                </span>
              </>
            ) : null}
          </div>
        ) : (
          <div className="pc-meta">
            <span>{t("meta_steps", { count: total })}</span>
            {finalSteps.length > 0 ? (
              <>
                <span className="mdot" />
                <span>
                  {t("meta_parallel", { parallel: railSteps.length, final: finalSteps.length })}
                </span>
              </>
            ) : null}
          </div>
        )}
      </div>

      {/* ── body: dependency graph (batch 5) + parallel rail + join + final ── */}
      <div className="pc-body">
        {/* Batch 5: graph stays atop the card, list below; graph shows shape/bottlenecks, list provides all actions and
            details (no graph/list switch; graph only collapses). Single-step plans render no graph. */}
        <PlanGraph
          steps={plan.steps}
          t={t}
          editable={editing}
          // Implementation review P1-C: hide the entire list recovery strip during revise preview; disable graph actions too.
          recovery={recovery ? { ...recovery, busy: recovery.busy || !!revise } : undefined}
          selectedStepId={hlStepId}
          onSelectStep={setHlStepId}
          displayNameFor={displayNameFor}
        />
        {/* Batch 5 list addition: name graph/list regions separately (design decision: users can recognize two expressions of one
            card). Only render when the graph actually appears; single-step plans have no graph and need no regions. */}
        {shouldRenderGraph(plan.steps) ? (
          <div className="plist-hd">
            {t("graph_list_label")}
            <span className="sp" />
            <b>{t("meta_steps", { count: total })}</b>
          </div>
        ) : null}
        {railSteps.length > 0 ? (
          <div className="prail">
            <div className="prail-tab">
              <span className="pulse" />
              {t("rail_parallel", { count: railSteps.length })}
            </div>
            {railSteps.map((s) => (
              <div key={s.id}>
                <StepRow
                  step={s}
                  isFinal={false}
                  showStatus={showStatus}
                  showCorpus={editable}
                  highlighted={hlStepId === s.id}
                  t={t}
                  displayNameFor={displayNameFor}
                  edit={editing ? edit : undefined}
                  drag={
                    editing && edit
                      ? {
                          draggable: true,
                          isOver: overId === s.id && dragId !== null && dragId !== s.id,
                          isDragging: dragId === s.id,
                          onDragStart: () => setDragId(s.id),
                          onDragOver: () => setOverId(s.id),
                          onDrop: () => {
                            if (dragId && dragId !== s.id) edit.onReorder(dragId, s.id);
                            setDragId(null);
                            setOverId(null);
                          },
                          onDragEnd: () => {
                            setDragId(null);
                            setOverId(null);
                          },
                        }
                      : undefined
                  }
                />
                {s.status === "blocked" || s.status === "failed" ? (
                  <BlockedDetail step={s} t={t} recovery={recovery} revise={revise} />
                ) : null}
              </div>
            ))}
          </div>
        ) : null}

        {finalSteps.length > 0 ? (
          <>
            <div className="pjoin">
              <span className="jline">
                <PlanIcon name="cornerDownRight" size={13} />
              </span>
              {t("join_before", { count: railSteps.length })} <b>{t("join_word")}</b>{" "}
              {t("join_after")}
            </div>
            {finalSteps.map((s) => (
              <div key={s.id}>
                <StepRow
                  step={s}
                  isFinal
                  showStatus={showStatus}
                  showCorpus={editable}
                  highlighted={hlStepId === s.id}
                  t={t}
                  displayNameFor={displayNameFor}
                  edit={editing ? edit : undefined}
                />
                {s.status === "blocked" || s.status === "failed" ? (
                  <BlockedDetail step={s} t={t} recovery={recovery} revise={revise} />
                ) : null}
              </div>
            ))}
          </>
        ) : null}
      </div>

      {/* ── Batch 3: full replan entry + preview (blocked variant only, local state) ── */}
      {variant === "blocked" && replan && !replan.preview ? (
        replanInput ? (
          <NlInput
            onAmend={(instruction) => {
              replan.onStart(instruction);
              setReplanInput(false);
              return true;
            }}
            busy={!!replan.busy}
            t={t}
            placeholderKey="replan_nl_placeholder"
          />
        ) : (
          <div className="pamend">
            <span className="pa-tx">{t("replan_hint")}</span>
            <button type="button" className="pa-btn" disabled={replan.busy} onClick={() => setReplanInput(true)}>
              {replan.busy ? <span className="spin" /> : null}
              {t("replan_button")}
            </button>
          </div>
        )
      ) : null}
      {variant === "blocked" && replan?.preview ? (
        <div className="previse">
          <div className="prv-title">
            <b>{t("replan_preview_title")}</b>
            <span className="prv-diff">
              {t("replan_diff", {
                kept: replan.preview.keptDone,
                superseded: replan.preview.superseded,
                added: replan.preview.newSteps.length,
              })}
            </span>
          </div>
          {replan.preview.newSteps.map((s) => (
            <div key={s.id} className="prv-row">
              <RoleChip agent={s.agent} />
              <span className="prv-obj">{s.objective}</span>
            </div>
          ))}
          {replan.preview.warning ? <div className="prv-warn">{replan.preview.warning}</div> : null}
          <div className="pblock-acts">
            <button type="button" className="bact primary" disabled={replan.busy} onClick={replan.preview.onConfirm}>
              {t("revise_confirm")}
            </button>
            <button type="button" className="bact" disabled={replan.busy} onClick={replan.preview.onDiscard}>
              {t("revise_discard")}
            </button>
          </div>
        </div>
      ) : null}

      {/* ── A (editable): natural-language plan changes + amend preview confirmation ── */}
      {editing && onAmend && !amendPreview ? (
        <NlInput onAmend={onAmend} busy={!!amendBusy} t={t} />
      ) : null}
      {amendPreview ? (
        <div className="pamend">
          <span className="pa-tx">
            <b>{t("amend_preview_title")}</b> {t("amend_preview_hint")}
          </span>
          <button type="button" className="pa-btn" onClick={onDiscardAmend} disabled={amendBusy}>
            {t("amend_discard")}
          </button>
          <button type="button" className="pa-btn confirm" onClick={onConfirmAmend} disabled={amendBusy}>
            {t("amend_confirm")}
          </button>
        </div>
      ) : null}

      {/* ── footer (only functional actions) ── */}
      {variant === "proposed" ? (
        <div className="pfoot">
          <span className="pfoot-meta">{t("foot_meta_proposed")}</span>
          <span className="pfoot-sp" />
          <button
            type="button"
            className="pbtn ghost"
            onClick={onCancel}
            disabled={approving || amendPreview}
          >
            {t("foot_cancel")}
          </button>
          <button
            type="button"
            className="pbtn primary"
            onClick={onApprove}
            disabled={approving || amendPreview}
          >
            {approving ? t("foot_approving") : t("foot_approve")}
            {approving ? null : <PlanIcon name="arrowUpRight" size={15} />}
          </button>
        </div>
      ) : variant === "completed" ? (
        <div className="pfoot">
          <span className="pfoot-meta">{t("foot_meta_completed")}</span>
          <span className="pfoot-sp" />
          {/* 116 dogfood 1.0.93 P2: ANSWER plans have no report to view; hide the button (answer is in chat). */}
          {plan.deliverableKind !== "answer" ? (
            <button type="button" className="pbtn pos" onClick={onViewReport}>
              <PlanIcon name="arrowUpRight" size={15} />
              {t("foot_view_report")}
            </button>
          ) : null}
        </div>
      ) : variant === "running" ? (
        // Feishu 07.06 #7: remove "取消执行", duplicating composer "停止" while only this button stamped durable
        // CANCELLED. Stop now cascades cancellation (page.tsx handleCancel + backend cancel_turn),
        // becoming the sole entry. Footer retains only "执行中" status.
        <div className="pfoot">
          <span className="pfoot-meta">{t("foot_meta_running")}</span>
        </div>
      ) : null}
    </div>
  );
}
