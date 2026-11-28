"use client";

/**
 * RunGroups — the running card's body (chat running state PRD R1.5–R1.8): Lead's steps and one group per
 * helper dispatch, in the order they happened. A returned helper folds to one line; a running group shows its
 * last three finished lines and its current line; more than two helpers at once show their current line only.
 *
 * Wording reuses the band's rows: "tool · intent · fact" labels, state copy for warnings, the current line as
 * "正在…". The owning agent's newest code line or prose sits under its current line, as the band showed it.
 */
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  Check as LucideCheck,
  Circle as LucideCircle,
  TriangleAlert as LucideTriangleAlert,
  X as LucideX,
} from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";

import { AgentBadge } from "./AgentBadge";
import { agentDisplayName } from "./agent-meta";
import { FoldedNoteRow } from "./folded-notes";
import { windowRunGroup, type RunGroup, type RunLine } from "./run-groups";
import type { StepView } from "./types";

export type BandT = ReturnType<typeof useTranslations<"step_list">>;

/** A step's own time shows once it has run this long (PRD R1.6). */
export const STEP_TIME_AFTER_MS = 10_000;

const glyphFor = (tone: RunLine["tone"]): ReactNode => {
  const icon = tone === "warn" ? LucideTriangleAlert : tone === "fail" ? LucideX : tone === "active" ? LucideCircle : LucideCheck;
  return <LucideGlyph icon={icon} size={13} aria-hidden />;
};

export interface RunGroupsProps {
  groups: RunGroup[];
  activeHelpers: number;
  /** Turn-relative running time of a step, in ms; null when unknown. */
  stepElapsedMs: (step: StepView) => number | null;
  /** Elapsed formatting shared with the heartbeat line. */
  formatTime: (ms: number) => string;
  /** The agent whose prose or code is streaming ("lead" or a peer id), with its throttled text. */
  owner: { id: string; text: string; isCode: boolean } | null;
}

function lineMeta(line: RunLine, t: BandT, stepTime: string): string {
  if (line.tone === "active") return stepTime;
  if (line.failures > 1) return t("step_failed_n", { count: line.failures });
  const parts: string[] = [];
  if (line.tone === "warn") {
    parts.push(t(line.stepState === "cancelled" ? "band_row_cancelled" : line.stepState === "partial" ? "band_row_partial" : "band_row_failed"));
  } else if (line.meta) {
    parts.push(line.meta);
  }
  if (line.retried > 0) parts.push(t("step_retried_n", { count: line.retried }));
  return parts.join(" · ");
}

function Line({ line, t, stepTime }: { line: RunLine; t: BandT; stepTime: string }) {
  const active = line.tone === "active";
  const label = line.label || (active ? t("band_working") : t("band_row_done"));
  return (
    <div className={`bh-band__row is-${line.tone}`} data-row-kind="step" data-step-id={line.step.stepId}>
      <span className="bh-band__glyph">{glyphFor(line.tone)}</span>
      <span className="bh-band__lab">{active ? t("band_doing", { what: label }) : label}</span>
      <span className="bh-band__rmeta">{lineMeta(line, t, stepTime)}</span>
    </div>
  );
}

/** A returned helper's one line ("已完成 · 9 步 · 2 分 38 秒"); shared by every display style. */
export function returnedLine(group: RunGroup, t: BandT, formatTime: RunGroupsProps["formatTime"]): { tone: RunLine["tone"]; text: string } {
  const state = group.status === "fail"
    ? { tone: "fail" as const, word: t("run_failed") }
    : group.status === "part"
      ? { tone: "warn" as const, word: t("band_row_partial") }
      : group.status === "bail"
        ? { tone: "warn" as const, word: t("band_row_cancelled") }
        : { tone: "done" as const, word: t("run_done") };
  const parts = [state.word];
  if (group.stepCount > 0) parts.push(t("group_steps", { count: group.stepCount }));
  if (group.durationMs !== null) parts.push(formatTime(group.durationMs));
  return { tone: state.tone, text: parts.join(" · ") };
}

