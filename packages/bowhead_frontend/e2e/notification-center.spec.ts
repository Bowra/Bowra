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
