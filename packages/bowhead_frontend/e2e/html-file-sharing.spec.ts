import { expect, test } from "@playwright/test";

import messages from "../edition/shared-messages/zh-CN.json";
import { mockAuthedApp } from "./_app-mocks";

test("the recipient opens a shared HTML result from the notification center", async ({ page, context }) => {
  await mockAuthedApp(page);
  const artifact = {
    id: "art_shared_html",
    filename: "counter.html",
    mime: "text/html",
    kind: "deliverable",
    source: "python_sandbox",
    size: 180,
    created_at: "2026-11-18T00:00:00Z",
  };
  const html = '<!doctype html><button onclick="this.textContent=Number(this.textContent)+1">0</button>';
  const group = { subject_kind: "group", subject_id: "grp_readers", subject_name: "Preview Readers" };
  let shared = false;
  let teamWide = false;
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50, interactive_html: true,
  } }));
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/sessions/s_shared_html/artifacts*", (route) => route.fulfill({ json: { items: [artifact] } }));
  await page.route("**/api/v1/files/art_shared_html/representations/details", (route) => route.fulfill({ json: {
    representation: "details", family: "text", revision: "fixture-1", details: artifact,
  } }));
  await page.route("**/api/v1/sessions/s_shared_html/artifacts/art_shared_html/content", (route) => route.fulfill({ contentType: "text/html", body: html }));
  await page.route("**/api/v1/files/art_shared_html/shares?html=true", (route) => route.fulfill({
    json: {
      file_id: artifact.id, sharing_enabled: true, shares: shared ? [group] : [],
      audience: teamWide ? "team_wide" : shared ? "custom" : "private",
    },
  }));
  await page.route("**/api/v1/team/directory*", (route) => route.fulfill({ json: {
    items: [], next_cursor: null,
  } }));
  await page.route("**/api/v1/team/groups-directory", (route) => route.fulfill({ json: {
    items: [{ id: group.subject_id, name: group.subject_name, member_count: 1 }],
  } }));
  await page.route("**/api/v1/files/art_shared_html/share", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ subject_kind: "group", subject_id: group.subject_id });
    shared = true;
    await route.fulfill({ status: 201, json: group });
  });
  await page.route("**/api/v1/files/art_shared_html/shares/audience", (route) => {
    expect(route.request().method()).toBe("PUT");
    const { audience } = route.request().postDataJSON();
    teamWide = audience === "team_wide";
    if (audience === "private") shared = false;
    return route.fulfill({ status: 204 });
  });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: (value: string) => { sessionStorage.setItem("test-copied-link", value); return Promise.resolve(); } },
    });
  });

  const ownerPath = "/?session=s_shared_html&result=art_shared_html&mode=preview";
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(ownerPath);
  const toolbar = page.locator(".artifact-viewer-head").last();
  const more = toolbar.getByRole("button", { name: messages.artifact_viewer.more_actions, exact: true });
  const share = page.getByRole("menuitem", { name: messages.share.header_eyebrow, exact: true });
  await more.click();
  await share.click();
  const dialog = page.getByRole("dialog", { name: messages.share.aria_share.replace("{title}", artifact.filename) });
  const onlyMe = dialog.getByRole("radio", { name: messages.share.audience_private });
  const specificPeople = dialog.getByRole("radio", { name: messages.share.audience_custom });
  const company = dialog.getByRole("radio", { name: messages.share.audience_team_wide });
  await expect(onlyMe).toBeChecked();
  await expect(dialog.getByRole("radio")).toHaveCount(3);
  await specificPeople.click();
  await expect(dialog.getByText(messages.share.empty_roster_title)).toBeVisible();
  await dialog.getByRole("textbox", { name: messages.share.search_placeholder }).fill("Preview");
  await dialog.getByRole("button", { name: /Preview Readers/ }).click();
  await expect(dialog.getByText(group.subject_name, { exact: true })).toBeVisible();
  await company.click();
  await expect(company).toBeChecked();
  await specificPeople.click();
  await expect(dialog.getByText(group.subject_name, { exact: true })).toBeVisible();
  expect(teamWide).toBe(false);
  await dialog.getByRole("button", { name: messages.share.copy_link }).click();
  const link = await page.evaluate(() => sessionStorage.getItem("test-copied-link"));
  expect(link).toBe(new URL("/files/view/?file=art_shared_html", page.url()).href);

  const recipient = await context.newPage();
  await mockAuthedApp(recipient);
  await recipient.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50, interactive_html: true,
  } }));
  await recipient.route("**/api/v1/files/shared/art_shared_html/view", (route) => shared
    ? route.fulfill({ json: { filename: artifact.filename, mime: artifact.mime, size: artifact.size, version_id: "art_current", version: 2 } })
    : route.fulfill({ status: 404, json: { detail: { error: { code: "file_not_found" } } } }));
  await recipient.route("**/api/v1/files/shared/art_shared_html/content?version_id=art_current", (route) => route.fulfill({ contentType: "text/html", body: html }));
  const noticeUrl = "/files/view/?file=art_shared_html";
  const actorName = "File Owner";
  const noticeTime = "2026-11-18T00:00:00Z";
  await recipient.route("**/api/v1/notifications/unread-count", (route) => route.fulfill({
    json: { actionRequired: 0, updatesUnread: 1, totalBadge: 0 },
  }));
  await recipient.route(/\/api\/v1\/notifications(?:\?.*)?$/, (route) => route.fulfill({ json: {
    items: new URL(route.request().url()).searchParams.get("tab") === "action" ? [] : [{
      id: "ntf_html_share",
      teamId: "team_smoke",
      category: "share",
      type: "content_shared",
      priority: "p2",
      title: "File shared",
      actionUrl: noticeUrl,
      actions: [],
      metadata: { actorName, resourceLabel: artifact.filename },
      state: "open",
      actionRequired: false,
      deliveredAt: noticeTime,
      createdAt: noticeTime,
      updatedAt: noticeTime,
    }],
    nextCursor: null,
  } }));
  await recipient.route("**/api/v1/notifications/ntf_html_share/read", (route) => route.fulfill({ json: { ok: true } }));
  await recipient.goto("/");
  const bell = recipient.getByRole("button", { name: /^通知/ });
  await expect(bell).toBeVisible();
  await bell.evaluate((button: HTMLElement) => button.click());
  const center = recipient.getByRole("region", { name: "通知中心" });
  await expect(center.getByText(messages.notifications.empty_action)).toBeVisible();
  await center.getByRole("tab", { name: new RegExp(`^${messages.notifications.tab_updates}`) }).click();
  await center.getByRole("button", { name: /File Owner.*counter\.html/ }).click();
  await expect(recipient).toHaveURL(/\/files\/view\/?\?file=art_shared_html$/);
  await recipient.setViewportSize({ width: 390, height: 844 });
  const frame = recipient.frameLocator('iframe[title="counter.html"]').frameLocator('iframe[title="HTML content"]');
  await frame.getByRole("button", { name: "0" }).click();
  await expect(frame.getByRole("button", { name: "1" })).toBeVisible();
  await expect(recipient.getByRole("link", { name: messages.artifact_viewer.download, exact: true }))
    .toHaveAttribute("href", /version_id=art_current$/);
  await expect(recipient.getByRole("button", { name: messages.share.header_eyebrow, exact: true })).toHaveCount(0);

  await onlyMe.click();
  await expect.poll(() => shared).toBe(true);
  await page.getByRole("dialog", { name: messages.share.revoke_all_title })
    .getByRole("button", { name: messages.share.stop_access }).click();
  await expect(onlyMe).toBeChecked();
  await expect(dialog.getByText(messages.share.empty_roster_title)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(more).toBeFocused();
  await recipient.reload();
  await expect(recipient.getByRole("alert").filter({ hasText: messages.file_share.unavailable })).toBeVisible();
});

