import { expect, test } from "@playwright/test";

import zh from "../messages/zh-CN.json";
import { mockAuthedApp } from "./_app-mocks";

test("notification center shows action/update counts and supports read-all/archive", async ({ page }) => {
  test.setTimeout(120_000);
  await mockAuthedApp(page);

  let updateRead = false;
  let actionArchived = false;
  const action = {
    id: "ntf_action",
    teamId: "team_smoke",
    category: "approval",
    type: "approval.required",
    priority: "p1",
    title: "有操作需要审批",
    body: "teammcp.demo.write_record 请求执行写操作，风险等级 high",
    subjectType: "approval",
    subjectId: "apr_action",
    actorUserId: "usr_owner",
    actionUrl: "/admin/approvals/?approvalId=apr_action",
    actions: [{ id: "review", label: "Review", kind: "link", url: "/admin/approvals/?approvalId=apr_action" }],
    metadata: { approvalId: "apr_action" },
    state: "open",
      actionRequired: true,
    recipientRole: "approver_admin",
    deliveredAt: "2026-06-10T08:00:00Z",
    readAt: null,
    archivedAt: null,
    createdAt: "2026-06-10T08:00:00Z",
    updatedAt: "2026-06-10T08:00:00Z",
  };
  const update = {
    id: "ntf_update",
    teamId: "team_smoke",
    category: "approval",
    type: "approval.rejected",
    priority: "p2",
    title: "审批已拒绝",
    body: "teammcp.demo.write_record 审批已拒绝",
    subjectType: "approval",
    subjectId: "apr_update",
    actorUserId: "usr_admin",
    actionUrl: "/",
    actions: [{ id: "view", label: "View", kind: "link", url: "/" }],
    metadata: { approvalId: "apr_update" },
    state: "open",
      actionRequired: false,
    recipientRole: "requester",
    deliveredAt: "2026-06-10T08:01:00Z",
    readAt: null as string | null,
    archivedAt: null,
    createdAt: "2026-06-10T08:01:00Z",
    updatedAt: "2026-06-10T08:01:00Z",
  };
  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/notifications/unread-count", async (route) => {
    await route.fulfill(json({
      actionRequired: actionArchived ? 0 : 1,
      updatesUnread: updateRead ? 0 : 1,
      totalBadge: actionArchived ? 0 : 1,
    }));
  });
  await page.route(/\/api\/v1\/notifications(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const tab = url.searchParams.get("tab");
    const updateItem = { ...update, readAt: updateRead ? "2026-06-10T08:05:00Z" : null };
    const items = tab === "action"
      ? (actionArchived ? [] : [action])
      : tab === "updates"
      ? [updateItem]
      : [...(actionArchived ? [] : [action]), updateItem];
    await route.fulfill(json({ items, nextCursor: null }));
  });
  await page.route("**/api/v1/notifications/read-all", async (route) => {
    updateRead = true;
    await route.fulfill(json({ ok: true }));
  });
  await page.route(/\/api\/v1\/notifications\/[^/]+\/archive$/, async (route) => {
    actionArchived = true;
    await route.fulfill(json({ ok: true }));
  });
  await page.route(/\/api\/v1\/notifications\/[^/]+\/read$/, async (route) => {
    await route.fulfill(json({ ok: true }));
  });

  await page.goto("/");
  const bell = page.getByRole("button", { name: "通知，1 个待处理" });
  await expect(bell).toBeVisible();
  await bell.evaluate((btn) => (btn as HTMLButtonElement).click());

  const panel = page.getByRole("region", { name: "通知中心" });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("1 待处理 · 1 未读更新");
  await expect(panel.getByRole("button", { name: /有操作需要审批/ })).toBeVisible();

  await panel.getByRole("tab", { name: "更新 1" }).click();
  await expect(panel).toContainText("审批已拒绝");
  await panel.getByRole("button", { name: "全部已读" }).click();
  await expect(panel).toContainText("1 待处理 · 0 未读更新");
  await expect(page.getByRole("button", { name: "通知，1 个待处理" })).toBeVisible();

  await panel.getByRole("tab", { name: "待处理 1" }).click();
  await panel.getByRole("button", { name: "归档通知" }).click();
  await expect(panel).toContainText("0 待处理 · 0 未读更新");
  await expect(panel).toContainText("没有待处理事项");
  await expect(page.getByRole("button", { name: "通知" })).toBeVisible();
});

test("scheduled notification action opens the live scheduled settings tab", async ({ page }) => {
  await mockAuthedApp(page);

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  const action = {
    id: "ntf_scheduled",
    teamId: "team_smoke",
    category: "scheduled",
    type: "scheduled.paused",
    priority: "p1",
    // Match the backend scheduled.paused default persisted copy; the frontend rewrites it in the current language using type + metadata.
    title: "定时任务已自动暂停",
    body: "「日报」连续失败已被自动暂停, 请检查后手动恢复",
    subjectType: "scheduled_job",
    subjectId: "job_1",
    actorUserId: null,
    actionUrl: "/team/settings/scheduled/",
    actions: [{
      id: "view",
      label: "查看定时任务",
      kind: "link",
      url: "/team/settings/scheduled/",
    }],
    metadata: { jobId: "job_1", jobName: "日报", reason: "repeated_failures" },
    state: "open",
      actionRequired: true,
    recipientRole: "owner",
    deliveredAt: "2026-07-17T08:00:00Z",
    readAt: null,
    archivedAt: null,
    createdAt: "2026-07-17T08:00:00Z",
    updatedAt: "2026-07-17T08:00:00Z",
  };

  await page.route("**/api/v1/notifications/unread-count", async (route) => {
    await route.fulfill(json({
      actionRequired: 1,
      updatesUnread: 0,
      totalBadge: 1,
    }));
  });
  await page.route(/\/api\/v1\/notifications(?:\?.*)?$/, async (route) => {
    await route.fulfill(json({ items: [action], nextCursor: null }));
  });
  await page.route(/\/api\/v1\/notifications\/ntf_scheduled\/read$/, async (route) => {
    await route.fulfill(json({ ok: true }));
  });
  await page.route("**/api/v1/scheduled/jobs", async (route) => {
    await route.fulfill(json({ items: [] }));
  });

  await page.goto("/");
  const bell = page.getByRole("button", { name: "通知，1 个待处理" });
  await expect(bell).toBeVisible();
  await bell.evaluate((button) => (button as HTMLButtonElement).click());
  const row = page.getByRole("button", { name: new RegExp(zh.notifications.copy.scheduled_paused_title) });
  await expect(row).toContainText(zh.notifications.copy.scheduled_paused_body.replace("{name}", "日报"));
  await row.click();

  await expect(page).toHaveURL(/\/team\/settings\/scheduled\/?$/);
  await expect(page.getByRole("heading", { name: "定时任务" })).toBeVisible();
});

