/**
 * run-groups — what the running card shows, grouped by agent appearance (chat running state PRD R1.5–R1.8).
 *
 * An ordinary Turn has no plan, so the card reports only what has happened: Lead's own steps, and one group
 * per helper dispatch placed where Lead dispatched it. A repeated dispatch of the same helper is a new group
 * ("第 2 次"); two helpers of the same kind running at once are told apart by their task.
 *
 * Lines inside a group:
 *   · Protocol steps (`subagent_return`, `review_delivery`) are never lines. While one runs, the group says
 *     it is handing back its result or Lead is checking the delivery.
 *   · Consecutive identical failures of one agent merge into one line ("失败 3 次"). Raw arguments are not
 *     stored, so the visible label is the identity: calls that read the same are shown as the same. A failure
 *     followed by the same call succeeding becomes that success with "重试 N 次".
 *
 * Pure: steps and the turn-store peer maps in, groups out. `windowRunGroup` decides what a running card shows.
 */
import { buildAgentGrouping, type PeerBlock } from "./agent-grouping";
import type { StepStatus } from "./block-primitives";
import type { FoldedNote } from "./folded-notes";
import { humanLabel, rowOf, stripInternal, type BandRow } from "./running-band-model";
import type { StepView } from "./types";
import type { PeerTerminal } from "@/lib/turn-store";

export interface RunLine {
  /** Stable across merges: the first step of a merged run keeps the key. */
  key: string;
  /** The latest step behind this line; its detail opens on click. */
  step: StepView;
  label: string;
  meta: string;
  tone: BandRow["tone"];
  stepState: StepView["state"];
  /** Identical consecutive failures merged into this line; 0 when the line did not fail. */
  failures: number;
  /** Failures of the same call before this line succeeded. */
  retried: number;
}

/** The protocol step a group is in: a helper handing back its result, or Lead checking the delivery. */
export type RunProtocol = "return" | "review";

/** A group's content in order: step lines, and Lead's folded text where Lead wrote it (PRD R1.9). */
export type RunItem = { kind: "line"; line: RunLine } | { kind: "note"; note: FoldedNote };

/** One entry of a Lead run before lines are built: a step or a folded note. */
export type LeadEntry = { kind: "step"; step: StepView } | { kind: "note"; note: FoldedNote };

export interface RunGroup {
  kind: "lead" | "helper";
  key: string;
  /** Helper groups only. */
  peerId: string | null;
  /** Backend agent name for the badge; "lead" for Lead. */
  agent: string;
  displayName: string | null;
  /** Shown when another group of the same agent overlaps this one in time. */
  task: string | null;
  /** 2 and up for a later, non-overlapping dispatch of the same agent ("第 N 次"). */
  repeat: number | null;
  status: StepStatus;
  lines: RunLine[];
  /** Lines and folded notes in order; notes appear in Lead's runs only. */
  items: RunItem[];
  /** The line still running in this group, if any. */
  active: RunLine | null;
  protocol: RunProtocol | null;
  /** Calls behind the lines; merged failures count every call. */
  stepCount: number;
  durationMs: number | null;
  /** Preformatted duration for helpers ("12s"), as the trace shows it. */
  time: string | null;
  /** The helper's own caveats on a warning return. */
  caveats: string[];
  /** Helper facts (failure reason, run limit) for the line that replaces a returned group. */
  block: PeerBlock | null;
}

export interface BuildRunGroupsInput {
  steps: StepView[];
  peerNames: Record<string, string>;
  peerProse?: Record<string, string>;
  peerTasks?: Record<string, string>;
  peerDisplayNames?: Record<string, string>;
  peerTerminalById?: Record<string, PeerTerminal>;
  peerFailureRecovered?: boolean;
  /** Lead's folded text, placed before the step that followed it. */
  notes?: readonly FoldedNote[];
}

const PROTOCOL_TOOLS: Record<string, RunProtocol> = { subagent_return: "return", review_delivery: "review" };

function isFailureTone(tone: BandRow["tone"]): boolean {
  return tone === "warn" || tone === "fail";
}

function lineOf(step: StepView): RunLine {
  const row = rowOf(step);
  const failed = step.state === "failed" || step.state === "partial";
  // The current line is worded "正在{what}": name a running step by its plain name, since the catalog's active
  // name already starts with 正在 ("正在整理结果") and would read "正在正在整理结果".
  const plain = humanLabel(step, false);
  const detail = stripInternal(step.label);
  const label = row.tone === "active"
    ? (plain && detail && detail !== plain ? `${plain} · ${detail}` : plain)
    : row.label;
  return {
    key: step.stepId,
    step,
    label,
    meta: row.meta,
    tone: row.tone,
    stepState: step.state,
    failures: failed ? 1 : 0,
    retried: 0,
  };
}

/**
 * Lines for a group's entries, with protocol steps removed and identical failures merged. A note ends a run of
 * merged failures: the reader saw something happen between them.
 */
