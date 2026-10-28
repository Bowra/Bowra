"use client";

/**
 * turn-elapsed — the **single** algorithm for how long this turn has been running.
 *
 * ## Why it is needed (116 dogfood 2026-08-13 observation)
 *
 * `runner.startedAt` comes from backend `turn_started.started_at` (**server clock**),
 * while `Date.now()` uses the **browser clock**. On 116, the server was **31.4 seconds ahead**:
 *
 * ```
 * server /health timestamp : 1786624285000
 * browser Date.now()       : 1786624253622   → skew +31378ms
 * ```
 *
 * Thus `Date.now() - startedAt` stayed **negative** throughout the skew window and `Math.max(0, …)`
 * clamped it to 0: the band **showed「0 秒」throughout a real 19-second turn** (verified frame by frame).
 * PRD required「第 7 步 · 3 分 25 秒」but rendered「第 8 步 · 0 秒」— false reporting, not missing information.
 *
 * ⚠ **Local development can never reproduce this**: frontend/backend share a machine and skew is always 0.
 * Likewise for unit tests. Only actual device dogfood could catch this.
 *
 * ## Solution
 *
 * Future `startedAt` ⇒ server ahead of browser ⇒ anchor to when **this turn was first seen locally**.
 * Cost: F5 halfway through a turn undercounts by one skew (31 seconds), but **never** produces the obviously false
 * frozen-zero duration. The real cure is global clock-skew correction, requiring shared infrastructure changes outside this sprint.
 */

/** Anchor table: key → local anchor. Bounded to avoid unlimited growth in long sessions. */
const anchors = new Map<string | number, number>();
const ANCHOR_CAP = 32;

export function turnElapsedMs(
  startedAt: number | null | undefined,
  now: number = Date.now(),
  /**
   * Anchor key. **Defaults to `startedAt`, but that changes within a turn**, so always provide turnId when available:
   *
   * ★ On 116 v1.1.6 (user reported「一上来就十秒」), displayed seconds within one turn went
   *   `0 → 1 → **0** → 1 → 2`, moving **backward**. Cause: `startedAt` **mixes two clocks**:
   *     · at submission, `use-turn-runner.ts:447` writes `Date.now()` (**browser** clock);
   *     · when `turn_started` arrives, `:2194` overwrites it with `Date.parse(payload.started_at)`,
   *       the **server** clock (deliberate per comments: duration follows the latest execution generation).
   *   Two values = two keys = two anchors; arrival of the second value therefore **reanchors**:
   *     · server **ahead** (116 measured +32.5s) ⇒ new anchor = now ⇒ seconds **jump back to 0**;
   *     · server **behind** ⇒ new anchor = startedAt ⇒ seconds **immediately jump to the skew**,
   *       matching the user's「一上来就十秒，实际才过去一两秒」report.
   *   A key **stable across clock replacement** shares one anchor per execution generation, preventing a second reanchor.
   *
   * ⚠ **Key must be `turnId#executionGeneration`, never just turnId** (caught by independent review):
   *   clarification resume resets `startedAt` on the **same turnId**
   *   (`use-turn-runner.ts:1229`; comments prohibit paused-generation duration leaking in).
   *   Using only turnId preserves the first anchor, counting the user's 3-minute clarification response in the second generation;
   *   measured `turnElapsedMs(resumeAt, resumeAt, "T")` returned 200000 instead of 0.
   */
  key: string | number | null = null,
): number {
  if (!startedAt) return 0;
  const anchorKey = key ?? startedAt;
  let anchor = anchors.get(anchorKey);
  if (anchor === undefined) {
    anchor = startedAt > now ? now : startedAt;
    anchors.set(anchorKey, anchor);
    if (anchors.size > ANCHOR_CAP) {
      const oldest = anchors.keys().next().value;
      if (oldest !== undefined) anchors.delete(oldest);
    }
  }
  return Math.max(0, now - anchor);
}

/** For tests: clear the anchor cache (module state otherwise leaks across cases). */
export function _resetTurnElapsedAnchorsForTesting(): void {
  anchors.clear();
}

/**
 * The **single** construction site for timer anchor keys: `turnId#executionGeneration`.
 *
 * Do not assemble them separately at call sites: omitting generation silently leaks wall time from the previous generation.
 */
export function elapsedAnchorKeyOf(
  turnId: string | null | undefined,
  executionGeneration: number | null | undefined,
): string | null {
  return turnId ? `${turnId}#${executionGeneration ?? 0}` : null;
}