test("the recipient pins a shared file from its shared page", async ({ page }) => {
  await mockAuthedApp(page);
  await page.setViewportSize({ width: 1440, height: 860 });
  const html = "<!doctype html><h1>Shared</h1>";
  const changedAt = "2026-11-25T00:00:00Z";
  const pinRequests: unknown[] = [];
  let pins: unknown[] = [];
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50, interactive_html: true,
  } }));
  await page.route("**/api/v1/files/shared/art_shared_root/view", (route) => route.fulfill({
    json: { filename: "board.html", mime: "text/html", size: 30, version_id: "art_shared_v5", version: 5 },
  }));
  await page.route("**/api/v1/files/shared/art_shared_root/content?version_id=art_shared_v5", (route) => route.fulfill({
    contentType: "text/html", body: html,
  }));
  await page.route((url) => url.pathname === "/api/v1/result-pins", (route) => {
    if (route.request().method() !== "POST") return route.fulfill({ json: { items: pins } });
    pinRequests.push(route.request().postDataJSON());
    const pin = {
      kind: "file", pin_id: "art_shared_root", artifact_id: "art_shared_v5", title: "board.html", mime: "text/html",
      version_no: 5, session_id: null, opens: "shared_view", changed_at: changedAt, pinned_at: changedAt,
    };
    pins = [pin];
    return route.fulfill({ json: pin });
  });

  await page.goto("/files/view/?file=art_shared_root");
  await expect(page.getByRole("link", { name: messages.artifact_viewer.download, exact: true }))
    .toHaveAttribute("href", /version_id=art_shared_v5$/);
  // A file shared with the person is listed nowhere else, so its shared page pins it (result pins acceptance F4),
  // by the version shown: the link's lineage root may name a pruned first version.
  const more = page.getByRole("button", { name: messages.artifact_viewer.more_actions, exact: true });
  await more.click();
  await page.getByRole("menuitem", { name: messages.result_pins.pin, exact: true }).click();
  await expect.poll(() => pinRequests).toEqual([{ kind: "file", id: "art_shared_v5" }]);
  await expect(page.getByText(messages.result_pins.pinned_toast)).toBeVisible();
  await more.click();
  await expect(page.getByRole("menuitem", { name: messages.result_pins.unpin, exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto("/");
  const row = page.locator("[data-result-pin='file:art_shared_root']");
  await expect(row).toContainText("board.html");
  await expect(row).toContainText("第 5 版");
  await row.getByText("board.html").click();
  await expect(page).toHaveURL(/\/files\/view\/?\?file=art_shared_root$/);
});
