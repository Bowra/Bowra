/**
 * agent-grouping.ts — the pure transformer that turns a turn's flat StepView[]
 * into the agent-block tree the redesign renders (P4a). Grounded in the real
 * backend contract (subagent.py / events.py):
 *
 *   · A peer's INNER tool steps arrive on the parent card tagged with the peer's
 *     subagent id → `step.subagentId`. We resolve that id to an agent identity
 *     via the turn-store `peerNames` map (peer_dispatched.peer_name).
 *   · The lead's `subagent_spawn` row that introduced a peer carries
 *     `peerProseKey === <peer id>` (its own subagentId is the LEAD id) — it
 *     supplies the peer block's header (task / time / status) + live prose key.
 *   · Everything else is a lead-owned step.
 *
 * Pure + fully unit-testable (no live backend): feed it the StepView[] plus the
 * turn-store peer identity, task, prose, and terminal maps. The container
 * renders leadSteps in the Lead flow and each PeerBlock as an <AgentBlock>.
 */
import type { StepStatus } from "./block-primitives";
import type { StepState, StepView } from "./types";
import type { ErrorDiagnostics } from "@/lib/error-presentation";
import type { PeerTerminal } from "@/lib/turn-store";

export interface PeerBlock {
  /** the peer's subagent id (groups its inner steps + keys prose/name). */
  peerId: string;
  /** backend agent_name (peer_dispatched.peer_name) → AgentBlock identity; falls
   *  back to no peer block until authoritative identity has arrived. */
  agent: string;
  /** Configured user-facing name captured by the spawn event. */
  displayName: string | null;
  /** the lead's subagent_spawn row (header source); null if not yet seen. */
  spawnStep: StepView | null;
  /** the peer's own tool trace, in emit order. */
  steps: StepView[];
  /** accumulated live peer prose (peerProse[peerId]). */
  prose: string;
  /** block-level status derived from the spawn row + inner steps. */
  status: StepStatus;
  /** task preview for the block header (spawn row label). */
  task: string | null;
  /** wall-clock duration label for the header (spawn row duration). */
  time: string | null;
  /** Finite peer-terminal reason rendered through the local i18n registry. */
  failureReason: string | null;
  /** Agent run limit that stopped a partially completed peer. */
  stopReason: string | null;
  /** Why a Continue could not pick up the saved work, so the peer started fresh. */
  resumeRefused: string | null;
  /** Authenticated finite facts for progressive disclosure and support copy. */
  failureDiagnostics: ErrorDiagnostics | null;
}

export interface AgentGrouping {
  /** lead-owned steps (not spawn rows, not peer steps), in emit order. */
  leadSteps: StepView[];
  /** one block per peer, in first-appearance order. */
  peerBlocks: PeerBlock[];
}

/** Map a backend StepState onto the block-model StepStatus glyph set. */
export function stepStateToStatus(state: StepState): StepStatus {
  switch (state) {
    case "active":
      return "run";
    case "failed":
      return "fail";
    case "partial":
      return "part";
    case "pending":
      return "pend";
    // ⚠ Enumerate explicitly: default below returns "done" (green ✓); omitting this would
    // falsely report a user-stopped step as successfully completed, worse than a spinner. `bail` renders as ⚠,
    // meaning aborted, neither failed nor successful.
    case "cancelled":
      return "bail";
    default:
      return "done";
  }
}

/** Fallback status from spawn/child steps. Canonical peer terminal facts, when
 *  present, take precedence in buildAgentGrouping. */
function derivePeerStatus(spawnStep: StepView | null, steps: StepView[]): StepStatus {
  const states: StepState[] = [];
  if (spawnStep) states.push(spawnStep.state);
  for (const s of steps) states.push(s.state);
  if (states.some((s) => s === "active")) return "run";
  // A failed child tool is handled per-step; without a peer terminal fact the
  // block is partial rather than incorrectly declaring that peer dead.
  if (states.some((s) => s === "failed" || s === "partial")) return "part";
  if (states.length > 0 && states.every((s) => s === "done")) return "done";
  return states.length ? "pend" : "run";
}

/** Peer terminal facts → block status. **Export for running-band reuse**: inventory §2.5 explicitly
 *  forbids a separate terminal-state decision in the band; band and ledger must use the same condition. */
