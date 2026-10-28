import { expect, test } from "@playwright/test";
import { existsSync } from "node:fs";

/**
 * Chat-redesign Phase 1.7-F — composition v8 fidelity spec.
 *
 * Verifies the DOM contract baked by Phase 1.7-B / 1.7-C / 1.7-D / 1.7-E /
 * 1.7-G against the v8 design intent. 12 concrete DOM assertions per
 * tech-design §8.1, run at desktop 1440×900 + mobile 412×915.
 *
 * Theme pinning (P0-r3.2): every test sets `localStorage.bowhead.theme=light`
 * BEFORE first paint via `addInitScript`, AND playwright.config.ts pins
 * `use.colorScheme: 'light'`. Without both, OS-dark CI hosts let the inline
 * theme script (app/layout.tsx) flip to dark via matchMedia → sage palette
 * → tests silently pass against the wrong baseline.
 *
 * Mock SSE mode (NEXT_PUBLIC_BOWHEAD_SSE_MODE=mock, set by playwright config
 * webServer.env) keeps the runner deterministic. In mock mode the user
 * bubble's stable `bub-msg-${ulid}` (Phase 1.7-A) is gated on the SSE-mode
 * runner path — mock runner doesn't allocate one — so the assertions key
 * off `[data-testid="user-msg"]` (always set in TurnContainer.tsx) instead
 * of `[data-msg-id^="bub-msg-"]`. Both selectors identify the same DOM
 * node; the testid is the cross-mode invariant.
 *
 * The 12 assertions are bundled into two tests per viewport to reduce
 * dev-server thrash (24 navigations → 4). The "landing" test covers
 * assertions that hit the empty page; the "post-query" test covers the
 * rest including the archived-turn parity check (assertion 11) which
 * requires two consecutive turns.
 *
 * @fidelity-tagged tests at bottom capture full-page screenshots for
 * visual regression (advisory only, non-blocking — `playwright test
 * --grep-invert "@fidelity"` excludes them from default CI run).
 */

const QUERY_1 = "帮我看看最近的销售情况";

async function pinLightTheme(page: import("@playwright/test").Page) {
  // Runs BEFORE any page script (including the inline theme bootstrap at
  // app/layout.tsx:39-43). The bootstrap reads bowhead.theme; setting it to
  // 'light' makes the bootstrap skip the dark-flip branch entirely (matches
  // neither 't==="dark"' nor 't==null', so prefers-color-scheme check skipped).
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem("bowhead.theme", "light");
    } catch {
      /* no-op (some test contexts disallow localStorage; pinning then falls
         back to playwright.config colorScheme:'light' which still works) */
    }
  });
}

const VIEWPORTS: Array<{ name: string; width: number; height: number }> = [
  { name: "desktop-1440x900", width: 1440, height: 900 },
  { name: "mobile-412x915", width: 412, height: 915 },
];

function shouldUpdateAdvisorySnapshots() {
  return (
    process.env.BOWHEAD_UPDATE_FIDELITY_SNAPSHOTS === "1" ||
    process.argv.some(
      (arg) =>
        arg === "--update-snapshots" || arg.startsWith("--update-snapshots="),
    )
  );
}