export function itemsOf(entries: readonly LeadEntry[]): { lines: RunLine[]; items: RunItem[]; protocol: RunProtocol | null; stepCount: number } {
  const lines: RunLine[] = [];
  const items: RunItem[] = [];
  let protocol: RunProtocol | null = null;
  let stepCount = 0;
  for (const entry of entries) {
    if (entry.kind === "note") {
      items.push({ kind: "note", note: entry.note });
      continue;
    }
    const step = entry.step;
    const kind = step.toolName ? PROTOCOL_TOOLS[step.toolName] : undefined;
    if (kind) {
      if (step.state === "active") protocol = kind;
      continue;
    }
    stepCount += 1;
    const line = lineOf(step);
    const lastItem = items[items.length - 1];
    const previous = lastItem?.kind === "line" ? lastItem.line : undefined;
    const sameCall = previous !== undefined
      && previous.failures > 0
      && previous.step.toolName === step.toolName
      && previous.label === line.label;
    let merged: RunLine | null = null;
    if (sameCall && line.failures > 0 && (previous.step.error?.code ?? "") === (step.error?.code ?? "")) {
      merged = { ...line, key: previous.key, failures: previous.failures + 1 };
    } else if (sameCall && step.state === "done") {
      merged = { ...line, key: previous.key, retried: previous.failures };
    }
    if (merged) {
      lines[lines.length - 1] = merged;
      items[items.length - 1] = { kind: "line", line: merged };
      continue;
    }
    lines.push(line);
    items.push({ kind: "line", line });
  }
  return { lines, items, protocol, stepCount };
}

/** Lines for a group's steps, with protocol steps removed and identical failures merged. */
export function linesOf(steps: StepView[]): { lines: RunLine[]; protocol: RunProtocol | null; stepCount: number } {
  const { lines, protocol, stepCount } = itemsOf(steps.map((step) => ({ kind: "step" as const, step })));
  return { lines, protocol, stepCount };
}

function activeLineOf(lines: RunLine[]): RunLine | null {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i].tone === "active") return lines[i];
  }
  return null;
}

function leadGroup(entries: readonly LeadEntry[]): RunGroup {
  const { lines, items, protocol, stepCount } = itemsOf(entries);
  const first = entries[0];
  return {
    kind: "lead",
    key: `lead:${first ? (first.kind === "step" ? first.step.stepId : first.note.key) : "start"}`,
    peerId: null,
    agent: "lead",
    displayName: null,
    task: null,
    repeat: null,
    status: activeLineOf(lines) || protocol ? "run" : "done",
    lines,
    items,
    active: activeLineOf(lines),
    protocol,
    stepCount,
    durationMs: null,
    time: null,
    caveats: [],
    block: null,
  };
}

function helperGroup(block: PeerBlock, terminal: PeerTerminal | undefined): RunGroup {
  const { lines, items, protocol, stepCount } = itemsOf(block.steps.map((step) => ({ kind: "step" as const, step })));
  const durationMs = terminal?.durationMs ?? block.spawnStep?.durationMs ?? null;
  return {
    kind: "helper",
    key: `peer:${block.peerId}`,
    peerId: block.peerId,
    agent: block.agent,
    displayName: block.displayName,
    task: null,
    repeat: null,
    status: block.status,
    lines,
    items,
    active: block.status === "run" ? activeLineOf(lines) : null,
    protocol: block.status === "run" ? protocol : null,
    stepCount,
    durationMs,
    time: block.time,
    caveats: terminal?.status === "succeeded_with_warnings" ? (terminal.caveats ?? []) : [],
    block,
  };
}

/** When a helper group ran: from its dispatch to its return, open-ended while it runs. */
function spanOf(group: RunGroup): { start: number; end: number } | null {
  const start = group.block?.spawnStep?.startedAtMs ?? group.block?.steps[0]?.startedAtMs;
  if (start === undefined) return null;
  const end = group.status === "run" || group.durationMs === null ? Number.POSITIVE_INFINITY : start + group.durationMs;
  return { start, end };
}

/** Name same-agent groups: a task when they overlap in time, "第 N 次" when one follows another. */
function nameSameAgentGroups(groups: RunGroup[]): void {
  const seen = new Map<string, RunGroup[]>();
  for (const group of groups) {
    if (group.kind !== "helper") continue;
    const identity = group.displayName || group.agent;
    const earlier = seen.get(identity) ?? [];
    const span = spanOf(group);
    const overlapping = earlier.filter((other) => {
      const otherSpan = spanOf(other);
      return span !== null && otherSpan !== null && span.start < otherSpan.end && otherSpan.start < span.end;
    });
    if (overlapping.length > 0) {
      group.task = group.block?.task ?? null;
      for (const other of overlapping) other.task = other.block?.task ?? null;
    }
    if (earlier.length > 0 && overlapping.length < earlier.length) group.repeat = earlier.length + 1;
    seen.set(identity, [...earlier, group]);
  }
}

/** One piece of a Turn's work in the order it happened: a run of Lead's own steps, or one helper dispatch. */
export type AgentWork =
  | { kind: "lead"; steps: StepView[]; entries: LeadEntry[] }
  | { kind: "helper"; block: PeerBlock };