function GroupView({ group, props, t, isOwner }: { group: RunGroup; props: RunGroupsProps; t: BandT; isOwner: boolean }) {
  const name = agentDisplayName(group.kind === "lead" ? "lead" : group.agent, group.displayName);
  const running = group.status === "run";
  const view = windowRunGroup(group, props.activeHelpers);
  const visible = new Set(view.lines);
  const header = [name, group.task ?? (group.repeat ? t("group_repeat", { n: group.repeat }) : "")].filter(Boolean).join(" · ");
  const stepTime = (step: StepView): string => {
    const ms = props.stepElapsedMs(step);
    return ms !== null && ms > STEP_TIME_AFTER_MS ? t("step_elapsed", { time: props.formatTime(ms) }) : "";
  };
  const prose = isOwner && props.owner?.text ? props.owner : null;

  return (
    <div className="bh-band__group" data-group-kind={group.kind} data-status={group.status} data-testid="run-group">
      <div className="bh-band__ghead">
        <AgentBadge agent={group.kind === "lead" ? "lead" : group.agent} size={16} />
        <span className="bh-band__gname">{header}</span>
        {view.compact ? <span className="bh-band__gmeta">{t("group_steps_done", { count: group.stepCount })}</span> : null}
      </div>
      {group.kind === "helper" && !running ? (
        (() => {
          const folded = returnedLine(group, t, props.formatTime);
          return (
            <>
              <div className={`bh-band__row is-${folded.tone}`} data-row-kind="group-end">
                <span className="bh-band__glyph">{glyphFor(folded.tone)}</span>
                <span className="bh-band__lab">{folded.text}</span>
              </div>
              {group.caveats.length > 0 ? (
                <div className="bh-band__row is-warn" data-row-kind="caveat">
                  <span className="bh-band__glyph">{glyphFor("warn")}</span>
                  <span className="bh-band__lab">{t("group_caveat", { text: group.caveats[0] })}</span>
                </div>
              ) : null}
            </>
          );
        })()
      ) : (
        <>
          {view.hiddenBefore > 0 ? (
            <div className="bh-band__row bh-band__row--more" data-row-kind="more">
              <span className="bh-band__glyph" aria-hidden />
              <span className="bh-band__lab">{t("group_more_before", { count: view.hiddenBefore })}</span>
            </div>
          ) : null}
          {/* Lead's folded text always shows where Lead wrote it; step lines follow the window. */}
          {group.items.map((item) => (
            item.kind === "note"
              ? <FoldedNoteRow key={`note:${item.note.key}`} note={item.note} />
              : visible.has(item.line)
                ? <Line key={item.line.key} line={item.line} t={t} stepTime={stepTime(item.line.step)} />
                : null
          ))}
          {running && !group.active && group.protocol ? (
            <div className="bh-band__row is-active" data-row-kind="protocol">
              <span className="bh-band__glyph">{glyphFor("active")}</span>
              <span className="bh-band__lab">{t(group.protocol === "review" ? "band_closeout_review" : "band_closeout_return")}</span>
            </div>
          ) : null}
          {prose ? (
            <div className="bh-band__foot" data-row-kind="prose">
              <span
                className={prose.isCode ? "bh-band__foot-prose bh-band__foot-prose--code" : "bh-band__foot-prose"}
                data-testid="running-band-prose"
              >
                {prose.text}
              </span>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

export function RunGroups(props: RunGroupsProps) {
  const t = useTranslations("step_list");
  const ownerId = props.owner?.id ?? null;
  // Lead's prose belongs to its latest run; a helper's to its own group.
  const lastLead = [...props.groups].reverse().find((group) => group.kind === "lead") ?? null;
  return (
    <div className="bh-band__groups">
      {props.groups.map((group) => (
        <GroupView
          key={group.key}
          group={group}
          props={props}
          t={t}
          isOwner={ownerId !== null && (group.kind === "lead" ? ownerId === "lead" && group === lastLead : group.peerId === ownerId)}
        />
      ))}
    </div>
  );
}
