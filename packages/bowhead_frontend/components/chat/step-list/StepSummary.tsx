"use client";

import { ChevronDown, CircleAlert, CircleCheck, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCard } from "@/components/ui/card";

import { buildAgentGrouping } from "./agent-grouping";
// Duration copy is human-readable and localized (`16 步完成 · 4 分 30 秒`, en `4 min 30 s`), never `270s` or
// raw milliseconds: see `formatSummaryDuration` below.
import { countStepIssues } from "./self-healing";
import type { StepView } from "./types";
import { stepIssueSummary, type StepOutcome } from "@/lib/turn-feedback";

export interface StepSummaryProps {
  /** Steps that have run this turn. Length drives the N completed steps count. */
  steps: StepView[];
  /** Peer identities (agent_spawned.profile). When the turn ran ≥1 peer, the
   *  fold shows the participating agents as colour badges (agent shape) instead
   *  of a flat step count — the design's "folded state keeps the agent shape". */
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
  /** Backend `done.step_outcome` from turn-store (chat turn feedback D6). Live and archive/F5
   *  read the same store field. Absent (old turns) keeps the neutral locally counted copy. */
  stepOutcome?: StepOutcome | null;
  /** The turn ended without an answer (failed or cancelled): the row reads "Not finished · 3 / 5 steps · 1 min"
   *  (spec §8). `plannedTotal` is the announced plan length; without it the "/ N" part is left out. */
  failed?: { plannedTotal?: number | null } | null;
  /** Click handler — expands back to full step list. */
  onExpand: () => void;
}

/**
 * StepSummary — the folded trace of a finished turn (Chat Cards Spec §8, D6): a row ChatCard whose whole row
 * is the "Expand trace" button. "N steps done · used N Agents", then (secondary) whether recorded issues were
 * handled automatically or (warning) affected the answer, then the wall-clock duration. Agent colour badges are
 * replaced by the Agent count. State lives in the parent (TurnContainer / ArchivedTurnView).
 */
export function StepSummary({ steps, peerNames, totalDurationMs, stepOutcome, failed, onExpand }: StepSummaryProps) {
  const tStep = useTranslations("step_list");
  const total = steps.length;
  if (failed) {
    return (
      <FailedStepSummary
        steps={steps}
        plannedTotal={failed.plannedTotal}
        durText={formatDurationText(totalDurationMs, tStep) ?? deriveTotalDurationText(steps, tStep)}
        onExpand={onExpand}
      />
    );
  }
  const completionLabel = total === 0
    ? tStep("execution_done")
    : tStep("steps_done", { count: total });
  const durText = formatDurationText(totalDurationMs, tStep)
    ?? deriveTotalDurationText(steps, tStep);
  const { peerBlocks } = buildAgentGrouping(steps, peerNames ?? {}, {}, {});
  const agentCount = peerBlocks.length;
  const hasPeerStep = peerBlocks.length > 0 || steps.some((s) => s.stepType === "subagent");
  // Recorded step issues remain discoverable after completion. Only the backend's structural
  // `step_outcome` may claim they were handled or that they affected the answer (D6); without it
  // the locally counted issues are shown neutrally.
  const issues = stepIssueSummary(stepOutcome, countStepIssues(steps));
  const issueText = issues ? tStep(issues.key, { count: issues.count }) : "";
  const warning = issues?.tone === "warning";
  const lead = agentCount > 0 ? `${completionLabel} · ${tStep("summary_agents", { count: agentCount })}` : completionLabel;

  return (
    <ChatCard
      intent="info"
      size="row"
      icon={warning ? TriangleAlert : CircleCheck}
      iconTone={warning ? "warning" : "muted"}
      data-testid="step-summary"
      data-state={hasPeerStep ? "peer" : undefined}
      onActivate={onExpand}
      activateLabel={[
        total <= 1 ? tStep("expand_trace") : tStep("expand_trace_steps", { count: total }),
        issueText,
      ].filter(Boolean).join(" · ")}
      title={
        <>
          {lead}
          {warning ? (
            <>
              {" · "}
              <span className="ui-card__line-warn" data-testid="step-summary-issues" data-tone="warning">{issueText}</span>
            </>
          ) : null}
        </>
      }
      meta={
        <>
          {issues && !warning ? (
            <>
              {" · "}
              <span data-testid="step-summary-issues" data-tone={issues.tone}>{issueText}</span>
            </>
          ) : null}
          {durText ? <span data-testid="step-summary-duration">{` · ${durText}`}</span> : null}
        </>
      }
      // Action copy and arrow are one item: split, the arrow could wrap alone onto a second line.
      trailing={
        <span className="ui-card__trailing-label">
          {tStep("expand_trace")}
          <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" />
        </span>
      }
    />
  );
}

/**
 * The folded trace of a turn that ended without an answer: one line, never the expanded trace with tool names
 * and repeated reasons (the failure card above already says what went wrong and what to do).
 */
function FailedStepSummary({ steps, plannedTotal, durText, onExpand }: {
  steps: StepView[];
  plannedTotal?: number | null;
  durText: string | undefined;
  onExpand: () => void;
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
      onActivate={onExpand}
      activateLabel={steps.length <= 1 ? tStep("expand_trace") : tStep("expand_trace_steps", { count: steps.length })}
      title={tStep.rich("summary_not_finished", { b: (chunks) => <b>{chunks}</b> })}
      meta={
        <>
          {progress ? <span data-testid="step-summary-progress">{` · ${progress}`}</span> : null}
          {durText ? <span data-testid="step-summary-duration">{` · ${durText}`}</span> : null}
        </>
      }
      trailing={
        <span className="ui-card__trailing-label">
          {tStep("expand_trace")}
          <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" />
        </span>
      }
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
