"use client";

import { ChevronDown, ChevronUp, CircleAlert, ListTree } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCard } from "@/components/ui/card";

import { buildAgentGrouping } from "./agent-grouping";
// Duration copy is human-readable and localized (`16 步完成 · 4 分 30 秒`, en `4 min 30 s`), never `270s` or
// raw milliseconds: see `formatSummaryDuration` below.
import type { StepView } from "./types";
import type { StepOutcome } from "@/lib/turn-feedback";

export interface StepSummaryProps {
  /** Original execution records, retained for Agent grouping and duration fallback. */
  steps: StepView[];
  /** Captured peer identities, used by the original Agent grouping and count. */
  peerNames?: Record<string, string>;
  /** This turn's **wall-time** total duration (ms), authoritative backend `done.duration_ms`, arriving through
   *  turn-store `totalDurationMs`. Live and archive/F5 read the same store field, rendering
   *  the same number (CLAUDE.md's live/persisted/replayed consistency invariant).
   *
   *  ⚠ If absent, use `deriveTotalDurationText` (summed step durations), an approximation **wrong in both
   *  directions**: undercounts large thinking gaps (116 plan turn actually ~2 minutes displayed
   *  `· 15s`, read as running 15 seconds); overcounts concurrent peers by adding parallel durations.
   *  Use only when wall time is unavailable. */
  totalDurationMs?: number | null;
  /** Original execution receipt, not proof that user requirements remain unmet. */
  stepOutcome?: StepOutcome | null;
  /** The turn ended without an answer (failed or cancelled): the row reads "Not finished · 3 / 5 steps · 1 min"
   *  (spec §8). `plannedTotal` is the announced plan length; without it the "/ N" part is left out. */
  failed?: { plannedTotal?: number | null } | null;
  /** Click handler — expands back to full step list. */
  onExpand: () => void;
  /** The trace below is open. The row then stays as the control that folds it again. */
  expanded?: boolean;
  /** Folds the trace again. Required for the row to act as a collapse control while `expanded`. */
  onCollapse?: () => void;
}

/**
 * StepSummary — the folded trace of a finished turn (Chat Cards Spec §8, D6): a row ChatCard whose whole row
 * is the trace disclosure. Execution ended, participating Agents and wall-clock duration.
 * The original final answer owns delivery caveats. Agent colour badges are
 * replaced by the Agent count. State lives in the parent (TurnContainer / ArchivedTurnView).
 *
 * The row is one persistent disclosure button, like the running band's bar: expanding keeps it above the trace
 * with `aria-expanded`, so keyboard focus is not lost and the reader can fold the trace again. Its accessible name
 * is the visible row text, so the outcome ("Execution ended · 2 s") is announced with the action.
 */
export function StepSummary({ steps, peerNames, totalDurationMs, failed, onExpand, expanded = false, onCollapse }: StepSummaryProps) {
  const tStep = useTranslations("step_list");
  const open = Boolean(expanded && onCollapse);
  const toggle = open ? onCollapse! : onExpand;
  const toggleTrailing = (
    <span className="ui-card__trailing-label">
      {/* The row's accessible name is its text: keep the outcome and the action apart when it is read aloud. */}
      <span className="sr-only">, </span>
      {tStep(open ? "collapse_trace" : "expand_trace")}
      <LucideGlyph icon={open ? ChevronUp : ChevronDown} size={14} aria-hidden="true" />
    </span>
  );
  if (failed) {
    return (
      <FailedStepSummary
        steps={steps}
        plannedTotal={failed.plannedTotal}
        durText={formatDurationText(totalDurationMs, tStep) ?? deriveTotalDurationText(steps, tStep)}
        onToggle={toggle}
        open={open}
        trailing={toggleTrailing}
      />
    );
  }
  const completionLabel = tStep("execution_ended");
  const durText = formatDurationText(totalDurationMs, tStep)
    ?? deriveTotalDurationText(steps, tStep);
  const { peerBlocks } = buildAgentGrouping(steps, peerNames ?? {}, {}, {});
  const agentCount = peerBlocks.length;
  const hasPeerStep = peerBlocks.length > 0 || steps.some((s) => s.stepType === "subagent");
  const lead = agentCount > 0 ? `${completionLabel} · ${tStep("summary_agents", { count: agentCount })}` : completionLabel;

  return (
    <ChatCard
      intent="info"
      size="row"
      icon={ListTree}
      iconTone="muted"
      data-testid="step-summary"
      data-state={hasPeerStep ? "peer" : undefined}
      onActivate={toggle}
      activateExpanded={open}
      title={lead}
      meta={
        <>
          {durText ? <span data-testid="step-summary-duration">{` · ${durText}`}</span> : null}
        </>
      }
      // Action copy and arrow are one item: split, the arrow could wrap alone onto a second line.
      trailing={toggleTrailing}
    />
  );
}

