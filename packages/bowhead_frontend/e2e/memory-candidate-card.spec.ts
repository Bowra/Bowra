import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * memory-revive S1 · inline chat memory-candidate card e2e (mock mode + route interception).
 *
 * In-process real-LLM dogfood verifies backend generation/deduplication/recall; this spec verifies UI contracts:
 * after turn completion, polling the turn memory receipt API shows cards, renders at 412/1440 widths, then Remember/Ignore
 * decisions remove cards. Candidates and approve/reject use page.route interception (truth = API state,
 * matching hook delivery design, discovery §6.3).
 */

const CANDIDATE = {
  id: "cand_e2e_1",
  userId: "u_smoke",
  kind: "preference",
  key: "profitability_metric",
  value: "看盈利只认净利率，毛利率参考意义不大",
  reason: "从对话中识别到的口径偏好",
  sourceTurnId: null,
  status: "pending",
  decidedBy: null,
  decidedAt: null,
  createdAt: "2026-07-22T00:00:00Z",
  updatedAt: "2026-07-22T00:00:00Z",
  // Since 8bf6259e5 the server names where a candidate would be written; Remember writes to
  // default_target and is refused without one.
  default_target: {
    type: "space",
    space_id: "ws_smoke",
    space_name: "Smoke workspace",
    availability: "available",
    label: "Smoke workspace",
    requires_confirmation: false,
  },
  capabilities: { approve: true, reject: true, reason: null },
};

async function installCandidateRoutes(page: import("@playwright/test").Page, opts: { writeError?: string } = {}) {
  let decided = false;
  await page.route("**/api/v1/memory/turn-receipt**", async (route) => {
    // Receipt polling: return the candidate before decision, empty afterward (card truth = server state).
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        turn_id: new URL(route.request().url()).searchParams.get("turn_id"),
        explicit_request: null,
        pending_candidates: decided ? [] : [CANDIDATE],
        entries: [],
      }),
    });
  });
  await page.route("**/api/v1/memory/candidates**", async (route) => {
    const req = route.request();
    const url = req.url();
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    if (req.method() === "POST" && /\/approve/.test(url)) {
      decided = true;
      return json({
        ...CANDIDATE,
        status: "approved",
        write_error: opts.writeError ?? null,
        approved_entry_id: opts.writeError ? null : "mem_e2e_1",
      });
    }
    if (req.method() === "POST" && /\/reject/.test(url)) {
      decided = true;
      return json({ ...CANDIDATE, status: "rejected" });
    }
    return route.fallback();
  });
}

async function completeATurn(page: import("@playwright/test").Page) {
  await page.goto("/");
  const ta = page.locator("textarea").first();
  await ta.waitFor({ state: "visible", timeout: 15_000 });
  await ta.fill("帮我看看最近的销售情况");
  await ta.press("Enter");
  await expect(page.locator(".chat-content-frame")).toBeVisible({ timeout: 15_000 });
  // Rendering turn byline enters the completion sequence (mock running→done).
  await page.locator(".bh-bot-byline").first().waitFor({ state: "visible", timeout: 15_000 });
}

const VIEWPORTS = [
  { label: "mobile-412", w: 412, h: 915 },
  { label: "desktop-1440", w: 1440, h: 900 },
] as const;

for (const vp of VIEWPORTS) {
  test(`记忆候选卡：出现 + 记住决策 @ ${vp.label}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.w, height: vp.h });
    await mockAuthedApp(page);
    await installCandidateRoutes(page);
    await completeATurn(page);

    // Card arrives through polling (first poll at 2.5s; allow generous wait).
    const card = page.getByTestId("memory-candidate-card");
    await expect(card).toBeVisible({ timeout: 20_000 });
    await expect(card.getByText(/净利率/)).toBeVisible();
    await expect(card.getByTestId("memory-candidate-remember")).toBeEnabled();
    await expect(card.getByTestId("memory-candidate-dismiss")).toBeEnabled();

    // Remember → intercepted approve succeeds → the card settles in place: the question and its
    // actions are gone, and a "Remembered" receipt links to the memory settings page.
    await card.getByTestId("memory-candidate-remember").click();
    const receipt = card.getByTestId(`memory-candidate-settled-${CANDIDATE.id}`);
    await expect(receipt).toContainText("已记住", { timeout: 10_000 });
    await expect(card.getByTestId("memory-candidate-remember")).toHaveCount(0);
    await expect(card.getByTestId("memory-candidate-dismiss")).toHaveCount(0);
    await expect(receipt.getByTestId(`memory-candidate-undo-${CANDIDATE.id}`)).toBeVisible();
    await expect(receipt.getByRole("link")).toHaveAttribute("href", /\/settings\/memory\/$/);
  });
}

test("记忆候选卡：忽略决策", async ({ page }) => {
  await mockAuthedApp(page);
  await installCandidateRoutes(page);
  await completeATurn(page);

  const card = page.getByTestId("memory-candidate-card");
  await expect(card).toBeVisible({ timeout: 20_000 });
  await card.getByTestId("memory-candidate-dismiss").click();
  // "This time only" settles in place: nothing was remembered, so there is no Undo or link.
  const receipt = card.getByTestId(`memory-candidate-settled-${CANDIDATE.id}`);
  await expect(receipt).toContainText("仅本次生效", { timeout: 10_000 });
  await expect(receipt).toContainText("没有记住");
  await expect(card.getByTestId("memory-candidate-remember")).toHaveCount(0);
  await expect(receipt.getByRole("link")).toHaveCount(0);
});

test("记忆候选卡：写失败(满库)时候选保留可重试", async ({ page }) => {
  await mockAuthedApp(page);
  await installCandidateRoutes(page, { writeError: "notes_char_limit:3200" });
  await completeATurn(page);

  const card = page.getByTestId("memory-candidate-card");
  await expect(card).toBeVisible({ timeout: 20_000 });
  // Remember → backend write_error → card remains (review-fix P1: no false success).
  await card.getByTestId("memory-candidate-remember").click();
  await page.waitForTimeout(1500);
  await expect(card).toBeVisible();
});
