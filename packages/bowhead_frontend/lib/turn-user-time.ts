/**
 * User-message bubble time = submission time for this turn, unchanged by completion or clarification resume.
 *
 * Observed on 116: sent at 07:46, but the completed bubble showed “你 · 07:53”; archived turns previously used
 * `archivedAt` (= completion time) for user bubbles. Priority:
 *   1. turn-store `startedAt` — first `turn_started.started_at` (only the first counts;
 *      same-turn resumes never overwrite it); the event is persisted, so live and refresh replay use the same value;
 *   2. Submission time (live: when sent; hydrate: `turns.started_at`), until the event arrives;
 *   3. Caller's old fallback (old archived snapshots lack both).
 */
export function resolveUserMessageTimeMs(input: {
  firstTurnStartedAt?: Date | null;
  submittedAtMs?: number | null;
  fallbackMs?: number | null;
}): number | null {
  const first = input.firstTurnStartedAt?.getTime();
  if (typeof first === "number" && Number.isFinite(first)) return first;
  const submitted = input.submittedAtMs;
  if (typeof submitted === "number" && Number.isFinite(submitted)) return submitted;
  const fallback = input.fallbackMs;
  if (typeof fallback === "number" && Number.isFinite(fallback)) return fallback;
  return null;
}
