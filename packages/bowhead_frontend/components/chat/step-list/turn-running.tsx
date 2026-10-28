"use client";

/**
 * Shared current-task activity for trace descendants. This signal controls
 * disclosure and current Composer guidance, never a promise of automatic retry.
 * Missing providers default to inactive for archived, replayed and standalone views.
 */
import { createContext, useContext, type ReactNode } from "react";

const TurnRunningContext = createContext(false);

/**
 * The running band owns live progress: route this cross-cutting signal through context
 * rather than props for this file's established reasons (reaching `AgentBlock` passes through `TurnTraceRegion`/`AgentTrace`/`PeerBlockView`).
 *
 * Why: on 116 (1.0.97 plan turn), band and peer ledger **each showed a spinner on the same screen**;
 * the band current row said「正在派子 agent · <任务全文>」while the `主分析` block heading and
 * 「当前任务」repeated that text — two live-progress surfaces reporting the same fact.
 *
 * ★ My decision in invariant inventory §2.5 (keep `traceEnabled`, clear only `stepViews`)
 * was **applied too broadly**: it protected peer **terminal** disclosure (fail / bail / outcomeReason /
 * provenance footer, so a hard-failed peer would not disappear), not running task rows or spinners.
 *
 * Thus this flag suppresses only **live progress** (heading task row + run-state glyph + full「当前任务」body text);
 * keep peer prose lanes and all terminal facts — the user explicitly chose to retain prose.
 *
 * Default `false` is likewise deliberate: without a provider (archive / replay / spike harness),
 * behavior remains exactly as today.
 */
const BandOwnsLiveStatusContext = createContext(false);

export function TurnRunningProvider({
  running,
  bandOwnsLiveStatus = false,
  children,
}: {
  running: boolean;
  /** Running band is present and owns who is running and which step they have reached. */
  bandOwnsLiveStatus?: boolean;
  children: ReactNode;
}) {
  return (
    <TurnRunningContext.Provider value={running}>
      <BandOwnsLiveStatusContext.Provider value={bandOwnsLiveStatus}>
        {children}
      </BandOwnsLiveStatusContext.Provider>
    </TurnRunningContext.Provider>
  );
}

/** Whether this turn is still running. False without a provider (archive/replay/standalone harness). */
export function useTurnRunning(): boolean {
  return useContext(TurnRunningContext);
}

/** Whether the running band owns live progress (true prevents peer blocks from rendering their own task row and spinner). */
export function useBandOwnsLiveStatus(): boolean {
  return useContext(BandOwnsLiveStatusContext);
}
