import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * Shared-request ownership guard.
 *
 * Every browser-side resource that more than one mounted component needs
 * (identity, spaces, session list, home snapshot) has exactly one owner. Other
 * components subscribe to that owner instead of issuing their own request, so a
 * cold page load asks the gateway once per resource.
 *
 * Counts are per *cold load*, before any refresh interval elapses. The spec runs
 * in the gate profile (SSE_MODE=mock), where the session list is served from
 * fixtures — session-list deduplication is covered by
 * `__tests__/lib/shared-data-ownership.test.tsx` instead, which selects the
 * API-backed branch explicitly.
 */

const sessionUser = {
  id: "u_dedup",
  email: "dedup@example.com",
  display_name: "Dedup User",
  avatar_url: null,
  team_id: "team_dedup",
  tier: "pro",
  role: "member",
  team_role: "member",
  status: "active",
  email_verified: true,
  credit_balance: 1_000,
  credit_quota: 10_000,
  locale: "zh-CN",
  timezone: "Asia/Singapore",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
  capabilities: {
    workflow_creator: true,
    subagent_creator: true,
    access_requests: true,
  },
};

/**
 * Paths whose repeats are meaningful rather than duplicated ownership:
 * telemetry posts carry distinct payloads, and the file browser pages through
 * its own list with distinct query strings.
 */
const REPEAT_ALLOWED = [
  "/api/v1/metrics/event",
  "/api/v1/files/",
  "/api/v1/files/usage",
];

interface Counter {
  counts: Map<string, number>;
  urls: string[];
}

function countApiRequests(page: Page): Counter {
  const counter: Counter = { counts: new Map(), urls: [] };
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/v1/")) return;
    counter.urls.push(url.pathname + url.search);
    counter.counts.set(
      url.pathname,
      (counter.counts.get(url.pathname) ?? 0) + 1,
    );
  });
  return counter;
}

async function mockedAuthedPage(page: Page) {
  await mockAuthedApp(page);
  await page.unroute("**/api/v1/auth/me");
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(sessionUser),
    });
  });
  await page.unroute("**/api/v1/sessions**");
  await page.route("**/api/v1/sessions**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ groups: [], archived_count: 0 }),
    });
  });
}

function report(counter: Counter): string {
  const summary = [...counter.counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([path, n]) => `${n}× ${path}`)
    .join("\n");
  // Full URLs for whatever repeated: the query string is usually the reason.
  const repeated = [...counter.counts.entries()]
    .filter(([, n]) => n > 1)
    .map(([path]) => path);
  const detail = counter.urls.filter((url) =>
    repeated.some((path) => url.startsWith(path)),
  );
  return `${summary}\n\nrepeated request URLs:\n${detail.join("\n")}`;
}

function assertNoDuplicates(counter: Counter) {
  const detail = report(counter);
  const duplicated = [...counter.counts.entries()]
    .filter(([path, n]) => n > 1 && !REPEAT_ALLOWED.includes(path))
    .map(([path, n]) => `${n}× ${path}`);
  expect(duplicated, detail).toEqual([]);
}

test("cold home load requests each shared resource once", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockedAuthedPage(page);
  const counter = countApiRequests(page);

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { level: 1, name: "你的工作进展" }),
  ).toBeVisible({ timeout: 20_000 });
  // Settle: every mount-time effect has run, no refresh interval yet.
  await page.waitForTimeout(3_000);

  const detail = report(counter);
  expect.soft(counter.counts.get("/api/v1/auth/me") ?? 0, detail).toBe(1);
  expect
    .soft(counter.counts.get("/api/v1/auth/me/workspaces") ?? 0, detail)
    .toBe(1);
  expect.soft(counter.counts.get("/api/v1/home/snapshot") ?? 0, detail).toBe(1);
  assertNoDuplicates(counter);
});

test("returning user with a stored space loads the home snapshot once", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockedAuthedPage(page);
  // A returning browser already knows its space, so the snapshot fetcher has a
  // workspace id on its very first render — before the space *name* resolves.
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.space_id", "ws_smoke");
    window.localStorage.setItem("bowhead.workspace_id", "ws_smoke");
  });
  const counter = countApiRequests(page);

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { level: 1, name: "你的工作进展" }),
  ).toBeVisible({ timeout: 20_000 });
  await page.waitForTimeout(3_000);

  const detail = report(counter);
  expect.soft(counter.counts.get("/api/v1/home/snapshot") ?? 0, detail).toBe(1);
  expect
    .soft(counter.counts.get("/api/v1/home/proposals") ?? 0, detail)
    .toBe(1);
  expect
    .soft(counter.counts.get("/api/v1/auth/me/workspaces") ?? 0, detail)
    .toBe(1);
  assertNoDuplicates(counter);
});

test("cold files-page load requests each shared resource once", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockedAuthedPage(page);
  await page.route("**/api/v1/files**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], total: 0 }),
    });
  });
  const counter = countApiRequests(page);

  await page.goto("/files/", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".unified-sidebar")).toBeVisible({
    timeout: 20_000,
  });
  await page.waitForTimeout(3_000);

  const detail = report(counter);
  expect.soft(counter.counts.get("/api/v1/auth/me") ?? 0, detail).toBe(1);
  expect
    .soft(counter.counts.get("/api/v1/auth/me/workspaces") ?? 0, detail)
    .toBe(1);
  assertNoDuplicates(counter);
});
