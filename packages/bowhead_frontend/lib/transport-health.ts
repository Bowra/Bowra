/**
 * transport-health — single measurement source for whether bytes are still arriving.
 *
 * An intermediary may buffer an SSE stream until completion. Server writes can succeed even when
 * the browser receives no bytes during execution, so delivery health must be measured in the browser.
 *
 * ⚠ This module **measures and judges only, making no UI decisions**. Three consumers share it:
 *   ① Current row stops inventing progress (only `msSinceLastEvent`, **no attribution**, hence no false attribution)
 *   ② Administrator troubleshooting diagnostics (`verdict`, **attribution**, all guards live here)
 *   ③ Delivery-time self-check
 *
 * ★ Why `done.duration_ms` ÷ event-arrival span, not heartbeat arrival gaps:
 * heartbeat gaps are fragile: background tabs / busy main thread / network jitter / blocked server loop all mimic them.
 * `turn_started` always emits at turn start: healthy transport delivers it then ⇒ span ≈ duration ⇒ ratio ≈ 1;
 * buffered delivery sends it with done, shrinking the observed span and increasing the duration-to-span ratio.
 * No clock synchronization required: server computes duration_ms as **duration**, not a timestamp; span is a local timestamp difference.
 */

import { staticTranslator } from "./locale-messages";

/** Healthy transport has a ratio near one; require a clear margin before reporting buffering. */
export const BUFFERED_RATIO_THRESHOLD = 4;

/**
 * Silence duration before **stopping invented progress** and stating facts.
 *
 * ★ Why 30s is safe: backend guarantees keepalive every 15s, and keepalive **counts as
 * incoming bytes** (see `noteEvent` wiring). A healthy path thus never reaches 30s:
 * reaching it means **even heartbeats are missing**, genuinely silent transport, not slow backend work.
 * ⇒ Almost no room for false positives, justifying its first position in the decision chain.
 */
export const NO_SIGNAL_MS = 30_000;

/**
 * Minimum duration for a verdict; below it abstain, but **far lower than the first version**.
 *
 * ❗ Initially 30s because short turns supposedly lack span to compare: **that reasoning was backward**.
 * A synthetic eight-second turn delivered within 50ms has a 160× ratio.
 * A large minimum-duration floor would hide such a strong signal merely because the turn is short.
 * ⇒ **Ratio** supplies confidence; minimum duration only ensures meaningful absolute silence.
 *
 * Why 5s: ratio ≥4 requires a 5s turn's arrival span ≤1.25s,
 * meaning at least 3.75s with no bytes before clustered delivery. Already abnormal on a healthy path.
 * ★ Lesson: **my fixtures were all long turns, covering none of the real short turns**
 * (`feedback_my_own_fixtures_are_the_happy_path`, the Nth recurrence in this repo).
 */
export const MIN_TURN_MS_FOR_VERDICT = 5_000;

export type TransportVerdict =
  | { kind: "ok"; ratio: number }
  | { kind: "buffered"; ratio: number }
  /** Evidence insufficient for attribution: **abstain, never call it healthy**. */
  | { kind: "unknown"; reason: "hidden" | "too_short" | "reconnected" | "no_events" | "mid_turn" };

export interface TransportSample {
  firstEventAt: number | null;
  lastEventAt: number | null;
  keepaliveArrivals: number[];
  reconnects: number;
  hiddenAtSomePoint: boolean;
  /** Mid-turn attachment (F5 / hydrate running turn / follow-up stream).
   *  Sample starts at replay, while `done.duration_ms` covers the **entire turn**;
   *  truncated span inflates the ratio. **Must abstain**, with no alternative remedy. */
  attachedMidTurn: boolean;
  /** Backend `done.duration_ms` (server-stamped duration). Store in the sample instead of every consumer
   *  retrieving it separately: `runner.summary.total` is a **formatted display string** ("3.8s"),
   *  reverse parsing would turn data retrieval into copy parsing. */
  doneDurationMs: number | null;
}

