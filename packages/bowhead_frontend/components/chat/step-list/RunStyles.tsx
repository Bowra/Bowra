"use client";

/**
 * The running view in the reader's chosen style (chat running state R3.1, R3.3, R3.4).
 *
 * 列表 is the slice-1 card (`RunGroups`). 胶囊, 泳道 and 时间轴 draw the same groups differently; everything around
 * them (the heartbeat line, Lead's text being written, cards, confirmations, the failure banner) is shared and
 * unchanged. Each group is a list labelled by its header, and every chip, dot and segment carries its step's sentence,
 * so a screen reader hears the same words the list shows. The heartbeat line stays the single live announcement.
 */
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";

import { AgentBadge } from "./AgentBadge";
import { agentDisplayName } from "./agent-meta";
import type { RunGroup } from "./run-groups";
import { RunGroups, returnedLine, type BandT, type RunGroupsProps, STEP_TIME_AFTER_MS } from "./RunGroups";
import {
  chipsOf,
  longestSegment,
  marksOf,
  segmentsOf,
  timelineSpan,
  windowMarks,
  type RunMark,
  type RunMarkWords,
} from "./run-style-model";
import type { StepView } from "./types";
import type { RunningViewStyle } from "@/lib/running-view-style";

export interface RunStyleViewProps extends RunGroupsProps {
  style: RunningViewStyle;
  /** The Turn is still running: the timeline runs to now and shows its now marker. */
  running: boolean;
  /** Server-clock now and Turn start, for the timeline; null when unknown. */
  nowMs: number | null;
  turnStartMs: number | null;
  /** The expanded overview above the trace: wider windows, the longest segment named, marks open their step. */
  expanded?: boolean;
  onSelectStep?: (step: StepView) => void;
}

/** How many marks a group shows before counting the rest, collapsed and expanded. */
const LIMITS = { chips: [12, 60], lanes: [48, 160] } as const;

function useWords(t: BandT): RunMarkWords {
  return {
    note: t("style_mark_note"),
    draft: t("note_draft"),
    failedTimes: (count) => t("step_failed_n", { count }),
  };
}

function GroupHead({ group, t, formatTime, compactCount }: { group: RunGroup; t: BandT; formatTime: (ms: number) => string; compactCount: boolean }) {
  const name = agentDisplayName(group.kind === "lead" ? "lead" : group.agent, group.displayName);
  const title = [name, group.task ?? (group.repeat ? t("group_repeat", { n: group.repeat }) : "")].filter(Boolean).join(" · ");
  const returned = group.kind === "helper" && group.status !== "run" ? returnedLine(group, t, formatTime) : null;
  return (
    <>
      <div className="bh-style__head">
        <AgentBadge agent={group.kind === "lead" ? "lead" : group.agent} size={16} />
        <span className="bh-style__name">{title}</span>
        {returned ? (
          <span className={`bh-style__meta is-${returned.tone}`}>{returned.text}</span>
        ) : compactCount && group.stepCount > 0 ? (
          <span className="bh-style__meta">{t("group_steps_done", { count: group.stepCount })}</span>
        ) : null}
      </div>
      {/* A returned helper's caveat reads as in the list ("有提醒：…"). */}
      {returned && group.caveats.length > 0 ? (
        <p className="bh-style__caveat" data-row-kind="caveat">{t("group_caveat", { text: group.caveats[0] })}</p>
      ) : null}
    </>
  );
}

function headText(group: RunGroup, t: BandT): string {
  const name = agentDisplayName(group.kind === "lead" ? "lead" : group.agent, group.displayName);
  return [name, group.task ?? (group.repeat ? t("group_repeat", { n: group.repeat }) : "")].filter(Boolean).join(" · ");
}