for (const vp of VIEWPORTS) {
  test.describe(`chat-composition v8 fidelity — ${vp.name}`, () => {
    test.beforeEach(async ({ page }) => {
      await pinLightTheme(page);
      await page.setViewportSize({ width: vp.width, height: vp.height });
    });

    test("landing-page assertions (3 + 7 + 12): no .step-list-card wrapper, EmptyState + verified-intelligence, --c-accent terracotta", async ({ page }) => {
      await page.goto("/");

      // ── Assertion 7 — EmptyState mounted ("verified intelligence" copy).
      await expect(page.getByText(/verified intelligence/i)).toBeVisible({
        timeout: 10_000,
      });

      // ── Assertion 12 — light-mode token resolved. With theme pinned the
      // computed `--c-accent` value MUST be #b04a2a (terracotta). Dark mode
      // (sage) would yield #62B89C — that's the failure signal we want to
      // catch if theme pinning regresses on OS-dark CI hosts.
      const accent = await page.evaluate(() => {
        const el = document.documentElement;
        return getComputedStyle(el).getPropertyValue("--c-accent").trim();
      });
      expect(accent.toLowerCase()).toMatch(/^(#b04a2a|rgb\(176,\s*74,\s*42\))$/);

      // ── Assertion 3 — no legacy .step-list-card header wrapper. On the
      // landing page no turn has rendered yet, but a regression that
      // re-introduced a global `section.step-list-card` (e.g. a leftover
      // skeleton wrapper) would surface here too.
      await expect(page.locator("section.step-list-card")).toHaveCount(0);
    });

    test("post-query assertions (1+2+4+5+6+8+9+10+11): byline + frame + composer + steer + archived parity", async ({ page }) => {
      await page.goto("/");

      // Send first turn.
      const textarea = page.locator("textarea").first();
      await textarea.waitFor({ state: "visible", timeout: 10_000 });
      await textarea.fill(QUERY_1);
      await textarea.press("Enter");

      // chat-content-frame mounts after dispatch.
      await expect(page.locator(".chat-content-frame")).toBeVisible({
        timeout: 10_000,
      });

      // Wait for byline + step region to render (downstream assertions key
      // off these).
      const byline = page.locator(".bh-bot-byline").first();
      await byline.waitFor({ state: "visible", timeout: 10_000 });
      const stepRegion = page.getByRole("region", { name: "步骤列表" }).first();
      await stepRegion.waitFor({ state: "visible", timeout: 15_000 });

      // ── Assertion 1 — BotByline visible.
      await expect(byline).toBeVisible();

      // ── Assertion 2 — BotByline precedes first StepRow in DOM order.
      const order = await page.evaluate(
        ([a, b]) => {
          if (!a || !b) return 0;
          // DOCUMENT_POSITION_FOLLOWING (4) — b comes after a in document
          // order. a.compareDocumentPosition(b) returns a bitmask where
          // bit 2 (=4) means b follows a.
          return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING;
        },
        [
          await byline.elementHandle(),
          await stepRegion.elementHandle(),
        ] as const,
      );
      expect(order).toBeTruthy();

      // ── Assertion 4 — content frame max-width = min(920px, 100%).
      // chat-polish-r2 G6 (2026-05-26): bumped from 740 to 920 for wide screens;
      // min() formula keeps grid-track-aware sizing when canvas opens.
      const frame = page.locator(".chat-content-frame").first();
      // 920 is the cap on viewport ≥ 920+48 padding; below that, frame = 100% of parent.
      // At viewport 1440 (test default) and canvas closed, frame caps at 920.
      await expect(frame).toHaveCSS("max-width", "min(920px, 100%)");
      const frameBox = await frame.boundingBox();
      expect(frameBox?.width ?? 0).toBeLessThanOrEqual(
        Math.min(920, vp.width) + 0.5,
      );

      // ── Assertion 5 — legacy classes purged.
      await expect(
        page.locator(".chat-panel, .tline, .ub, .bub, .ab"),
      ).toHaveCount(0);

      // ── Assertion 6 — no CanvasHandoffPill in tree.
      await expect(
        page.locator('[data-testid="canvas-handoff-pill"]'),
      ).toHaveCount(0);

      // ── Assertion 8 — SteerMessage mount-point lives OUTSIDE
      // .chat-content-frame. workspace.tsx renders .bh-steer-mount as
      // sibling of .v2-messages (which holds .chat-content-frame). Mock
      // scenarios don't emit emoji_ack so .bh-steer-mount may not render
      // at all — the negative assertion still catches any future
      // regression that accidentally re-nests SteerMessage back into the
      // BotTurn scroll body.
      await expect(
        page.locator(".chat-content-frame .bh-steer-mount"),
      ).toHaveCount(0);

      // ── Assertion 9 — Composer border-radius = 14px (.v2-composer).
      await expect(page.locator(".v2-composer").first()).toHaveCSS(
        "border-radius",
        "14px",
      );

      // ── Assertion 10 — UserBubble no background fill. Mock runner
      // doesn't allocate `bub-msg-${ulid}` (1.7-A is SSE-mode only), so
      // we key off `data-testid="user-msg"` which is the cross-mode
      // invariant set in TurnContainer.tsx UserMessage.
      const ub = page.locator('[data-testid="user-msg"]').first();
      await ub.waitFor({ state: "visible", timeout: 10_000 });
      await expect(ub).toHaveCSS(
        "background-color",
        /rgba\(0, 0, 0, 0\)|transparent/,
      );

      // ── Assertion 11 — Archived turn DOM parity contract.
      // Mock and real SSE completed turns share the same archive projection.
      // Wait for the folded archived summary, then expand it and verify the
      // same byline + trace structure that was visible while running.
      const summary = page.getByTestId("step-summary").first();
      await expect(summary).toBeVisible({ timeout: 15_000 });
      await expect(page.locator('.bh-bot-block[data-archived="false"]')).toHaveCount(0);
      const archivedBlock = page.locator('.bh-bot-block[data-archived="true"]').first();
      await expect(archivedBlock).toHaveCount(1);
      await expect(archivedBlock.locator(".bh-bot-byline")).toHaveCount(1);
      await summary.click();
      await expect(
        archivedBlock.getByRole("region", { name: "步骤列表" }),
      ).toHaveCount(1);
    });
  });
}

/**
 * @fidelity — full-page pixel diff snapshots (advisory, non-blocking).
 *
 * Missing baselines are skipped in default `playwright test` runs. Invoke
 * explicitly with `BOWHEAD_UPDATE_FIDELITY_SNAPSHOTS=1 playwright test
 * --grep @fidelity --update-snapshots` to capture baselines, then
 * `--grep @fidelity` (no flag) to diff against them.
 * Pixel failures only log a warning — the assertion is wrapped to never
 * fail CI.
 *
 * Frozen-time fixture: Date.now() stubbed at a fixed epoch + ulid seeded
 * via Math.random override so mock-scenario timing + bubble IDs are
 * deterministic.
 */
test.describe("@fidelity chat-composition-v8 pixel diff (advisory)", () => {
  test.beforeEach(async ({ page }) => {
    await pinLightTheme(page);
    // Frozen-time + seeded Math.random for deterministic snapshots.
    await page.addInitScript(() => {
      const FROZEN_EPOCH = 1716422400000; // 2024-05-23T00:00:00Z, arbitrary stable point
      const _origDateNow = Date.now;
      Date.now = () => FROZEN_EPOCH;
      // Seeded Math.random — Mulberry32 PRNG
      let seed = 0x12345678;
      Math.random = () => {
        seed = (seed + 0x6D2B79F5) | 0;
        let t = seed;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
      (window as unknown as { __origDateNow?: () => number }).__origDateNow =
        _origDateNow;
    });
  });

  for (const vp of VIEWPORTS) {
    test(`pixel snapshot — ${vp.name} (advisory)`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto("/");

      const textarea = page.locator("textarea").first();
      await textarea.waitFor({ state: "visible", timeout: 10_000 });
      await textarea.fill(QUERY_1);
      await textarea.press("Enter");
      await expect(page.locator(".chat-content-frame")).toBeVisible({
        timeout: 10_000,
      });
      await page
        .getByRole("region", { name: "步骤列表" })
        .first()
        .waitFor({ state: "visible", timeout: 15_000 });

      // Settle any post-mount layout shifts.
      await page.waitForTimeout(800);

      const snapshotName = `phase-1.7-baseline-pixel-${vp.name}.png`;
      const expectedSnapshotPath = testInfo.snapshotPath(snapshotName, {
        kind: "screenshot",
      });
      test.skip(
        !existsSync(expectedSnapshotPath) && !shouldUpdateAdvisorySnapshots(),
        `[@fidelity] missing advisory baseline for ${vp.name}`,
      );

      try {
        // Non-blocking: 0.1% pixel diff allowed.
        await expect(page).toHaveScreenshot(snapshotName, {
          fullPage: true,
          maxDiffPixelRatio: 0.001,
          animations: "disabled",
        });
      } catch (err) {
        // Advisory only — log + continue. Pixel drift is signal, not a
        // hard gate. Phase 1.7-F is a DOM-contract sprint; pixel diff
        // requires a curated baseline checked in separately.
        // eslint-disable-next-line no-console
        console.warn(
          `[@fidelity] ${vp.name} pixel diff non-zero (advisory only):`,
          err instanceof Error ? err.message.split("\n")[0] : err,
        );
      }
    });
  }
});
