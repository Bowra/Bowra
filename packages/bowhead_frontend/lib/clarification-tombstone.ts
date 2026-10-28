/**
 * dingsiyan #11 — answered-clarification tombstones (turn-level truth, surviving attachStream).
 *
 * Why: after resume, attachStream reconnects and replays full event history; old
 * `clarification_required` restores the card as unanswered. The inverse event intended to remove it,
 * `clarification.resolved`, always failed in production before the delivery fix (emit_out_of_band)
 * because sequence collisions caused deduplication. Even after the fix, **client correctness** must not depend on
 * guaranteed delivery. Tombstones provide a second frontend truth: successful clicks (local optimism +
 * resolve HTTP response) mark them; a replayed `clarification_required` hitting a tombstone
 * renders an answered summary instead of restoring the card.
 *
 * key = requestId (globally unique UUID; θ heuristic payload has no turn_id,
 * so bucketing by turnId is impossible). Module-level Map + FIFO cap shares
 * `turn-store-dispatch._appliedSeqByTurn`'s approach.
 *
 * ⚠ Undeclared invariant (review FE-P2-5): optimistic marks / memo / clarification_required replay
 * use **`clarification_id`** as key, while `clarification.resolved` / `turn.resumed` use
 * **`request_id`**. They are currently identical (backend θ turn-terminating payload
 * `clarification_id == request_id`, pinned by `test_lead_hitl_fix_ch1_ch2.py`). If backend
 * separates them, resolved/resumed tombstones mark the wrong key; clarification_required
 * replay misses it and restores the repeated-click failure. Align keys here before changing those backend fields.
 *
 * Reads have no subscription mechanism: each write accompanies runner/component state changes (resolveWith's
 * setResolving, runner reducer's setState), so rerenders naturally read fresh values.
 * New write sites must preserve this accompaniment.
 */

export interface AnsweredClarification {
  /** User-selected values for summary rendering; empty array on skip/dismiss. */
  values: string[];
  /** Skip the entire card ("让模型自己判断"). */
  skipped?: boolean;
  /** 「换个问法」dismiss: prevent restoration without rendering a summary row. */
  dismissed?: boolean;
}

const _answered = new Map<string, AnsweredClarification>();
const _MAX_ENTRIES = 128;

export function markClarificationAnswered(
  requestId: string | null | undefined,
  info: AnsweredClarification,
): void {
  if (!requestId) return;
  if (_answered.size >= _MAX_ENTRIES && !_answered.has(requestId)) {
    const oldest = _answered.keys().next().value;
    if (oldest !== undefined) _answered.delete(oldest);
  }
  _answered.set(requestId, info);
}

/** Undo the optimistic mark on resolve failure (return card to pending for retry). */
export function unmarkClarificationAnswered(
  requestId: string | null | undefined,
): void {
  if (requestId) _answered.delete(requestId);
}

export function getAnsweredClarification(
  requestId: string | null | undefined,
): AnsweredClarification | null {
  if (!requestId) return null;
  return _answered.get(requestId) ?? null;
}

export function isClarificationAnswered(
  requestId: string | null | undefined,
): boolean {
  return Boolean(requestId && _answered.has(requestId));
}

/** For tests only. */
export function _resetClarificationTombstonesForTest(): void {
  _answered.clear();
}
