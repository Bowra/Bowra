/**
 * The running view's three other styles (chat running state R3.1, R3.3), drawn from the same groups as the list
 * (`run-groups.ts`). This module only shapes the marks; the renderers live in `RunStyles.tsx`.
 *
 *   · 胶囊: one chip per step; consecutive finished steps of the same kind merge into one chip with a count.
 *   · 泳道: one dot per step or note, in order.
 *   · 时间轴: one segment per step, placed by its start and duration on the Turn's own clock.
 *
 * Every mark carries the step's sentence (the list's line: "what · fact") for its tooltip and accessible name, and
 * one of the shared tones: done, running, warning (a failure that was worked around, a retry, a cancel), failed,
 * needs you (a step waiting for confirmation), and Lead (a note or a draft).
 */
import type { FoldedNote } from "./folded-notes";
import type { RunGroup, RunLine } from "./run-groups";
import { humanLabel } from "./running-band-model";
import type { StepView } from "./types";

export type RunMarkTone = "done" | "active" | "warn" | "fail" | "needs" | "lead";

export interface RunMark {
  key: string;
  tone: RunMarkTone;
  /** Short name of what the step did ("查询数据"); a note's or draft's kind word for Lead's marks. */
  label: string;
  /** The full sentence, as the list words the line. */
  sentence: string;
  /** The step behind the mark; null for Lead's notes. */
  step: StepView | null;
  /** Merged steps behind a chip (1 for a single step). */
  count: number;
  /** Identical failures merged into the mark. */
  failures: number;
  meta: string;
}

export interface RunMarkWords {
  note: string;
  draft: string;
  /** "失败 N 次". */
  failedTimes: (count: number) => string;
}

function waitsForConfirmation(step: StepView): boolean {
  return Boolean(step.confirmation && !step.confirmation.outcome);
}

function toneOfLine(line: RunLine): RunMarkTone {
  if (line.tone === "active" && waitsForConfirmation(line.step)) return "needs";
  return line.tone;
}

function noteMark(note: FoldedNote, words: RunMarkWords): RunMark {
  const label = note.draft ? words.draft : words.note;
  const text = note.text.replace(/[*_`#>]+/g, "").split(/\n+/).map((part) => part.trim()).find(Boolean) ?? "";
  return {
    key: `note:${note.key}`,
    tone: "lead",
    label,
    sentence: text ? `${label} · ${text}` : label,
    step: null,
    count: 1,
    failures: 0,
    meta: "",
  };
}

function lineMark(line: RunLine, words: RunMarkWords): RunMark {
  const meta = line.failures > 1 ? words.failedTimes(line.failures) : line.meta;
  return {
    key: line.key,
    tone: toneOfLine(line),
    label: humanLabel(line.step, false) || line.label,
    sentence: [line.label, meta].filter(Boolean).join(" · "),
    step: line.step,
    count: 1,
    failures: line.failures,
    meta,
  };
}

/** Every step and note of a group, in order, as marks (泳道). */
export function marksOf(group: RunGroup, words: RunMarkWords): RunMark[] {
  return group.items.map((item) => (item.kind === "note" ? noteMark(item.note, words) : lineMark(item.line, words)));
}

/** Chips (胶囊): consecutive finished steps of the same kind merge, so a group of 30 queries reads as one chip. */
export function chipsOf(group: RunGroup, words: RunMarkWords): RunMark[] {
  const chips: RunMark[] = [];
  for (const mark of marksOf(group, words)) {
    const last = chips[chips.length - 1];
    if (last && mark.tone === "done" && last.tone === "done" && mark.step && last.step && mark.label === last.label) {
      last.count += 1;
      last.step = mark.step;
      last.sentence = `${last.label} × ${last.count}`;
      continue;
    }
    chips.push({ ...mark });
  }
  return chips;
}

/** Keep the newest `limit` marks; earlier ones are counted ("前面还有 N 个"). Failures and the current step stay. */
export function windowMarks(marks: RunMark[], limit: number): { marks: RunMark[]; hiddenBefore: number } {
  if (marks.length <= limit) return { marks, hiddenBefore: 0 };
  const kept = new Set(marks.slice(-limit));
  for (const mark of marks) if (mark.tone === "fail" || mark.tone === "warn" || mark.tone === "needs") kept.add(mark);
  const shown = marks.filter((mark) => kept.has(mark));
  return { marks: shown, hiddenBefore: marks.length - shown.length };
}

export interface RunSegment {
  mark: RunMark;
  /** Percent of the span from its start. */
  left: number;
  width: number;
  durationMs: number;
}

/** The smallest segment width, so a 0.2 s step is still a mark (percent of the span). */
const MIN_SEGMENT = 1.2;

/**
 * Time segments (时间轴) on the Turn's clock: a step starts at `startedAtMs` and lasts `durationMs`; a running step
 * lasts until `nowMs`. Steps without a start time (older events) are left out of the timeline, not guessed.
 */
export function segmentsOf(group: RunGroup, words: RunMarkWords, startMs: number, spanMs: number, nowMs: number): RunSegment[] {
  if (spanMs <= 0) return [];
  const out: RunSegment[] = [];
  for (const item of group.items) {
    if (item.kind !== "line") continue;
    const step = item.line.step;
    if (step.startedAtMs === undefined) continue;
    const begin = Math.max(0, step.startedAtMs - startMs);
    const duration = item.line.tone === "active"
      ? Math.max(0, nowMs - step.startedAtMs)
      : Math.max(0, step.durationMs ?? 0);
    const left = Math.min(100, (begin / spanMs) * 100);
    const width = Math.max(MIN_SEGMENT, Math.min(100 - left, (duration / spanMs) * 100));
    out.push({ mark: lineMark(item.line, words), left, width, durationMs: duration });
  }
  return out;
}

/** The Turn's span for the timeline: from the earliest step start to now (running) or the latest step end. */
export function timelineSpan(groups: RunGroup[], nowMs: number, running: boolean, turnStartMs: number | null): { startMs: number; spanMs: number } {
  let start = turnStartMs ?? Number.POSITIVE_INFINITY;
  let end = 0;
  for (const group of groups) {
    for (const line of group.lines) {
      const begin = line.step.startedAtMs;
      if (begin === undefined) continue;
      start = Math.min(start, begin);
      end = Math.max(end, begin + Math.max(0, line.step.durationMs ?? 0));
    }
  }
  if (!Number.isFinite(start)) return { startMs: 0, spanMs: 0 };
  if (running) end = Math.max(end, nowMs);
  return { startMs: start, spanMs: Math.max(1, end - start) };
}

/** The longest finished step, named in the expanded timeline ("最长的一段"); null when nothing took a while. */
export function longestSegment(segments: RunSegment[], minMs = 15_000): RunSegment | null {
  let longest: RunSegment | null = null;
  for (const segment of segments) if (!longest || segment.durationMs > longest.durationMs) longest = segment;
  return longest && longest.durationMs >= minMs ? longest : null;
}
