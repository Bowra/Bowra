import { expect, test, type Page } from "@playwright/test";
import { mobilePaneTab } from "./_mobile-pane";
import { mockAuthedApp } from "./_app-mocks";

// At narrow widths the conversation/editor segmented control switches panes. Since independent
// Agent editors (1c7ef5209) the open editor also has its own tab, so scope to the segmented control;
// its label follows the draft title once the draft loads.
const mobileEditorTab = (page: Page) =>
  mobilePaneTab(page);

for (const width of [412, 1440]) {
  test(`saved conversational draft keeps newer edits and restores after refresh at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: {
      id: "u_smoke", email: "smoke@example.com", name: "Smoke User", tier: "pro", team_id: "team_smoke",
      capability_revision: "draft-e2e-v1", auth_session_generation: "auth-generation-smoke-1",
      capabilities: { subagent_creator: true, subagent_authoring: { authoring: true, unified_draft: true, draft_chat_edit: false } },
    } }));
    await page.route("**/api/v1/team-subagents/tools", (route) => route.fulfill({ json: { team_tools: [], native_tools: ["bi_query"], text_only_allowed: false } }));
    await page.route("**/api/v1/team-subagents/skills", (route) => route.fulfill({ json: { builtin: [], team_skills: [] } }));
    let saved = {
      schema_version: 2, kind: "team_subagent_manual_draft_detail", draft_id: "sad_browser", source_mode: "turn",
      entry_mode: "canvas", draft_status: "editing", workspace_id: "ws_smoke", purpose: "Analyze sales",
      definition: { name: "sales-bot", display_name: "Sales helper", description: "Analyze sales", system_prompt: "Analyze evidence", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
      warnings: [], validation: { status: "unknown", issues: [] }, version: 1, etag: 'W/"sad_browser:1"',
      mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2099-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    let releaseFirstSave!: () => void;
    const firstSave = new Promise<void>((resolve) => { releaseFirstSave = resolve; });
    let writes = 0;
    await page.route("**/api/v1/team-subagents/drafts/**", async (route) => {
      if (new URL(route.request().url()).pathname.endsWith("/suggestions")) {
        await route.fulfill({ json: { schema_version: 2, kind: "team_subagent_canvas_suggestions", items: [] } });
        return;
      }
      if (route.request().method() === "PATCH") {
        const payload = route.request().postDataJSON();
        expect(route.request().headers()["if-match"]).toBe(saved.etag);
        writes += 1;
        if (writes === 1) await firstSave;
        const version = saved.version + 1;
        saved = { ...saved, purpose: payload.purpose, definition: payload.definition, version, etag: `W/"sad_browser:${version}"` };
      }
      await route.fulfill({ json: saved });
    });
    await page.goto("/?session=s_browser&agent_draft=sad_browser");
    if (width === 412) await mobileEditorTab(page).click();
    // D4: the Canvas draft saves itself; a newer edit made during a save is saved next.
    const name = page.getByRole("textbox", { name: "工作说明", exact: true });
    await expect(name).toHaveValue("Analyze evidence");
    await name.fill("first-edit");
    await expect.poll(() => writes).toBe(1);
    await name.fill("latest-edit");
    releaseFirstSave();
    await expect(name).toHaveValue("latest-edit");
    await expect.poll(() => saved.definition.system_prompt).toBe("latest-edit");
    expect(writes).toBe(2);
    await expect(page).toHaveURL(/agent_draft=sad_browser/);
    await page.reload();
    if (width === 412) await mobileEditorTab(page).click();
    await expect(name).toHaveValue("latest-edit");
    expect(writes).toBe(2);
    const inputBox = await name.boundingBox();
    expect(inputBox && inputBox.x >= 0 && inputBox.x + inputBox.width <= width + 1).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath("saved-draft.png") });
    await page.evaluate(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("session", "s_next");
      window.history.pushState(null, "", url);
    });
    await expect(page).toHaveURL(/session=s_next/);
    await expect(page).not.toHaveURL(/agent_draft=/);
    await expect(name).toHaveCount(0);
    expect(saved.definition.system_prompt).toBe("latest-edit");
  });
}
