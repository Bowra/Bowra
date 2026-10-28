/**
 * turn-store-replay — hydrate the turn-store from a persisted event stream.
 *
 * Used by the F5 / page-load path to populate `lib/turn-store` for archived
 * turns. Walks the events returned by `GET /api/v1/turns/{turnId}/events`
 * and feeds each one through `dispatchToTurnStore`, which is the same
 * routing path live SSE uses. Net effect: state A (live) === state B
 * (archived, post-done snapshot) === state C (F5 hydrate) — all three
 * read from one canonical turn-store keyed by turnId.
 *
 * Chat-polish-r3 Phase 1 (state-unify) — replaces the
 * `runner.traceSteps` / `archived.traceSteps` lossy snapshot path. See
 * `docs/sprints/chat-polish-r3-2026-05-26/phase-0-design-lock.md` §5.
 */
import { fetchTurnEvents, type TurnEventDTO } from "./api";
import { dispatchToTurnStore } from "./turn-store-dispatch";

export interface ReplayTurnEventsResult {
  turnId: string;
  eventCount: number;
  /** Count of events dispatchToTurnStore claimed (true return). Diagnostic. */
  claimed: number;
}

/**
 * Fetch persisted events for `turnId` and feed each one through
 * `dispatchToTurnStore` in arrival order. Idempotent: turn-store reducers
 * (applyStepStarted / applyStepDone / etc.) are designed to be replay-safe
 * — repeat dispatch of the same event yields the same final state.
 *
 * Errors fetching the event log are caught and logged; callers receive
 * `eventCount: 0` so the UI can fall through to its empty-state branch
 * instead of crashing on a malformed response.
 */
export async function replayTurnEvents(
  turnId: string,
): Promise<ReplayTurnEventsResult> {
  if (!turnId) {
    return { turnId, eventCount: 0, claimed: 0 };
  }
  let events: Awaited<ReturnType<typeof fetchTurnEvents>> = [];
  try {
    events = await fetchTurnEvents(turnId);
  } catch (e) {
    if (typeof console !== "undefined") {
      console.warn("[replayTurnEvents] fetch failed", turnId, e);
    }
    return { turnId, eventCount: 0, claimed: 0 };
  }
  return replayTurnEventRows(turnId, events);
}

/** Replay rows already fetched by the hydrate projection path.
 *
 * Session hydration needs both the visual projection and the canonical turn
 * store.  Accepting the fetched rows here prevents two sequential GETs for
 * every turn while preserving the exact same reducer path as live replay.
 */
export function replayTurnEventRows(
  turnId: string,
  events: TurnEventDTO[],
): ReplayTurnEventsResult {
  let claimed = 0;
  for (const ev of events) {
    const name = ev?.event_type;
    if (typeof name !== "string" || !name) continue;
    // dispatchToTurnStore routes by card_id (== turnId). Some events carry the
    // turn's peer identity but omit card_id — notably `agent_spawned`, which the
    // LIVE path backfills from the stream URL (use-turn-runner.ts:951). The replay
    // path had no equivalent, so on F5 / session reload agent_spawned never
    // populated peerNames and every archived multi-agent turn collapsed to a
    // single lead block (no peer blocks). In replay every event belongs to this
    // turnId, so backfill card_id when absent.
    // Backfill card_id WITHOUT mutating the fetched event — clone when absent.
    // UX-3 G1 (2026-07-03): likewise for emoji_ack — dispatcher routes by `__turnId`;
    // live use-turn-runner already injects it from the stream URL; without replay backfill, F5 loses
    // the steer 🤝 chip (live≠replay).
    const raw = ev.payload as Record<string, unknown> | null | undefined;
    let payload = raw;
    if (raw && typeof raw === "object") {
      // graph-mode batch 1 implementation review F-2: plan-scoped payloads (step_*/plan_* events with plan_id but no card_id)
      // are exempt from card_id injection: dispatch's plan branch checks
      // `!payload.card_id`; injection would misroute replayed plan step_done/step_started
      // into step-list, never reaching plan-store (live≠replay).
      const isPlanScoped = typeof raw.plan_id === "string" && typeof raw.card_id !== "string";
      const needsCardId = !isPlanScoped && typeof raw.card_id !== "string";
      const needsTurnId = name === "emoji_ack" && typeof raw.__turnId !== "string";
      if (needsCardId || needsTurnId) {
        payload = {
          ...raw,
          ...(needsCardId ? { card_id: turnId } : {}),
          ...(needsTurnId ? { __turnId: turnId } : {}),
        };
      }
    }
    if (dispatchToTurnStore(name, payload, "replay", turnId)) {
      claimed += 1;
    }
  }
  return { turnId, eventCount: events.length, claimed };
}