export function createTransportSample(): TransportSample {
  return {
    firstEventAt: null,
    lastEventAt: null,
    keepaliveArrivals: [],
    reconnects: 0,
    hiddenAtSomePoint: false,
    attachedMidTurn: false,
    doneDurationMs: null,
  };
}

export function noteEvent(s: TransportSample, at: number): void {
  if (s.firstEventAt === null) s.firstEventAt = at;
  s.lastEventAt = at;
}

export function noteKeepalive(s: TransportSample, at: number): void {
  s.keepaliveArrivals.push(at);
}

/** Time since last inbound byte. **Fall back to turn start if nothing has ever arrived**, otherwise
 *  the very case needing a notice (zero events) returns null and leaves the gate blind. */
export function msSinceLastEvent(s: TransportSample, now: number, turnStartedAt: number | null): number | null {
  const anchor = s.lastEventAt ?? turnStartedAt;
  if (anchor === null) return null;
  return Math.max(0, now - anchor);
}

/**
 * Arrival gaps between consecutive live heartbeats (ms). Server guarantees 15s send gaps, so tens-of-ms arrival gaps indicate accumulation.
 * ⚠ **Corroboration only**, never conclusive alone (see header). Exclude the first: its distance from connection was never 15s.
 */
export function keepaliveGaps(s: TransportSample): number[] {
  const gaps: number[] = [];
  for (let i = 1; i < s.keepaliveArrivals.length; i++) {
    gaps.push(s.keepaliveArrivals[i] - s.keepaliveArrivals[i - 1]);
  }
  return gaps;
}

/**
 * Retrospective verdict. **Guards first**: if any fails, return `unknown`, never guess.
 *
 * @param doneDurationMs Backend `done.duration_ms` (server-stamped duration)
 */
export function verdictOf(s: TransportSample, doneDurationMs: number): TransportVerdict {
  // Background/frozen page accumulates handlers then fires together, **identical** to buffering.
  // Most likely false-positive source, indistinguishable afterward; invalidate the entire sample.
  if (s.hiddenAtSomePoint) return { kind: "unknown", reason: "hidden" };
  // Reconnect replays from cursor, clustering historical events with the same shape as buffering.
  if (s.reconnects > 0) return { kind: "unknown", reason: "reconnected" };
  // Mid-turn attachment: numerator is whole-turn duration, denominator only post-attachment span; incomparable scales.
  if (s.attachedMidTurn) return { kind: "unknown", reason: "mid_turn" };
  if (doneDurationMs < MIN_TURN_MS_FOR_VERDICT) return { kind: "unknown", reason: "too_short" };
  if (s.firstEventAt === null || s.lastEventAt === null) return { kind: "unknown", reason: "no_events" };

  const spread = Math.max(1, s.lastEventAt - s.firstEventAt);
  const ratio = doneDurationMs / spread;
  return ratio >= BUFFERED_RATIO_THRESHOLD ? { kind: "buffered", ratio } : { kind: "ok", ratio };
}


/** Plain-language transport diagnostic for troubleshooting copy; self-contained sample already holds duration.
 *
 * ⚠ **Attribution happens only here**, protected by every `verdictOf` guard.
 * With insufficient evidence, honestly say「未判定」+ reason, **never「正常」**:
 * reporting unmeasured as healthy is precisely this sprint's target problem.
 */
export function describeTransport(s: TransportSample): string {
  const t = staticTranslator("transport_health");
  if (s.doneDurationMs === null) return t("unfinished");
  const v = verdictOf(s, s.doneDurationMs);
  const spread = s.firstEventAt !== null && s.lastEventAt !== null
    ? s.lastEventAt - s.firstEventAt
    : null;
  const shape = spread === null
    ? ""
    : t("shape", {
      backend: (s.doneDurationMs / 1000).toFixed(1),
      spread: (spread / 1000).toFixed(1),
      reconnects: s.reconnects,
    });
  switch (v.kind) {
    case "buffered":
      return t("buffered", { ratio: v.ratio.toFixed(1), shape });
    case "ok":
      return t("ok", { ratio: v.ratio.toFixed(1), shape });
    case "unknown":
      return t("unknown", { why: t(`unknown_reasons.${v.reason}`), shape });
  }
}
