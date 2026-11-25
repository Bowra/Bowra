// Result pins in the sidebar's pinned group (docs/sprints/result-pins-2026-11-24/prd.md §4.2–§4.3): pinned files and
// reports render with their kind's icon, open where the PRD says, and the phone drawer shows the same group.
import { expect, test, type Page } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

const PINS = {
  items: [
    {
      kind: "file",
      pin_id: "art_root01",
      artifact_id: "art_v3file",
      title: "季度复盘_网页版.pptx",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      version_no: 3,
      session_id: "s_pinned",
      opens: "session",
      changed_at: "2026-11-24T11:30:00+00:00",
      pinned_at: "2026-11-24T11:31:00+00:00",
    },
    {
      kind: "report",
      pin_id: "cv_store01",
      title: "",
      first_section_title: "门店经营三视角分析",
      created_at: "2026-11-20T08:00:00+00:00",
      session_id: "s_store",
      opens: "session",
      changed_at: "2026-11-24T08:20:00+00:00",
      pinned_at: "2026-11-24T08:21:00+00:00",
    },
    {
      kind: "file",
      pin_id: "art_upload1",
      artifact_id: "art_upload1",
      title: "用户模板-9页.pptx",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      version_no: 1,
      session_id: null,
      opens: "files",
      changed_at: "2026-11-23T16:00:00+00:00",
      pinned_at: "2026-11-24T07:00:00+00:00",
    },
  ],
};

async function mockPins(page: Page) {
  await page.addInitScript(() => {
    const style = document.createElement("style");
    // The Next development-tools badge is not product UI and covers the bottom-left corner.
    style.textContent = "nextjs-portal { display: none !important; }";
    document.addEventListener("DOMContentLoaded", () => document.head.appendChild(style));
  });
  await page.route(
    (url) => url.pathname === "/api/v1/result-pins",
    (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PINS) }),
  );
}

test.beforeEach(async ({ page }) => {
  await mockPins(page);
});

test("pinned results show with their icons and open in their conversation's Results", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 860 });
  await page.goto("/");

  const pinned = page.locator(".unified-work-group", { has: page.getByRole("heading", { name: "置顶", level: 3 }) });
  await expect(pinned.locator("[data-result-pin]")).toHaveCount(3);
  await expect(pinned.locator(".unified-work-title")).toHaveText([
    "季度复盘_网页版.pptx",
    "门店经营三视角分析",
    "用户模板-9页.pptx",
  ]);
  await expect(pinned.locator(".unified-work-item-icon")).toHaveCount(3);
  await expect(pinned.getByText("PPTX · 第 3 版")).toBeVisible();

  // The icon sits on the title line, not on a line of its own.
  const fileRow = pinned.locator("[data-result-pin='file:art_root01']");
  const iconBox = await fileRow.locator(".unified-work-item-icon").boundingBox();
  const titleBox = await fileRow.locator(".unified-work-title").boundingBox();
  expect(iconBox && titleBox && iconBox.x + iconBox.width <= titleBox.x).toBe(true);
  expect(iconBox && titleBox && Math.abs(iconBox.y - titleBox.y) < titleBox.height).toBe(true);

  await fileRow.locator(".unified-work-item-main").click();
  await expect(page).toHaveURL(/\?session=s_pinned&result=art_v3file&mode=preview/, { timeout: 30_000 });
});

test("a pinned upload without a conversation opens in Files", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 860 });
  await page.goto("/");
  await page.locator("[data-result-pin='file:art_upload1'] .unified-work-item-main").click();
  await expect(page).toHaveURL(/\/files\/?\?focus=art_upload1/, { timeout: 60_000 });
});

test("keyboard reaches a pinned result and its row menu offers Unpin", async ({ page, browserName }) => {
  await page.setViewportSize({ width: 1440, height: 860 });
  await page.goto("/");
  const link = page.locator("[data-result-pin='file:art_root01'] .unified-work-item-main");
  await link.focus();
  await expect(link).toBeFocused();
  // Safari's default Tab skips links and buttons (a system setting); Alt+Tab walks the full order.
  await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
  const more = page.getByRole("button", { name: "更多成果操作：季度复盘_网页版.pptx" });
  await expect(more).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "取消置顶" })).toBeVisible();
});

test("the phone drawer shows the same pinned results", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByTestId("mobile-topbar-hamburger").click();
  const drawer = page.getByTestId("sessions-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.locator("[data-result-pin]")).toHaveCount(3);
});

test("the collapsed sidebar's recent conversations panel shows the same pinned results", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await page.getByRole("button", { name: "收起侧栏" }).click();
  await page.getByRole("button", { name: "最近会话", exact: true }).click();
  await expect(page.locator("[data-result-pin]")).toHaveCount(3);
});

test.describe("opening a pin in its conversation", () => {
  const CONVERSATION_FILES = [
    { id: "art_soft_a", filename: "门店营业额.txt", text: "Pinned file A body" },
    { id: "art_soft_b", filename: "季度备注.txt", text: "Pinned file B body" },
  ];

  async function mockPinnedConversation(page: Page) {
    await mockAuthedApp(page);
    await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
    await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
      dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50,
    } }));
    const artifacts = CONVERSATION_FILES.map((f) => ({
      id: f.id, filename: f.filename, size: 20, source: "sandbox_output", kind: "deliverable", mime: "text/plain",
      created_at: "2026-11-24T00:00:00Z", preview_available: true,
    }));
    await page.route("**/api/v1/sessions/s_soft/artifacts*", (route) => route.fulfill({ json: { items: artifacts } }));
    for (const [index, f] of CONVERSATION_FILES.entries()) {
      await page.route(`**/api/v1/files/${f.id}/representations/*`, (route) => {
        const representation = new URL(route.request().url()).pathname.split("/").pop();
        return route.fulfill({ json: representation === "details"
          ? { representation, family: "text", revision: `${f.id}-1`, details: artifacts[index] }
          : { representation, family: "text", revision: `${f.id}-1`, mode: "text", text: f.text, truncated: false } });
      });
    }
    await page.route((url) => url.pathname === "/api/v1/result-pins", (route) => route.fulfill({ json: { items: CONVERSATION_FILES.map((f, i) => ({
      kind: "file", pin_id: f.id, artifact_id: f.id, title: f.filename, mime: "text/plain", version_no: 1,
      session_id: "s_soft", opens: "session", changed_at: `2026-11-24T1${i}:00:00Z`, pinned_at: `2026-11-24T1${i}:00:00Z`,
    })) } }));
  }

  test("a pinned file opens in Results from home and from inside its own conversation", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    // Inside the conversation, Results does not follow a soft URL change: the pin must open it in place.
    await mockPinnedConversation(page);
    await page.goto("/");
    await page.locator("[data-result-pin='file:art_soft_a'] .unified-work-item-main").click();
    await expect(page).toHaveURL(/session=s_soft&result=art_soft_a/, { timeout: 30_000 });
    await expect(page.getByText("Pinned file A body").first()).toBeVisible({ timeout: 30_000 });
    await page.locator("[data-result-pin='file:art_soft_b'] .unified-work-item-main").click();
    await expect(page).toHaveURL(/session=s_soft&result=art_soft_b/, { timeout: 30_000 });
    await expect(page.getByText("Pinned file B body").first()).toBeVisible({ timeout: 30_000 });
  });
});