/** A mark is a button only where it opens its step (the expanded overview); in the card it is a list item. */
function Mark({
  mark,
  className,
  children,
  onSelectStep,
}: {
  mark: RunMark;
  className: string;
  children?: ReactNode;
  onSelectStep?: (step: StepView) => void;
}) {
  const step = mark.step;
  if (onSelectStep && step) {
    return (
      <li className="bh-style__item">
        <button type="button" className={className} title={mark.sentence} aria-label={mark.sentence} onClick={() => onSelectStep(step)} data-step-id={step.stepId}>
          {children}
        </button>
      </li>
    );
  }
  return (
    <li className="bh-style__item">
      <span className={className} title={mark.sentence} aria-label={mark.sentence} role="img" data-step-id={step?.stepId}>
        {children}
      </span>
    </li>
  );
}

function stepTime(props: RunStyleViewProps, t: BandT, step: StepView | null): string {
  if (!step) return "";
  const ms = props.stepElapsedMs(step);
  return ms !== null && ms > STEP_TIME_AFTER_MS ? t("step_elapsed", { time: props.formatTime(ms) }) : "";
}

function Prose({ props }: { props: RunStyleViewProps }) {
  const prose = props.owner?.text ? props.owner : null;
  if (!prose) return null;
  return (
    <div className="bh-band__foot" data-row-kind="prose">
      <span className={prose.isCode ? "bh-band__foot-prose bh-band__foot-prose--code" : "bh-band__foot-prose"} data-testid="running-band-prose">
        {prose.text}
      </span>
    </div>
  );
}

