const FINAL_TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "error",
  "cancelled",
  "aborted",
]);

function normalizedTerminalStatus(status: string | null | undefined): string | null {
  if (!status) return null;
  if (status === "error") return "failed";
  if (status === "aborted") return "cancelled";
  return status;
}

function finiteDuration(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function timestampMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatTurnDuration(durationMs: number): string {
  return `${(Math.max(0, durationMs) / 1000).toFixed(1)}s`;
}

export function resolveLiveTurnDurationMs(input: {
  wireDurationMs: unknown;
  executionStartedAtMs: number | null | undefined;
  nowMs?: number;
}): number | null {
  const wire = finiteDuration(input.wireDurationMs);
  if (wire !== null) return wire;
  const startedAt = finiteDuration(input.executionStartedAtMs);
  if (startedAt === null) return null;
  const now = finiteDuration(input.nowMs ?? Date.now());
  return now === null ? null : Math.max(0, now - startedAt);
}

/**
 * A terminal-protocol recovery may close a Turn without appending a legacy
 * `done` row. In that shape the old summary can still describe an earlier
 * clarification-pending generation. Reconstruct the current generation from
 * the last replayed `turn_started` and the durable Turn end timestamp.
 */
export function resolveHydratedTurnDurationMs(input: {
  summaryTotalMs: number | null | undefined;
  terminalStatus: string | null | undefined;
  lastPersistedDoneStatus: string | null | undefined;
  lastExecutionStartedAt: string | null | undefined;
  endedAt: string | null | undefined;
}): number | null {
  const durableSummary = finiteDuration(input.summaryTotalMs);
  const terminalStatus = normalizedTerminalStatus(input.terminalStatus);
  const persistedDoneStatus = normalizedTerminalStatus(
    input.lastPersistedDoneStatus,
  );
  if (
    terminalStatus
    && FINAL_TERMINAL_STATUSES.has(terminalStatus)
    && persistedDoneStatus !== terminalStatus
  ) {
    const startedAt = timestampMs(input.lastExecutionStartedAt);
    const endedAt = timestampMs(input.endedAt);
    if (startedAt !== null && endedAt !== null && endedAt >= startedAt) {
      return endedAt - startedAt;
    }
  }
  return durableSummary;
}
