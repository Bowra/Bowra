/**
 * Chat byline times. Each byline has one meaning taken from server timestamps, so the live, archived and
 * reloaded views of a turn show the same time, and the browser clock never stamps one (dogfood N3 on 116: a reply
 * read 18:48 and became 18:50 when the next message was sent; the user bubble read the local 18:50, then the
 * server's 18:51, from a browser clock 49.5 s slow).
 *
 * User byline = the server start of the turn's first generation:
 *   1. turn-store `startedAt` — the first `turn_started.started_at` (same-turn resumes never overwrite it); the
 *      event is persisted, so live delivery and replay read the same value;
 *   2. the persisted `turns.started_at` of a hydrated turn, until the event log supplies (1);
 *   3. otherwise no time: before the server acknowledges the turn, nothing is shown rather than a local value
 *      that would later change.
 *
 * Reply (bot) byline = the server time of the turn's latest root terminal `done` (`done.ended_at`):
 *   - while the turn is running (including a resumed generation) no time is shown; the running band already shows
 *     the elapsed time;
 *   - a clarification pause shows the pause's `done`; after the resumed generation ends, its final `done` (the
 *     same value `turns.ended_at` holds after reload);
 *   1. turn-store `endedAt` — written by the shared dispatcher `done` branch, live and on replay;
 *   2. the persisted `turns.ended_at` of a hydrated turn (or of a `done` that predates `ended_at`);
 *   3. otherwise no time.
 */

function finiteMs(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

export function resolveUserMessageTimeMs(input: {
  firstTurnStartedAt?: Date | null;
  persistedStartedAtMs?: number | null;
}): number | null {
  return finiteMs(input.firstTurnStartedAt?.getTime()) ?? finiteMs(input.persistedStartedAtMs);
}

export function resolveReplyTimeMs(input: {
  running: boolean;
  terminalEndedAt?: Date | null;
  persistedEndedAtMs?: number | null;
}): number | null {
  if (input.running) return null;
  return finiteMs(input.terminalEndedAt?.getTime()) ?? finiteMs(input.persistedEndedAtMs);
}

/** Epoch milliseconds of a server ISO timestamp, or null when it is missing or unparseable. */
export function parseServerTimeMs(value: string | null | undefined): number | null {
  if (!value) return null;
  return finiteMs(Date.parse(value));
}
