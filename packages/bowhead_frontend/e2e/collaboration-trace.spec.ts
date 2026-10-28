import { expect, test } from "@playwright/test";
import axe from "axe-core";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";

for (const width of [412, 1440]) for (const locale of ["en-US", "zh-CN"] as const) {
  test(`collaboration disclosure at ${width}px ${locale}`, async ({ page }) => {
    const t = (locale === "zh-CN" ? zh : en).step_list.collaboration;
    await page.addInitScript(() => { (globalThis as any).process = { env: {} }; });
    await page.setViewportSize({ width, height: 900 });
    await installReferenceComponent(page, "/bowhead", "_collaboration-trace-entry.tsx");
    let verified = false;
    let revoked = false;
    await page.route("**/api/v1/turns/turn-browser/collaboration", route => route.fulfill(revoked ? { status: 403, json: {} } : { json: {
      turn_id: "turn-browser", revision: verified ? 2 : 1, turn_status: "running",
      participants: [
        { execution_id: "analyst", profile: "Data analyst", is_owner: false, status: "running", waiting: false },
        { execution_id: "expert", profile: "Revenue specialist", is_owner: false, status: "running", waiting: false },
      ],
      topics: [{ topic_id: "topic", sender_id: "analyst", recipient_id: "expert", required: true, response_status: "answered", dependency_status: verified ? "resolved" : "open", messages: [
        { operation_id: "question", action: "ask", sender_id: "analyst", recipient_id: "expert", body: "Should the refund calculation include shipping charges?" },
        { operation_id: "reply", action: "reply", sender_id: "expert", recipient_id: "analyst", body: "Refunds exclude shipping. Apply the same definition to the comparison period.", references: [{ identity: "art_refunds", version: "art_refunds" }] },
      ] }],
    } }));
    await page.goto(`/bowhead/__reference_component_test?locale=${locale}`);
    const summary = page.locator("details summary");
    await expect(summary).toContainText(t.verification);
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText("Refunds exclude shipping.", { exact: false })).toBeVisible();
    verified = true;
    await page.getByRole("button", { name: "Publish reply verification" }).evaluate((button: HTMLButtonElement) => button.click());
    await expect(summary).toContainText(t.resolved);
    await expect(summary).toBeFocused();
    await expect(page.locator("details")).toHaveAttribute("open", "");
    await expect(page.getByRole("textbox", { name: "Draft" })).toHaveValue("Keep my draft");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.addScriptTag({ content: axe.source });
    expect(await page.evaluate(async () => (await (window as any).axe.run(".bh-collaboration-trace")).violations)).toEqual([]);
    await page.screenshot({ fullPage: true, path: test.info().outputPath("collaboration.png") });
    await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ fullPage: true, path: test.info().outputPath("collaboration-zoom.png") });
    revoked = true;
    await page.getByRole("button", { name: t.refresh, exact: true }).click();
    await expect(page.getByText(t.load_error)).toBeVisible();
    await expect(page.getByText("Refunds exclude shipping.", { exact: false })).toHaveCount(0);
  });
}

for (const locale of ["en-US", "zh-CN"] as const) {
  test(`collaboration settles without a card-finalized event and survives replay: ${locale}`, async ({ page }) => {
    const t = (locale === "zh-CN" ? zh : en).step_list.collaboration;
    await page.addInitScript(() => { (globalThis as any).process = { env: {} }; });
    await installReferenceComponent(page, "/bowhead", "_collaboration-trace-entry.tsx");
    let reads = 0;
    // Keep returning the pre-terminal snapshot to exercise a late/stale read.
    await page.route("**/api/v1/turns/turn-browser/collaboration", route => {
      reads += 1;
      return route.fulfill({ json: {
        turn_id: "turn-browser", revision: 1, turn_status: "running",
        participants: [
          { execution_id: "analyst", profile: "compute", is_owner: false, status: "running", waiting: true },
          { execution_id: "expert", profile: "business-analyst", is_owner: false, status: "running", waiting: true },
        ],
        topics: [{ topic_id: "pending", required: true, response_status: "pending", dependency_status: "open", messages: [
          { operation_id: "question", action: "ask", sender_id: "analyst", recipient_id: "expert", body: "Confirm the refund policy." },
        ] }],
      } });
    });
    await page.goto(`/bowhead/__reference_component_test?locale=${locale}`);
    const summary = page.locator("details summary");
    await expect(summary).toContainText(t.pending);
    await page.getByRole("button", { name: "Confirm stop" }).click();
    await expect(summary).toContainText(t.unresolved);
    await expect.poll(() => reads).toBeGreaterThanOrEqual(2);
    await expect(page.getByRole("button", { name: t.refresh, exact: true })).toBeEnabled();
    await expect(summary).toContainText(t.unresolved);
    await expect(page.getByRole("textbox", { name: "Draft" })).toHaveValue("Keep my draft");
    await page.screenshot({ fullPage: true, path: test.info().outputPath("cancelled-live.png") });
    await page.goto(`/bowhead/__reference_component_test?locale=${locale}&history=cancelled`);
    await expect(summary).toContainText(t.unresolved);
    await page.reload();
    await expect(summary).toContainText(t.unresolved);
  });
}