/**
 * Lead's runs of steps and each helper where Lead dispatched it, in the order they happened. Shared by the running
 * card and the finished trace, so both show the same structure (PRD R1.5).
 *
 * A dispatch whose helper is not named yet is not Lead's work: "正在派子 agent" froze for 105 s on 116 while the
 * helper ran. The heartbeat says Lead is waiting; a finished or failed dispatch stays in Lead's run.
 */
export function orderAgentWork(
  steps: StepView[],
  leadSteps: StepView[],
  peerBlocks: PeerBlock[],
  notes: readonly FoldedNote[] = [],
): AgentWork[] {
  // Lead's folded text sits right before the step that followed it, in Lead's run.
  const notesBefore = new Map<string, FoldedNote[]>();
  for (const note of notes) notesBefore.set(note.beforeStepId, [...(notesBefore.get(note.beforeStepId) ?? []), note]);
  const leadIds = new Set(leadSteps.map((step) => step.stepId));
  const blockBySpawn = new Map<string, PeerBlock>();
  const blockByPeer = new Map<string, PeerBlock>();
  for (const block of peerBlocks) {
    blockByPeer.set(block.peerId, block);
    if (block.spawnStep) blockBySpawn.set(block.spawnStep.stepId, block);
  }
  const work: AgentWork[] = [];
  const placed = new Set<string>();
  let leadRun: LeadEntry[] = [];
  const closeLeadRun = () => {
    if (leadRun.length > 0) {
      const runSteps = leadRun.flatMap((entry) => (entry.kind === "step" ? [entry.step] : []));
      work.push({ kind: "lead", steps: runSteps, entries: leadRun });
    }
    leadRun = [];
  };
  const placeBlock = (block: PeerBlock) => {
    if (placed.has(block.peerId)) return;
    placed.add(block.peerId);
    closeLeadRun();
    work.push({ kind: "helper", block });
  };
  for (const step of steps) {
    for (const note of notesBefore.get(step.stepId) ?? []) leadRun.push({ kind: "note", note });
    notesBefore.delete(step.stepId);
    const spawned = blockBySpawn.get(step.stepId);
    if (spawned) {
      placeBlock(spawned);
      continue;
    }
    if (leadIds.has(step.stepId)) {
      if (!(step.state === "active" && (step.toolName === "subagent_spawn" || step.peerProseKey))) leadRun.push({ kind: "step", step });
      continue;
    }
    // A helper without a dispatch row (plan path): place it where its first step appears.
    const owner = step.subagentId ? blockByPeer.get(step.subagentId) : undefined;
    if (owner && !owner.spawnStep) placeBlock(owner);
  }
  // A note whose step is not shown yet (paced in, or left out) waits at the end of Lead's last run.
  for (const pending of notesBefore.values()) for (const note of pending) leadRun.push({ kind: "note", note });
  closeLeadRun();
  // Helpers known only from their facts (no row, no step yet: an early failure, a direct answer).
  for (const block of peerBlocks) placeBlock(block);
  return work;
}

/** Groups in the order things happened: Lead's runs of steps, and each helper where Lead dispatched it. */
export function buildRunGroups(input: BuildRunGroupsInput): RunGroup[] {
  const terminals = input.peerTerminalById ?? {};
  const { leadSteps, peerBlocks } = buildAgentGrouping(
    input.steps,
    input.peerNames,
    input.peerProse ?? {},
    input.peerTasks ?? {},
    input.peerDisplayNames ?? {},
    terminals,
    input.peerFailureRecovered ?? false,
  );
  const groups = orderAgentWork(input.steps, leadSteps, peerBlocks, input.notes ?? []).map((item) =>
    item.kind === "lead" ? leadGroup(item.entries) : helperGroup(item.block, terminals[item.block.peerId]),
  );
  nameSameAgentGroups(groups);
  return groups;
}

export interface RunGroupWindow {
  /** Finished lines left out before the shown ones. */
  hiddenBefore: number;
  lines: RunLine[];
  /** Only the header with its step count and the current line are shown (P1). */
  compact: boolean;
}

/** Finished lines a running card keeps per group, besides failures and the current line. */
export const RECENT_LINES = 3;

/**
 * What a running card shows of one group. More than two helpers at once shrink each to its header and current
 * line (P1). Otherwise the last three finished lines and the current line show; failures never fold away.
 */
export function windowRunGroup(group: RunGroup, activeHelpers: number): RunGroupWindow {
  if (group.kind === "helper" && activeHelpers > 2) {
    return { hiddenBefore: 0, lines: group.active ? [group.active] : [], compact: true };
  }
  const finished = group.lines.filter((line) => line !== group.active);
  const recent = new Set(finished.slice(-RECENT_LINES));
  const kept = finished.filter((line) => recent.has(line) || isFailureTone(line.tone));
  return {
    hiddenBefore: finished.length - kept.length,
    lines: group.active ? [...kept, group.active] : kept,
    compact: false,
  };
}

/** Helper groups still running; drives P1 and "N 个助手在做". */
export function activeHelperCount(groups: RunGroup[]): number {
  return groups.filter((group) => group.kind === "helper" && group.status === "run").length;
}
