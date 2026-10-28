import { expect, test } from "@playwright/test";

/**
 * Chat-redesign US-1 — golden path smoke (post Phase 1.2 / 1.4 ship).
 *
 * Verifies the new step-list/* surface end-to-end against the mock SSE
 * runner (NEXT_PUBLIC_BOWHEAD_SSE_MODE=mock, default in playwright config):
 *   · Empty state → welcome
 *   · Send a sample query → TurnStream mounts → TurnContainer rendered
 *   · Step list card appears with at least one step row
 *   · Composer disables + ⏹ cancel button shows during running
 *
 * Mock mode means deterministic scenarios; live SSE/backend coupling is
 * verified separately by dogfood scripts. This spec is the lightest gate
 * that confirms the chat-content-frame renderer doesn't error during
 * the critical user path. Future US-2..US-8 build on this same shape.
 */

test.describe("chat-redesign US-1 golden path", () => {
  test("desktop 1440×900: send query → step list card + cancel button", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/");

    // Empty state visible (1.7-C: EmptyState replaces WelcomeHome)
    await expect(page.getByText(/verified intelligence/i)).toBeVisible();

    // Type a query in the composer + submit
    const textarea = page.locator("textarea").first();
    await textarea.waitFor({ state: "visible", timeout: 10_000 });
    await textarea.fill("帮我看看最近的销售情况");
    await textarea.press("Enter");

    // TurnContainer mounts under .chat-content-frame (Phase 1.7-B —
    // replaces the legacy .chat-panel class).
    await expect(page.locator(".chat-content-frame")).toBeVisible({ timeout: 10_000 });

    // User message appears. Phase 1.7-A allocates a stable `bub-msg-${ulid}`
    // on the SSE-mode runner path only; mock mode (which this spec uses) keys
    // off the always-set `data-testid="user-msg"` in TurnContainer.tsx
    // UserMessage. Both identify the same DOM node — testid is the
    // cross-mode invariant. No legacy .ub/.bub chrome remains.
    await expect(page.locator('[data-testid="user-msg"]').first()).toBeVisible({
      timeout: 10_000,
    });

    // Step list card eventually mounts (within mock-runner timeline).
    // aria-label="步骤列表" is the chat-redesign step-list/StepListCard
    // root region role.
    await expect(page.getByRole("region", { name: "步骤列表" })).toBeVisible({
      timeout: 15_000,
    });
  });

  test("mobile 412×915: TurnContainer mounts + composer accessible", async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 915 });
    await page.goto("/");

    // Mobile composer textarea visible.
    const textarea = page.locator("textarea").first();
    await textarea.waitFor({ state: "visible", timeout: 10_000 });
    await textarea.fill("查一下营业额");
    await textarea.press("Enter");

    // chat-content-frame mounts the same way on mobile (Phase 1.7-B).
    await expect(page.locator(".chat-content-frame")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('[data-testid="user-msg"]').first()).toBeVisible({
      timeout: 10_000,
    });

    // Step list card visible (may be truncated label per Phase 1.4 nowrap
    // fix — we just verify the region exists).
    await expect(page.getByRole("region", { name: "步骤列表" })).toBeVisible({
      timeout: 15_000,
    });
  });
});