export function deriveTerminalStatus(
  terminal: PeerTerminal,
  peerFailureRecovered: boolean,
): StepStatus {
  // The same Agent is running again (an @Agent continuation in this turn).
  if (terminal.rerunning) return "run";
  if (terminal.status === "failed") {
    return peerFailureRecovered ? "bail" : "fail";
  }
  // Stopped at an agent run limit with its finished work returned.
  if (terminal.status === "partial") return "part";
  // `succeeded_with_warnings` is still a completed peer. Its caveats stay in
  // the peer prose / child-step facts instead of mislabelling the agent dead.
  return "done";
}

/** Peer terminal duration copy. **Export for running-band reuse**: the same peer on the band and ledger
 *  must use the same number and format (the previous collapsed strip failed because one rule had two implementations). */
export function formatPeerDuration(durationMs: number | undefined): string | null {
  if (durationMs === undefined) return null;
  if (durationMs < 1000) return `${durationMs}ms`;
  const seconds = durationMs / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds)}s`;
}

export function buildAgentGrouping(
  steps: StepView[],
  peerNames: Record<string, string>,
  peerProse: Record<string, string>,
  peerTasks: Record<string, string> = {},
  peerDisplayNames: Record<string, string> = {},
  peerTerminalById: Record<string, PeerTerminal> = {},
  peerFailureRecovered = false,
): AgentGrouping {
  const blocks = new Map<string, PeerBlock>();
  const order: string[] = [];
  const leadSteps: StepView[] = [];

  const ensure = (peerId: string): PeerBlock => {
    let b = blocks.get(peerId);
    if (!b) {
      b = {
        peerId,
        agent: peerNames[peerId] ?? "",
        displayName: peerDisplayNames[peerId] ?? null,
        spawnStep: null,
        steps: [],
        prose: peerProse[peerId] ?? "",
        status: "run",
        task: null,
        time: null,
        failureReason: null,
        stopReason: null,
        resumeRefused: null,
        failureDiagnostics: null,
      };
      blocks.set(peerId, b);
      order.push(peerId);
    }
    return b;
  };

  // Peer facts can exist without a child tool step (e.g. direct LLM answer,
  // early failure, plan-path peer). Seed blocks from every replayable peer map
  // before walking steps; object insertion order preserves event order.
  const peerIds = new Set([
    ...Object.keys(peerNames),
    ...Object.keys(peerTerminalById),
    ...Object.keys(peerProse),
    ...Object.keys(peerTasks),
    ...Object.keys(peerDisplayNames),
  ]);
  peerIds.forEach((peerId) => ensure(peerId));

  for (const s of steps) {
    // A peer's inner step: its subagentId is a known peer (peer_dispatched landed
    // first — the backend emits peer_dispatched before the peer's runner starts).
    if (s.subagentId && (peerNames[s.subagentId] || blocks.has(s.subagentId))) {
      ensure(s.subagentId).steps.push(s);
      continue;
    }
    // The lead's subagent_spawn row that introduced a peer.
    if (s.peerProseKey) {
      // peer_dispatched can precede agent_spawned. Until the authoritative
      // profile arrives, keep the spawn row in the neutral Lead flow instead
      // of rendering an Agent header with an internal `sub_*` id.
      if (peerNames[s.peerProseKey] || blocks.has(s.peerProseKey)) {
        ensure(s.peerProseKey).spawnStep = s;
      } else {
        leadSteps.push(s);
      }
      continue;
    }
    // Otherwise a lead-owned step.
    leadSteps.push(s);
  }

  const peerBlocks = order.map((id) => {
    const b = blocks.get(id)!;
    const terminal = peerTerminalById[id];
    b.status = terminal
      ? deriveTerminalStatus(terminal, peerFailureRecovered)
      : derivePeerStatus(b.spawnStep, b.steps);
    // task: spawn-step label (non-plan path) else agent_spawned.task (plan path,
    // where there's no spawn step) — distinguishes same-agent peers (e.g. compute×2).
    b.task = b.spawnStep?.label ?? peerTasks[id] ?? null;
    b.time =
      formatPeerDuration(terminal?.durationMs) ??
      b.spawnStep?.durationText ??
      null;
    b.failureReason = terminal?.failureReason ?? null;
    b.stopReason = terminal?.status === "partial" ? (terminal.stopReason ?? "") : null;
    b.resumeRefused = terminal?.resumeRefused ?? null;
    b.failureDiagnostics = terminal?.failureDiagnostics ?? null;
    return b;
  });

  return { leadSteps, peerBlocks };
}
