/**
 * Chat question cards and loop reports from `coach.alignment_card` (alignment-loop technical design §13.1).
 *
 * The event is a content-free pointer that live and replayed Turns project through the shared dispatcher;
 * the card itself reads the loop through the Owner's own route, so live, persisted and replayed Turns
 * show the same state. A pointer grants nothing: a reader who isn't the loop's Owner gets a 404.
 */

export type AlignmentCardPointer =
  | { kind: "agent_eval_alignment_card"; schema_version: 1; agent_id: string; loop_id: string; point_id: string; card_seq: number }
  | { kind: "agent_eval_alignment_report"; agent_id: string; loop_id: string; report_id?: string };

export interface CoachAlignmentCardOpen {
  requestId: string;
  turnId: string;
  userId: string;
  teamId: string;
  workspaceId: string;
  sessionId: string;
  pointer: AlignmentCardPointer;
  notice: "held_out_once_per_point" | null;
  /** Projection order across Turns: a later pointer to the same card supersedes an earlier one. */
  order: number;
}

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

const AGENT_ID = /^osa_[A-Za-z0-9_-]{1,156}$/;
const LOOP_ID = /^aeal_[0-9a-f]{32}$/;
const POINT_ID = /^p_[0-9a-f]{4,16}$/;
const REPORT_ID = /^aealr_[0-9a-f]{8,64}$/;

/** Canonical pointers only; extra fields can't carry content or authority. */
export function parseAlignmentCardPointer(value: unknown): AlignmentCardPointer | null {
  const item = record(value);
  const keys = Object.keys(item).sort().join(",");
  const agent = typeof item.agent_id === "string" && AGENT_ID.test(item.agent_id);
  const loop = typeof item.loop_id === "string" && LOOP_ID.test(item.loop_id);
  if (item.kind === "agent_eval_alignment_card") {
    if (
      keys === "agent_id,card_seq,kind,loop_id,point_id,schema_version" && agent && loop && item.schema_version === 1
      && typeof item.point_id === "string" && POINT_ID.test(item.point_id)
      && Number.isSafeInteger(item.card_seq) && (item.card_seq as number) >= 1
    )
      return {
        kind: item.kind, schema_version: 1, agent_id: item.agent_id as string, loop_id: item.loop_id as string,
        point_id: item.point_id as string, card_seq: item.card_seq as number,
      };
    return null;
  }
  if (item.kind === "agent_eval_alignment_report" && agent && loop) {
    if (keys === "agent_id,kind,loop_id") return { kind: item.kind, agent_id: item.agent_id as string, loop_id: item.loop_id as string };
    if (keys === "agent_id,kind,loop_id,report_id" && typeof item.report_id === "string" && REPORT_ID.test(item.report_id))
      return { kind: item.kind, agent_id: item.agent_id as string, loop_id: item.loop_id as string, report_id: item.report_id };
  }
  return null;
}

const opens = new Map<string, readonly CoachAlignmentCardOpen[]>();
const listeners = new Set<() => void>();
const EMPTY: readonly CoachAlignmentCardOpen[] = [];
let revision = 0;
let order = 0;

export const subscribeCoachAlignmentCards = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
export const coachAlignmentCardsVersion = () => revision;
export const coachAlignmentCards = (turnId: string | null | undefined) => (turnId ? (opens.get(turnId) ?? EMPTY) : EMPTY);

const cardKey = (pointer: AlignmentCardPointer) =>
  pointer.kind === "agent_eval_alignment_card" ? `card:${pointer.loop_id}:${pointer.card_seq}` : `report:${pointer.loop_id}`;

/**
 * Whether this pointer is the newest one shown for its card. Asking for a replacement row adds to the same
 * card, so Coach's later pointer carries the live card and earlier ones collapse to their settled line.
 */
export function isLatestAlignmentPointer(open: CoachAlignmentCardOpen): boolean {
  const key = cardKey(open.pointer);
  for (const items of opens.values()) {
    for (const item of items) {
      if (item.sessionId === open.sessionId && item.order > open.order && cardKey(item.pointer) === key) return false;
    }
  }
  return true;
}

export function projectCoachAlignmentCard(payload: Record<string, unknown>): void {
  const pointer = parseAlignmentCardPointer(payload.pointer);
  if (
    !pointer
    || typeof payload.request_id !== "string"
    || !/^[0-9a-f]{32}$/.test(payload.request_id)
    || !["turn_id", "user_id", "team_id", "workspace_id", "session_id"].every(
      (key) => typeof payload[key] === "string" && (payload[key] as string).length > 0 && (payload[key] as string).length <= 256,
    )
  )
    return;
  const turnId = payload.turn_id as string;
  const current = opens.get(turnId) ?? EMPTY;
  if (current.some((item) => item.requestId === payload.request_id)) return;
  order += 1;
  opens.set(turnId, [
    ...current.slice(-31),
    {
      pointer,
      requestId: payload.request_id,
      turnId,
      userId: payload.user_id as string,
      teamId: payload.team_id as string,
      workspaceId: payload.workspace_id as string,
      sessionId: payload.session_id as string,
      notice: payload.notice === "held_out_once_per_point" && pointer.kind === "agent_eval_alignment_card" ? "held_out_once_per_point" : null,
      order,
    },
  ]);
  if (opens.size > 256) opens.delete(opens.keys().next().value!);
  revision += 1;
  listeners.forEach((listener) => listener());
}

/** Test hook: forget every projected pointer. */
export function resetCoachAlignmentCardsForTest(): void {
  opens.clear();
  order = 0;
  revision += 1;
}

// ── Telling Coach to continue ─────────────────────────────────────────────

export interface CoachAlignmentContinueRequest {
  sessionId: string;
  /** The message sent to Coach (as "@Coach <text>") through the composer's ordinary Send path. */
  text: string;
  /** Called when a taken message could not be sent after all (Coach unavailable, the Owner started typing). */
  onUnsent?: () => void;
}

const continueListeners = new Set<(request: CoachAlignmentContinueRequest) => boolean>();

export function subscribeCoachAlignmentContinue(listener: (request: CoachAlignmentContinueRequest) => boolean): () => void {
  continueListeners.add(listener);
  return () => {
    continueListeners.delete(listener);
  };
}

/** Returns whether the open conversation took the message; false leaves the Owner to tell Coach themselves. */
export function requestCoachAlignmentContinue(request: CoachAlignmentContinueRequest): boolean {
  if (!request.sessionId || !request.text.trim()) return false;
  let taken = false;
  for (const listener of continueListeners) taken = listener(request) || taken;
  return taken;
}
