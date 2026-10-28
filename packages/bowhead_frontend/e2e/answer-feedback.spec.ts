import { mkdir } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * Real-browser check of the thumbs-down on team Agent answers against Claude Design "Agent Eval Alignment
 * Loop" L0-a…L0-d, at desktop width and 412 px: the action bar button, the dialog (two-column reason
 * checkboxes, optional intro, consent with the owner line), the Undo toast, pressing again, and the
 * Owner's Pending row. Fixture in `_answer-feedback-entry.tsx`; every API is answered by page.route.
 * Set ANSWER_FEEDBACK_SHOTS to a directory to keep screenshots.
 */
const SHOTS = process.env.ANSWER_FEEDBACK_SHOTS;

interface Calls {
  feedback: Array<{ method: string; body: unknown }>;
  share: string[];
  handled: unknown[];
}

async function open(page: Page): Promise<Calls> {
  const calls: Calls = { feedback: [], share: [], handled: [] };
  let shared = false;
  let feedback: unknown = null;
  const item = () => ({ turn_id: "t_team", agent_id: "osa_1", agent_name: "业务增长分析师", shared, shareable: true, owner_name: "刘一鑫", feedback });
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 404, contentType: "application/json", body: "{}" }));
  await page.route("**/api/v1/sessions/s_1/eval-shares", (route) =>
    route.fulfill({ json: { kind: "turn_eval_shares", schema_version: 1, items: [item()] } }),
  );
  await page.route("**/api/v1/turns/t_team/feedback", (route) => {
    const method = route.request().method();
    const body = method === "PUT" ? route.request().postDataJSON() : null;
    calls.feedback.push({ method, body });
    feedback = method === "PUT" ? { score: -1, ...body, given_at: "2026-10-19T08:00:00Z" } : null;
    return route.fulfill({ json: { kind: "turn_answer_feedback", schema_version: 1, ...item() } });
  });
  await page.route("**/api/v1/turns/t_team/eval-share", (route) => {
    calls.share.push(route.request().method());
    shared = route.request().method() === "PUT";
    return route.fulfill({ json: { kind: "turn_eval_share", schema_version: 1, turn_id: "t_team", shared } });
  });
  await page.route("**/api/v1/agents/osa_1/member-feedback/*/handled", (route) => {
    calls.handled.push(route.request().postDataJSON());
    return route.fulfill({ json: { kind: "agent_member_feedback_handled", schema_version: 1, turn_id: "t_shared", handled: "coach" } });
  });
  await installReferenceComponent(page, "", "_answer-feedback-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "answer feedback" })).toBeVisible();
  return calls;
}

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  await mkdir(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

for (const [label, viewport] of [["desktop", { width: 1280, height: 900 }], ["412", { width: 412, height: 915 }]] as const) {
  test(`thumbs-down, consent, undo and the Owner's Pending row at ${label}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const calls = await open(page);

    const thumbs = page.getByRole("button", { name: "这条回答不好" });
    await expect(thumbs).toHaveAttribute("aria-pressed", "false");
    await shot(page, `${label}-1-actions-and-pending`);

    await thumbs.click();
    const dialog = page.getByRole("dialog", { name: "这条回答哪里不好？" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("都可以不填，直接提交也会记下。")).toBeVisible();
    const consent = dialog.getByRole("checkbox", { name: /允许智能体负责人用这一轮做评测用例（负责人会看到问题、回答和你的反馈）/ });
    await expect(consent).not.toBeChecked();
    await expect(dialog.getByText("负责人：刘一鑫。之后可以在这条回答的「⋯」里撤回。")).toBeVisible();
    // Two columns of reason checkboxes.
    const reasons = dialog.locator(".bh-feedback-dialog__option").filter({ hasNot: page.locator("small") });
    await expect(reasons).toHaveCount(7);
    const [first, second] = await Promise.all([reasons.nth(0).boundingBox(), reasons.nth(1).boundingBox()]);
    expect(Math.abs(first!.y - second!.y)).toBeLessThan(2);
    expect(second!.x).toBeGreaterThan(first!.x);
    await dialog.getByRole("checkbox", { name: "漏了重要内容" }).check();
    await dialog.getByRole("textbox", { name: /补充说明/ }).fill("没写是哪个月的数据，我得自己去查");
    await consent.check();
    await shot(page, `${label}-2-dialog`);

    await dialog.getByRole("button", { name: "提交" }).click();
    await expect(dialog).toBeHidden();
    const toast = page.getByText("已记下。负责人可以用这一轮改进智能体。");
    await expect(toast).toBeVisible();
    await expect(page.getByTestId("toast-info").getByRole("button", { name: "撤销", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "已标记为不好，再点一次撤销" })).toHaveAttribute("aria-pressed", "true");
    expect(calls.feedback[0]).toEqual({ method: "PUT", body: { reason_codes: ["missing_content"], comment: "没写是哪个月的数据，我得自己去查" } });
    expect(calls.share).toEqual(["PUT"]);
    await shot(page, `${label}-3-submitted-toast`);

    // Undo takes back the thumbs-down and the share it added.
    await page.getByTestId("toast-info").getByRole("button", { name: "撤销", exact: true }).click();
    await expect(page.getByRole("button", { name: "这条回答不好" })).toHaveAttribute("aria-pressed", "false");
    expect(calls.feedback.at(-1)!.method).toBe("DELETE");
    expect(calls.share).toEqual(["PUT", "DELETE"]);

    // Again without consent, then press the button again to remove it.
    await page.getByRole("button", { name: "这条回答不好" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "提交" }).click();
    await expect(page.getByText("已记下。", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "已标记为不好，再点一次撤销" }).click();
    await expect(page.getByText("已撤销点踩。")).toBeVisible();
    expect(calls.share).toEqual(["PUT", "DELETE"]);
    await shot(page, `${label}-4-removed`);

    // The Owner's Pending rows: one neutral label, question, who · when · reasons · comment or "no comment".
    const rows = page.getByTestId("member-feedback-item");
    await expect(rows).toHaveCount(2);
    await expect(page.getByRole("heading", { name: "2 条点踩待处理" })).toBeVisible();
    await expect(rows.nth(0)).toContainText("陈晓");
    await expect(rows.nth(0)).toContainText("漏了重要内容");
    await expect(rows.nth(0)).toContainText("「没写是哪个月的数据，我得自己去查」");
    await expect(rows.nth(1)).toContainText("没有补充说明");
    await rows.nth(0).scrollIntoViewIfNeeded();
    await shot(page, `${label}-5-pending`);
    await rows.nth(0).getByRole("button", { name: "用它来改进智能体" }).click();
    await expect.poll(() => calls.handled).toEqual([{ action: "coach" }]);
    await rows.nth(1).getByRole("button", { name: "忽略" }).click();
    expect(await page.evaluate(() => window.answerFeedbackEvents.filter((event) => event.startsWith("dismiss")))).toEqual(["dismiss:t_silent"]);
  });
}