test("update-only badge and retry preserve composer text and keyboard focus", async ({ page }) => {
  await mockAuthedApp(page);
  let unavailable = true;
  await page.route("**/api/v1/notifications/unread-count", (route) => route.fulfill({
    json: { actionRequired: 0, updatesUnread: 3, totalBadge: 0 },
  }));
  await page.route(/\/api\/v1\/notifications(?:\?.*)?$/, (route) => route.fulfill(
    unavailable ? { status: 503, json: {} } : { json: { items: [], nextCursor: null } },
  ));
  await page.goto("/");
  const composer = page.locator("textarea").first();
  await composer.fill("保留这条未发送的问题");
  const bell = page.getByRole("button", { name: zh.notifications.bell_aria_updates.replace("{updates}", "3") });
  await expect(bell.locator(".notification-bell__dot")).toBeVisible();
  await bell.focus();
  await bell.press("Enter");
  await expect(page.getByText(zh.notifications.load_failed_notice)).toBeVisible();
  unavailable = false;
  await page.getByRole("button", { name: zh.notifications.retry, exact: true }).click();
  await expect(page.locator("[data-notification-center]").getByText(zh.notifications.empty_action, { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(bell).toBeFocused();
  await expect(page.locator("[data-notification-center]")).toHaveCount(0);
  await expect(composer).toHaveValue("保留这条未发送的问题");
});

test("space assignment notification opens the named member's grants and requires explicit authorization", async ({ page }) => {
  await mockAuthedApp(page, { subagentManagement: true });
  const json = (body: unknown) => ({ contentType: "application/json", body: JSON.stringify(body) });
  const actionUrl = "/team/settings/people/?member=u_requester&team=team_smoke";
  const notification = {
    id: "ntf_space", teamId: "team_smoke", category: "space_assignment", type: "space_assignment_requested",
    priority: "p1", title: "Alice needs a space", body: null, subjectType: "team_space_assignment",
    subjectId: "team_smoke", actorUserId: "u_requester", actionUrl, actions: [],
    metadata: { requesterUserId: "u_requester", requesterName: "Alice" }, state: "open", actionRequired: true,
    recipientRole: "space_assignment_admin", deliveredAt: "2026-11-02T00:00:00Z", readAt: null,
    archivedAt: null, createdAt: "2026-11-02T00:00:00Z", updatedAt: "2026-11-02T00:00:00Z",
  };
  await page.route("**/api/v1/notifications/unread-count", route => route.fulfill(json({ actionRequired: 1, updatesUnread: 0, totalBadge: 1 })));
  await page.route(/\/api\/v1\/notifications(?:\?.*)?$/, route => route.fulfill(json({ items: [notification], nextCursor: null })));
  await page.route("**/api/v1/notifications/ntf_space/read", route => route.fulfill(json({ ok: true })));
  await page.route(/\/api\/v1\/admin\/members(?:\?.*)?$/, route => route.fulfill(json({
    items: [], total: 60, page: 1, size: 25, your_role: "owner", owner_id: "u_smoke",
  })));
  await page.route(/\/api\/v1\/admin\/members\/invites(?:\?.*)?$/, route => route.fulfill(json({ invites: [], total: 0, page: 1, size: 25 })));
  let granted = false;
  await page.route("**/api/v1/admin/members/u_requester/workspace-grants", async route => {
    if (route.request().method() === "PUT") {
      expect(route.request().postDataJSON()).toEqual({ workspace_id: "ws_sales", role: "member" });
      granted = true;
      await route.fulfill(json({ ok: true }));
    } else {
      await route.fulfill(json({ user_id: "u_requester", display_name: "Alice", email: "alice@example.com", items: [
        { workspace_id: "ws_sales", name: "Sales", description: null, status: "active", role: granted ? "member" : null, joined_at: null },
      ] }));
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "通知，1 个待处理" }).click();
  await page.getByRole("button", { name: /Alice 需要分配空间/ }).click();
  await expect(page).toHaveURL(/\/team\/settings\/people\/?\?.*member=u_requester/, { timeout: 30_000 });
  const dialog = page.getByRole("dialog", { name: "Alice 的空间授权" });
  await expect(dialog).toBeVisible();
  expect(granted).toBe(false);
  await dialog.getByRole("button", { name: "添加空间", exact: true }).click();
  await expect(dialog.getByText("Sales", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "授权", exact: true }).click();
  await expect.poll(() => granted).toBe(true);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page).not.toHaveURL(/member=/);
});
