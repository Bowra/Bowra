import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Regression guard for the P0 found in the whole-session review (2026-06-25):
 * the `wire()` EventSource whitelist in use-turn-runner.ts registers one
 * `es.addEventListener(name, …)` per name, and the browser SILENTLY DROPS any
 * event whose name isn't registered. The 7 plan-card SSE events were missing →
 * `handle()` never fired → `plan_proposed` never seeded the card → the feature
 * was non-functional in a real browser (curl SSE + the static mockup both bypass
 * this listener, which is why it slipped per-track review + dogfood).
 *
 * This asserts every plan event the dispatcher (turn-store-dispatch.ts) routes
 * is present in the whitelist array, so a future edit can't silently re-break it.
 */
const PLAN_EVENTS = [
  "plan_proposed",
  "plan_running",
  "plan_completed",
  "plan_blocked",
  "plan_failed",
  "step_blocked",
  "step_failed",
] as const;

describe("EventSource whitelist covers plan-card events", () => {
  const src = readFileSync(join(__dirname, "use-turn-runner.ts"), "utf8");
  // Extract just the addEventListener whitelist array. Anchor on the unique
  // comma-separated sequence (the dispatch calls use single quoted names, not
  // this triple) so we test the listener registration, not an incidental mention.
  const start = src.indexOf('"step_started", "step_progress", "step_done"');
  const end = src.indexOf("as const).forEach", start);
  const whitelist = start >= 0 && end > start ? src.slice(start, end) : "";

  it("extracted the whitelist region", () => {
    expect(whitelist.length).toBeGreaterThan(0);
  });

  for (const ev of PLAN_EVENTS) {
    it(`registers a listener for "${ev}"`, () => {
      expect(
        whitelist.includes(`"${ev}"`),
        `EventSource whitelist is missing "${ev}" → browser will silently drop it`,
      ).toBe(true);
    });
  }
});