function Chips({ props, t }: { props: RunStyleViewProps; t: BandT }) {
  const words = useWords(t);
  const compact = props.activeHelpers > 2;
  return (
    <div className="bh-style bh-style--chips" data-run-style="chips">
      {props.groups.map((group) => {
        const all = chipsOf(group, words);
        const limit = props.expanded ? LIMITS.chips[1] : compact && group.kind === "helper" ? 3 : LIMITS.chips[0];
        const { marks, hiddenBefore } = windowMarks(all, limit);
        return (
          <div key={group.key} className="bh-style__group" data-group-kind={group.kind} data-status={group.status}>
            <GroupHead group={group} t={t} formatTime={props.formatTime} compactCount={compact} />
            <ul className="bh-chips" aria-label={headText(group, t)}>
              {hiddenBefore > 0 ? <li className="bh-style__item"><span className="bh-chip is-more">{t("style_more_before", { count: hiddenBefore })}</span></li> : null}
              {marks.map((mark) => {
                const fact = mark.tone === "active" || mark.tone === "needs"
                  ? stepTime(props, t, mark.step)
                  : mark.count > 1
                    ? `× ${mark.count}`
                    : mark.failures > 0 || mark.tone === "fail" || mark.tone === "warn"
                      ? mark.meta
                      : "";
                return (
                  <Mark key={mark.key} mark={mark} className={`bh-chip is-${mark.tone}`} onSelectStep={props.onSelectStep}>
                    {mark.tone === "active" ? <span className="bh-chip__pulse" aria-hidden /> : null}
                    <span className="bh-chip__label">{mark.label}</span>
                    {fact ? <span className="bh-chip__fact">{fact}</span> : null}
                  </Mark>
                );
              })}
            </ul>
            {props.owner && ((group.kind === "lead" && props.owner.id === "lead") || group.peerId === props.owner.id) && group.status === "run" ? (
              <Prose props={props} />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function nowSentence(groups: RunGroup[], t: BandT, words: RunMarkWords): { text: string; step: StepView } | null {
  for (let index = groups.length - 1; index >= 0; index -= 1) {
    const group = groups[index];
    if (group.status !== "run" || !group.active) continue;
    const mark = marksOf(group, words).find((item) => item.key === group.active?.key);
    if (!mark) continue;
    return { text: `${agentDisplayName(group.kind === "lead" ? "lead" : group.agent, group.displayName)} · ${t("band_doing", { what: mark.label })}`, step: group.active.step };
  }
  return null;
}

function Lanes({ props, t }: { props: RunStyleViewProps; t: BandT }) {
  const words = useWords(t);
  const now = props.running ? nowSentence(props.groups, t, words) : null;
  return (
    <div className="bh-style bh-style--lanes" data-run-style="lanes">
      {props.groups.map((group) => {
        const { marks, hiddenBefore } = windowMarks(marksOf(group, words), props.expanded ? LIMITS.lanes[1] : LIMITS.lanes[0]);
        return (
          <div key={group.key} className="bh-style__group bh-lane" data-group-kind={group.kind} data-status={group.status}>
            <GroupHead group={group} t={t} formatTime={props.formatTime} compactCount={false} />
            <ul className="bh-dots" aria-label={headText(group, t)}>
              {hiddenBefore > 0 ? <li className="bh-style__item"><span className="bh-dots__more">{t("style_more_before", { count: hiddenBefore })}</span></li> : null}
              {marks.map((mark) => (
                <Mark key={mark.key} mark={mark} className={`bh-dot is-${mark.tone}`} onSelectStep={props.onSelectStep} />
              ))}
            </ul>
          </div>
        );
      })}
      {now ? (
        <div className="bh-style__now" data-row-kind="now">
          <span className="bh-style__now-text">{now.text}</span>
          <span className="bh-style__now-time">{stepTime(props, t, now.step)}</span>
        </div>
      ) : null}
      <Prose props={props} />
    </div>
  );
}

function Timeline({ props, t }: { props: RunStyleViewProps; t: BandT }) {
  const words = useWords(t);
  const nowMs = props.nowMs ?? Date.now();
  const { startMs, spanMs } = timelineSpan(props.groups, nowMs, props.running, props.turnStartMs);
  const tracks = props.groups.map((group) => ({ group, segments: segmentsOf(group, words, startMs, spanMs, nowMs) }));
  const longest = props.expanded && !props.running ? longestSegment(tracks.flatMap((track) => track.segments)) : null;
  const now = props.running ? nowSentence(props.groups, t, words) : null;
  return (
    <div className="bh-style bh-style--timeline" data-run-style="timeline">
      {tracks.map(({ group, segments }) => (
        <div key={group.key} className="bh-track" data-group-kind={group.kind} data-status={group.status}>
          <span className="bh-track__name">
            <AgentBadge agent={group.kind === "lead" ? "lead" : group.agent} size={14} />
            <span className="bh-track__label">{headText(group, t)}</span>
          </span>
          <ul className="bh-track__bar" aria-label={headText(group, t)}>
            {segments.map((segment) => (
              <li
                key={segment.mark.key}
                className="bh-track__slot"
                style={{ left: `${segment.left}%`, width: `${segment.width}%` }}
              >
                {props.onSelectStep && segment.mark.step ? (
                  <button
                    type="button"
                    className={`bh-seg is-${segment.mark.tone}`}
                    title={segment.mark.sentence}
                    aria-label={segment.mark.sentence}
                    onClick={() => segment.mark.step && props.onSelectStep?.(segment.mark.step)}
                    data-step-id={segment.mark.step.stepId}
                  />
                ) : (
                  <span className={`bh-seg is-${segment.mark.tone}`} title={segment.mark.sentence} aria-label={segment.mark.sentence} role="img" />
                )}
              </li>
            ))}
            {props.running ? <li className="bh-track__now" aria-hidden /> : null}
          </ul>
        </div>
      ))}
      <div className="bh-track__axis" aria-hidden>
        <span>0</span>
        <span>{props.formatTime(spanMs)}</span>
      </div>
      {now ? (
        <div className="bh-style__now bh-style__now--large" data-row-kind="now">
          <span className="bh-style__now-text">{now.text}</span>
          <span className="bh-style__now-time">{stepTime(props, t, now.step)}</span>
        </div>
      ) : null}
      <Prose props={props} />
      {longest ? (
        <p className="bh-track__longest" data-testid="timeline-longest">
          {t("style_longest", { what: longest.mark.sentence, time: props.formatTime(longest.durationMs) })}
        </p>
      ) : null}
    </div>
  );
}

export function RunStyleView(props: RunStyleViewProps) {
  const t = useTranslations("step_list");
  if (props.style === "chips") return <Chips props={props} t={t} />;
  if (props.style === "lanes") return <Lanes props={props} t={t} />;
  if (props.style === "timeline") return <Timeline props={props} t={t} />;
  return <RunGroups {...props} />;
}
