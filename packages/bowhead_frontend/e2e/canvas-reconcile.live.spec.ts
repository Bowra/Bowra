import { expect, test } from "@playwright/test";

/**
 * Terminal reconciliation — real-stack live skeleton (canvas-comprehensive Batch 1 WP-0/FR-1.7b).
 *
 * Follow the live-profile pattern in nu1-kb-ref-citation.spec.ts: skip by default; run only against a real stack
 * (BOWHEAD_E2E_PROFILE=live + BOWHEAD_RUN_REAL_E2E=1 + real workspace/session env values).
 * The deterministic version (mock engine + dropped-frame injection) is in e2e/canvas-cc/canvas-reconcile.spec.ts (WP-2).
 *
 * Real-stack purpose: run one report turn in a real workspace/session on 116, capture the terminal browser
 * __CANVAS_PROJECTION__ → page.reload() → capture projection again → normalized diff==0 (live==F5).
 * For real-stack integration (BASE_URL / JWT cookie / workspace / session), see sprint ship-gate-batch1.md.
 */

const RUN = process.env.BOWHEAD_RUN_REAL_E2E === "1";

test.describe("canvas reconcile · live", () => {
  test.skip(!RUN, "live-only: set BOWHEAD_RUN_REAL_E2E=1 + real-stack env");

  test("live report round: live projection == F5-hydrate projection (diff==0)", async ({ page }) => {
    const baseURL = process.env.BOWHEAD_LIVE_BASE_URL;
    expect(baseURL, "BOWHEAD_LIVE_BASE_URL required for live run").toBeTruthy();
    // Skeleton — real-stack auth/workspace/session wiring is filled in at ship-gate
    // time per the runbook. Kept as a compiling placeholder so the live profile has
    // a home for the FR-1.7b assertion without blocking Batch 1 deterministic tests.
    test.info().annotations.push({ type: "todo", description: "wire real-stack login + report query per ship-gate-batch1.md" });
  });
});
