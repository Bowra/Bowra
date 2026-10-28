import { expect, test } from "@playwright/test";
import axe from "axe-core";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import zh from "../messages/zh-CN.json";
import en from "../messages/en-US.json";

for (const width of [412, 1440]) for (const locale of ["zh-CN", "en-US"] as const) {
  test(`queued source at ${width}px ${locale}`, async ({ page }) => {
    const t = (locale === "zh-CN" ? zh : en).reference_evidence;
    const submissionId = "00000000-0000-4000-8000-000000000001";
    await page.addInitScript(() => { (globalThis as any).process = { env: {} }; });
    await page.setViewportSize({ width, height: 900 });
    await installReferenceComponent(page, "/bowhead", "_reference-evidence-entry.tsx");
    let queueReads = 0;
    await page.route("**/reference-queue?*", route => route.fulfill({ json: { items: queueReads++ ? [] : [{
      client_submission_id: submissionId, display_text: "/杜邦分析 盈利能力", resources: [{ kind: "skill", label: "杜邦分析" }], attachment_count: 0, accepted_at: "2026-09-21T00:00:00Z",
    }] } }));
    const reads: string[] = [];
    let revoked = false;
    await page.route("**/reference-queue/*/evidence?*", route => {
      const url = route.request().url();
      reads.push(url);
      if (revoked) return route.fulfill({ status: 403, json: {} });
      const content = url.includes("include_content=true");
      return route.fulfill({ json: {
        facts_version: 1, turn_id: "original-parent",
        source_locator: { kind: "queued_submission", submission_id: submissionId, accepted_parent_turn_id: "original-parent", turn_id: null, text_spans: content ? [{ text: "original queued task" }] : [] },
        queue: { state: "queued" }, content_included: content,
        references: [
          { resource_kind: "skill", resource_visibility: "visible", label: "杜邦分析", ...(content ? { parameters: { note: "<script>original parameter</script>".repeat(40) } } : {}) },
          { resource_kind: "skill", resource_visibility: "unavailable", label: "hidden method", parameters: { note: "hidden parameter" } },
        ],
        evidence_completeness: { status: "partial" }, execution_evidence_turn_id: null,
      } });
    });
    await page.goto(`/bowhead/__reference_component_test?locale=${locale}&queue=1`);
    const toggle = page.getByRole("button", { name: t.queue_source });
    await expect(toggle).toBeVisible();
    expect(reads).toHaveLength(0);
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText(t.queue_queued)).toBeVisible();
    await expect(page.getByText("original queued task")).toHaveCount(0);
    await page.getByRole("button", { name: t.queue_content }).click();
    await expect(page.getByText("original queued task")).toBeVisible();
    await expect(page.locator(".reference-evidence-excerpt").first()).toContainText("<script>original parameter</script>");
    await expect(page.getByText("hidden method")).toHaveCount(0);
    await expect(page.getByText("hidden parameter")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const explanation = page.getByText(t.queue_explanation);
    expect(await explanation.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe("none");
    await page.addScriptTag({ content: axe.source });
    expect(await page.evaluate(async () => (await (window as any).axe.run(".reference-follow-up-queue")).violations)).toEqual([]);
    await page.screenshot({ fullPage: true, path: test.info().outputPath("queued-source.png") });
    expect(reads.every(url => url.includes(`/bowhead/api/v1/chat/sessions/session-browser/reference-queue/${submissionId}/evidence?workspace_id=ws_browser`))).toBe(true);
    revoked = true;
    await page.getByRole("button", { name: t.refresh }).click();
    await expect(page.getByText(t.empty)).toBeVisible();
    await expect(page.getByText("original queued task")).toHaveCount(0);
    await page.getByRole("button", { name: t.close }).click();
    await expect(toggle).toBeFocused();
    await page.getByRole("button", { name: "Switch User" }).click();
    await expect(toggle).toHaveCount(0);
  });
}
