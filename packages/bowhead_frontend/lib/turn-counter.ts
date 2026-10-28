/**
 * chat-redesign Phase 1.7-D — `turn-NN` header caption helper.
 *
 * Counter source per discovery §2 P1 #17 resolution + tech-design §6.4
 * (off-by-one fixed in p0-followup F5 / dogfood D-4):
 *
 *  · live turn (running OR errored): `archivedTurns.length + 1` — the
 *    current turn has NOT been moved to archivedTurns yet, so we add it.
 *  · turn done (running=false, no error): a live-completed turn has already
 *    moved into archivedTurns, while F5/session hydration keeps the latest
 *    completed turn in the active projection and archives only its
 *    predecessors. `hasUnarchivedActiveTurn` distinguishes those two shapes.
 *  · before any turn: `0`.
 *
 * A durable `completed` status proves execution completion, not numerical or
 * source verification.  The header therefore never projects it as
 * "verified"; evidence linkage is rendered separately by the report surface.
 *
 * Output format: `turn-NN` (NN zero-padded to two digits).
 *
 * NOTE: `hasActiveTurnId` is kept on the input shape for backward
 * compatibility with the page.tsx call site, but the counter logic no
 * longer relies on it; the running/error pair carries the same signal
 * accurately (a stale `turnId` after done(completed) used to double-count).
 */

export interface TurnCaptionInput {
  archivedTurnsLength: number;
  /** Stable ids let the counter collapse hydration/live duplicates. */
  archivedTurnIds?: Array<string | null | undefined>;
  /** Retained on input shape for back-compat; not consumed by the counter. */
  hasActiveTurnId: boolean;
  /** Current live projection identity; a same-turn resume keeps this id. */
  activeTurnId?: string | null;
  /** True when the visible active projection is not represented in archivedTurns. */
  hasUnarchivedActiveTurn: boolean;
  running: boolean;
  hasError: boolean;
  /** Retained for call-site compatibility; completion is not verification. */
  terminalStatus?: string | null;
}

/**
 * The turn number behind the caption (0 before any turn). The topbar shows it in user language
 * ("Turn 3", P2 batch 3).
 */
export function turnOrdinal(input: TurnCaptionInput): number {
  const archivedIds = input.archivedTurnIds
    ? new Set(input.archivedTurnIds.filter((id): id is string => Boolean(id)))
    : null;
  const anonymousArchivedCount = input.archivedTurnIds
    ? input.archivedTurnIds.filter((id) => !id).length
    : 0;
  const archivedCount = archivedIds
    ? archivedIds.size + anonymousArchivedCount
    : input.archivedTurnsLength;
  const activeAlreadyArchived = Boolean(
    input.activeTurnId && archivedIds?.has(input.activeTurnId),
  );
  const activeProjection = input.running
    || input.hasError
    || input.hasUnarchivedActiveTurn;
  const counter = archivedCount + (
    activeProjection && !activeAlreadyArchived ? 1 : 0
  );
  return counter;
}

/**
 * Session turn ordinals by turn id, on the same count as `turnOrdinal`: archived turns in order (duplicate
 * ids collapse, anonymous turns still take a number), then the active turn when it is not archived yet. The
 * active turn's entry always equals `turnOrdinal(input)`. Used so the report
 * history's "Turn N" matches the topbar (P2 batch 3).
 */
export function sessionTurnOrdinals(input: TurnCaptionInput): Map<string, number> {
  const ordinals = new Map<string, number>();
  let n = 0;
  if (input.archivedTurnIds) {
    for (const id of input.archivedTurnIds) {
      if (!id) {
        n += 1;
        continue;
      }
      if (!ordinals.has(id)) ordinals.set(id, ++n);
    }
  }
  const total = turnOrdinal(input);
  if (input.activeTurnId && !ordinals.has(input.activeTurnId) && total > n) {
    ordinals.set(input.activeTurnId, total);
  }
  return ordinals;
}
