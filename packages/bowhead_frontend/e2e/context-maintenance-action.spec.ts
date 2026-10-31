import { expect, test } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

for (const bootstrap of [false, true]) {
test(`the existing menu recovers the exact admitted action (bootstrap=${bootstrap})`, async ({ page }, info) => {
  await page.setViewportSize({ width: 360, height: 760 });
  await installReferenceComponent(page, "", "_context-maintenance-entry.tsx");
  const context = { contract: "context-maintenance.v1", expected_generation: bootstrap ? null : `mcg_${"b".repeat(32)}` };
  await page.route("**/api/v1/sessions/s_context", (route) => route.fulfill({ json: {
    id: "s_context", context_generation: context.expected_generation, turns: [{ id: "t_previous", status: "done", operation_kind: "chat" }], chat_execution: { write_protocol: "terminal_v1" },
  } }));
  const requests: Record<string, unknown>[] = [];
  let actionId = "";
  let turnId = "";
  await page.route("**/api/v1/chat/send", async (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    actionId = body.client_info.request_id;
    turnId = `t_${actionId.slice("act_start_".length)}`;
    // The server admitted the action but its HTTP acknowledgement was lost.
    await route.abort("failed");
  });
  const lookups: string[] = [];
  await page.route("**/api/v1/chat/actions/*", (route) => {
    lookups.push(route.request().url());
    return route.fulfill({ json: { action_id: actionId, action_kind: "start", outcome: "admitted", session_id: "s_context", turn_id: turnId,
      stream_url: `/api/v1/chat/stream/${turnId}`, execution_generation: 1, terminal_protocol_version: 1, context_maintenance: context } });
  });
  await page.goto("/__reference_component_test");
  await page.getByRole("textbox", { name: "Unsent question" }).fill("Still editing this question");
  await page.getByRole("button", { name: /Quarterly analysis/ }).click();
  await page.screenshot({ path: info.outputPath("maintenance-menu.png") });
  await page.getByRole("menuitem", { name: "Compact context", exact: true }).click();
  await expect(page.getByRole("status", { name: "Maintenance status" })).toContainText("check the same request");
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({ message: "", session_id: "s_context", context_maintenance: context, planning_mode: "off" });
  expect(requests[0]).not.toHaveProperty("client_submission_id");
  expect(requests[0]).not.toHaveProperty("input_document");
  await expect(page.getByRole("textbox", { name: "Unsent question" })).toHaveValue("Still editing this question");
  await page.reload();
  await expect(page.locator("#attached-turn")).toHaveText(turnId);
  expect(lookups).toHaveLength(1);
  expect(lookups[0]).toContain(actionId);
  expect(requests).toHaveLength(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});
}

test("a legacy maintenance response must confirm admission before attachment", async ({ page }) => {
  await installReferenceComponent(page, "", "_context-maintenance-entry.tsx");
  const context = { contract: "context-maintenance.v1", expected_generation: `mcg_${"b".repeat(32)}` };
  await page.route("**/api/v1/sessions/s_context", (route) => route.fulfill({ json: {
    id: "s_context", context_generation: context.expected_generation, turns: [], chat_execution: { write_protocol: "legacy_v0" },
  } }));
  let posts = 0;
  await page.route("**/api/v1/chat/send", (route) => {
    posts++;
    return route.fulfill({ json: { status: "buffered", session_id: "s_context", turn_id: "t_context",
      stream_url: "/api/v1/chat/stream/t_context", execution_generation: 1, context_maintenance: context } });
  });
  await page.route("**/api/v1/chat/submissions/*", (route) => route.fulfill({ status: 404, json: {} }));
  await page.goto("/__reference_component_test");
  await page.getByRole("button", { name: /Quarterly analysis/ }).click();
  await page.getByRole("menuitem", { name: "Compact context", exact: true }).click();
  await expect(page.getByRole("status", { name: "Maintenance status" })).toContainText("check the same request");
  await expect(page.locator("#attached-turn")).toHaveText("");
  expect(posts).toBe(1);
});