/**
 * The folded trace of a turn that ended without an answer: one line, never the expanded trace with tool names
 * and repeated reasons (the failure card above already says what went wrong and what to do).
 */
function FailedStepSummary({ steps, plannedTotal, durText, onToggle, open, trailing }: {
  steps: StepView[];
  plannedTotal?: number | null;
  durText: string | undefined;
  onToggle: () => void;
  open: boolean;
  trailing: ReactNode;
}) {
  const tStep = useTranslations("step_list");
  const done = steps.filter((step) => step.state === "done").length;
  // "0 steps" says nothing: without a plan length the count appears only once a step has finished.
  const progress = typeof plannedTotal === "number" && plannedTotal >= done && plannedTotal > 0
    ? tStep("summary_steps_progress", { done, total: plannedTotal })
    : done > 0
      ? tStep("summary_steps_count", { count: done })
      : null;
  return (
    <ChatCard
      intent="failure"
      size="row"
      icon={CircleAlert}
      data-testid="step-summary"
      data-state="failed"
      onActivate={onToggle}
      activateExpanded={open}
      title={tStep.rich("summary_not_finished", { b: (chunks) => <b>{chunks}</b> })}
      meta={
        <>
          {progress ? <span data-testid="step-summary-progress">{` · ${progress}`}</span> : null}
          {durText ? <span data-testid="step-summary-duration">{` · ${durText}`}</span> : null}
        </>
      }
      trailing={trailing}
    />
  );
}

/**
 * ms → collapsed-strip duration, human-readable and localized like the spec (§8): "不到 1 秒" / "under 1 s"
 * below a second, then whole seconds ("18 秒" / "18 s") and minutes ("1 分 12 秒" / "1 min 12 s"). Success and
 * failure rows share it; never raw milliseconds.
 *
 * `<= 0` and nonnumeric return undefined; caller renders nothing instead of a zero duration.
 */
export function formatSummaryDuration(
  ms: number | null | undefined,
  t: (key: string, values?: Record<string, number>) => string,
): string | undefined {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return undefined;
  if (ms < 1000) return t("summary_duration_under_1s");
  const total = Math.round(ms / 1000);
  if (total < 60) return t("summary_duration_sec", { count: total });
  const m = Math.floor(total / 60);
  const s = total % 60;
  return s === 0 ? t("summary_duration_min", { m }) : t("summary_duration_min_sec", { m, s });
}

const formatDurationText = formatSummaryDuration;

/**
 * Fallback: sum step durations as turn duration, only when at least one step yields a parseable number.
 *
 * ⚠ Approximation **errs both ways**: omits thinking gaps between steps (undercount), sums
 * concurrent peers' parallel durations (overcount). Always prefer available wall time (`totalDurationMs`);
 * retain only to avoid no number for old archived turns whose persisted done lacks duration_ms.
 */
function deriveTotalDurationText(
  steps: StepView[],
  t: Parameters<typeof formatSummaryDuration>[1],
): string | undefined {
  let totalMs = 0;
  let anyParsed = false;
  for (const s of steps) {
    const ms = parseDurationToMs(s.durationText);
    if (ms != null) {
      totalMs += ms;
      anyParsed = true;
    }
  }
  if (!anyParsed) return undefined;
  return formatDurationText(totalMs, t);
}

function parseDurationToMs(text: string | undefined): number | null {
  if (!text) return null;
  // "X.Xs" → ms; "Xms" → ms; "Xs" → ms.
  const msMatch = /^(\d+(?:\.\d+)?)\s*ms$/i.exec(text);
  if (msMatch) return parseFloat(msMatch[1]);
  const sMatch = /^(\d+(?:\.\d+)?)\s*s$/i.exec(text);
  if (sMatch) return parseFloat(sMatch[1]) * 1000;
  return null;
}
